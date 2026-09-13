# Garmin AI Codex overlay (collision-safe)

This directory is an additive overlay for a fork of `VelkyVenik/missingmcp`.
It intentionally does **not** replace the upstream `README.md`, `docs/`, `CLAUDE.md`, `CONTEXT.md`, or other project files.

After copying this package into the repository root, the only new top-level items are:

- `garmin-ai-codex/`
- `run_garmin_codex.sh`

Run:

```bash
chmod +x run_garmin_codex.sh
./run_garmin_codex.sh --dry-run
./run_garmin_codex.sh
```

If GPT-6 Astra is unavailable:

```bash
./run_garmin_codex.sh --fallback-sol
```

Stage outputs are written under `.codex-run/garmin-ai/`.
