import hashlib
import json
from fastapi import FastAPI, HTTPException, Request
from sqlalchemy.exc import IntegrityError
from .config import settings
from .database import SessionLocal
from .models import InboundEvent
from .security import verify_ghl_signature

app = FastAPI(title="Clearcoat", version="0.1.0")


@app.get("/health")
def health():
    return {"status": "ok", "service": "clearcoat"}


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

