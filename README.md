# Clearcoat — M1 foundation

First implementation slice: authenticated GoHighLevel webhook capture, duplicate-safe event persistence, an internal event router, and deterministic price-response timers. No customer-facing sends are implemented.

## Local setup

1. Provision PostgreSQL (locally or managed Render PostgreSQL).
2. Copy `.env.example` to `.env`, set `DATABASE_URL`, the GHL webhook public key, and the allowed location IDs.
3. Install: `pip install -e '.[test]'`.
4. Create tables: `python -m clearcoat.init_db` (development only; replace with Alembic migrations before production).
5. Run: `uvicorn clearcoat.main:app --reload`.
6. Test: `pytest -q`.

## First external integration

- Configure the GHL subscription to deliver to `POST /webhooks/ghl` using its documented Ed25519 signature.
- Confirm the exact webhook payloads and identifiers for the approved Iconic account, then add event-type adapters. Unrecognized events are safely recorded but do not create actions.
- Set `GHL_WRITE_ENABLED=false` during shadow testing. This starter does not write to GHL.
- Do not use production customer data until retention, access, secret handling, and GHL event coverage are approved.

## Architecture progression

The `webhooks` route stores the envelope, then a worker will process events from the durable ledger. Add account-specific Playbook rules, Celery/Redis scheduling, a reconciliation cursor, audit/outbox and an explicit Action Gateway before enabling GHL writes. The current synchronous event-save endpoint is intentionally only a first build slice.

