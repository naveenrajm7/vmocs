#!/bin/bash
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
if test "${VMOCS_E2E_USE_LOCAL_CONF:-1}" = 1 && \
   test -f "$SCRIPT_DIR/local.conf"; then
    # shellcheck source=/dev/null
    source "$SCRIPT_DIR/local.conf"
fi

PARTITION=${PARTITION:-vm}
ACCOUNT=${ACCOUNT:-vm}
NODELIST=${NODELIST:-vm-gpu-node.example.com}
VM_IMAGE=${VM_IMAGE:-vfio-user-sidecars-e2e}
CPUS_PER_TASK=${CPUS_PER_TASK:-4}
MEM=${MEM:-10G}

args=(
    --nodes=1
    --ntasks=1
    --nodelist="$NODELIST"
    --partition="$PARTITION"
    --cpus-per-task="$CPUS_PER_TASK"
    --mem="$MEM"
    --vm-image="$VM_IMAGE"
)
if test -n "$ACCOUNT" && test "$ACCOUNT" != your-account; then
    args+=(--account="$ACCOUNT")
fi

srun "${args[@]}" -- \
    sh -c 'sudo -n mkdir -p /mnt/vmocs-e2e; sudo -n mount -t virtiofs vmocs-e2e /mnt/vmocs-e2e; exec sh /mnt/vmocs-e2e/tests/slurm-e2e/guest-check.sh'

echo 'VMOCS_SLURM_E2E_OK'
