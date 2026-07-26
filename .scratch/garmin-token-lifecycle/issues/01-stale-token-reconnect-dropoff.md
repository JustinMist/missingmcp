# 01 — Half the users hitting stale Garmin tokens never reconnect

Type: task
Status: needs-triage

## Context

Found on 2026-07-26 while diagnosing why the PostHog alert
`Worker start failures ≥3/hour` fires several times a day. The alert itself was a
signal problem, fixed separately (`fix(worker): split stale credentials from real
worker start failures`, branch `fix/worker-start-signal`). This ticket is the
*user-facing* problem that diagnosis surfaced.

When an account's stored Garmin tokens go stale, `garmin_mcp` refuses to start and
the gateway answers the RFC 9728 re-auth 401. In theory the MCP client re-runs
authorization and the user signs in again — the designed self-heal path. In
practice only about half of them come through it.

## Measurement (24 h window ending 2026-07-26 ~09:00 UTC)

Log-derived (`worker-start-failed` rows, PostHog Logs) cross-referenced against
`accounts.updated_at` and `tool_usage.last_used` in the production DB:

- 23 failure rows across **17 distinct accounts** (of 149 accounts / 144 with a
  live token at the time)
- **7 accounts re-authed and resumed within minutes** — typical shape: failure
  08:05 → `accounts.updated_at` 08:12 → successful tool call 08:14
- **10 accounts never came back** — no re-login, no further tool call. Five of
  those failed the previous afternoon/evening, so they had 15+ hours of
  opportunity; the rest failed overnight and had less
- Every one of the 23 was a clean `rc=0` worker self-exit on stale tokens; none
  was an infrastructure fault

So a stale token costs roughly one in two affected users, and at ~17 accounts/day
that is a steady leak, not an edge case.

## Why it might be happening (unverified)

The 401 only helps a user who is *actively* driving the connector at that moment
and whose client surfaces the re-auth prompt clearly. Candidates worth checking
before designing a fix:

- Does the MCP client actually prompt on the 401, or does it silently show a
  broken tool call? (Differs per client — mobile / desktop / web.)
- Does the user understand that "reconnect" means signing in again on
  missingmcp.com, given the message is `Your Garmin session expired. Please
  reconnect the Garmin MCP server.`?
- Is the expiry itself avoidable? See ticket 02 — refreshed tokens are currently
  discarded, which may be shortening credential life.

## Options (not decided)

1. **Proactive notification** — detect stale credentials and e-mail the account a
   reconnect link. Needs an outbound mail path, which the gateway does not have
   today (`/subscribe` is storage-only, no mail is sent).
2. **Reduce expiries at the source** — ticket 02, if it turns out refresh
   persistence extends credential life.
3. **Better in-client copy** — the re-auth message is the one string the user
   reliably sees; make it carry the full instruction and the URL.
4. **Measure first** — add a funnel on the new `worker-forward-auth-stale` event
   → subsequent successful sign-in, so recovery rate is tracked continuously
   rather than hand-queried once.

Option 4 is cheap and makes the rest decidable; likely the right first step.

## Notes

- Do not put account e-mails in this repo — it is public. The numbers above are
  aggregates; the per-account detail is reproducible from the DB and PostHog Logs.
