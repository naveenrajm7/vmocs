#!/usr/bin/env bash
# Install spank_vmocs.so to PLUGIN_INSTALL_DIR and generate vfio-gpu.map on NODELIST.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.sh
source "$SCRIPT_DIR/lib/common.sh"
load_config

SO="$REPO_ROOT/plugins/slurm/spank_vmocs.so"
[[ -f "$SO" ]] || die "Run build-plugin.sh first"

DEST="$PLUGIN_INSTALL_DIR/spank_vmocs.so"
echo "==> Installing plugin to $DEST"
sudo install -m755 "$SO" "$DEST"
ls -la "$DEST"
md5sum "$SO" "$DEST"

echo "==> Generating $VFIO_MAP on $NODELIST"
node_exec "
set -e
python3 '$REPO_PATH/plugins/slurm/gres-conf-gen.py' --check
sudo mkdir -p \$(dirname '$VFIO_MAP')
sudo python3 '$REPO_PATH/plugins/slurm/gres-conf-gen.py' --map '$VFIO_MAP'
echo '--- vfio map ---'
cat '$VFIO_MAP'
"

echo "==> Plugstack entry (verify on cluster):"
echo "optional  $DEST vmocs_conf=$VMOCS_CONF vfio_map=$VFIO_MAP"

echo ""
echo "NOTE: slurmd must reload the plugin after install."
echo "      On compute nodes: sudo systemctl restart slurmd"
echo "      (Skipped here — run manually if jobs still use old plugin behaviour.)"
