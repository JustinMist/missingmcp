# One-click Codex workflow

This pack includes `run_codex.sh`, which starts **three separate Codex exec sessions** and explicitly selects the model/reasoning level for each stage. Codex does not need to decide which model to use.

## Default routing

| Stage | Model | Reasoning | Sandbox | Purpose |
|---|---|---|---|---|
| Audit | `gpt-6-astra` | `high` | read-only | Understand current MissingMCP interfaces and produce the exact plan |
| Implement | `gpt-5.6-sol` | `high` | workspace-write | Make the Garmin CN/Global multi-tenant changes and run tests |
| Review | `gpt-6-astra` | `high` | read-only | Security/backward-compatibility review |

If GPT-6 Astra is not available to your Codex account yet, run:

```bash
./run_codex.sh --fallback-sol
```

That changes Audit + Review to `gpt-5.6-sol` with `xhigh` reasoning while keeping implementation on Sol `high`.

## How to use

1. Fork/clone `VelkyVenik/missingmcp`.
2. Copy **the contents of this starter pack** into the repository root. After copying, the repository root must contain `AGENTS.md`, `run_codex.sh`, `docs/IMPLEMENTATION_SPEC.md`, and `codex/prompts/...`.
3. Make sure the Git working tree is clean.
4. From the repository root, run:

```bash
./run_codex.sh
```

The script creates `.codex-run/` and writes each stage's final handoff there:

```text
.codex-run/01-audit.md
.codex-run/02-implement.md
.codex-run/03-review.md
```

The implementation prompt reads the Audit artifact; the Review prompt reads both previous artifacts and the actual Git diff. This makes the three independent sessions behave like a controlled pipeline rather than three unrelated chats.

## Safety behavior

- Audit and Review are launched with a read-only sandbox.
- Implementation is launched with `workspace-write` rather than unrestricted filesystem access.
- Approval policy is set to `never` for headless execution, but the sandbox remains in force.
- The script refuses to start on a dirty working tree unless `--allow-dirty` is explicitly supplied.
- It never uses `--dangerously-bypass-approvals-and-sandbox`.

## Useful commands

Preview the routing without spending model usage:

```bash
./run_codex.sh --dry-run
```

Run/re-run one stage:

```bash
./run_codex.sh --stage audit
./run_codex.sh --stage implement --allow-dirty
./run_codex.sh --stage review --allow-dirty
```

The `--allow-dirty` flag is normally needed for the later stages when you run them individually, because the implementation stage intentionally changes tracked files.

## Optional Codex profiles

The one-click script does **not** depend on profiles. It uses explicit CLI model overrides, which makes the pipeline self-contained.

If you also want convenient interactive/headless profiles, run:

```bash
./install_codex_profiles.sh
```

This installs:

```text
$CODEX_HOME/astra.config.toml
$CODEX_HOME/sol.config.toml
$CODEX_HOME/sol-medium.config.toml
$CODEX_HOME/sol-xhigh.config.toml
```

Existing files are not overwritten unless `--force` is supplied.
