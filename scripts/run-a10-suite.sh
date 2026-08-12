#!/usr/bin/env bash
#
# Record A10 research artifacts and the vLLM decode comparison.
#
# Run this after ./scripts/bootstrap-a10.sh. Every pass writes one self-describing
# artifact under runtime/artifacts/a10-research/. The harness refuses to write if
# the GPU present is not an A10, so a mislabeled run fails instead of producing
# false evidence.
#
# Usage:
#   ./scripts/run-a10-suite.sh            # full set
#   SKIP_VLLM=1 ./scripts/run-a10-suite.sh
#   DRIFT_STEPS=8192 ./scripts/run-a10-suite.sh

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TARGET="a10-research"
DRIFT_STEPS="${DRIFT_STEPS:-4096}"
BATCHES="${BATCHES:-1 2 4 8 16 32}"
SKIP_VLLM="${SKIP_VLLM:-0}"

log()  { printf '\n\033[1m== %s\033[0m\n' "$*"; }
warn() { printf '\033[33mWARNING: %s\033[0m\n' "$*" >&2; }
die()  { printf '\033[31mERROR: %s\033[0m\n' "$*" >&2; exit 1; }

RUNTIME_PYTHON="$REPO_ROOT/runtime/.venv/bin/python"
SERVING_PYTHON="$REPO_ROOT/serving/.venv/bin/python"
[ -x "$RUNTIME_PYTHON" ] || die "runtime environment missing; run ./scripts/bootstrap-a10.sh first"

# --------------------------------------------------------------------------
log "Provenance"
# --------------------------------------------------------------------------

cd "$REPO_ROOT"
COMMIT="$(git rev-parse --short HEAD)"
echo "commit: $COMMIT"
if [ -n "$(git status --porcelain)" ]; then
    warn "the worktree is dirty, so artifacts will be filenamed '-dirty' and are not reproducible evidence."
    warn "commit or stash first if these runs are meant to be citable."
fi

# --------------------------------------------------------------------------
log "FP16 correctness, drift, sweep, baseline, profile"
# --------------------------------------------------------------------------

# Run from runtime/ so the packages resolve whether or not they are installed
# editable into the environment.
cd "$REPO_ROOT/runtime"

# shellcheck disable=SC2086
"$RUNTIME_PYTHON" -m harness.runner \
    --target "$TARGET" \
    --dtype float16 \
    --drift-steps "$DRIFT_STEPS" \
    --batches $BATCHES

# --------------------------------------------------------------------------
log "BF16 pass (A10 supports it natively; T4 does not)"
# --------------------------------------------------------------------------

# shellcheck disable=SC2086
"$RUNTIME_PYTHON" -m harness.runner \
    --target "$TARGET" \
    --dtype bfloat16 \
    --drift-steps "$DRIFT_STEPS" \
    --batches $BATCHES

# --------------------------------------------------------------------------
log "vLLM decode comparison"
# --------------------------------------------------------------------------

if [ "$SKIP_VLLM" = "1" ]; then
    echo "skipped by request"
elif [ ! -x "$SERVING_PYTHON" ]; then
    warn "serving environment missing; skipping the vLLM comparison"
else
    OUTPUT="$REPO_ROOT/runtime/artifacts/$TARGET/vllm-compare-$COMMIT.json"
    mkdir -p "$(dirname "$OUTPUT")"
    # Measure once, then summarize from the recorded JSON.
    cd "$REPO_ROOT/serving"
    # shellcheck disable=SC2086
    "$SERVING_PYTHON" -m fabric_serving.compare --sequences $BATCHES --json > "$OUTPUT"
    "$SERVING_PYTHON" - "$OUTPUT" <<'PY'
import json
import sys

rows = json.load(open(sys.argv[1]))
print(f"{'seqs':>5} {'vllm_ms':>9} {'fabric_ms':>10} {'speedup':>8} {'out_delta':>10}")
for row in rows:
    print(
        f"{row['sequences']:>5} {row['vllm_ms']:>9.4f} {row['fabric_ms']:>10.4f} "
        f"{row['fabric_speedup_vs_vllm']:>7.2f}x {row['output_max_abs_vs_vllm']:>10.3g}"
    )
PY
    echo "written: ${OUTPUT#"$REPO_ROOT"/}"
fi

# --------------------------------------------------------------------------
log "Artifacts"
# --------------------------------------------------------------------------

cd "$REPO_ROOT/runtime"
"$RUNTIME_PYTHON" - "$REPO_ROOT/runtime/artifacts/$TARGET" <<'PY'
import pathlib
import sys

from harness.artifact import read_artifact

root = pathlib.Path(sys.argv[1])
for path in sorted(root.glob("*.json")):
    if path.name.startswith("vllm-compare"):
        print(f"{path.name}  (vLLM comparison, not an artifact)")
        continue
    artifact = read_artifact(path)  # raises if the content hash does not match
    config = artifact["config"]
    print(
        f"{path.name}  v{artifact['schema_version']} {config['dtype']} "
        f"status={artifact['status']} production_citable={artifact['citable_as_production']}"
    )
PY

cat <<EOF

Next:

  git add runtime/artifacts && git commit -m "Record A10 research artifacts"
  git push

A10 runs are research evidence. They are marked citable_as_production: false, and
the release gate is T4 only.
EOF
