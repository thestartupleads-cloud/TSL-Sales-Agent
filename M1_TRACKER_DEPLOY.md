# Clearcoat M1 - Lead Tracker Release

This release is for the existing **single Iconic Wraps** GHL location only.
It adds a rebuildable local lead state and basic response deadlines while
preserving the existing five-minute sync. It **does not** send messages,
create GHL notes or tasks, call an LLM, or make any customer-facing changes.

## What the tracker does

- Keeps one `lead_states` record per `(account_id, conversation_id)`.
- Marks text conversations `needs_reply`, `waiting_customer`, `outbound_only`,
  or `needs_review` based on stored GHL message timestamps and directions.
- Does not mistake automated workflow/campaign/API outbound messages for
  confirmed human replies.
- Uses `userId` on a non-automated outbound message as evidence of a rep reply.
  Unknown attribution is marked for review, not silently declared answered.
- Records an adjustable UTC deadline for `needs_reply` messages. This initial
  deadline is elapsed wall-clock time, **not** a business-hours SLA.
- Exposes secret-protected read-only summary and lead-list endpoints.
- Rebuilds records for conversations imported before the tracker was deployed.

## Safe rollout: preserve the working scheduler

1. Copy this release into your existing Git checkout, preserving your own
   `.env` and any unrelated changes. Run `python -m pytest -q` (16 tests).
2. In Render, set `TRACKER_ENABLED=false` first. Keep
   `GHL_WRITE_ENABLED=false` and `CUSTOMER_SEND_ENABLED=false`.
3. Push the code yourself. On this **new-table** deployment, the Render start
   command must initialize the schema before launching FastAPI:

   ```sh
   python -m clearcoat.init_db && uvicorn clearcoat.main:app --host 0.0.0.0 --port $PORT
   ```

   This is an additive `lead_states` table, not a change to old tables.
   Confirm the deployment log says `Initialized Clearcoat tables` and that
   `/health` returns HTTP 200. Do **not** enable the tracker before that.
4. After confirming the table exists, set these in Render and let it redeploy:

   ```text
   TRACKER_ENABLED=true
   TRACKER_RESPONSE_SLA_MINUTES=30
   ```

   The 30-minute value is an **unapproved pilot assumption**. Change it to
   the client's final playbook value before evaluating SLA compliance.
5. Observe a few scheduled Cloudflare runs; they should still be `Ok`.
   If the new tracker causes a 500 or failed sync, set
   `TRACKER_ENABLED=false`, redeploy, and inspect the Render traceback.

## First tracker test from PowerShell

Do not put the trigger secret into code, GitHub or this chat. Type it only
in your local secure prompt. From your own PowerShell session:

```powershell
$renderUrl = "https://clearcoat-api.onrender.com"
$secure = Read-Host "Enter Render SYNC_TRIGGER_SECRET" -AsSecureString
$plain = [System.Net.NetworkCredential]::new("", $secure).Password
$headers = @{"X-Sync-Secret" = $plain}

Invoke-RestMethod -Method POST -Uri "$renderUrl/internal/tracker/rebuild?limit=50" -Headers $headers
Invoke-RestMethod -Method GET -Uri "$renderUrl/internal/tracker/summary?active_days=14" -Headers $headers
Invoke-RestMethod -Method GET -Uri "$renderUrl/internal/tracker/leads?state=needs_reply&limit=20" -Headers $headers

Remove-Variable secure,plain,headers
```

The rebuild response returns `processed`, `has_more` and `next_after`.
If `has_more=true`, call the rebuild again with
`&after=<URL-encoded next_after>` until no pages remain. A `409` means a GHL
sync is currently running; wait briefly rather than overlapping it.
`force=true` is available later for recalculating an updated SLA. Never use
the Render database URL or API token in a query string.

## Known limits before treating alerts as authoritative

- Older messages still require historical GHL backfill; this rebuild only
  analyzes messages **already stored** in PostgreSQL.
- Messages without a valid timestamp/direction, ambiguous outbound sources,
  and unsupported latest message channels are marked for manual review.
- Exported GHL calls, review requests, group chats and activity messages are
  not covered by this first reply SLA rule.
- No working-hours calendar, lead ownership, booking state, rep tasks,
  delivery confirmation, or follow-up automation yet.
- The current FastAPI process lock is suitable only for one instance.
  A shared database lock is needed before horizontally scaling Render.
- A successful Cloudflare run confirms import, not proof of complete historical
  coverage or of correct lead categorization. Sample the results against GHL.

Keep this feature in observation-only mode until the client's playbook,
message attribution and business-hours policies are explicitly verified.
