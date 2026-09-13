# Codex Execution Guide

## Model policy

### Preferred

**Architecture / invasive cross-cutting changes / final review:** GPT-6 Astra, reasoning `high`.

Use it only for checkpoints that benefit from the strongest end-to-end reasoning:

1. Baseline repo audit + implementation plan.
2. Review after region/blob/worker changes are complete.
3. Security and backward-compatibility review before merge.

Do not spend Astra on repetitive unit-test edits or formatting.

### Main implementation

**GPT-5.6 Sol, reasoning `high`.**

Use for the actual implementation PR: adapters, login flow, worker environment, tests, docs, and fixing integration failures. This is the default model for most of the coding work.

### Cheap/routine work

**GPT-5.6 Sol, reasoning `medium`** for test fixture updates, typing, lint fixes, and mechanical documentation changes. If Codex exposes a cheaper suitable model in your account, those mechanical tasks can be delegated, but do not split a security-sensitive change across weak agents.

### Fallback when Astra is not yet available

Use **GPT-5.6 Sol `xhigh`** for the initial audit and final security review; keep implementation at `high`.

## Codex version

Update Codex before starting. GPT-6 Astra requires Codex CLI 0.153.0 or newer. GPT-5.6 requires 0.144.0 or newer.

## Working style

Use one branch and small atomic commits. Do not let Codex rewrite MissingMCP architecture unnecessarily.

Suggested branch:

```text
feat/garmin-cn-multitenant
```

Suggested commits:

```text
1. test: define Garmin region blob behavior
2. feat: add versioned Garmin region credential blob
3. feat: make Garmin login and verify region-aware
4. feat: propagate Garmin region into per-user worker
5. feat: add Garmin region selector to authorize UI
6. test: add legacy-global and CN MFA regression coverage
7. docs: document CN/global multi-tenant behavior
```

## Guardrails for Codex

Put these in `AGENTS.md` at the repo root:

- Preserve MissingMCP's existing OAuth, encryption and worker lifecycle architecture.
- Do not persist Garmin passwords.
- Region must be account-scoped, not process-global.
- Never trust MCP request parameters to choose Garmin region after authorization.
- Legacy raw Garmin token blobs must continue to mean Global.
- Same normalized email in CN and Global must map to distinct accounts.
- Do not change other adapters.
- Pin upstream garmin_mcp to a reviewed commit.
- Add tests before or with every behavior change.
- Run the complete test suite before declaring completion.
- Do not make real Garmin network calls in automated tests.
