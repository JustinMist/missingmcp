# 03 — Make the re-auth message carry the full instruction

Type: task
Status: resolved

Execution ticket (hybrid map — "worth a sentence, not a project", per
garmin-token-lifecycle 01 option 3).

## Question

The one string an affected user reliably sees is the
`proxy._reauth_required` body message ("Your <X> session expired. Please
reconnect the <X> MCP server."). Make it carry the complete instruction —
what "reconnect" concretely means and where (the missingmcp.com sign-in URL
for the adapter) — so the ~5% of users who *are* at the keyboard when the 401
lands can recover without guessing.

Keep the 401 + RFC 9728 challenge shape and all log event names/values
unchanged; this is copy only.

## Comments

- 2026-07-31: copy approved by the operator (variant A: full instruction +
  Claude path + landing-page URL), implemented on `fix/reauth-copy`, **PR #16
  open** (https://github.com/VelkyVenik/missingmcp/pull/16), awaiting
  CodeRabbit + merge. Resolves on merge + deploy.

## Answer (2026-07-31)

Merged and deployed: **PR #16**, merge commit `6779bc0` (CodeRabbit had no
actionable comments). The 401 body message now reads, per adapter:

> Your Garmin session expired. Please sign in to Garmin again to reconnect —
> your MCP client will prompt you (in Claude: Settings → Connectors → Garmin).
> Help: https://missingmcp.com/garmin

Copy only — the 401 status, RFC 9728 challenge and log events are unchanged;
applies to all three forward strategies via `proxy._reauth_required`. The
four byte-for-byte test assertions were updated first (TDD); suite 366
passed.
