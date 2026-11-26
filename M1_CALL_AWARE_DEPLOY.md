Clearcoat M1 — Call-aware tracker upgrade
========================================

What changes
------------
- Handles known call activity types (`CALL`, `TYPE_CALL`) separately from chat.
- A call is never proof of a human reply or a completed sales conversation.
- An unanswered customer message followed by a call is conservatively marked
  `needs_review / call_after_inbound_unverified` rather than automatically overdue.
- A confirmed human chat reply remains `waiting_customer`, even if a call
  activity appears later. A newer customer message starts a fresh reply timer.
- Call-only records are marked `needs_review / no_supported_chat_messages`.
- The protected leads endpoint now includes `last_message_type`,
  `last_message_direction`, `last_message_source`, and `last_call_at`.
  It does not return message bodies, recordings, or transcripts.

Safety and scope
----------------
- No new table, migration, scheduled Worker, GHL write, or AI call is required.
- Existing `GHL_WRITE_ENABLED=false` and `CUSTOMER_SEND_ENABLED=false` remain
  unchanged.
- Only verified `CALL` and `TYPE_CALL` are treated as call activities. Other
  unknown types still require review until checked against live GHL export.
- Business-hour/holiday-aware SLA calculations and call outcomes are NOT
  implemented here. Current deadlines still use the configured wall-clock SLA.

Local installation (in your repo root)
--------------------------------------
1. Extract the upgrade ZIP over the repo root, preserving folder structure.
   It contains only `clearcoat/tracker.py`, `tests/test_tracker.py`, and this file.
2. Run `python -m pytest -v`.
3. Check the changed files with `git status`.
4. Stage and push only the intended files:

   git add clearcoat/tracker.py tests/test_tracker.py M1_CALL_AWARE_DEPLOY.md
   git commit -m "Make M1 tracker call-aware"
   git push origin main

Production verification
-----------------------
1. Wait for Render to finish deploying the new commit.
2. Keep the existing `$renderUrl` and `$headers` PowerShell variables.
3. Force-rebuild already-tracked conversation states; a normal rebuild only
   processes NEW rows, so it will not update these five existing review cases:

   $r = Invoke-RestMethod -Method POST -Uri "$renderUrl/internal/tracker/rebuild?force=true&limit=100" -Headers $headers
   $r | Format-List

4. If `$r.has_more` is true, continue from the returned cursor:

   $after = [uri]::EscapeDataString($r.next_after)
   $r = Invoke-RestMethod -Method POST -Uri "$renderUrl/internal/tracker/rebuild?force=true&limit=100&after=$after" -Headers $headers

   Repeat while `has_more` is true; if a concurrent cron run returns 409,
   wait for it to finish before retrying that page.

5. Recheck review reasons and raw type metadata:

   $review = Invoke-RestMethod -Method GET -Uri "$renderUrl/internal/tracker/leads?state=needs_review&limit=100" -Headers $headers
   $review.leads | Group-Object review_reason | Select-Object Name,Count | Format-Table -AutoSize
   $review.leads | Select-Object last_message_type,last_message_direction,last_message_source,review_reason | Format-Table -AutoSize

6. Compare several cases manually in GHL. Do not share message bodies or
   customer contact details in logs or test reports.
7. Confirm Cloudflare's existing 5-minute sync continues returning HTTP 200.

Next: confirm Iconic Wraps' timezone, business days, opening/closing hours,
and holiday policy before building business-hour-aware timers.
