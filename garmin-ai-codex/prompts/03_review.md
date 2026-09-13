Perform a security and backward-compatibility review of the Garmin CN/Global patch. Do not assume the implementation is correct and do not edit tracked source files during this review.

First read:
- upstream `CLAUDE.md` and `CONTEXT.md` if present
- `garmin-ai-codex/IMPLEMENTATION_SPEC.md`
- `.codex-run/garmin-ai/01-audit.md`
- `.codex-run/garmin-ai/02-implement.md`
- the current `git diff` and relevant tests/source files

Check specifically:
1. Can an authenticated caller switch region via MCP params/headers?
2. Can CN and Global accounts with the same email collide?
3. Can a legacy raw-token Global record still start a worker and refresh tokens?
4. Can token refresh accidentally erase region?
5. Is CN used consistently in credential login, MFA continuation, token verification and worker execution?
6. Is any plaintext Garmin password, Garmin session token, OAuth bearer token or MFA code newly logged or persisted?
7. Are token and region files mode 0600?
8. Is malformed region/blob input fail-closed?
9. Did unrelated adapters or generic OAuth behavior change?
10. Are there tests for all of the above?

Run the full test suite in a way compatible with a read-only source tree (for pytest, disable source-tree bytecode/cache writes if needed; temporary-directory writes are fine). Do not weaken or skip meaningful tests merely to satisfy the sandbox.

For every issue found, give severity, exact file/line, exploit/failure scenario and smallest fix. Distinguish blockers from non-blocking hardening. If no blocker remains, state the exact manual two-account smoke-test procedure for one CN + one Global account before merge.
