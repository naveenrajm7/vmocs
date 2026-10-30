#!/usr/bin/env bash
# Direct vmocs integration check. This script does not invoke Slurm.

set -uo pipefail

repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
vmocs_bin=${VMOCS_BIN:-/usr/local/bin/vmocs}
template=${VMOCS_WINDOWS_TEMPLATE:?set VMOCS_WINDOWS_TEMPLATE to an insert-key-enabled Windows template}
job_id=${VMOCS_TEST_JOB_ID:-99003001}
guest_repo=${VMOCS_WINDOWS_GUEST_REPO:-'Z:\devcloud\vmocs'}

export SLURM_GHA_SERVICE_URL='https://slurm-gha-host:8443/healthz?x=1&y=2'
export SLURM_GHA_LEASE_ID='lease-"quoted"-☃'
export SLURM_GHA_CLASS='windows rocm'
export SLURM_GHA_CLAIM_TOKEN=$'line one\r\nline two '\''"`$() % & | < > ^ snowman ☃'
export SLURM_JOB_ID='99003001'
export SLURM_GHA_CA_FILE='C:\slurm-gha\callback-ca.cer'
export VMOCS_EMPTY=''

run_vmocs() {
    PYTHONPATH="$repo_root/lib${PYTHONPATH:+:$PYTHONPATH}" "$vmocs_bin" run \
        "$template" --cores 4 --memory 8192 --job-id "$job_id" \
        --forward-env=SLURM_GHA_SERVICE_URL \
        --forward-env=SLURM_GHA_LEASE_ID \
        --forward-env=SLURM_GHA_CLASS \
        --forward-env=SLURM_GHA_CLAIM_TOKEN \
        --forward-env=SLURM_JOB_ID \
        --forward-env=SLURM_GHA_CA_FILE \
        --forward-env=VMOCS_EMPTY \
        -- powershell.exe -NoLogo -NoProfile -NonInteractive \
        -File "${guest_repo}\\tests\\windows-forward-env-check.ps1" \
        '' 'space value' 'quote"value' 'trailing\' 'meta&chars|<>^%'
}

if [[ ${VMOCS_TEST_TTY:-0} == 1 ]]; then
    run_vmocs
    status=$?
else
    printf '%s\n' 'child stdin remains available' | run_vmocs
    status=${PIPESTATUS[1]}
fi

if [[ $status -ne 37 ]]; then
    echo "Windows environment forwarding check failed with status $status" >&2
    exit 1
fi

echo 'PASS: Windows environment forwarding, argv, stdin, cleanup, and exit status'
