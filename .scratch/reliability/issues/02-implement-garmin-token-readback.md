# 02 — Implement the Garmin token read-back (persist rotated tokens)

Type: task
Status: resolved

Execution ticket (hybrid map — the design decision is already made).

## Question

Implement the fix proposed in
[garmin-token-lifecycle 02](../../garmin-token-lifecycle/issues/02-persist-refreshed-garmin-tokens.md)
("## Fix"): persist the worker-rotated `garmin_tokens.json` back to the store
so the next spawn materializes the *current* token, mirroring the WHOOP
persist-before-use invariant.

Constraints carried over from the proposal:

- **Trigger:** mtime check in the lifespan loop that already runs `reap_idle`,
  plus on worker terminate/shutdown — never the request hot path.
- **Locking:** the read-back takes the same per-account lock `ensure_worker`
  holds, so it can't interleave with a `materialize()`.
- **Torn writes:** if the file doesn't parse as JSON, skip and retry next tick
  — never persist a truncated blob.
- **Black box:** no modification or import of `garmin_mcp`; reading the file
  the worker wrote is fine.
- Feature branch + PR; tests against the fake worker; keep log event
  names/values stable (add new events rather than renaming).

Resolution = PR merged and deployed (push to main auto-deploys on Railway;
verify with `railway deployment list --json`).

## Comments

- 2026-07-30: implemented on branch `fix/garmin-token-readback`, **PR #15
  open** (https://github.com/VelkyVenik/missingmcp/pull/15), awaiting
  CodeRabbit review + operator merge. 8 new tests, full suite 365 passed.
  Design deltas vs the original proposal, both documented in
  CLAUDE.md/architecture.md: content-compare via a process-local `_persisted`
  baseline instead of an mtime stat (simpler, no false `updated_at` bumps),
  plus a respawn-path recovery hook (materialize the captured rotation, not
  the caller's stale blob) the proposal didn't call out. Ticket resolves on
  merge + deploy; the ~84-account backfill stays ticket 05.
- 2026-07-31: CodeRabbit round addressed (`93b09ba`) — the `read_back` call is
  guarded per account like the persist write, so one account's disk error
  can't fail a batch capture point or abort shutdown.

## Answer (2026-07-31)

Merged and deployed: **PR #15**, merge commit `0f7833b` on main (Railway
auto-deploys on push to main). `WorkerManager` persists worker-rotated tokens
back to the store via an injected `persist(key, blob)` callback:

- capture points: periodic `persist_rotated()` in the lifespan loop (same
  per-account lock `ensure_worker` holds; held locks skipped), reap, evict,
  `shutdown()`, and `ensure_worker`'s respawn path — which materializes the
  recovered rotation instead of the caller's now-stale blob;
- new contract method `WorkerForward.read_back(workdir)` → `None` for a
  missing or torn file (never persisted; next tick retries); both the file
  read and the store write are guarded per account;
- a fresh process trusts the store over the disk (no baseline), so the ~84
  pre-fix drifted accounts are **not** auto-repaired — that is
  [ticket 05](05-backfill-decision.md)'s deliberate decision;
- events: `worker-tokens-persisted` / `worker-tokens-persist-failed` (field
  `trigger`: periodic|reap|evict|respawn|shutdown) — nothing renamed.

9 new tests in `tests/test_workers.py`; full suite 366 passed. The invariant
is documented in CLAUDE.md ("Garmin rotates too") and
`docs/architecture.md` → workers.py. Whether expiries actually collapse is
measured by [ticket 06](06-post-fix-verification.md) (~a week of data).
