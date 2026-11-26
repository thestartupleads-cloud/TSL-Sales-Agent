import hashlib
import hmac
import json
from datetime import datetime
from threading import Lock

from fastapi import FastAPI, Header, HTTPException, Query, Request
from sqlalchemy.exc import IntegrityError
from .config import settings
from .database import SessionLocal
from .ghl_client import GHLAPIError, GHLClient
from .ghl_export_sync import sync_export_window, sync_incremental
from .ghl_sync import sync_recent_conversations
from .models import InboundEvent
from .security import verify_ghl_signature
from .tracker import list_tracked_leads, rebuild_lead_states, tracker_summary

app = FastAPI(title="Clearcoat", version="0.1.0")
_sync_guard = Lock()


@app.get("/health")
def health():
    return {"status": "ok", "service": "clearcoat"}


def _validate_sync_secret(value: str | None) -> None:
    if not settings.sync_trigger_secret or not settings.ghl_private_token or not settings.ghl_location_id:
        raise HTTPException(503, "GHL sync is not configured")
    if not value or not hmac.compare_digest(value, settings.sync_trigger_secret):
        raise HTTPException(401, "Unauthorized")


def _validate_tracker_access(value: str | None) -> None:
    _validate_sync_secret(value)
    if not settings.tracker_enabled:
        raise HTTPException(503, "Lead tracker has not been enabled")


@app.get("/internal/tracker/summary")
def internal_tracker_summary(
    x_sync_secret: str | None = Header(default=None, alias="X-Sync-Secret"),
    active_days: int = Query(default=14, ge=1, le=90),
):
    """PII-free, read-only metrics for the single configured GHL location."""
    _validate_tracker_access(x_sync_secret)
    with SessionLocal() as session:
        return tracker_summary(session, settings.ghl_location_id, active_days=active_days)


@app.get("/internal/tracker/leads")
def internal_tracker_leads(
    x_sync_secret: str | None = Header(default=None, alias="X-Sync-Secret"),
    state: str | None = Query(default=None, pattern="^(needs_reply|waiting_customer|needs_review|outbound_only)$"),
    overdue_only: bool = False,
    active_days: int = Query(default=14, ge=1, le=90),
    limit: int = Query(default=50, ge=1, le=100),
):
    """Return IDs and state timestamps only, never conversation bodies."""
    _validate_tracker_access(x_sync_secret)
    with SessionLocal() as session:
        return {"leads": list_tracked_leads(
            session, settings.ghl_location_id, state=state,
            overdue_only=overdue_only, active_days=active_days, limit=limit,
        )}


@app.post("/internal/tracker/rebuild")
def internal_tracker_rebuild(
    x_sync_secret: str | None = Header(default=None, alias="X-Sync-Secret"),
    limit: int = Query(default=100, ge=1, le=200),
    after: str | None = Query(default=None, max_length=128),
    force: bool = False,
):
    """Bounded historical replay over our database; no CRM network requests."""
    _validate_tracker_access(x_sync_secret)
    if not _sync_guard.acquire(blocking=False):
        raise HTTPException(409, "A sync or tracker rebuild is already running")
    try:
        with SessionLocal() as session:
            return rebuild_lead_states(
                session, settings.ghl_location_id,
                limit=limit, after=after, force=force,
                sla_minutes=settings.tracker_response_sla_minutes,
            )
    finally:
        _sync_guard.release()


@app.post("/internal/sync/ghl")
def manual_ghl_sync(
    x_sync_secret: str | None = Header(default=None, alias="X-Sync-Secret"),
    limit: int = Query(default=5, ge=1, le=5),
):
    """Protected pilot sync, currently capped at five Iconic Wraps conversations."""
    _validate_sync_secret(x_sync_secret)
    if not _sync_guard.acquire(blocking=False):
        raise HTTPException(409, "A sync is already running on this instance")

    try:
        with GHLClient(settings.ghl_private_token) as client, SessionLocal() as session:
            return sync_recent_conversations(session, client, settings.ghl_location_id, limit=limit)
    except GHLAPIError as exc:
        raise HTTPException(exc.status_code, exc.code) from None
    finally:
        _sync_guard.release()


@app.post("/internal/sync/ghl/incremental")
def incremental_ghl_sync(x_sync_secret: str | None = Header(default=None, alias="X-Sync-Secret")):
    """Read-only catch-up from durable per-stream UTC time checkpoints."""
    _validate_sync_secret(x_sync_secret)
    if not _sync_guard.acquire(blocking=False):
        raise HTTPException(409, "A sync is already running on this instance")
    try:
        with GHLClient(settings.ghl_private_token) as client, SessionLocal() as session:
            return sync_incremental(session, client, settings.ghl_location_id)
    except GHLAPIError as exc:
        raise HTTPException(exc.status_code, exc.code) from None
    finally:
        _sync_guard.release()


@app.post("/internal/sync/ghl/window")
def historical_ghl_window(
    start_at: datetime,
    end_at: datetime,
    stream: str = Query(default="non_email", pattern="^(non_email|email)$"),
    x_sync_secret: str | None = Header(default=None, alias="X-Sync-Secret"),
):
    """Manually backfill one bounded UTC window without modifying cron checkpoints."""
    _validate_sync_secret(x_sync_secret)
    if not _sync_guard.acquire(blocking=False):
        raise HTTPException(409, "A sync is already running on this instance")
    try:
        with GHLClient(settings.ghl_private_token) as client, SessionLocal() as session:
            return sync_export_window(session, client, settings.ghl_location_id, start_at, end_at, stream=stream)
    except GHLAPIError as exc:
        raise HTTPException(exc.status_code, exc.code) from None
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None
    finally:
        _sync_guard.release()


@app.post("/webhooks/ghl", status_code=202)
async def ghl_webhook(request: Request):
    body = await request.body()
    if len(body) > 1024 * 1024:
        raise HTTPException(413, "Payload too large")
    if not verify_ghl_signature(body, request.headers.get("X-GHL-Signature"), settings.ghl_webhook_public_key):
        raise HTTPException(401, "Invalid signature")
    try:
        payload = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        raise HTTPException(400, "Invalid JSON")
    if not isinstance(payload, dict):
        raise HTTPException(400, "Expected event object")
    # Payload format varies across GHL event families; this field mapping is validated in M0.
    location = payload.get("locationId") or payload.get("location_id")
    if not location or location not in settings.allowed_locations:
        raise HTTPException(403, "Location not allowed")
    event_type = str(payload.get("type") or "unknown")[:128]
    # Prefer a provider event ID when available; otherwise hash the exact signed body.
    source_id = payload.get("webhookId") or payload.get("eventId")
    dedup_key = str(source_id)[:128] if source_id else hashlib.sha256(body).hexdigest()
    event = InboundEvent(account_id=location, dedup_key=dedup_key, event_type=event_type, source_payload=payload)
    with SessionLocal() as session:
        session.add(event)
        try:
            session.commit()
        except IntegrityError:
            session.rollback()
            return {"accepted": True, "duplicate": True}
    return {"accepted": True, "duplicate": False}

