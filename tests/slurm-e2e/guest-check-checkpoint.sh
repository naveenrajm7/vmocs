#!/bin/sh
set -eu

echo '=== checkpoint guest PCI devices ==='
lspci -Dnn

# rocJitsu provides the GPU-like vfio-user device used to prove that cold
# resume recreates device sidecars instead of attempting device-state restore.
lspci -Dnnd 1002:75c1 | grep -q .
if test "${VMOCS_EXPECT_PASSTHROUGH_GPU:-0}" = 1; then
    lspci -Dnnd 1002:744c | grep -q .
    echo 'VMOCS_PASSTHROUGH_GPU_OK'
fi

for _attempt in $(seq 1 30); do
    if test -e /dev/tpm0 || test -e /dev/tpmrm0; then
        break
    fi
    sleep 1
done
test -e /dev/tpm0 || test -e /dev/tpmrm0

sudo -n mkdir -p /mnt/vmocs-e2e
if ! mountpoint -q /mnt/vmocs-e2e; then
    sudo -n mount -t virtiofs vmocs-e2e /mnt/vmocs-e2e
fi
test -f /mnt/vmocs-e2e/tests/slurm-e2e/guest-check-checkpoint.sh

echo 'VMOCS_CHECKPOINT_DEVICE_E2E_OK'
