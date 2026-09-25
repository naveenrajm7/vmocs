#!/usr/bin/env bash
#
# Daemon-free smoke test for the vmocs SPANK plugin.
#
# Builds spank_vmocs.so against the installed Slurm headers, checks its exported
# and required symbols, then loads it through the real Slurm option parser to
# confirm the plugin registers its options and validates their arguments. It
# needs only libslurm-dev and the Slurm client (srun/sbatch); no slurmctld,
# slurmd, munge, or cgroups are required.
#
# Usage: tests/slurm-plugin-smoke.sh
set -euo pipefail

repo_root=$(cd "$(dirname "$0")/.." && pwd)
plugin_dir="$repo_root/plugins/slurm"
plugin_so="$plugin_dir/spank_vmocs.so"

fail() { echo "FAIL: $*" >&2; exit 1; }
pass() { echo "ok: $*"; }

echo "== build =="
make -C "$plugin_dir" clean >/dev/null
# Treat warnings as errors so a header/API drift fails the build here.
make -C "$plugin_dir" CFLAGS="-std=gnu11 -O2 -Wall -Werror -fstack-protector-strong -fPIC"
[ -f "$plugin_so" ] || fail "plugin was not built at $plugin_so"
pass "compiled with -Werror"

echo "== exported symbols =="
defined=$(nm -D --defined-only "$plugin_so")
for sym in plugin_name plugin_type plugin_version \
           slurm_spank_init slurm_spank_init_post_opt \
           slurm_spank_task_init slurm_spank_exit; do
    echo "$defined" | grep -qw "$sym" || fail "missing exported symbol: $sym"
done
pass "all SPANK hooks and plugin metadata exported"

echo "== required Slurm API =="
# The seamless-session path relies on spank_prepend_task_argv, which only
# exists in Slurm >= 23.11. Its presence guards that dependency.
nm -D --undefined-only "$plugin_so" | grep -qw spank_prepend_task_argv \
    || fail "plugin does not reference spank_prepend_task_argv (Slurm >= 23.11 API)"
pass "references spank_prepend_task_argv"

echo "== option registration and validation =="
workdir=$(mktemp -d)
trap 'rm -rf "$workdir"' EXIT
cat >"$workdir/plugstack.conf" <<EOF
optional $plugin_so
EOF
cat >"$workdir/slurm.conf" <<EOF
ClusterName=vmocs-ci
SlurmctldHost=localhost
PlugStackConfig=$workdir/plugstack.conf
NodeName=localhost CPUs=1 State=UNKNOWN
PartitionName=debug Nodes=ALL Default=YES State=UP
EOF
export SLURM_CONF="$workdir/slurm.conf"

# srun --help loads the plugin locally and lists the options it registered.
# If the compiled-in Slurm version were incompatible, the loader would reject
# the plugin and these options would be absent.
help_out=$(srun --help 2>&1 || true)
for opt in --vm-image --vm-save --vm-resume --vm-attach; do
    echo "$help_out" | grep -q -- "$opt" || fail "srun --help missing $opt"
done
pass "srun --help lists --vm-image, --vm-save, --vm-resume, --vm-attach"

sbatch --help 2>&1 | grep -q -- --vm-image \
    || fail "sbatch --help missing --vm-image"
pass "sbatch --help lists --vm-image"

# A bad --vm-attach value must be rejected by the plugin's option callback.
if attach_out=$(srun --vm-attach=bogus /bin/true 2>&1); then
    fail "srun accepted an invalid --vm-attach value"
fi
echo "$attach_out" | grep -q "must be 'auto' or 'none'" \
    || fail "expected --vm-attach validation error, got: $attach_out"
pass "srun rejects an invalid --vm-attach value"

echo "PASS: slurm plugin smoke test"
