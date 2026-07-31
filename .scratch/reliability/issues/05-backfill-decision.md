# 05 — Backfill: repair the ~84 accounts whose DB blob holds a spent token?

Type: grilling
Status: resolved
Blocked by: 02

## Question

For roughly half the garmin accounts, the token file on disk is *ahead* of
the DB blob (the worker rotated tokens the store never learned about —
measured 2026-07-27: 84 of 168). Once the read-back fix
([02](02-implement-garmin-token-readback.md)) is deployed, decide whether to
repair the existing accounts **in place** by persisting each account's
current token file into the store once — saving those users a forced
re-login — or let them re-auth naturally through the 401 path.

Touching stored credentials for real users is a deliberate operator decision
(flagged in garmin-token-lifecycle 02's "Backfill question for the
operator"). If yes, also decide the procedure — dry-run first, skip
non-parsing files, aggregate-only logging — and execute it (hybrid map:
execution allowed once decided).

Note: accounts whose file holds an *already-expired* rotation can't be saved
by backfill; count them, don't guess.

## Answer (2026-07-31)

**Decision: yes** — the operator ordered the repair ("udelej backfill") and
approved the apply after reviewing the dry-run. Executed the same day via
`scripts/backfill_garmin_tokens.py` (shipped on main; dry-run by default,
masked keys, WAL-safe beside the live gateway, idempotent).

Safety rule that made it safe: a file is persisted only when it parses,
differs from the DB blob, **and is newer than `updated_at`** — a re-login
after the file was written always wins (the gateway's unknown-provenance rule,
applied here too).

Production numbers (244 garmin accounts — the base grew since the 84/168
measurement on 2026-07-27):

| verdict | count |
|---|---|
| persisted (drifted, repaired) | **117** |
| in-sync | 116 |
| db-newer (recent re-logins, correctly skipped — incl. today's outage re-signs) | 5 |
| no-file | 6 |
| torn | 0 |

Verification: a second dry-run right after reads **0 drifted / 233 in-sync**.
Those 117 accounts will materialize their latest working rotation on the next
spawn instead of a spent token — no forced re-login. How many expiries remain
overall is [ticket 06](06-post-fix-verification.md)'s measurement (~a week).
