# 7-day breakdown of hourly-digest "problem" rows

Resolves [ticket 01](../issues/01-digest-error-breakdown.md). Analysis date
2026-07-30; window **2026-07-23 15:00 → 2026-07-30 15:00 UTC** (7×24 h).
Aggregates only — no account e-mails, tokens, or IPs (public repo).

## Methodology

- **Source:** PostHog Logs (project MissingMCP.com, id 227772, EU), service
  `missingmcp` — the OTLP tee of the gateway's structured stdout
  (`telemetry.py::_attach_log_tee`, body = the verbatim JSON log line).
  Coverage confirmed continuous since before 2026-07-20, so the window is
  fully covered. Railway fallback not needed.
- **Problem definition** reproduced from `scripts/hourly_digest.py::summarize`:
  a row is a *problem* iff it has a 5xx `status`, OR severity error/critical
  (PostHog `severity_text` error/fatal) with `event` not in
  `SELF_HEAL_EVENTS` = {worker-forward-auth-stale, local-forward-auth-stale,
  remote-forward-auth-stale}; `worker-log` error rows whose `line` lacks
  `\b(ERROR|CRITICAL)\b` are traceback continuations and fold into their head
  row.
- **Queries:** ClickHouse SQL over the `logs` table via the PostHog MCP
  (`execute-sql`), `JSONExtractString(body, …)` for `event`/`account`/`line`,
  e.g. `SELECT JSONExtractString(body,'event') AS event, count(),
  uniqExact(JSONExtractString(body,'account')) FROM logs WHERE service_name =
  'missingmcp' AND timestamp >= toDateTime('2026-07-23 15:00:00') AND
  timestamp < toDateTime('2026-07-30 15:00:00') AND severity_text IN
  ('error','fatal') GROUP BY event`. Cross-checked against `logs-count`
  pre-flights (486 error/fatal rows both ways).

## Headline numbers

| | 7 days |
|---|---|
| mcp-requests / active accounts (mcp-response) | 37,409 / 204 |
| error+fatal rows | 486 |
| − worker-log traceback continuations (folded) | 53 |
| **problem rows (digest definition)** | **433** |
| rows with a 5xx status | **0** (regex verified; 2xx control matched 33,787) |
| distinct accounts on ≥1 problem row | **88** (43 % of active) |
| … of which hit by the Garmin stale-token class | **74** (36 % of active) |
| self-heal `worker-forward-auth-stale` rows (context, not problems) | 81 — first seen 2026-07-26, when the signal-split deploy landed |

## Breakdown by class (problem rows, after folding)

| class | rows | % | accounts | note |
|---|---|---|---|---|
| **Garmin stale tokens** — `worker-log` head `ERROR: OAuth tokens not found and no interactive terminal available.` (129 rows / 74 accts) + `worker-start-failed` (48 rows / 33 accts, **all ≤ 2026-07-26** = pre-split naming of the same failure; the event has been silent since) | 177 | 41 % | 74 | root cause = discarded token rotation (garmin-token-lifecycle 02); fixed by the read-back |
| **garminconnect sign-in failures** (`stdlib-log`, logger `garminconnect`) — `Login failed: All login strategies exhausted: Portal login failed (non-JSON): HTTP 403 …` (101) + `… Portal web login failed: {'serviceURL': None …` (27) | 128 | 30 % | n/a (no account field) | interactive sign-in attempts failing; HTTP 403 = Garmin/edge blocking the login, arrives in bursts; volume is partly *driven by* expiries forcing re-logins |
| **Garmin API call failures in workers** — `worker-log` heads `ERROR API call failed for path …` / `Download failed for path …` (their 53 tracebacks are the folded continuations) | 53 | 12 % | 21 | upstream Garmin API flakiness during tool calls; bursty |
| **authorize-flow noise** — `authorize-unknown-client` 17, `authorize-client-id-not-dcr` 16, `authorize-csrf-invalid` 11 | 44 | 10 % | n/a | scanners + users pasting client IDs (the "leave OAuth fields empty" fix landed 2026-07-28); no working-user impact |
| **login-start-failed** (`reason: auth`, all 19) | 19 | 4 % | n/a | the `oauth.py` side of failed sign-ins; overlaps the garminconnect class (multiple garminconnect lines per attempt) |
| **ASGI exceptions** (`stdlib-log`, logger `uvicorn.error`) — all 9 end in `httpx.ReadTimeout` | 9 | 2 % | n/a | proxy/upstream read timeout escaping as `Exception in ASGI application` |
| **mfa-resume-failed** | 2 | <1 % | n/a | user retries of expired MFA sessions |
| **mcp-forward-error** — `{"error": "ConnectError", "adapter": "garmin", "tool": null, "ms": 2991}` | 1 | <1 % | 1 | one-off worker connection failure |
| **total** | **433** | 100 % | 88 | |

## Ping-frequency baseline (ANOMALY_MIN = 3)

Problems per hour, distribution over the 168-hour window:

| | hours | /day |
|---|---|---|
| silent (0 problems) | 75 | — |
| minor post (1–2) | 32 | ~4.6 |
| **loud `<!here>` (≥3)** | **61** | **~8.7** |
| worst hours | 27 and 29 problems | |

**Post-fix simulation** — same query minus the Garmin stale-token classes
(`worker-start-failed` + stale `worker-log` heads):

| | hours | /day |
|---|---|---|
| silent | 110 | — |
| minor (1–2) | 30 | ~4.3 |
| **loud (≥3)** | **28** | **~4.0** |

The simulation is **conservative**: the garminconnect 403 bursts and
`login-start-failed` rows are largely expiry-driven re-login attempts, so the
fix should shrink them too — but they are kept in, since Garmin also blocks
some genuinely fresh sign-ins.

## Answers to the ticket's three questions

1. **Garmin stale-token share:** 177 of 433 problem rows = **41 %**, hitting
   74 distinct accounts (36 % of the week's 204 active). Counting the
   correlated sign-in failure classes (garminconnect + login-start-failed,
   together 147 rows) as expiry-driven would push the Garmin-expiry share to
   ~70 % — the fix's true reach lies between those bounds. Self-heal
   (`worker-forward-auth-stale`, 81 rows since the 07-26 split) is correctly
   excluded from problems by the digest already.
2. **Remaining classes:** garminconnect sign-in 403s (128), worker Garmin API
   call failures (53 heads + 53 folded continuations), authorize-flow noise
   (44), login-start-failed (19), ASGI `httpx.ReadTimeout` (9), mfa-resume
   (2), one ConnectError. Representative sanitized examples in the table.
3. **What still pages post-fix:** ~4 loud hours/day, dominated by
   garminconnect 403 bursts, worker API-failure bursts, and authorize noise.
   For triage design (ticket 08) these fall into four signature families:
   *user-credential expiry* (self-heals, never page), *upstream Garmin
   flakiness* (page only on sustained bursts), *auth-flow noise/scanners*
   (never page, weekly summary at most), *gateway faults* (ASGI timeouts,
   post-split `worker-start-failed` — always surface, these are real).

## Caveats

- `worker-start-failed` changed meaning mid-window (2026-07-26 signal
  split): its 48 rows are all pre-split, i.e. the *old* name for stale
  tokens; zero post-split rows means **no real worker start faults all
  week**.
- stdlib-log rows carry no `account` field, so per-account counts for the
  sign-in classes aren't derivable from logs alone.
- The digest's 5xx leg contributed nothing this week (the 2026-07-18 reauth
  401 change removed the 502 path) — problems ≈ error-row count.
- PostHog `severity_text` maps our `critical` → `fatal`; zero fatal rows in
  the window.
