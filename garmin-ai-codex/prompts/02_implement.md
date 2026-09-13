Implement the approved Garmin CN/Global multi-tenant plan.

Before editing anything:
1. Read the upstream repository's `CLAUDE.md` and `CONTEXT.md` if present, then read `garmin-ai-codex/IMPLEMENTATION_SPEC.md`.
2. Read `.codex-run/garmin-ai/01-audit.md` from the prior audit session.
3. Validate the audit against the current working tree. If the audit is stale or contradicts the code/spec, follow the current code + spec and explicitly note the discrepancy in your final handoff.

Requirements:
- region values are exactly `cn` and `global`;
- missing legacy region means global;
- login and verification pass `is_cn` to garminconnect;
- MFA state preserves region without retaining password;
- CN/global same email are different accounts;
- worker env derives GARMIN_IS_CN from persisted account data, never from incoming MCP request parameters;
- worker materialization writes raw Garmin tokens and a private region marker;
- token read-back preserves region after refresh;
- no DB migration unless tests prove the encrypted adapter blob cannot carry the wrapper;
- keep other adapters unchanged;
- add/adjust tests before considering work complete.

Work autonomously end-to-end. Run targeted tests after each logical change, then run the full suite. Fix only failures caused by this work unless you can prove an upstream pre-existing failure. Do not stop for progress updates; stop only if genuinely blocked by a decision that changes product behavior.

End with a concise change summary, security invariant checklist, exact test commands/results, files changed, and any remaining manual smoke-test steps. This final answer will be saved to `.codex-run/garmin-ai/02-implement.md` for the review session.
