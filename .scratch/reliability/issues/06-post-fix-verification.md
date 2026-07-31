# 06 — Measure what remains after the fix

Type: task
Status: open
Blocked by: 02, 05

## Question

About a week after the read-back fix (and the backfill, if
[05](05-backfill-decision.md) says yes) is live, re-run the seven-day
measurement from garmin-token-lifecycle 01's triage:

- how many accounts still hit stale-token failures
  (`worker-forward-auth-stale` / `worker-start-failed`), and why (password
  change, MFA reset, upstream invalidation — anything that isn't our own
  discarded rotation);
- the recovery rate and time-to-recovery of those still affected;
- the total problem volume the hourly digest still sees.

The answer gates two decisions: the mail channel
([07](07-mail-channel-decision.md) — if expiries collapse to near-zero, mail
may be unnecessary) and the triage rollout tuning (follow-up of
[08](08-autonomous-triage-design.md)). Aggregates only — public repo.

## Comments

- 2026-07-31: a **scheduled cloud agent** will resolve this ticket — one-off
  routine "Reliability 06 — post-fix measurement" fires 2026-08-07T14:30Z
  (7 days after the fix + backfill went live), measures the window from
  2026-07-31T14:00Z via PostHog, and opens a PR with the resolution
  (branch `research/post-fix-verification`). Don't work this ticket by hand
  before then unless the routine failed.
