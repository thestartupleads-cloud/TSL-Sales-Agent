from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from clearcoat.config import settings
from clearcoat.database import Base
from clearcoat.ghl_sync import _store_conversation
from clearcoat.models import GHLConversation, GHLMessage, GHLSyncRun, LeadState
from clearcoat.tracker import (
    list_tracked_leads,
    rebuild_lead_states,
    refresh_conversation_state,
    tracker_summary,
)


ACCOUNT = "iconic-test-location"
OTHER = "another-location"
NOW = datetime(2026, 11, 26, 20, 0, tzinfo=timezone.utc)


@pytest.fixture
def sessions():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    try:
        yield sessionmaker(bind=engine, expire_on_commit=False)
    finally:
        engine.dispose()


def message(session, msg_id, *, conversation="conv-1", account=ACCOUNT, minutes_ago=10,
            direction="inbound", kind="TYPE_SMS", source=None, user_id=None):
    if session.get(GHLConversation, (account, conversation)) is None:
        session.add(GHLConversation(account_id=account, conversation_id=conversation, contact_id="contact-1"))
    timestamp = NOW - timedelta(minutes=minutes_ago) if minutes_ago is not None else None
    session.add(GHLMessage(
        account_id=account, ghl_message_id=msg_id,
        conversation_id=conversation, contact_id="contact-1",
        direction=direction, message_type=kind, source=source,
        user_id=user_id, sent_at=timestamp, body="PRIVATE CUSTOMER TEXT",
    ))
    session.commit()


def test_inbound_due_time_and_idempotent_rebuild(sessions):
    with sessions() as session:
        message(session, "in-1", minutes_ago=10)
        row = refresh_conversation_state(session, ACCOUNT, "conv-1", sla_minutes=30, now=NOW)
        assert row.state == "needs_reply"
        assert row.response_due_at.replace(tzinfo=timezone.utc) == NOW + timedelta(minutes=20)
        assert row.last_inbound_message_id == "in-1"
        session.commit()

        refreshed = refresh_conversation_state(session, ACCOUNT, "conv-1", sla_minutes=30, now=NOW)
        assert refreshed.state == "needs_reply"
        assert session.query(LeadState).count() == 1
        assert tracker_summary(session, ACCOUNT, now=NOW)["response_overdue"] == 0
        assert tracker_summary(session, ACCOUNT, now=NOW + timedelta(minutes=21))["response_overdue"] == 1


def test_out_of_order_messages_and_confirmed_replies(sessions):
    with sessions() as session:
        message(session, "in-1", minutes_ago=30)
        message(session, "auto-1", minutes_ago=25, direction="outbound", source="workflow", user_id="bot")
        row = refresh_conversation_state(session, ACCOUNT, "conv-1", now=NOW)
        assert row.state == "needs_reply"  # A workflow did not answer the customer.

        message(session, "unknown-out", minutes_ago=20, direction="outbound", source="app")
        row = refresh_conversation_state(session, ACCOUNT, "conv-1", now=NOW)
        assert row.state == "needs_review"
        assert row.review_reason == "unverified_outbound_reply"

        message(session, "rep-1", minutes_ago=15, direction="outbound", user_id="rep-1")
        row = refresh_conversation_state(session, ACCOUNT, "conv-1", now=NOW)
        assert row.state == "waiting_customer"
        assert row.response_due_at is None

        message(session, "late-imported-old", minutes_ago=40)
        row = refresh_conversation_state(session, ACCOUNT, "conv-1", now=NOW)
        assert row.state == "waiting_customer"  # Late imports cannot reset the clock.

        message(session, "in-2", minutes_ago=5)
        row = refresh_conversation_state(session, ACCOUNT, "conv-1", now=NOW)
        assert row.state == "needs_reply"
        assert row.last_inbound_message_id == "in-2"


def test_missing_timestamps_or_unsupported_channels_require_review(sessions):
    with sessions() as session:
        message(session, "in-1", minutes_ago=10)
        message(session, "missing-date", minutes_ago=None)
        row = refresh_conversation_state(session, ACCOUNT, "conv-1", now=NOW)
        assert row.state == "needs_review"
        assert row.response_due_at is None
        session.query(GHLMessage).filter(GHLMessage.ghl_message_id == "missing-date").delete()
        session.commit()

        message(session, "phone-call", minutes_ago=1, kind="TYPE_CALL")
        row = refresh_conversation_state(session, ACCOUNT, "conv-1", now=NOW)
        assert row.state == "needs_review"
        assert row.review_reason == "latest_channel_not_supported"


def test_campaign_message_after_inbound_does_not_hide_unanswered_lead(sessions):
    with sessions() as session:
        message(session, "in-1", minutes_ago=20)
        message(
            session, "promo", minutes_ago=10, direction="outbound",
            kind="TYPE_CAMPAIGN_SMS", source="campaign", user_id="system",
        )
        row = refresh_conversation_state(session, ACCOUNT, "conv-1", now=NOW)
        assert row.state == "needs_reply"
        assert row.last_inbound_message_id == "in-1"


def test_conversation_scoping_paged_rebuild_and_no_message_body_leak(sessions):
    with sessions() as session:
        for i in range(3):
            message(session, f"in-{i}", conversation=f"conv-{i}", minutes_ago=60)
        message(session, "another-in", account=OTHER, minutes_ago=3)

        result = rebuild_lead_states(session, ACCOUNT, limit=2, sla_minutes=30, now=NOW)
        assert result == {"processed": 2, "has_more": True, "next_after": "conv-1"}
        last = rebuild_lead_states(session, ACCOUNT, limit=2, after=result["next_after"], now=NOW)
        assert last == {"processed": 1, "has_more": False, "next_after": None}
        assert session.query(LeadState).count() == 3

        assert rebuild_lead_states(session, ACCOUNT)["processed"] == 0
        summary = tracker_summary(session, ACCOUNT, now=NOW)
        assert summary["conversations_tracked"] == 3
        assert summary["response_overdue"] == 3
        leads = list_tracked_leads(session, ACCOUNT, overdue_only=True, now=NOW)
        assert len(leads) == 3
        assert "PRIVATE CUSTOMER TEXT" not in str(leads)
        assert all(item["overdue"] for item in leads)


def test_ingestion_refreshes_only_when_enabled(sessions, monkeypatch):
    with sessions() as session:
        first_at = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
        second_at = (datetime.now(timezone.utc) - timedelta(minutes=2)).isoformat()
        monkeypatch.setattr(settings, "tracker_enabled", False)
        run = GHLSyncRun(account_id=ACCOUNT)
        session.add(run)
        _store_conversation(session, ACCOUNT, {"id": "conv-1", "contactId": "contact-1"}, [
            {"id": "in-1", "conversationId": "conv-1", "locationId": ACCOUNT,
             "contactId": "contact-1", "direction": "inbound", "messageType": "TYPE_SMS",
             "dateAdded": first_at},
        ], run)
        session.commit()
        assert session.query(LeadState).count() == 0

        monkeypatch.setattr(settings, "tracker_enabled", True)
        monkeypatch.setattr(settings, "tracker_response_sla_minutes", 15)
        _store_conversation(session, ACCOUNT, {"id": "conv-1", "contactId": "contact-1"}, [
            {"id": "in-2", "conversationId": "conv-1", "locationId": ACCOUNT,
             "contactId": "contact-1", "direction": "inbound", "messageType": "TYPE_SMS",
             "dateAdded": second_at},
        ], run)
        session.commit()
        row = session.scalar(select(LeadState))
        assert row.state == "needs_reply"
        assert row.response_sla_minutes == 15


def test_internal_api_is_secret_protected_and_read_only(sessions, monkeypatch):
    import clearcoat.main as main

    monkeypatch.setattr(main, "SessionLocal", sessions)
    monkeypatch.setattr(settings, "sync_trigger_secret", "test-secret")
    monkeypatch.setattr(settings, "ghl_private_token", "test-token")
    monkeypatch.setattr(settings, "ghl_location_id", ACCOUNT)
    monkeypatch.setattr(settings, "tracker_enabled", True)
    client = TestClient(main.app)

    assert client.get("/internal/tracker/summary").status_code == 401
    headers = {"X-Sync-Secret": "test-secret"}
    with sessions() as session:
        message(session, "in-1", minutes_ago=3)
    result = client.post("/internal/tracker/rebuild", headers=headers)
    assert result.status_code == 200
    assert result.json()["processed"] == 1
    listing = client.get("/internal/tracker/leads", headers=headers)
    assert listing.status_code == 200
    assert len(listing.json()["leads"]) == 1
    assert "PRIVATE CUSTOMER TEXT" not in listing.text
    assert client.get("/internal/tracker/summary", headers=headers).status_code == 200

    # An active sync must not race a historical tracker rebuild.
    assert main._sync_guard.acquire(blocking=False)
    try:
        assert client.post("/internal/tracker/rebuild", headers=headers).status_code == 409
    finally:
        main._sync_guard.release()

    monkeypatch.setattr(settings, "tracker_enabled", False)
    assert client.get("/internal/tracker/leads", headers=headers).status_code == 503
