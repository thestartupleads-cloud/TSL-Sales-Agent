from datetime import datetime, timedelta, timezone

import httpx
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from clearcoat.database import Base
from clearcoat.ghl_client import GHLAPIError, GHLClient
from clearcoat.ghl_export_sync import sync_export_window, sync_incremental
from clearcoat.models import GHLExportCheckpoint, GHLMessage, GHLSyncRun


LOCATION = "iconic-test-location"
NOW = datetime(2026, 11, 26, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def sessions():
    engine = create_engine("sqlite+pysqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    try:
        yield sessionmaker(bind=engine, expire_on_commit=False)
    finally:
        engine.dispose()


def export_transport(*, fail_next_page=False, wrong_location=False):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        assert request.method == "GET"
        assert request.url.path == "/conversations/messages/export"
        assert request.headers["Version"] == "v3"
        assert request.url.params["locationId"] == LOCATION
        assert request.url.params["sortBy"] == "createdAt"
        assert request.url.params["sortOrder"] == "asc"
        assert "startDate" in request.url.params and "endDate" in request.url.params

        if request.url.params.get("channel") == "Email":
            return httpx.Response(200, json={"messages": [{
                "id": "email-1", "conversationId": "conv-email", "contactId": "c-2",
                "locationId": LOCATION, "body": "What is the PPF warranty?", "messageType": "TYPE_EMAIL",
            }], "nextCursor": None, "total": 1})

        if "cursor" not in request.url.params:
            return httpx.Response(200, json={"messages": [{
                "id": "sms-1", "conversationId": "conv-1", "contactId": "c-1",
                "locationId": "not-iconic" if wrong_location else LOCATION,
                "body": "Full front PPF price?", "messageType": "TYPE_SMS",
                "direction": "inbound", "dateAdded": "2026-11-25T22:50:00.000Z",
            }], "nextCursor": "page-two", "total": 2})
        assert request.url.params["cursor"] == "page-two"
        if fail_next_page:
            return httpx.Response(429, json={"message": "rate limited"})
        return httpx.Response(200, json={"messages": [{
            "id": "sms-2", "conversationId": "conv-1", "contactId": "c-1",
            "locationId": LOCATION, "body": "Sedan or SUV?", "direction": "outbound",
        }], "nextCursor": None, "total": 2})

    return httpx.MockTransport(handler), calls


def test_manual_window_and_repeated_window_are_idempotent(sessions):
    transport, calls = export_transport()
    with GHLClient("testing-only", transport=transport) as client, sessions() as session:
        start = NOW - timedelta(hours=12)
        first = sync_export_window(session, client, LOCATION, start, NOW, stream="non_email")
        assert first["status"] == "completed"
        assert first["messages_inserted"] == 2
        assert first["conversations_seen"] == 2  # Two pages of the same conversation.
        assert session.query(GHLMessage).count() == 2
        assert session.query(GHLExportCheckpoint).count() == 0  # Manual backfill must not advance cron.

        again = sync_export_window(session, client, LOCATION, start, NOW, stream="non_email")
        assert again["messages_duplicate"] == 2
        assert again["messages_inserted"] == 0
        assert session.query(GHLMessage).count() == 2
    assert len(calls) == 4


def test_both_streams_incremental_checkpoints_resume(sessions):
    transport, calls = export_transport()
    with GHLClient("testing-only", transport=transport) as client, sessions() as session:
        first = sync_incremental(session, client, LOCATION, now=NOW)
        assert first["status"] == "completed"
        assert {r["stream"] for r in first["streams"]} == {"non_email", "email"}
        assert session.query(GHLMessage).count() == 3
        assert session.query(GHLExportCheckpoint).count() == 2

        second = sync_incremental(session, client, LOCATION, now=NOW)
        assert second["streams"][0]["messages_inserted"] == 0
        assert second["streams"][1]["messages_inserted"] == 0

        done = sync_incremental(session, client, LOCATION, now=NOW)
        assert done["streams"] == []
        assert session.query(GHLMessage).count() == 3

        future = sync_incremental(session, client, LOCATION, now=NOW + timedelta(minutes=5))
        assert len(future["streams"]) == 2
        assert all(item["messages_inserted"] == 0 for item in future["streams"])

    assert len(calls) == 9  # Three windows: two non-email pages and one email page each.


def test_failed_export_keeps_checkpoint_and_records_failure(sessions):
    transport, calls = export_transport(fail_next_page=True)
    with GHLClient("testing-only", transport=transport) as client, sessions() as session:
        with pytest.raises(GHLAPIError) as exc:
            sync_incremental(session, client, LOCATION, now=NOW)
        assert exc.value.code == "ghl_rate_limited"
        checkpoint = session.get(GHLExportCheckpoint, (LOCATION, "non_email"))
        assert checkpoint is not None
        assert checkpoint.high_water_at.replace(tzinfo=timezone.utc) == NOW - timedelta(hours=1)
        assert session.query(GHLMessage).count() == 1  # First page is durable and safely retryable.
        run = session.scalars(select(GHLSyncRun)).one()
        assert run.status == "failed"
        assert run.error_code == "ghl_rate_limited"
    assert len(calls) == 2


def test_invalid_location_never_advances_checkpoint(sessions):
    transport, _ = export_transport(wrong_location=True)
    with GHLClient("testing-only", transport=transport) as client, sessions() as session:
        with pytest.raises(GHLAPIError, match="ghl_export_invalid_message"):
            sync_export_window(session, client, LOCATION, NOW - timedelta(minutes=10), NOW, stream="non_email")
        assert session.query(GHLMessage).count() == 0
        assert session.query(GHLExportCheckpoint).count() == 0


def test_window_larger_than_day_is_rejected_before_api(sessions):
    transport, calls = export_transport()
    with GHLClient("testing-only", transport=transport) as client, sessions() as session:
        with pytest.raises(ValueError):
            sync_export_window(session, client, LOCATION, NOW - timedelta(days=2), NOW, stream="non_email")
    assert calls == []
