# 07 — Proactive user notification: do we need a mail channel at all?

Type: grilling
Status: open
Blocked by: 06

## Question

garmin-token-lifecycle 01 established that the re-auth 401 works as a
protocol but not as a notification (median recovery ~35 h, one in three never
returns) and that proactive e-mail is the only option addressing the
mechanism — the user isn't at the keyboard. But the read-back fix should
remove most expiries at the source.

With post-fix numbers ([06](06-post-fix-verification.md)) in hand, decide
whether the *remaining* expiry volume (password changes, MFA resets,
upstream invalidation) justifies building an outbound mail path — the
gateway has none today (`/subscribe` stores an address, sends nothing). If
yes: provider, where the credentials live, policy (at most one mail per
expiry, unsubscribe, or it becomes spam), and the copy (operator's voice —
show before anything outward-facing ships). If the volume is negligible,
close as not-needed.
