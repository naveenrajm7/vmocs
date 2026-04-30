# spank_vmocs — Slurm SPANK plugin for vmocs

Integrates [vmocs](../../README.md) into Slurm via the SPANK plugin interface.
When a job requests `--vm-image`, the plugin boots a VM from the specified
vmocs template using Slurm-allocated CPUs and memory, holds the job alive
for the VM's lifetime, and tears down cleanly on job exit.

## Requirements

- Slurm with SPANK support (any version ≥ 20.11)
- `slurm-devel` headers matching the deployed Slurm version (build-time only)
- `vmocs` installed and reachable (via PATH or `vmocs_path=` plugin arg)
- `/etc/vmocs/vmocs.yaml` and `/etc/vmocs/templates.yaml` on compute nodes,
  or a config path supplied via the `vmocs_conf=` plugin arg

## Installation

```bash
# Build
make -C plugins/slurm

# Install the .so to the Slurm plugin directory
sudo make -C plugins/slurm install

# Register the plugin with Slurm — append to plugstack.conf or drop a file
# in plugstack.conf.d/
sudo tee /etc/slurm/plugstack.conf.d/vmocs.conf <<EOF
optional spank_vmocs.so vmocs_path=/usr/local vmocs_conf=/etc/vmocs/vmocs.yaml
EOF

sudo systemctl restart slurmd
```

> **Note:** `spank_vmocs.so` must be compiled against the `spank.h` from the
> exact Slurm release running on your cluster. Mismatched headers will cause
> Slurm to refuse loading the plugin.

## Plugin Arguments

These are set in `plugstack.conf` after the `.so` name, not on the `srun`
command line.

| Argument | Default | Description |
|----------|---------|-------------|
| `vmocs_path=PREFIX` | (use PATH) | Prefix containing `bin/vmocs`, e.g. `/usr/local` or `/path/to/.venv` |
| `vmocs_conf=PATH` | (vmocs default) | Path to `vmocs.yaml` passed as `--config` to every vmocs invocation |

Example entry in `plugstack.conf`:
```
optional spank_vmocs.so vmocs_path=/opt/vmocs vmocs_conf=/etc/vmocs/vmocs.yaml
```

## Usage

Once installed, both options appear in `srun --help`:

```
$ srun --help
...
      --vm-image=TEMPLATE     Boot a VM with the specified vmocs template name
      --vm-save=PATH          Flatten VM disk into a new qcow2 image when the job ends
```

### Resource mapping

The plugin reads Slurm environment variables and passes them to vmocs:

| Slurm flag | env var | VM argument |
|------------|---------|-------------|
| `-c N` / `--cpus-per-task=N` | `SLURM_CPUS_PER_TASK` | `--cores N` |
| `--mem=XG` | `SLURM_MEM_PER_NODE` | `--memory M` (minus headroom) |
| `--mem-per-cpu=X` | `SLURM_MEM_PER_CPU` | `--memory M×cores` (minus headroom) |

**Memory headroom:** the plugin subtracts `max(5%, 256 MB)` from the Slurm
allocation before passing it to QEMU, reserving space for QEMU's own overhead.
A `--mem=4G` allocation results in `--memory 3840` passed to vmocs, and the
guest OS will see ~3.6 GB after kernel/firmware consumption.

## Examples

### srun

```bash
# Boot a VM and hold the allocation — VM stays up until you cancel the job
$ srun --vm-image base-ubuntu sleep infinity

# Save VM disk state after the job ends — all changes inside the VM are
# flattened into a new standalone qcow2 image
$ srun --vm-image base-ubuntu --vm-save /shared/images/base-ubuntu-modified.qcow2 sleep infinity
Launching VM from template 'base-ubuntu' (2 cores, 1792 MB)...
VM ready  job_id=855  ssh -i /tmp/vmocs/855/id_ed25519 -p 60222 ubuntu@127.0.0.1

# From another terminal, SSH into the running VM
$ ssh -i /tmp/vmocs/855/id_ed25519 -o StrictHostKeyChecking=no \
      -p 60222 ubuntu@127.0.0.1

# Request specific resources — VM will reflect them
$ srun -c 4 --mem=8G --vm-image base-ubuntu sleep infinity
Launching VM from template 'base-ubuntu' (4 cores, 7680 MB)...
VM ready  ...
```

### sbatch

```bash
$ sbatch <<'EOF'
#!/bin/bash
#SBATCH --job-name=vmocs-job
#SBATCH --output=/tmp/vmocs-%j.out
#SBATCH --cpus-per-task=2
#SBATCH --mem=2G
#SBATCH --vm-image=base-ubuntu

# These lines run on the host after the VM exits (Option A behavior).
# To run work inside the VM, SSH into it from here using the key and
# port printed in the output above.
echo "VM exited for job $SLURM_JOB_ID"
EOF
```

## Behavior

The plugin uses SPANK's `slurm_spank_task_init` hook, which runs **before**
the user's command. `vmocs launch` blocks until QEMU exits, so the Slurm job
stays alive for exactly the VM's lifetime. The user command in the job script
runs on the **host** after the VM exits.

This means `--vm-image` implements a **VM-as-job** model: the VM is the job
boundary. Running commands *inside* the VM requires SSH-ing into it from
within the job script or from another terminal while the job is running.

On job cancellation, Slurm sends SIGTERM to the vmocs process. vmocs attempts
a graceful ACPI shutdown (up to two attempts, 10 s each), then issues a forced
QMP quit followed by SIGKILL if the guest does not respond.

## Hooks

| Hook | Context | Action |
|------|---------|--------|
| `slurm_spank_init` | all | Register `--vm-image` option |
| `slurm_spank_init_post_opt` | allocator | Persist `VMOCS_TEMPLATE` into job env |
| `slurm_spank_task_init` | remote (job user) | `vmocs launch <template> --cores N --memory M --job-id J` (blocking) |
| `slurm_spank_exit` | remote | `vmocs stop <job_id>` (best-effort cleanup) |
