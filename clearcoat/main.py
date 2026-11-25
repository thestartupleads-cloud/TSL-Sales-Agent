import hashlib
import hmac
import json
from threading import Lock

from fastapi import FastAPI, Header, HTTPException, Query, Request
from sqlalchemy.exc import IntegrityError
from .config import settings
from .database import SessionLocal
from .ghl_client import GHLAPIError, GHLClient
from .ghl_sync import sync_recent_conversations
from .models import InboundEvent
from .security import verify_ghl_signature

app = FastAPI(title="Clearcoat", version="0.1.0")
_sync_guard = Lock()


@app.get("/health")
def health():
    return {"status": "ok", "service": "clearcoat"}


@app.post("/internal/sync/ghl")
def manual_ghl_sync(
    x_sync_secret: str | None = Header(default=None, alias="X-Sync-Secret"),
    limit: int = Query(default=5, ge=1, le=5),
):
    """Protected pilot sync, currently capped at five Iconic Wraps conversations."""
    if not settings.sync_trigger_secret or not settings.ghl_private_token or not settings.ghl_location_id:
        raise HTTPException(503, "GHL sync is not configured")
    if not x_sync_secret or not hmac.compare_digest(x_sync_secret, settings.sync_trigger_secret):
        raise HTTPException(401, "Unauthorized")
    if not _sync_guard.acquire(blocking=False):
        raise HTTPException(409, "A sync is already running on this instance")

    try:
        with GHLClient(settings.ghl_private_token) as client, SessionLocal() as session:
            return sync_recent_conversations(session, client, settings.ghl_location_id, limit=limit)
    except GHLAPIError as exc:
        raise HTTPException(exc.status_code, exc.code) from None
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

