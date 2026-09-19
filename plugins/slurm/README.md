# spank_vmocs — Slurm SPANK plugin for vmocs

Integrates [vmocs](../../README.md) into Slurm via the SPANK plugin interface.
When a job requests `--vm-image`, the plugin boots a VM from the specified
vmocs template using Slurm-allocated CPUs and memory, holds the job alive
for the VM's lifetime, and tears down cleanly on job exit.

## Requirements

- Slurm with SPANK support (version ≥ 23.11, for `spank_prepend_task_argv`)
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

Once installed, the options appear in `srun --help`:

```
$ srun --help
...
      --vm-image=TEMPLATE     Boot a VM with the specified vmocs template name
      --vm-save=PATH          Flatten VM disk into a new qcow2 image when the job ends
      --vm-attach=MODE        Guest attachment mode: auto (default) or none
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
# Open an interactive shell directly inside the guest
$ srun -n1 --pty --vm-image base-ubuntu bash -l

# Run a non-interactive command in the guest
$ srun -n1 --vm-image base-ubuntu hostname

# Save VM disk state after the job ends — all changes inside the VM are
# flattened into a new standalone qcow2 image
$ srun -n1 --vm-image base-ubuntu \
    --vm-save /shared/images/base-ubuntu-modified.qcow2 bash -l
Launching VM from template 'base-ubuntu' (2 cores, 1792 MB)...
VM ready  job_id=855  ssh -i /tmp/vmocs/855/id_ed25519 -p 60222 ubuntu@127.0.0.1

# Retain the manual VM-as-job workflow without opening SSH automatically
$ srun -n1 --vm-image base-ubuntu --vm-attach=none /bin/true

# Request specific resources — VM will reflect them
$ srun -n1 -c 4 --mem=8G --vm-image base-ubuntu bash -l
Launching VM from template 'base-ubuntu' (4 cores, 7680 MB)...
VM ready  ...
```

The seamless-session implementation currently requires one Slurm task. Direct
`sbatch` script wrapping also requires the script and its dependencies to be
visible at the same paths inside the guest; automatic script staging is planned.

## Behavior

The plugin uses `spank_prepend_task_argv()` in `slurm_spank_task_init` to wrap
the original command with `vmocs run`. The supervisor boots QEMU inside the
Slurm task cgroup, connects to the guest over SSH, forwards stdio and a PTY,
and returns the guest command's status to Slurm. `--vm-attach=none` selects the
older VM-as-job behavior explicitly.

For attached interactive sessions, a QMP `RESET` or SSH transport loss while
QEMU remains alive causes vmocs to wait for SSH and reconnect. A normal shell
exit shuts down the guest and ends the Slurm task.

On job cancellation, Slurm sends SIGTERM to the vmocs process. vmocs attempts
a graceful ACPI shutdown (up to two attempts, 10 s each), then issues a forced
QMP quit followed by SIGKILL if the guest does not respond.

## GPU Passthrough

The plugin automatically detects GPUs allocated by Slurm and passes them into
the VM via VFIO. No changes to job scripts are needed — `--gres=gpu:1` is
sufficient.

Two hardware configurations are supported:

| Configuration | How it works |
|---|---|
| **Bare VFIO** | Physical GPU PF bound to `vfio-pci` — exclusive, one job at a time |
| **GIM SR-IOV** | AMD GIM driver creates VFs and binds them to `vfio-pci` — shared, one VF per job |

In both cases Slurm allocates a `/dev/vfio/<N>` device file to the job. The
plugin reads which device the cgroup allows, resolves all BDFs in that IOMMU
group via sysfs, and appends `--pci <BDF>` for each to the `vmocs launch`
command.

### Node preparation (one-time, run as root)

**Step 1 — Bind GPUs to `vfio-pci`**

Use `driverctl` (RHEL/Fedora) for persistent binding across reboots:

```bash
dnf install -y driverctl

# Find GPU BDFs
lspci -d 1002: -D | grep -E "VGA|Processing|Display"

# Bind each function to vfio-pci (repeat for companion audio if present)
driverctl set-override 0000:03:00.0 vfio-pci
driverctl set-override 0000:03:00.1 vfio-pci   # HDMI audio, if present

# Verify
lspci -ks 0000:03:00.0 | grep "Kernel driver"
# → Kernel driver in use: vfio-pci
```

For GIM SR-IOV nodes the GIM driver handles this automatically — VFs are bound
to `vfio-pci` by the GIM driver at load time. No manual `driverctl` needed.

**Step 2 — Allow `kvm` group members to open VFIO devices**

Slurm's cgroup device whitelist enforces per-job isolation. The udev rule just
ensures users in the `kvm` group can open the device file (file permission check
happens before the cgroup check):

```bash
echo 'SUBSYSTEM=="vfio", KERNEL!="vfio", GROUP="kvm", MODE="0660"' \
    > /etc/udev/rules.d/99-vfio-kvm.rules

udevadm control --reload
udevadm trigger --subsystem-match=vfio

# Verify
ls -la /dev/vfio/
# → crw-rw----. 1 root kvm 235, 0 ... 61
```

**Step 3 — Add job users to the `kvm` group**

```bash
usermod -aG kvm <username>
# User must log out and back in (or start a new session) for this to take effect
```

**Step 4 — Generate `gres.conf`**

```bash
python3 plugins/slurm/gres-conf-gen.py
# → Name=gpu File=/dev/vfio/61,/dev/vfio/62,...

# Verify the BDF → group → device mapping:
python3 plugins/slurm/gres-conf-gen.py --check
```

Append the output line to `/etc/slurm/gres.conf`. Each node should have its own
`NodeName=<host>` prefixed entry if the cluster is heterogeneous.

**Step 5 — Update `slurm-nodes.conf`**

Change the node's gres entry from a typed GPU (`gres=gpu:gfx906:1`) to a plain
count (`gres=gpu:1`) so it matches the untyped gres.conf entry:

```
# Before
NodeName=mynode ... gres=gpu:gfx906:1

# After
NodeName=mynode ... gres=gpu:1
```

**Step 6 — Restart Slurm daemons**

```bash
systemctl restart slurmctld slurmd
scontrol update NodeName=<host> State=resume
```

### Template requirement

Templates used with GPU passthrough **must** set `pci-root-port: true`.
Without it QEMU crashes on AMD GPUs with an IRQ assertion failure
(`pci_irq_handler: 0 <= irq_num && irq_num < PCI_NUM_PINS`) because the
Q35 machine needs a dedicated PCIe root port per passthrough device to route
interrupts correctly.

```yaml
# /etc/vmocs/templates.yaml
base-ubuntu:
  machine-type: q35
  pci-root-port: true   # required for GPU passthrough
  ...
```

### Usage

```bash
# Request 1 GPU — plugin discovers the allocated /dev/vfio/<N> automatically
srun --gres=gpu:1 --vm-image base-ubuntu hostname

# Verify GPU is visible inside the VM (from another terminal while job runs)
ssh -i /tmp/vmocs/<jobid>/id_ed25519 -p <ssh_port> ubuntu@localhost lspci
# → Advanced Micro Devices, Inc. [AMD/ATI] ...
```

### Consumer GPU note (ACS / multi-function devices)

Consumer GPUs (e.g. RX 7900 XTX) expose two PCI functions on the same slot:
a display controller (`0x03xx`) and an HDMI/DP audio device (`0x04xx`).

- **ACS off (default):** both functions share one IOMMU group and one
  `/dev/vfio/<N>`. One `gres.conf` entry covers both — the plugin passes
  all BDFs in the group to `--pci`, so both appear in the VM.
- **ACS on:** each function gets its own IOMMU group. List only the GPU's
  `/dev/vfio/<N>` in `gres.conf` for compute workloads (audio is excluded).
  For Windows GUI VMs needing audio, add a separate `Type=audio` gres entry.

`gres-conf-gen.py` automatically identifies GPU-representing groups (those
containing a display-class device) and excludes audio-only groups.

## Hooks

| Hook | Context | Action |
|------|---------|--------|
| `slurm_spank_init` | all | Register vmocs options |
| `slurm_spank_init_post_opt` | allocator | Persist `VMOCS_TEMPLATE` into job env |
| `slurm_spank_task_init` | remote (job user) | Prepend `vmocs run ... --` to the task argv |
| `slurm_spank_exit` | remote | `vmocs stop <job_id>` (best-effort cleanup) |
