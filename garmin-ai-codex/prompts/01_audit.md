You are implementing `garmin-ai-codex/IMPLEMENTATION_SPEC.md` in a fork of VelkyVenik/missingmcp.

First do a read-only audit. Do not edit files yet.

1. Read the upstream repository's `CLAUDE.md` and `CONTEXT.md` if present, then read `garmin-ai-codex/IMPLEMENTATION_SPEC.md` and `garmin-ai-codex/GARMIN_ARCHITECTURE.md`.
2. Inspect the current Garmin adapter, login wrapper, worker manager, account store, authorize/MFA templates, and Garmin tests.
3. Confirm exactly how `LoginOk.blob` is encrypted/persisted and how worker `materialize`, `env`, and `read_back` are called.
4. Identify every test that will need updating.
5. Produce a minimal implementation plan with exact file paths and explain how backward compatibility for legacy raw token blobs will work.
6. Explicitly call out any mismatch between the spec and current upstream interfaces.

Bias toward the smallest safe patch. Do not propose a new gateway architecture or DB migration unless the current code makes the versioned adapter blob impossible.
