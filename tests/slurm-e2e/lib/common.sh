#!/usr/bin/env bash
# Shared helpers for slurm-e2e harness.
set -euo pipefail

HARNESS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPO_ROOT="$(cd "$HARNESS_DIR/../.." && pwd)"
CONF_FILE="${HARNESS_CONF:-$HARNESS_DIR/local.conf}"

die() { echo "ERROR: $*" >&2; exit 1; }

load_config() {
    [[ -f "$CONF_FILE" ]] || die "Missing $CONF_FILE — copy local.conf.example to local.conf"
    # shellcheck source=/dev/null
    source "$CONF_FILE"
    : "${PARTITION:?PARTITION required in local.conf}"
    : "${ACCOUNT:?ACCOUNT required in local.conf}"
    : "${NODELIST:?NODELIST required in local.conf}"
    : "${VM_IMAGE:?VM_IMAGE required in local.conf}"
    : "${GRES:?GRES required in local.conf}"
    CPUS_PER_TASK="${CPUS_PER_TASK:-4}"
    MEM="${MEM:-8G}"
    PLUGIN_INSTALL_DIR="${PLUGIN_INSTALL_DIR:-/etc/slurm}"
    VMOCS_CONF="${VMOCS_CONF:-/etc/vmocs/vmocs.yaml}"
    VFIO_MAP="${VFIO_MAP:-/etc/vmocs/vfio-gpu.map}"
    REPO_PATH="${REPO_PATH:-$REPO_ROOT}"
    OUTPUT_DIR="${OUTPUT_DIR:-/tmp}"
    VM_READY_TIMEOUT="${VM_READY_TIMEOUT:-300}"
}

wait_job_state() {
    local jobid="$1" want="$2" deadline=$((SECONDS + ${3:-300}))
    while (( SECONDS < deadline )); do
        local st
        st=$(squeue -j "$jobid" -h -o '%T' 2>/dev/null || true)
        [[ "$st" == "$want" ]] && return 0
        [[ -z "$st" ]] && return 1
        sleep 3
    done
    return 1
}

wait_job_gone() {
    local jobid="$1" deadline=$((SECONDS + ${2:-120}))
    while (( SECONDS < deadline )); do
        squeue -j "$jobid" -h 2>/dev/null | grep -q . || return 0
        sleep 2
    done
    return 1
}

# Run a command on NODELIST via a short srun (for node-local checks).
node_exec() {
    srun -p "$PARTITION" -w "$NODELIST" --account="$ACCOUNT" --time=2 bash -c "$*"
}

# Run verification on the node hosting a running job (overlap into allocation).
job_node_exec() {
    local jobid="$1"; shift
    srun --jobid="$jobid" --overlap --account="$ACCOUNT" bash -c "$*"
}

render_job() {
    local template="$1" out="$2" name="$3"
    sed \
        -e "s|@PARTITION@|$PARTITION|g" \
        -e "s|@ACCOUNT@|$ACCOUNT|g" \
        -e "s|@NODELIST@|$NODELIST|g" \
        -e "s|@CPUS@|$CPUS_PER_TASK|g" \
        -e "s|@MEM@|$MEM|g" \
        -e "s|@GRES@|$GRES|g" \
        -e "s|@VM_IMAGE@|$VM_IMAGE|g" \
        -e "s|@JOB_NAME@|$name|g" \
        -e "s|@OUTPUT_DIR@|$OUTPUT_DIR|g" \
        "$template" > "$out"
}
