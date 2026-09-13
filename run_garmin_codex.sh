#!/usr/bin/env bash
set -Eeuo pipefail

STAGE="all"
ALLOW_DIRTY=0
FALLBACK_SOL=0
DRY_RUN=0

ASTRA_MODEL="${ASTRA_MODEL:-gpt-6-astra}"
ASTRA_EFFORT="${ASTRA_EFFORT:-high}"
IMPLEMENT_MODEL="${IMPLEMENT_MODEL:-gpt-5.6-sol}"
IMPLEMENT_EFFORT="${IMPLEMENT_EFFORT:-high}"
FALLBACK_MODEL="${FALLBACK_MODEL:-gpt-5.6-sol}"
FALLBACK_EFFORT="${FALLBACK_EFFORT:-xhigh}"

usage() {
  cat <<'USAGE'
Usage: ./run_garmin_codex.sh [options]

Options:
  --stage audit|implement|review|all
  --allow-dirty
  --fallback-sol
  --dry-run
  -h, --help
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --stage) [[ $# -ge 2 ]] || { echo "--stage requires a value" >&2; exit 2; }; STAGE="$2"; shift 2 ;;
    --allow-dirty) ALLOW_DIRTY=1; shift ;;
    --fallback-sol) FALLBACK_SOL=1; shift ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
done

case "$STAGE" in audit|implement|review|all) ;; *) echo "Invalid --stage: $STAGE" >&2; exit 2 ;; esac

command -v git >/dev/null 2>&1 || { echo "git is required" >&2; exit 1; }
command -v codex >/dev/null 2>&1 || { echo "Codex CLI not found in PATH." >&2; exit 1; }

REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || true)"
[[ -n "$REPO_ROOT" ]] || { echo "Run this inside your MissingMCP fork." >&2; exit 1; }
cd "$REPO_ROOT"

required=(
  "src/missingmcp"
  "pyproject.toml"
  "garmin-ai-codex/IMPLEMENTATION_SPEC.md"
  "garmin-ai-codex/prompts/01_audit.md"
  "garmin-ai-codex/prompts/02_implement.md"
  "garmin-ai-codex/prompts/03_review.md"
)
for p in "${required[@]}"; do
  [[ -e "$p" ]] || { echo "Missing required path: $p" >&2; exit 1; }
done

# Ignore only this overlay and its own run artifacts when checking cleanliness.
if [[ $ALLOW_DIRTY -eq 0 ]]; then
  unrelated="$(git status --porcelain --untracked-files=all | \
    grep -vE '^\?\? garmin-ai-codex/|^\?\? run_garmin_codex\.sh$|^\?\? \.codex-run/garmin-ai/' || true)"
  if [[ -n "$unrelated" ]]; then
    echo "The repo has unrelated local changes:" >&2
    echo "$unrelated" >&2
    echo "Commit/stash them, or rerun with --allow-dirty." >&2
    exit 1
  fi
fi

mkdir -p .codex-run/garmin-ai

if [[ $FALLBACK_SOL -eq 1 ]]; then
  CHECKPOINT_MODEL="$FALLBACK_MODEL"; CHECKPOINT_EFFORT="$FALLBACK_EFFORT"
else
  CHECKPOINT_MODEL="$ASTRA_MODEL"; CHECKPOINT_EFFORT="$ASTRA_EFFORT"
fi

run_codex() {
  local label="$1" model="$2" effort="$3" sandbox="$4" prompt_file="$5" output_file="$6"
  echo
  echo "================================================================"
  echo "$label"
  echo "model=$model  effort=$effort  sandbox=$sandbox"
  echo "================================================================"
  if [[ $DRY_RUN -eq 1 ]]; then
    echo "DRY RUN: codex exec -m '$model' -c model_reasoning_effort=\"$effort\" -c approval_policy=\"never\" -s '$sandbox' -o '$output_file' -"
    return 0
  fi
  codex exec -m "$model" \
    -c "model_reasoning_effort=\"$effort\"" \
    -c 'approval_policy="never"' \
    -s "$sandbox" \
    -o "$output_file" \
    - < "$prompt_file"
  [[ -s "$output_file" ]] || { echo "No output produced: $output_file" >&2; exit 1; }
}

echo "Repository: $REPO_ROOT"
echo "Stage: $STAGE"
echo "Audit: $CHECKPOINT_MODEL / $CHECKPOINT_EFFORT"
echo "Implement: $IMPLEMENT_MODEL / $IMPLEMENT_EFFORT"
echo "Review: $CHECKPOINT_MODEL / $CHECKPOINT_EFFORT"

if [[ "$STAGE" == audit || "$STAGE" == all ]]; then
  run_codex "STAGE 1/3 — Architecture audit" "$CHECKPOINT_MODEL" "$CHECKPOINT_EFFORT" "read-only" \
    "garmin-ai-codex/prompts/01_audit.md" ".codex-run/garmin-ai/01-audit.md"
fi

if [[ "$STAGE" == implement || "$STAGE" == all ]]; then
  [[ -s .codex-run/garmin-ai/01-audit.md ]] || { echo "Run audit first." >&2; exit 1; }
  run_codex "STAGE 2/3 — Implementation" "$IMPLEMENT_MODEL" "$IMPLEMENT_EFFORT" "workspace-write" \
    "garmin-ai-codex/prompts/02_implement.md" ".codex-run/garmin-ai/02-implement.md"
fi

if [[ "$STAGE" == review || "$STAGE" == all ]]; then
  [[ -s .codex-run/garmin-ai/02-implement.md ]] || { echo "Run implementation first." >&2; exit 1; }
  run_codex "STAGE 3/3 — Security & compatibility review" "$CHECKPOINT_MODEL" "$CHECKPOINT_EFFORT" "read-only" \
    "garmin-ai-codex/prompts/03_review.md" ".codex-run/garmin-ai/03-review.md"
fi

[[ $DRY_RUN -eq 1 ]] || {
  echo
  echo "Done. Inspect:"
  echo "  .codex-run/garmin-ai/01-audit.md"
  echo "  .codex-run/garmin-ai/02-implement.md"
  echo "  .codex-run/garmin-ai/03-review.md"
  echo "  git status && git diff"
}
