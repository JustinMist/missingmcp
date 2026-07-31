# 02 — Garmin token materialization is one-way; refreshed tokens are discarded

Type: research
Status: resolved

Triaged 2026-07-27 and **answered the same day — the hypothesis holds**: Garmin
rotates the refresh token and the gateway throws the rotation away, which is the
root cause of ticket 01's expiries. See "## Answer" below for the measurement and
"## Fix" for the proposed change; the fix itself is not implemented yet and needs
its own ticket once the operator picks up the backfill question.

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

## Answer (2026-07-27) — confirmed, and it is the rotation case

Measured on production without touching anyone's account: instead of driving a
worker, compare **the token file against the DB blob**. `materialize()` writes the
blob to the file on every spawn and nothing reads it back, so `file != blob` is
itself proof that the worker wrote something the DB never learned.

```
garmin accounts:  168
file == db blob:   79
file != db blob:   84      <- half the base
no token file:      5
```

For every one of the 84, the differing keys are the same two:

```
{'di_refresh_token': 'value-differs', 'di_token': 'value-differs'}
```

and the file's mtime is newer than the account's `updated_at` — in several cases by
more than a day (blob written 2026-07-26 16:24, file rewritten 2026-07-27 12:52).

**So Garmin rotates the refresh token, exactly like WHOOP.** The mechanism behind
ticket 01's expiries:

1. Sign-in stores `di_token` + `di_refresh_token` (v1) in the blob.
2. The worker starts from v1, refreshes, receives v2, writes v2 to the file.
3. The next spawn's `materialize()` truncates the file back to **v1** from the DB.
4. Garmin has retired v1, so the refresh fails, `garmin_mcp` prints "OAuth tokens
   not found … Exiting.", and the user is told their session expired.

That also explains the shape ticket 01 saw: accounts work for a few days (while
some grace on the old token holds, or until the access token needs a refresh) and
then die, and it hits a third of the base per week. Half the base is sitting in the
broken state right now — the DB holds a spent refresh token for 84 accounts.

This closes the ticket's question: **not** "merely wasteful", and not Garmin-side
invalidation. It is our own write-only materialization.

## Fix (proposed, not yet implemented)

A read-back that mirrors the WHOOP invariant — persist the rotated blob to the
store so the next spawn materializes the *current* token:

- **Where:** not the request hot path (a file read per proxied call buys nothing).
  Cheap trigger: `stat` the token file and compare mtime against the last value the
  manager persisted. Do it in the lifespan loop that already runs `reap_idle`, and
  again when a worker is terminated or the process shuts down.
- **Restart safety:** deploys are frequent, so a rewrite that is only picked up at
  reap time would be lost on redeploy. The periodic mtime check covers that; the
  terminate/shutdown hook is the belt-and-braces path.
- **Torn writes:** garth may not write atomically. If the file doesn't parse as
  JSON, skip it and retry on the next tick — never persist a truncated blob.
- **No new races:** the read-back must take the same per-account lock
  `ensure_worker` holds, so it cannot interleave with a `materialize()`.
- **Backfill question for the operator:** the 84 accounts whose file is ahead of the
  DB can be repaired in place by persisting the file content once, which would save
  them a forced re-login. It touches stored credentials for real users, so it is a
  deliberate, separate step — not part of the code change.

## Design constraint if it does get implemented

A read-back path introduces a write that no other code does today: the worker owns
the file, the gateway owns the DB row. Whatever the mechanism, it must not race a
concurrent `materialize` for the same account (which holds the per-account
`asyncio.Lock` in `ensure_worker`) and must not write a partially-written file.
The WHOOP rule — persist before use, serialized per account — is the precedent.

## Comments

- 2026-07-30: the fix now has its implementation ticket — **reliability map**
  (`.scratch/reliability/map.md`) ticket 02; the backfill question is its
  ticket 05. Nothing further happens in this file.
