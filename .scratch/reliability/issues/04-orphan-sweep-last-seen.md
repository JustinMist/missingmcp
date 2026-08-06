# 04 — Sweep orphan OAuth clients by last_seen, not creation age

Type: task
Status: resolved

Execution ticket (hybrid map — already specified in
[oauth-client-lifecycle §1](../../oauth-client-lifecycle/issue.md)).

## Question

Implement §1 of the oauth-client-lifecycle issue: add `last_seen` to
`oauth_clients` (guarded schema migration, following the existing
`user_version` pattern), update it on every authorize/token use, and sweep on
`last_seen < cutoff` instead of `created_at`. Prevents "unknown client_id"
for users returning after a >30-day pause while keeping scanner-spam bounded.

Feature branch + PR; tests for the migration and the sweep behaviour.

## Comments

- 2026-08-01: implemented on `fix/orphan-sweep-last-seen`, **PR #17 open**
  (https://github.com/VelkyVenik/missingmcp/pull/17), awaiting CodeRabbit +
  merge. `last_seen` stamped inside `store.get_client` (the single gate);
  migration seeds existing rows from `created_at`; suite 373 passed.
  Resolves on merge + deploy.

## Answer (2026-08-06)

Merged and deployed: **PR #17**, merge commit `6364359` (CodeRabbit had no
actionable comments; Railway auto-deploys main). `user_version` 1→2 migration
adds `oauth_clients.last_seen` (existing rows seeded from `created_at`);
`store.get_client` stamps it on every authorize/token use — the single gate,
same idiom as `access_tokens.last_used`; `cleanup_orphan_clients` sweeps on
`COALESCE(last_seen, created_at)` + no live token. A cached Claude-org
registration whose user pauses past the token TTL no longer ages into the
sweep while it's still being re-used — "unknown client_id" on return is gone.
Resolution note added to oauth-client-lifecycle §1 (that issue is now fully
closed). Suite 373 passed.
