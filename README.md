# Clearcoat - M1 read-only GHL sync

The first Iconic Wraps integration uses a GHL sub-account Private Integration Token (PIT).
This module **only reads** conversations and messages from GHL and stores them in PostgreSQL.
It includes the verified five-conversation pilot and a new time-windowed, location-wide
message export path for incremental monitoring and manual historical backfill.
The optional M1 tracker adds read-only lead states and provisional reply
deadlines. No AI classification, GHL write actions or customer messaging is
active. See `M1_TRACKER_DEPLOY.md` for the safe, opt-in deployment sequence.

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
| `TRACKER_ENABLED=false` | Safe default: keep new lead tracker disabled until the additive table exists |
| `TRACKER_RESPONSE_SLA_MINUTES=30` | Pilot wall-clock deadline (confirm against approved playbook) |

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

## New: location-wide incremental monitoring (test before scheduling)

The incremental endpoint fetches location-wide messages using the documented
`GET /conversations/messages/export` API, in two streams: non-email and email.
It keeps separate durable UTC checkpoints in `ghl_export_checkpoints`.
The first call starts one hour back; later calls catch up in completed 30-minute
windows, overlapping two minutes to avoid time-boundary gaps. Failed windows
retain their previous checkpoints and are retried safely on the next invocation.

Once the new PostgreSQL table is initialized, run a **manual** verification:

```powershell
$renderUrl = 'https://clearcoat-api.onrender.com'
$syncSecret = Read-Host 'Enter your Render sync trigger secret'
$headers = @{ 'X-Sync-Secret' = $syncSecret }
Invoke-RestMethod -Method POST -Uri "$renderUrl/internal/sync/ghl/incremental" -Headers $headers
```

The response returns `status=completed`, a `streams` list, and PII-free counts.
Repeat the call to verify existing message IDs are duplicates, and check both
streams. A request can return `409 ghl_export_window_too_large` if a time window
has more than 1,000 matching messages; this protects the checkpoint rather than
silently losing records. Reduce the window size in code before enabling cron.

For manual historical backfill, import one **bounded UTC date window at a time**,
once for each `stream=non_email` and `stream=email`. For example:

```powershell
$url = "$renderUrl/internal/sync/ghl/window?start_at=2026-11-25T00%3A00%3A00Z&end_at=2026-11-25T01%3A00%3A00Z&stream=non_email"
Invoke-RestMethod -Method POST -Uri $url -Headers $headers
```

Historical window imports do not advance incremental checkpoints. Repeat with
`stream=email`, then advance to the next historical window. Importing an entire
account's history is **not automatic yet**; first verify the export response with
one short window and plan backfill coverage. Export cursors expire after two
minutes, so this code never persists them across runs.

## Optional scheduler - enable only after the incremental test passes

`scheduler/worker.js` and `scheduler/wrangler.toml` provide a separate Cloudflare
Worker triggered every five minutes. It invokes the protected incremental endpoint.
The worker is deployed separately from Render/GitHub; uploading this code
package does not itself change the existing live Cloudflare schedule. Do not
attach it to an existing Dialpad worker.

To deploy after validation and with Cloudflare credentials configured locally:

```sh
cd scheduler
npx wrangler secret put CLEARCOAT_SYNC_SECRET
npx wrangler deploy
```

Set the Cloudflare secret to **the same value as Render's** `SYNC_TRIGGER_SECRET`.
The Worker reads the Render endpoint URL from `wrangler.toml` and does not log
customer content or authentication headers. Monitor Cloudflare scheduled-event
results and Render `ghl_sync_runs` records. Running the web service on multiple
instances still requires a database-level distributed lock before scheduling.

## Optional M1 lead tracker (observation only)

After the `lead_states` table is created, enable `TRACKER_ENABLED=true`.
Newly inserted GHL messages refresh the corresponding local lead state.
The protected endpoints are:

- `POST /internal/tracker/rebuild?limit=50` to process previously stored conversations.
- `GET /internal/tracker/summary?active_days=14` for safe counts and overdue totals.
- `GET /internal/tracker/leads?state=needs_reply&overdue_only=true` for ID-only review queues.

All three require the existing `X-Sync-Secret` header. No conversation body
is returned. Lead state is rule-based (not LLM-driven), and an outbound without
reliable human attribution is marked `needs_review`. Due times currently count
wall-clock minutes; holidays, business hours, and verified sales outcomes are
not yet incorporated. Follow `M1_TRACKER_DEPLOY.md` before production rollout.

## Limits and next implementation slice

- The original `POST /internal/sync/ghl` search still checks only five recent
  conversations; it is retained for smoke tests, not account-wide monitoring.
- The new location-wide export covers non-email messages and email messages in
  separate streams, but **GHL's export excludes activity messages, group chats,
  and SMS review requests**. Use conversation-level reconciliation in a later
  slice to close those documented gaps before claiming complete observation.
- GHL messages are stored in PostgreSQL and may contain customer information;
  restrict database access, set retention/backup policies, and get client approval.
- Each message is unique on `(account_id, ghl_message_id)`, so repeated polling is safe.
- Run metadata is stored in `ghl_sync_runs` without customer text.
- No timer worker, Redis, AI prompts or GHL writes are included. The new
  response due timestamp is persisted for reporting only; it sends no alerts.
- The older marketplace webhook route is **not part of this PIT integration**.
  Leave `GHL_WEBHOOK_PUBLIC_KEY` and `GHL_ALLOWED_LOCATION_IDS` blank; do not
  configure that webhook until its signature scheme has been separately verified.
- This single-instance trigger uses a process lock. Add a database-level lock
  before running multiple Render instances or automated concurrent schedulers.

Official GHL API docs:

- https://marketplace.gohighlevel.com/docs/ghl/conversations/search-conversation/
- https://marketplace.gohighlevel.com/docs/ghl/conversations/get-messages/
- https://marketplace.gohighlevel.com/docs/ghl/conversations/export-messages-by-location/
- https://marketplace.gohighlevel.com/docs/other/rate-limits/

