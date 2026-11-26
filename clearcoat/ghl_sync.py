"""Durable, duplicate-safe ingestion of a bounded sample of GHL conversations."""

from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .ghl_client import GHLAPIError, GHLClient
from .config import settings
from .models import GHLConversation, GHLMessage, GHLSyncRun


def _timestamp(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return result if result.tzinfo else result.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _optional_text(value: object, max_length: int) -> str | None:
    return value[:max_length] if isinstance(value, str) else None


def _store_conversation(
    session: Session, account_id: str, conversation: dict, messages: list[dict], run: GHLSyncRun
) -> None:
    conversation_id = conversation["id"]
    contact_id = _optional_text(conversation.get("contactId"), 128)
    row = session.get(GHLConversation, (account_id, conversation_id))
    if row is None:
        session.add(GHLConversation(account_id=account_id, conversation_id=conversation_id, contact_id=contact_id))
    else:
        row.contact_id = contact_id
        row.last_seen_at = datetime.now(timezone.utc)

    inserted = 0
    for message in messages:
        run.messages_seen += 1
        message_id = message.get("id")
        if (
            not isinstance(message_id, str)
            or not 0 < len(message_id) <= 128
            or message.get("locationId") != account_id
            or message.get("conversationId") != conversation_id
        ):
            run.messages_skipped += 1
            continue
        entry = GHLMessage(
            account_id=account_id,
            ghl_message_id=message_id,
            conversation_id=conversation_id,
            contact_id=_optional_text(message.get("contactId"), 128) or contact_id,
            direction=_optional_text(message.get("direction"), 24),
            message_type=_optional_text(message.get("messageType"), 64),
            body=message.get("body") if isinstance(message.get("body"), str) else None,
            source=_optional_text(message.get("source"), 64),
            user_id=_optional_text(message.get("userId"), 128),
            sent_at=_timestamp(message.get("dateAdded")),
        )
        try:
            # Savepoints preserve other messages if a duplicate ID races with another sync.
            with session.begin_nested():
                session.add(entry)
                session.flush()
        except IntegrityError:
            run.messages_duplicate += 1
        else:
            run.messages_inserted += 1
            inserted += 1

    # Opt-in only. Initial deployment cannot affect the working sync until the
    # additive lead_states table exists and TRACKER_ENABLED is explicitly set.
    if inserted and settings.tracker_enabled:
        from .tracker import refresh_conversation_state

        refresh_conversation_state(
            session,
            account_id,
            conversation_id,
            sla_minutes=settings.tracker_response_sla_minutes,
        )


def sync_recent_conversations(
    session: Session, client: GHLClient, account_id: str, *, limit: int = 5, max_pages: int = 3
) -> dict:
    """Fetch a small newest-first sample; never requests a GHL write operation."""
    if not 1 <= limit <= 5 or not 1 <= max_pages <= 10:
        raise ValueError("Sync limits are outside the allowed test range")

    run_id = str(uuid4())
    run = GHLSyncRun(id=run_id, account_id=account_id, status="running")
    session.add(run)
    session.commit()

    try:
        conversations = client.search_recent_conversations(account_id, limit=limit)
        for conversation in conversations:
            conv_id = conversation.get("id")
            if (
                not isinstance(conv_id, str)
                or not 0 < len(conv_id) <= 128
                or conversation.get("locationId") != account_id
            ):
                raise GHLAPIError("ghl_wrong_location_or_conversation")

            messages, truncated = client.get_messages(conv_id, max_pages=max_pages)
            _store_conversation(session, account_id, conversation, messages, run)
            run.conversations_seen += 1
            if truncated:
                run.truncated_conversations += 1
            session.commit()

        run.status = "partial" if run.truncated_conversations or run.messages_skipped else "completed"
        run.finished_at = datetime.now(timezone.utc)
        session.commit()
    except Exception as exc:
        session.rollback()
        run = session.get(GHLSyncRun, run_id)
        if run is not None:
            run.status = "failed"
            run.error_code = exc.code if isinstance(exc, GHLAPIError) else "sync_internal_error"
            run.finished_at = datetime.now(timezone.utc)
            session.commit()
        raise

    return {
        "run_id": run_id,
        "status": run.status,
        "conversations_seen": run.conversations_seen,
        "messages_seen": run.messages_seen,
        "messages_inserted": run.messages_inserted,
        "messages_duplicate": run.messages_duplicate,
        "messages_skipped": run.messages_skipped,
        "truncated_conversations": run.truncated_conversations,
    }
