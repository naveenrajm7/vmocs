#!/usr/bin/env bash
# Verify GPU passthrough for a running Slurm job with --vm-image.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.sh
source "$SCRIPT_DIR/lib/common.sh"
load_config

JOBID="${1:?usage: verify-gpu.sh JOBID | verify-gpu.sh --wait-only JOBID}"
WAIT_ONLY=false
if [[ "$JOBID" == "--wait-only" ]]; then
    WAIT_ONLY=true
    JOBID="${2:?usage: verify-gpu.sh --wait-only JOBID}"
fi

echo "==> Waiting for job $JOBID to reach RUNNING"
wait_job_state "$JOBID" RUNNING 120 || die "job $JOBID did not reach RUNNING"

echo "==> Waiting for VM runtime (vm.json) on job node"
WAIT_VM='
set -e
JOBID='"$JOBID"'
RUNTIME=/tmp/vmocs/$JOBID
deadline=$((SECONDS + '"$VM_READY_TIMEOUT"'))
while (( SECONDS < deadline )); do
  if [[ -f $RUNTIME/vm.json ]]; then
    echo "vm.json ready"
    exit 0
  fi
  sleep 5
done
echo "FAIL: $RUNTIME/vm.json not found after '"$VM_READY_TIMEOUT"'s"
ls -la /tmp/vmocs/ 2>/dev/null || true
exit 1
'
job_node_exec "$JOBID" "$WAIT_VM"

if $WAIT_ONLY; then
    echo "VM ready for job $JOBID"
    exit 0
fi

echo "==> vmocs list on job node"
job_node_exec "$JOBID" "vmocs list 2>/dev/null || true; ls -la /tmp/vmocs/ 2>/dev/null || true"

VERIFY='
set -e
JOBID='"$JOBID"'
RUNTIME=/tmp/vmocs/$JOBID
if [[ ! -d $RUNTIME ]]; then
  echo "FAIL: runtime dir $RUNTIME missing"
  exit 1
fi
PORT=$(python3 -c "import json; print(json.load(open(\"$RUNTIME/vm.json\"))[\"ssh_port\"])")
KEY=$RUNTIME/id_ed25519
echo "SSH port=$PORT"
ssh -i $KEY -o StrictHostKeyChecking=no -o ConnectTimeout=15 -o UserKnownHostsFile=/dev/null \
    -p $PORT ubuntu@127.0.0.1 \
    "echo GPU devices:; lspci -nn | grep -iE \"VGA|3D|Display|AMD|NVIDIA\" || lspci -nn | grep -i amd"
GPU_COUNT=$(ssh -i $KEY -o StrictHostKeyChecking=no -o ConnectTimeout=15 -o UserKnownHostsFile=/dev/null \
    -p $PORT ubuntu@127.0.0.1 "lspci -nn | grep -ciE \"VGA|3D|Display\" || true")
echo "GPU class device count in guest: $GPU_COUNT"
if [[ "${GPU_COUNT:-0}" -lt 1 ]]; then
  echo "FAIL: no GPU visible in guest"
  exit 1
fi
echo "PASS: GPU visible in guest for job '"$JOBID"'"
'

echo "==> Checking guest lspci via SSH"
job_node_exec "$JOBID" "$VERIFY"

echo "==> Slurm GPU env on job node"
job_node_exec "$JOBID" 'echo SLURM_STEP_GPUS=${SLURM_STEP_GPUS:-unset}; echo SLURM_JOB_GPUS=${SLURM_JOB_GPUS:-unset}'

echo "==> QEMU log tail (if present)"
job_node_exec "$JOBID" "tail -30 /tmp/vmocs/$JOBID/qemu.log 2>/dev/null || echo '(no qemu.log)'"

echo "==> Check vmocs launch used --pci (ordinal map path)"
job_node_exec "$JOBID" "grep -E 'pci|launch' /tmp/vmocs/$JOBID/qemu.log 2>/dev/null | head -5 || ps aux | grep -E 'qemu.*pci' | grep -v grep | head -3 || echo '(check slurm verbose log for vmocs: cmd line)'"
