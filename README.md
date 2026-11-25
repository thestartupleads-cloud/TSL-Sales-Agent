# Clearcoat - M1 read-only GHL sync pilot

The first Iconic Wraps integration uses a GHL sub-account Private Integration Token (PIT).
This module **only reads** conversations and messages from GHL and stores them in PostgreSQL.
No AI classification, follow-up actions, GHL notes, customer messages or automation are active yet.

## Local setup

1. Use Python 3.11 and a PostgreSQL database (local or Render).
2. Copy `.env.example` to `.env` and populate your own secrets; never commit `.env`.
3. Install dependencies: `python -m pip install -e '.[test]'`.
4. Create additive database tables: `python -m clearcoat.init_db`.
5. Run: `uvicorn clearcoat.main:app --reload`.
6. Run the test suite: `python -m pytest -q`.

## Render environment settings

| Variable | Purpose |
| --- | --- |
| `DATABASE_URL` | Render PostgreSQL internal URL, using `postgresql+psycopg://` |
| `GHL_PRIVATE_TOKEN` | Iconic Wraps sub-account PIT (server-side secret) |
| `GHL_LOCATION_ID` | Exact Iconic Wraps location ID |
| `SYNC_TRIGGER_SECRET` | Separate long, random secret for the protected trigger |
| `GHL_WRITE_ENABLED=false` | Keep GHL writes disabled |
| `CUSTOMER_SEND_ENABLED=false` | Keep customer messaging disabled |
| `APP_ENV=staging` | Label the staging environment |
| `PYTHON_VERSION=3.11.9` | Render Python version (when configured there) |

PIT scopes required: `conversations.readonly` and `conversations/message.readonly`.
Do **not** paste any secret into ChatGPT, GitHub, screenshots, request query strings or application logs.

## Deploy changes to Render

After copying these files into your existing local Git checkout, run your normal tests
and push to `main` yourself. The updated dependency list installs `httpx`.
Because `create_all()` adds tables but does not modify existing tables, run the
following **temporary** Render Start Command for the first new-schema deployment:

```sh
python -m clearcoat.init_db && uvicorn clearcoat.main:app --host 0.0.0.0 --port $PORT
```

After logs show `Initialized Clearcoat tables`, change the Render Start Command
back to:

```sh
uvicorn clearcoat.main:app --host 0.0.0.0 --port $PORT
```

Set `SYNC_TRIGGER_SECRET` in Render to a **different** randomly generated value
from your GHL token. Do not put the trigger secret in the Git repository.

## Safely test the first five conversations

From **your own** PowerShell session, without putting secrets into terminal history:

```powershell
$renderUrl = 'https://YOUR-RENDER-SERVICE.onrender.com'
$syncSecret = Read-Host 'Enter Render sync trigger secret'
$headers = @{ 'X-Sync-Secret' = $syncSecret }
Invoke-RestMethod -Uri "$renderUrl/internal/sync/ghl?limit=5" -Method POST -Headers $headers
Remove-Variable syncSecret,headers
```

The JSON result includes a run ID and counts, **not** customer message bodies.
Run the same command again: previously inserted messages should be counted as
`messages_duplicate`, with `messages_inserted` normally equal to zero if no new
messages appeared. `status=partial` means some messages were skipped or the
configured page limit was reached; this first pilot intentionally reads at most
three pages (up to 60 messages) per conversation. Check the response before
assuming the initial import is complete.

Common errors:

- `401`: Trigger secret missing or incorrect.
- `503`: A required Render environment variable is missing.
- `502 ghl_auth_or_scope_error`: PIT, selected location or required scopes may be incorrect.
- `429 ghl_rate_limited`: GHL quota reached; retry later, not immediately.
- `409`: Another sync is already running on the same web instance.

## Limits and next implementation slice

- Current search checks **only the five most recently active conversations**, not
  every conversation in the account. Do not claim complete ongoing monitoring yet.
- GHL messages are stored in PostgreSQL and may contain customer information;
  restrict database access, set retention/backup policies, and get client approval.
- Each message is unique on `(account_id, ghl_message_id)`, so repeated polling is safe.
- Run metadata is stored in `ghl_sync_runs` without customer text.
- No timer worker, cron scheduler, historical backfill, automatic retries, Redis,
  AI prompts or GHL writes are included. Those are subsequent milestones.
- The older marketplace webhook route is **not part of this PIT integration**.
  Leave `GHL_WEBHOOK_PUBLIC_KEY` and `GHL_ALLOWED_LOCATION_IDS` blank; do not
  configure that webhook until its signature scheme has been separately verified.
- This single-instance trigger uses a process lock. Add a database-level lock
  before running multiple Render instances or automated concurrent schedulers.

Official GHL API docs:

- https://marketplace.gohighlevel.com/docs/ghl/conversations/search-conversation/
- https://marketplace.gohighlevel.com/docs/ghl/conversations/get-messages/
- https://marketplace.gohighlevel.com/docs/other/rate-limits/

