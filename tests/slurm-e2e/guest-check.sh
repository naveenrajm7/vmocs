#!/bin/sh
set -eu

echo '=== guest PCI devices ==='
lspci -Dnn

# IDs are package-specific observations, not part of the vmocs configuration
# contract. rocm-ernic builds used in the two bring-ups advertised either the
# upstream ionic ID or the earlier Pensando ID. Both prove the NIC attached.
if ! lspci -Dnnd 1022:8001 | grep -q . && \
   ! lspci -Dnnd 1dd8:100a | grep -q .; then
    echo 'rocm-ernic PCI function not found' >&2
    exit 1
fi
lspci -Dnnd 1002:75c1 | grep -q .

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
test -f /mnt/vmocs-e2e/tests/slurm-e2e/guest-check.sh

echo 'VMOCS_SIDECAR_E2E_OK'
