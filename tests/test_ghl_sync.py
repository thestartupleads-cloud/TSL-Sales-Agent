"""No live GHL or PostgreSQL credentials are used by these tests."""

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from clearcoat.database import Base
from clearcoat.ghl_client import GHLAPIError, GHLClient
from clearcoat.ghl_sync import sync_recent_conversations
from clearcoat.main import app
from clearcoat.models import GHLConversation, GHLMessage, GHLSyncRun


LOCATION = "iconic-test-location"


@pytest.fixture
def sessions():
    engine = create_engine("sqlite+pysqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    try:
        yield sessionmaker(bind=engine, expire_on_commit=False)
    finally:
        engine.dispose()


def mock_ghl(*, error_status: int | None = None, wrong_location: bool = False):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer testing-only"
        assert request.headers["Version"] == "v3"
        assert request.method == "GET"
        calls.append(request)
        if error_status:
            return httpx.Response(error_status, json={"message": "no sensitive details"})
        if request.url.path == "/conversations/search":
            assert request.url.params["locationId"] == LOCATION
            assert request.url.params["sortBy"] == "last_message_date"
            assert request.url.params["sort"] == "desc"
            return httpx.Response(
                200,
                json={"conversations": [
                    {"id": "conversation-1", "locationId": "OTHER" if wrong_location else LOCATION, "contactId": "contact-1"},
                    {"id": "conversation-2", "locationId": LOCATION, "contactId": "contact-2"},
                ], "total": 2},
            )
        if request.url.path == "/conversations/conversation-1/messages":
            if "lastMessageId" not in request.url.params:
                return httpx.Response(200, json={"messages": {"nextPage": True, "lastMessageId": "message-1", "messages": [
                    {"id": "message-1", "locationId": LOCATION, "conversationId": "conversation-1", "contactId": "contact-1", "body": "PPF price?", "direction": "inbound", "messageType": "TYPE_SMS", "dateAdded": "2026-11-20T10:30:00.000Z"}
                ]}})
            assert request.url.params["lastMessageId"] == "message-1"
            return httpx.Response(200, json={"messages": {"nextPage": False, "lastMessageId": "message-2", "messages": [
                {"id": "message-2", "locationId": LOCATION, "conversationId": "conversation-1", "contactId": "contact-1", "body": "Which vehicle?", "direction": "outbound", "messageType": "TYPE_SMS", "source": "app", "userId": "rep-1"},
                {"id": "bad-other-account", "locationId": "different-location", "conversationId": "conversation-1", "body": "MUST NOT STORE"},
            ]}})
        if request.url.path == "/conversations/conversation-2/messages":
            return httpx.Response(200, json={"messages": {"nextPage": False, "lastMessageId": "message-3", "messages": [
                {"id": "message-3", "locationId": LOCATION, "conversationId": "conversation-2", "contactId": "contact-2", "body": "Hello", "direction": "inbound"},
            ]}})
        raise AssertionError(f"Unexpected URL path: {request.url.path}")

    return httpx.MockTransport(handler), calls


def test_read_only_sync_and_idempotency(sessions):
    transport, requests = mock_ghl()
    with GHLClient("testing-only", transport=transport) as client, sessions() as session:
        first = sync_recent_conversations(session, client, LOCATION)
        assert first["status"] == "partial"  # The other account's message was safely skipped.
        assert first["conversations_seen"] == 2
        assert first["messages_seen"] == 4
        assert first["messages_inserted"] == 3
        assert first["messages_skipped"] == 1
        assert first["messages_duplicate"] == 0
        assert session.query(GHLConversation).count() == 2
        assert session.query(GHLMessage).count() == 3
        assert session.scalar(select(GHLMessage).where(GHLMessage.ghl_message_id == "message-1")).sent_at is not None

        second = sync_recent_conversations(session, client, LOCATION)
        assert second["messages_inserted"] == 0
        assert second["messages_duplicate"] == 3
        assert session.query(GHLMessage).count() == 3
        assert session.query(GHLSyncRun).count() == 2

    assert len(requests) == 8  # Two searches and three message pages per sync.
    assert all(r.method == "GET" for r in requests)


def test_wrong_location_is_rejected_and_run_audited(sessions):
    transport, _ = mock_ghl(wrong_location=True)
    with GHLClient("testing-only", transport=transport) as client, sessions() as session:
        with pytest.raises(GHLAPIError, match="ghl_wrong_location_or_conversation"):
            sync_recent_conversations(session, client, LOCATION)
        assert session.query(GHLMessage).count() == 0
        run = session.scalars(select(GHLSyncRun)).one()
        assert run.status == "failed"
        assert run.error_code == "ghl_wrong_location_or_conversation"


def test_rate_limit_does_not_retry_or_store_customer_content(sessions):
    transport, requests = mock_ghl(error_status=429)
    with GHLClient("testing-only", transport=transport) as client, sessions() as session:
        with pytest.raises(GHLAPIError) as exc:
            sync_recent_conversations(session, client, LOCATION)
        assert exc.value.status_code == 429
        assert exc.value.code == "ghl_rate_limited"
        assert len(requests) == 1
        run = session.scalars(select(GHLSyncRun)).one()
        assert run.status == "failed"
        assert run.error_code == "ghl_rate_limited"
        assert session.query(GHLMessage).count() == 0


def test_manual_endpoint_requires_config_and_header(monkeypatch, sessions):
    from clearcoat import main

    monkeypatch.setattr(main.settings, "sync_trigger_secret", "")
    monkeypatch.setattr(main.settings, "ghl_private_token", "testing-only")
    monkeypatch.setattr(main.settings, "ghl_location_id", LOCATION)
    api = TestClient(app)
    assert api.post("/internal/sync/ghl").status_code == 503

    monkeypatch.setattr(main.settings, "sync_trigger_secret", "local-testing-secret")
    assert api.post("/internal/sync/ghl").status_code == 401
    assert api.post("/internal/sync/ghl", headers={"X-Sync-Secret": "bad"}).status_code == 401
    assert api.post("/internal/sync/ghl?limit=200", headers={"X-Sync-Secret": "local-testing-secret"}).status_code == 422

    transport, _ = mock_ghl()
    monkeypatch.setattr(main, "GHLClient", lambda _token: GHLClient("testing-only", transport=transport))
    monkeypatch.setattr(main, "SessionLocal", sessions)
    good = api.post("/internal/sync/ghl", headers={"X-Sync-Secret": "local-testing-secret"})
    assert good.status_code == 200
    assert good.json()["messages_inserted"] == 3
