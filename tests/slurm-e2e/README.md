# Slurm end-to-end test harness

Environment-agnostic scripts for validating the vmocs SPANK plugin on a live
Slurm cluster. Site-specific values (node names, accounts, GRES types) live in
`local.conf`, which is **not committed**.

## Quick start

```bash
cd tests/slurm-e2e
cp local.conf.example local.conf
# Edit local.conf with your partition, nodelist, account, GRES type

chmod +x build-plugin.sh deploy-plugin.sh verify-gpu.sh run-gpu-e2e.sh

./run-gpu-e2e.sh single    # one GPU job
./run-gpu-e2e.sh both      # two concurrent jobs (isolation test)
```

## What it does

1. **build-plugin.sh** — compile `spank_vmocs.so` with `slurm-devel` headers
2. **deploy-plugin.sh** — install `.so` to `PLUGIN_INSTALL_DIR` (default
   `/etc/slurm/`), generate `/etc/vmocs/vfio-gpu.map` on the target node via
   `gres-conf-gen.py --map`
3. **run-gpu-e2e.sh** — submit sbatch job(s), verify GPU in guest, cancel

## GPU passthrough validation

The harness checks that the **SLURM_STEP_GPUS ordinal → vfio-gpu.map** path works:

- Job must reach `RUNNING` with `--gres` and `--vm-image`
- `srun --jobid=J --overlap` runs checks on the allocated node
- SSH into the guest (127.0.0.1 on compute node) and run `lspci` for AMD/NVIDIA GPU

This validates the fix that replaced greedy `/dev/vfio/*` scanning with
Slurm-allocated ordinals.

## Prerequisites

On the target compute node:

- KVM, vmocs, and `gpu-ubuntu` (or configured) template with `pci-root-port: true`
- GPUs bound to `vfio-pci`; gres.conf `File=/dev/vfio/[...]` matches map lines
- Job user in `kvm` group
- `vfio-gpu.map` generated (deploy step does this)

After plugin install, **restart slurmd** on compute nodes so the new `.so` loads:

```bash
sudo systemctl restart slurmd
```

## Job templates

| Template | Purpose |
|----------|---------|
| `jobs/gpu-single.sbatch.in` | Single-GPU sanity |
| `jobs/gpu-concurrent.sbatch.in` | Second concurrent job for isolation |

Variables (`@NODELIST@`, `@GRES@`, etc.) are substituted from `local.conf`.

`both` mode staggers the second submission until the first VM is ready, avoiding
SSH port collisions on the default `60222` bind.

## Files not committed

- `local.conf` — site hostnames, accounts, GRES types
