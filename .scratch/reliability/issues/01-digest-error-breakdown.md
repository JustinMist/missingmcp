# 01 — What actually fills the hourly digest? 7-day breakdown of problem rows

Type: research
Status: resolved

## Question

Over the last 7 days of production logs, break down every row the hourly
digest counts as a "problem" — 5xx status, or level error/critical with an
event **not** in `SELF_HEAL_EVENTS` (see `scripts/hourly_digest.py::summarize`)
— by event name, status code, and adapter. Quantify:

1. What share of the problem volume is the known Garmin stale-token failure
   (`worker-start-failed` after the signal split, plus the
   `worker-forward-auth-stale` self-heal volume for context)?
2. What error classes remain besides it — name each one, with counts and one
   representative (sanitized) example row.
3. Which of those classes would still page the operator after the token
   read-back fix lands?

This feeds ticket [08](08-autonomous-triage-design.md) (triage signatures) and
tells us what the digest will still see once the fix is deployed.

Source: PostHog Logs (project MissingMCP.com) — fall back to Railway
`deploymentLogs` via the GraphQL API if needed. Write the full analysis to
`assets/error-breakdown-7d.md` (aggregates only — public repo, no account
e-mails, no token material).

## Answer (2026-07-30)

Full analysis: [assets/error-breakdown-7d.md](../assets/error-breakdown-7d.md)
(window 2026-07-23 15:00 → 2026-07-30 15:00 UTC, PostHog Logs via SQL over
the `logs` table; coverage complete, Railway fallback not needed).

- **433 problem rows** (486 error/fatal − 53 folded traceback continuations);
  **zero 5xx**. 88 of 204 active accounts (43 %) hit ≥1 problem row.
- **Garmin stale tokens = 41 %** (177 rows, 74 accounts): 129 `worker-log`
  "OAuth tokens not found" heads + 48 `worker-start-failed` rows that all
  predate the 2026-07-26 signal split (same failure, old name). Post-split
  `worker-start-failed` is silent — no real worker start faults all week.
- **Remaining classes:** garminconnect sign-in failures, dominated by
  HTTP 403 blocks (128, bursty, partly expiry-driven re-logins); worker
  Garmin API-call failures (53 heads, 21 accounts); authorize-flow
  noise/scanners (44); `login-start-failed` reason=auth (19); ASGI
  `httpx.ReadTimeout` (9); mfa-resume (2); one ConnectError.
- **Ping baseline at ANOMALY_MIN=3:** 61 of 168 hours loud (~8.7 `<!here>`
  pings/day), 32 minor. **Post-fix simulation: ~4 loud hours/day remain**
  (conservative — the 403/login classes should shrink with the fix too),
  so the token read-back alone does NOT quiet the channel; ticket 08's
  triage signatures matter. Suggested signature families for 08:
  credential-expiry (never page), upstream-Garmin flakiness (page on
  sustained bursts only), auth-flow noise (never page), gateway faults
  (always page).
