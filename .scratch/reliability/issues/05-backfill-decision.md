# 05 — Backfill: repair the ~84 accounts whose DB blob holds a spent token?

Type: grilling
Status: open
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
