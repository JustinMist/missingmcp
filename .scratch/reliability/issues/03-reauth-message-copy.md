# 03 — Make the re-auth message carry the full instruction

Type: task
Status: claimed

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
