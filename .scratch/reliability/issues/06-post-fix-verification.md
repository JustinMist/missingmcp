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
