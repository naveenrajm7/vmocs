#!/usr/bin/env bash
# Build spank_vmocs.so against the cluster's slurm-devel headers.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.sh
source "$SCRIPT_DIR/lib/common.sh"

echo "==> Building SPANK plugin"
rpm -q slurm-devel &>/dev/null || die "slurm-devel not installed — headers must match cluster Slurm"

make -C "$REPO_ROOT/plugins/slurm" clean
make -C "$REPO_ROOT/plugins/slurm"

SO="$REPO_ROOT/plugins/slurm/spank_vmocs.so"
[[ -f "$SO" ]] || die "build failed — $SO missing"
file "$SO"
echo "Built: $SO"
