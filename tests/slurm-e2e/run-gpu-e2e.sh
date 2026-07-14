#!/usr/bin/env bash
# End-to-end GPU passthrough test: build, deploy, submit, verify, cancel.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.sh
source "$SCRIPT_DIR/lib/common.sh"
load_config

MODE="${1:-single}"   # single | dual | both

TMPDIR=$(mktemp -d)
trap 'rm -rf "$TMPDIR"' EXIT

echo "==> Step 1: build plugin"
"$SCRIPT_DIR/build-plugin.sh"

echo "==> Step 2: deploy plugin + vfio map"
"$SCRIPT_DIR/deploy-plugin.sh"

submit_one() {
    local name="$1" template="$2"
    local rendered="$TMPDIR/${name}.sbatch"
    render_job "$template" "$rendered" "$name"
    echo "==> Submitting $name" >&2
    cat "$rendered" >&2
    sbatch --parsable "$rendered"
}

JOBIDS=()
case "$MODE" in
    single)
        JOBIDS+=("$(submit_one gpu-single "$SCRIPT_DIR/jobs/gpu-single.sbatch.in")")
        ;;
    dual)
        JOBIDS+=("$(submit_one gpu-concurrent "$SCRIPT_DIR/jobs/gpu-concurrent.sbatch.in")")
        ;;
    both)
        # Stagger submissions so vmocs port allocation does not collide on 60222.
        jid1=$(submit_one gpu-single "$SCRIPT_DIR/jobs/gpu-single.sbatch.in")
        JOBIDS+=("$jid1")
        echo "==> Waiting for first VM before submitting second job"
        "$SCRIPT_DIR/verify-gpu.sh" --wait-only "$jid1"
        JOBIDS+=("$(submit_one gpu-concurrent "$SCRIPT_DIR/jobs/gpu-concurrent.sbatch.in")")
        ;;
    *)
        die "usage: run-gpu-e2e.sh [single|dual|both]"
        ;;
esac

echo "==> Submitted job(s): ${JOBIDS[*]}"
squeue -j "$(IFS=,; echo "${JOBIDS[*]}")" 2>/dev/null || squeue -u "$USER" | head -10

FAILED=0
for jid in "${JOBIDS[@]}"; do
    echo ""
    echo "========================================"
    echo "Verifying job $jid"
    echo "========================================"
    if "$SCRIPT_DIR/verify-gpu.sh" "$jid"; then
        echo "Job $jid: PASS"
    else
        echo "Job $jid: FAIL"
        FAILED=$((FAILED + 1))
    fi
done

if [[ "$MODE" == "both" && ${#JOBIDS[@]} -eq 2 && $FAILED -eq 0 ]]; then
    echo ""
    echo "==> GPU isolation check"
    if "$SCRIPT_DIR/compare-gpu-isolation.sh" "${JOBIDS[0]}" "${JOBIDS[1]}"; then
        echo "Isolation: PASS"
    else
        echo "Isolation: FAIL"
        FAILED=$((FAILED + 1))
    fi
fi

echo ""
echo "==> Cancelling test job(s)"
for jid in "${JOBIDS[@]}"; do
    scancel "$jid" 2>/dev/null || true
done

for jid in "${JOBIDS[@]}"; do
    wait_job_gone "$jid" 180 || echo "WARN: job $jid still in queue after cancel"
done

if (( FAILED > 0 )); then
    echo "FAILED: $FAILED job(s)"
    exit 1
fi
echo "All GPU passthrough checks passed."
