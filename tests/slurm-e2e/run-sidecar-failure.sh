#!/bin/bash
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
REPO_PATH=$(cd -- "$SCRIPT_DIR/../.." && pwd)
VMOCS_BIN=${VMOCS_BIN:-vmocs}
JOB_ID=${VMOCS_E2E_JOB_ID:-$((950000 + $$))}
RUNTIME_DIR=/tmp/vmocs/$JOB_ID
TEST_HOME=/tmp/vmocs-sidecar-failure-home-$JOB_ID
LOG=/tmp/vmocs-sidecar-failure-$JOB_ID.log
launcher_pid=

cleanup() {
    if test -n "$launcher_pid" && kill -0 "$launcher_pid" 2>/dev/null; then
        kill "$launcher_pid" 2>/dev/null || true
        wait "$launcher_pid" 2>/dev/null || true
    fi
    HOME="$TEST_HOME" "$VMOCS_BIN" --config "$SCRIPT_DIR/target-vmocs.yaml" \
        stop --if-exists "$JOB_ID" >/dev/null 2>&1 || true
    rm -f "$LOG"
    rmdir "$TEST_HOME" >/dev/null 2>&1 || true
}
trap cleanup EXIT
mkdir -p "$TEST_HOME"

HOME="$TEST_HOME" PYTHONPATH="$REPO_PATH/lib" \
    "$VMOCS_BIN" --config "$SCRIPT_DIR/target-vmocs.yaml" run \
    --cores 4 --memory 8192 --job-id "$JOB_ID" --attach none \
    vfio-user-sidecars-e2e >"$LOG" 2>&1 &
launcher_pid=$!

for _attempt in $(seq 1 300); do
    if test -f "$RUNTIME_DIR/vm.json" && \
       grep -q '"state": "running"' "$RUNTIME_DIR/vm.json"; then
        break
    fi
    if ! kill -0 "$launcher_pid" 2>/dev/null; then
        cat "$LOG"
        exit 1
    fi
    sleep 1
done
grep -q '"state": "running"' "$RUNTIME_DIR/vm.json"

sidecar_pid=$(python3 -c \
    'import json,sys; d=json.load(open(sys.argv[1])); print(next(x["pid"] for x in d["sidecars"] if x["kind"] == "rocjitsu"))' \
    "$RUNTIME_DIR/vm.json")
kill -TERM "$sidecar_pid"

set +e
wait "$launcher_pid"
status=$?
set -e
launcher_pid=
cat "$LOG"

test "$status" -ne 0
grep -q 'critical sidecar rocjitsu-0 exited' "$LOG"
test ! -e "$RUNTIME_DIR"
echo 'VMOCS_SIDECAR_FAILURE_CLEANUP_OK'
