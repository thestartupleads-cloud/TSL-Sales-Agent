"""Bounded, resumable location-wide GHL message synchronization.

GHL export cursors expire after two minutes, so only completed date windows are
checkpointed. A failed or oversized window is safely retried using message IDs.
"""

from collections import defaultdict
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from sqlalchemy.orm import Session

from .ghl_client import GHLAPIError, GHLClient
from .ghl_sync import _store_conversation
from .models import GHLExportCheckpoint, GHLSyncRun


STREAM_CHANNELS = {"non_email": None, "email": "Email"}
MAX_WINDOW = timedelta(hours=24)
INCREMENTAL_WINDOW = timedelta(minutes=30)
OVERLAP = timedelta(minutes=2)


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("A UTC timezone offset is required")
    return value.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return _utc(value).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _persist_export_page(session: Session, account_id: str, messages: list[dict], run: GHLSyncRun) -> None:
    grouped: dict[str, list[dict]] = defaultdict(list)
    contacts: dict[str, str | None] = {}

    for message in messages:
        conversation_id = message.get("conversationId")
        message_id = message.get("id")
        if (
            message.get("locationId") != account_id
            or not isinstance(conversation_id, str)
            or not 0 < len(conversation_id) <= 128
            or not isinstance(message_id, str)
            or not 0 < len(message_id) <= 128
        ):
            # Fail closed rather than advancing a checkpoint past an unrecorded message.
            raise GHLAPIError("ghl_export_invalid_message")
        grouped[conversation_id].append(message)
        contact_id = message.get("contactId")
        contacts[conversation_id] = contact_id if isinstance(contact_id, str) else None

    for conversation_id, items in grouped.items():
        _store_conversation(
            session,
            account_id,
            {"id": conversation_id, "contactId": contacts[conversation_id]},
            items,
            run,
        )
    run.conversations_seen += len(grouped)


def sync_export_window(
    session: Session,
    client: GHLClient,
    account_id: str,
    start_at: datetime,
    end_at: datetime,
    *,
    stream: str,
    max_pages: int = 10,
    advance_checkpoint: bool = False,
) -> dict:
    """Import one bounded time window for one stream; checkpoint only on full success."""
    start_at, end_at = _utc(start_at), _utc(end_at)
    if stream not in STREAM_CHANNELS or not 1 <= max_pages <= 20:
        raise ValueError("Unsupported stream or page count")
    if not timedelta(0) < end_at - start_at <= MAX_WINDOW:
        raise ValueError("Time window must be more than zero and at most 24 hours")

    run_id = str(uuid4())
    run = GHLSyncRun(id=run_id, account_id=account_id, status="running")
    session.add(run)
    session.commit()

    try:
        cursor = None
        seen_cursors: set[str] = set()
        for _ in range(max_pages):
            messages, next_cursor = client.export_message_page(
                account_id,
                _iso(start_at),
                _iso(end_at),
                channel=STREAM_CHANNELS[stream],
                cursor=cursor,
                limit=100,
            )
            _persist_export_page(session, account_id, messages, run)
            session.commit()

            if next_cursor is None:
                break
            if next_cursor in seen_cursors:
                raise GHLAPIError("ghl_export_repeated_cursor")
            seen_cursors.add(next_cursor)
            cursor = next_cursor
        else:
            # A short-lived GHL export cursor must never be persisted across cron runs.
            raise GHLAPIError("ghl_export_window_too_large", 409)

        if run.messages_skipped:
            raise GHLAPIError("ghl_export_skipped_message")

        if advance_checkpoint:
            checkpoint = session.get(GHLExportCheckpoint, (account_id, stream))
            if checkpoint is None:
                session.add(GHLExportCheckpoint(account_id=account_id, stream=stream, high_water_at=end_at))
            else:
                checkpoint.high_water_at = end_at

        run.status = "completed"
        run.finished_at = datetime.now(timezone.utc)
        session.commit()
    except Exception as exc:
        session.rollback()
        run = session.get(GHLSyncRun, run_id)
        if run is not None:
            run.status = "failed"
            run.error_code = exc.code if isinstance(exc, GHLAPIError) else "export_internal_error"
            run.finished_at = datetime.now(timezone.utc)
            session.commit()
        raise

    return {
        "run_id": run_id,
        "stream": stream,
        "status": run.status,
        "window_start": _iso(start_at),
        "window_end": _iso(end_at),
        "conversations_seen": run.conversations_seen,
        "messages_seen": run.messages_seen,
        "messages_inserted": run.messages_inserted,
        "messages_duplicate": run.messages_duplicate,
    }


def sync_incremental(
    session: Session,
    client: GHLClient,
    account_id: str,
    *,
    now: datetime | None = None,
) -> dict:
    """Catch up both export streams without ever skipping a failed time window."""
    now = _utc(now or datetime.now(timezone.utc))
    runs = []
    for stream in STREAM_CHANNELS:
        checkpoint = session.get(GHLExportCheckpoint, (account_id, stream))
        if checkpoint is None:
            # Commit the starting point first, so failed first runs don't drift forward.
            checkpoint = GHLExportCheckpoint(
                account_id=account_id,
                stream=stream,
                high_water_at=now - timedelta(hours=1),
            )
            session.add(checkpoint)
            session.commit()

        high_water = checkpoint.high_water_at
        if high_water.tzinfo is None:
            high_water = high_water.replace(tzinfo=timezone.utc)
        end_at = min(now, high_water + INCREMENTAL_WINDOW)
        if end_at <= high_water:
            continue

        runs.append(
            sync_export_window(
                session,
                client,
                account_id,
                high_water - OVERLAP,
                end_at,
                stream=stream,
                advance_checkpoint=True,
            )
        )

    return {"status": "completed", "streams": runs}
