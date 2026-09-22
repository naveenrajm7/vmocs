#!/bin/bash
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
REPO_PATH=$(cd -- "$SCRIPT_DIR/../.." && pwd)
VMOCS_BIN=${VMOCS_BIN:-vmocs}
VMOCS_CONFIG=${VMOCS_E2E_CONFIG:-$SCRIPT_DIR/target-vmocs.yaml}
VM_IMAGE=${VMOCS_E2E_TEMPLATE:-checkpoint-e2e}
SAVE_JOB_ID=${VMOCS_E2E_JOB_ID:-$((920000 + $$))}
RESUME_JOB_ID=$((SAVE_JOB_ID + 1))
SAVE_RUNTIME=/tmp/vmocs/$SAVE_JOB_ID
RESUME_RUNTIME=/tmp/vmocs/$RESUME_JOB_ID
CHECKPOINT=${VMOCS_E2E_CHECKPOINT:-/tmp/vmocs-checkpoint-e2e-$SAVE_JOB_ID}
TEST_HOME=/tmp/vmocs-checkpoint-e2e-home-$SAVE_JOB_ID
MARKER="checkpoint-$SAVE_JOB_ID"
pci_args=()
for bdf in ${VMOCS_E2E_PCI_DEVICES:-}; do
    case "$bdf" in
        ????\:??\:??.?) pci_args+=(--pci "$bdf") ;;
        *) echo "invalid VMOCS_E2E_PCI_DEVICES entry: $bdf" >&2; exit 2 ;;
    esac
done
EXPECT_PASSTHROUGH_GPU=0
if test "${#pci_args[@]}" -gt 0; then
    EXPECT_PASSTHROUGH_GPU=1
fi

cleanup() {
    for job_id in "$SAVE_JOB_ID" "$RESUME_JOB_ID"; do
        HOME="$TEST_HOME" PYTHONPATH="$REPO_PATH/lib" \
            "$VMOCS_BIN" --config "$VMOCS_CONFIG" \
            stop --if-exists "$job_id" >/dev/null 2>&1 || true
    done
    rm -rf -- "$CHECKPOINT" "$TEST_HOME"
}
trap cleanup EXIT
cleanup
mkdir -p "$TEST_HOME"

# First generation: write an agent-workspace marker to the primary guest disk,
# cleanly stop QEMU, and publish a thin checkpoint bundle.
HOME="$TEST_HOME" PYTHONPATH="$REPO_PATH/lib" \
    "$VMOCS_BIN" --config "$VMOCS_CONFIG" run \
    --cores 4 --memory 8192 --job-id "$SAVE_JOB_ID" \
    "${pci_args[@]}" \
    --save "$CHECKPOINT" "$VM_IMAGE" -- \
    sh -c 'sudo -n mkdir -p /mnt/vmocs-e2e; sudo -n mount -t virtiofs vmocs-e2e /mnt/vmocs-e2e; VMOCS_EXPECT_PASSTHROUGH_GPU="$2" sh /mnt/vmocs-e2e/tests/slurm-e2e/guest-check-checkpoint.sh; printf "%s\n" "$1" | sudo -n tee /var/tmp/vmocs-agent-state >/dev/null; sudo -n sync' sh "$MARKER" "$EXPECT_PASSTHROUGH_GPU"

test -f "$CHECKPOINT/COMPLETE"
test -f "$CHECKPOINT/manifest.json"
test -f "$CHECKPOINT/disk.qcow2"
test ! -e "$SAVE_RUNTIME"

# Second generation: cold-boot through a fresh overlay, prove primary-disk
# state survived, and prove the GPU-like vfio-user sidecars were recreated.
HOME="$TEST_HOME" PYTHONPATH="$REPO_PATH/lib" \
    "$VMOCS_BIN" --config "$VMOCS_CONFIG" run \
    --cores 4 --memory 8192 --job-id "$RESUME_JOB_ID" \
    "${pci_args[@]}" \
    --resume "$CHECKPOINT" "$VM_IMAGE" -- \
    sh -c 'test "$(cat /var/tmp/vmocs-agent-state)" = "$1"; sudo -n mkdir -p /mnt/vmocs-e2e; sudo -n mount -t virtiofs vmocs-e2e /mnt/vmocs-e2e; VMOCS_EXPECT_PASSTHROUGH_GPU="$2" exec sh /mnt/vmocs-e2e/tests/slurm-e2e/guest-check-checkpoint.sh' sh "$MARKER" "$EXPECT_PASSTHROUGH_GPU"

test ! -e "$RESUME_RUNTIME"
echo 'VMOCS_CHECKPOINT_RESUME_OK'
