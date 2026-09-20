#!/bin/bash
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
REPO_PATH=$(cd -- "$SCRIPT_DIR/../.." && pwd)
VMOCS_BIN=${VMOCS_BIN:-vmocs}
JOB_ID=${VMOCS_E2E_JOB_ID:-$((900000 + $$))}
RUNTIME_DIR=/tmp/vmocs/$JOB_ID
TEST_HOME=/tmp/vmocs-sidecar-e2e-home-$JOB_ID

cleanup() {
    HOME="$TEST_HOME" "$VMOCS_BIN" --config "$SCRIPT_DIR/target-vmocs.yaml" \
        stop --if-exists "$JOB_ID" >/dev/null 2>&1 || true
    rmdir "$TEST_HOME" >/dev/null 2>&1 || true
}
trap cleanup EXIT
mkdir -p "$TEST_HOME"

HOME="$TEST_HOME" PYTHONPATH="$REPO_PATH/lib" \
    "$VMOCS_BIN" --config "$SCRIPT_DIR/target-vmocs.yaml" run \
    --cores 4 --memory 8192 --job-id "$JOB_ID" \
    vfio-user-sidecars-e2e -- \
    sh -c 'sudo -n mkdir -p /mnt/vmocs-e2e; sudo -n mount -t virtiofs vmocs-e2e /mnt/vmocs-e2e; exec sh /mnt/vmocs-e2e/tests/slurm-e2e/guest-check.sh'

test ! -e "$RUNTIME_DIR"
echo 'VMOCS_DIRECT_CLEANUP_OK'
