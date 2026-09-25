#!/bin/bash
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
REPO_PATH=$(cd -- "$SCRIPT_DIR/../.." && pwd)
VMOCS_BIN=${VMOCS_BIN:-vmocs}
VMOCS_CONFIG=${VMOCS_E2E_CONFIG:-$SCRIPT_DIR/target-vmocs.yaml}
VM_IMAGE=${VMOCS_E2E_TEMPLATE:-checkpoint-e2e}
SAVE_JOB_ID=${VMOCS_E2E_JOB_ID:-$((940000 + $$))}
RESUME_JOB_ID=$((SAVE_JOB_ID + 1))
SAVE_RUNTIME=/tmp/vmocs/$SAVE_JOB_ID
RESUME_RUNTIME=/tmp/vmocs/$RESUME_JOB_ID
CHECKPOINT=${VMOCS_E2E_CHECKPOINT:-/tmp/vmocs-checkpoint-cancel-e2e-$SAVE_JOB_ID}
TEST_HOME=/tmp/vmocs-checkpoint-cancel-e2e-home-$SAVE_JOB_ID
LOG=/tmp/vmocs-checkpoint-cancel-e2e-$SAVE_JOB_ID.log
MARKER="cancel-checkpoint-$SAVE_JOB_ID"
SUPERVISOR_PID=
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
    if test -n "$SUPERVISOR_PID"; then
        kill -KILL "$SUPERVISOR_PID" >/dev/null 2>&1 || true
    fi
    for job_id in "$SAVE_JOB_ID" "$RESUME_JOB_ID"; do
        HOME="$TEST_HOME" PYTHONPATH="$REPO_PATH/lib" \
            "$VMOCS_BIN" --config "$VMOCS_CONFIG" \
            stop --if-exists "$job_id" >/dev/null 2>&1 || true
    done
    rm -rf -- "$CHECKPOINT" "$TEST_HOME"
    rm -f -- "$LOG"
}
trap cleanup EXIT
cleanup
mkdir -p "$TEST_HOME"

# Keep the guest command active after its durable marker is written. Sending
# TERM to the vmocs supervisor reproduces Slurm's cancellation ordering.
HOME="$TEST_HOME" PYTHONPATH="$REPO_PATH/lib" \
    "$VMOCS_BIN" --config "$VMOCS_CONFIG" run \
    --cores 4 --memory 8192 --job-id "$SAVE_JOB_ID" \
    "${pci_args[@]}" \
    --save "$CHECKPOINT" "$VM_IMAGE" -- \
    sh -c 'printf "%s\n" "$1" | sudo -n tee /var/tmp/vmocs-agent-state >/dev/null; sudo -n sync; echo VMOCS_AGENT_STATE_DURABLE; while :; do sleep 1; done' sh "$MARKER" \
    >"$LOG" 2>&1 &
SUPERVISOR_PID=$!

ready=0
for _attempt in $(seq 1 600); do
    if grep -q VMOCS_AGENT_STATE_DURABLE "$LOG"; then
        ready=1
        break
    fi
    if ! kill -0 "$SUPERVISOR_PID" 2>/dev/null; then
        cat "$LOG" >&2
        echo 'vmocs exited before the guest marker became durable' >&2
        exit 1
    fi
    sleep 0.1
done
test "$ready" -eq 1

# Slurm can deliver TERM to every process in the step cgroup, not only the
# Python supervisor. Terminate QEMU and the supervisor back-to-back to exercise
# checkpointing when QEMU is already disappearing.
QEMU_PID=$(python3 -c \
    'import json,sys; print(json.load(open(sys.argv[1]))["pid"])' \
    "$SAVE_RUNTIME/vm.json")
kill -TERM "$QEMU_PID"
kill -TERM "$SUPERVISOR_PID"
set +e
wait "$SUPERVISOR_PID"
status=$?
set -e
SUPERVISOR_PID=
test "$status" -eq 143

test -f "$CHECKPOINT/COMPLETE"
test -f "$CHECKPOINT/manifest.json"
test -f "$CHECKPOINT/disk.qcow2"
test ! -e "$SAVE_RUNTIME"

HOME="$TEST_HOME" PYTHONPATH="$REPO_PATH/lib" \
    "$VMOCS_BIN" --config "$VMOCS_CONFIG" run \
    --cores 4 --memory 8192 --job-id "$RESUME_JOB_ID" \
    "${pci_args[@]}" \
    --resume "$CHECKPOINT" "$VM_IMAGE" -- \
    sh -c 'test "$(cat /var/tmp/vmocs-agent-state)" = "$1"; sudo -n mkdir -p /mnt/vmocs-e2e; sudo -n mount -t virtiofs vmocs-e2e /mnt/vmocs-e2e; VMOCS_EXPECT_PASSTHROUGH_GPU="$2" exec sh /mnt/vmocs-e2e/tests/slurm-e2e/guest-check-checkpoint.sh' sh "$MARKER" "$EXPECT_PASSTHROUGH_GPU"

test ! -e "$RESUME_RUNTIME"
echo 'VMOCS_CHECKPOINT_CANCEL_RESUME_OK'
