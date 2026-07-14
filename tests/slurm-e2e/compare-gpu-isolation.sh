#!/usr/bin/env bash
# Compare host-side GPU allocation between two concurrent jobs.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.sh
source "$SCRIPT_DIR/lib/common.sh"
load_config

J1="${1:?usage: compare-gpu-isolation.sh JOBID1 JOBID2}"
J2="${2:?}"

job_gpu_info() {
    local jobid="$1"
    job_node_exec "$jobid" "
set -e
RUNTIME=/tmp/vmocs/$jobid
PID=\$(python3 -c \"import json; print(json.load(open('\$RUNTIME/vm.json'))['pid'])\")
echo SLURM_STEP_GPUS=\${SLURM_STEP_GPUS:-unset}
tr '\\0' ' ' < /proc/\$PID/cmdline | grep -oE 'host=[0-9a-fA-F:.]+' || true
"
}

echo "==> Job $J1 allocation:"
OUT1=$(job_gpu_info "$J1" || true)
echo "$OUT1"

echo "==> Job $J2 allocation:"
OUT2=$(job_gpu_info "$J2" || true)
echo "$OUT2"

ORD1=$(echo "$OUT1" | grep SLURM_STEP_GPUS | cut -d= -f2)
ORD2=$(echo "$OUT2" | grep SLURM_STEP_GPUS | cut -d= -f2)
BDF1=$(echo "$OUT1" | grep -oE 'host=[0-9a-fA-F:.]+' | head -1 | cut -d= -f2)
BDF2=$(echo "$OUT2" | grep -oE 'host=[0-9a-fA-F:.]+' | head -1 | cut -d= -f2)

if [[ -z "$ORD1" || -z "$ORD2" ]]; then
    echo "FAIL: missing SLURM_STEP_GPUS"
    exit 1
fi

if [[ "$ORD1" == "$ORD2" ]]; then
    echo "FAIL: same SLURM_STEP_GPUS ordinal ($ORD1)"
    exit 1
fi
echo "PASS: different SLURM_STEP_GPUS ordinals ($ORD1 vs $ORD2)"

if [[ -n "$BDF1" && -n "$BDF2" ]]; then
    if [[ "$BDF1" == "$BDF2" ]]; then
        echo "FAIL: same host PCI BDF ($BDF1)"
        exit 1
    fi
    echo "PASS: different host PCI BDFs ($BDF1 vs $BDF2)"
else
    echo "WARN: could not read host BDF from QEMU cmdline (ordinals differ — likely OK)"
fi
