# 02 — Garmin token materialization is one-way; refreshed tokens are discarded

Type: research
Status: ready-for-agent

Triaged 2026-07-27. Promoted ahead of ticket 01: 01's seven-day numbers show the
re-auth 401 reaches almost nobody in time (3 of 58 accounts recover within 15
minutes, median ~35 hours), so preventing an expiry is worth more than explaining
it. The check below is cheap and decides whether prevention is even possible —
run it before committing to 01's mail channel. Needs production access
(`railway ssh --service gateway`, working as of 2026-07-27).

## Context

Found on 2026-07-26 alongside ticket 01, while diagnosing the
`Worker start failures ≥3/hour` alert. Not confirmed as the cause of anything —
this ticket is to verify or kill the hypothesis before any code changes.

## The observation

`GarminWorkerForward.materialize` (`src/missingmcp/adapters/garmin/__init__.py:27-31`)
writes the stored blob into `<token_dir>/garmin_tokens.json` with `O_TRUNC`, on
**every** worker spawn. There is no path back: the only place a garmin blob is ever
written to the store is `oauth.py:215`, i.e. an interactive sign-in.

`garth` (inside the worker) refreshes the short-lived OAuth2 token using the
long-lived OAuth1 token and writes the result to that same file. That refreshed
state lives only as long as the worker: the next spawn overwrites it with the
original blob from the DB.

The WHOOP adapter has exactly the reverse path — `whoop/api.py:157` persists the
rotated blob to the store *before* using it, and CLAUDE.md documents it as an
invariant because WHOOP rotates refresh tokens on every use. Garmin has no
equivalent.

## Hypothesis to test

If Garmin/garth ever rotates or invalidates the OAuth1 token as part of a refresh,
then discarding the refreshed file means every spawn replays an OAuth1 token that
the upstream has already retired — which would explain accounts working for a few
days and then failing with `OAuth tokens not found ... Exiting.` (the observed
failure in ticket 01, 17 accounts/day).

If OAuth1 is genuinely long-lived and never rotates, discarding the refresh is
merely wasteful (one extra refresh round-trip per spawn) and this ticket closes as
`wontfix`.

## How to check

All of this stays within the black-box contract — reading a file the worker wrote
is fine; **do not** modify or import `garmin_mcp`.

1. Pick one healthy account. Note the stored blob's hash, spawn its worker, let it
   serve a request, then hash `<token_dir>/garmin_tokens.json` again. Compare —
   does the worker rewrite it at all, and does the OAuth1 part change or only the
   OAuth2 part? (Compare hashes / structure, never log token values.)
2. If OAuth1 changes: that is the rotation case, and the fix is a read-back after
   the worker's first successful request, persisting via `store.upsert_account`
   under the same "persist before use" discipline WHOOP follows.
3. If only OAuth2 changes: check whether an expired OAuth2 + valid OAuth1 blob
   actually still starts a worker (it should, garth refreshes). If it does, the
   expiries in ticket 01 come from Garmin-side invalidation instead, and the
   answer to that is ticket 01's option 1 or 3, not this ticket.

## Design constraint if it does get implemented

A read-back path introduces a write that no other code does today: the worker owns
the file, the gateway owns the DB row. Whatever the mechanism, it must not race a
concurrent `materialize` for the same account (which holds the per-account
`asyncio.Lock` in `ensure_worker`) and must not write a partially-written file.
The WHOOP rule — persist before use, serialized per account — is the precedent.
