# vmocs: Lightweight VM Launcher for SLURM

**vmocs** — *Virtual Machine in Compute through SLURM*, pronounced **vimoksh** (ವಿಮೋಕ್ಷ, meaning "to set one free").

vmocs was started to allow SLURM users to change kernel space — giving HPC users full OS-level control over the compute environment, not just a container filesystem. Based on pcocc.

## Context

The goal is to create a new project (vmocs) following the Enroot/Pyxis model: a lightweight VM management tool + SLURM SPANK plugin. When a user passes `--vm-image <template>` to `srun`/`sbatch`, a single QEMU VM boots on the compute node, inherits the SLURM cgroup, and the user is dropped in via SSH. After the job ends, the VM is torn down.

This eliminates most of pcocc's complexity (etcd, multi-VM coordination, OVS, gRPC agent, containers) while reusing its proven QEMU launch machinery, VFIO passthrough, template system, and SPANK integration.

## Recommendation: Fork and extract from pcocc

Starting from scratch would mean rewriting ~2500 lines of battle-tested QEMU command line construction, CPU/NUMA binding, VFIO device management, and image handling. Instead, extract the reusable pieces (~40-50% of Hypervisor.py, ~100% of Templates.py, VMImage, QemuMonitor, VFIODev) and build the new scaffolding around them.

## Project Structure

```
vmocs/
├── plugins/slurm/
│   └── vm-setup.lua              # Simplified SPANK plugin
├── lib/vmocs/
│   ├── __init__.py
│   ├── error.py                  # vmocsError hierarchy
│   ├── config.py                 # Singleton: loads vmocs.yaml + templates.yaml
│   ├── templates.py              # Template loading with inheritance
│   ├── image.py                  # VMImage: qemu-img operations
│   ├── monitor.py                # QemuMonitor: QMP over Unix socket
│   ├── hypervisor.py             # QEMU cmdline building + fork/exec + lifecycle
│   ├── vfio.py                   # VFIODev + GPU passthrough (full & SR-IOV)
│   ├── slurm.py                  # SLURM env var reading (job_id, cores, memory)
│   ├── network.py                # macvtap/bridge/user-mode networking setup
│   └── cli.py                    # Click CLI
├── confs/
│   ├── vmocs.yaml               # Main config (GPU devices, paths, network)
│   └── templates.yaml            # VM template definitions
├── setup.py
└── tests/
```

## What to extract from pcocc (specific sources)

| vmocs module | pcocc source | Lines | What to extract | Changes needed |
|---|---|---|---|---|
| `monitor.py` | `Hypervisor.py` | 749-1086 | `QemuMonitor` class | Accept `socket_path` directly instead of `vm` object. Remove migration/checkpoint methods. |
| `hypervisor.py` | `Hypervisor.py` | 1328-1654, 1664-1674, 1698-1710, 1897-1948, 2363-2467 | QEMU cmdline building, fork/exec, CPU binding, mount points, block device helpers | Remove all etcd calls (`_set_vm_state`, image locking, heartbeat). Remove SPICE, Docker, agent serial ports. Add GPU and network args. |
| `templates.py` | `Templates.py` | 56-372 | `TemplateConfig`, `Template` classes with inheritance | Remove `resource-set`, image repo resolution. Add `gpu`, `gpu-count`, `ssh-user`, `ssh-timeout` fields. |
| `image.py` | `Image.py` | 117-330 | `VMImage` class (qemu-img wrappers) | Remove container/repo support. Add `create_snapshot()` convenience function. |
| `vfio.py` | `NetUtils.py` + `GenPCINetwork.py` | NetUtils 179-263, 973-987 | `VFIODev` class, `pci_enable_driver()` | Remove Tracker/TrackableObject base. Standalone bind/unbind. |
| `slurm.py` | `Batch.py` SlurmManager | 1570-1966 | Env var reading for cores, memory, job_id | Extract as plain functions. Drop etcd, rank maps, alloc/run/batch methods. Add GPU GRES index→PCI mapping, IB VF GRES reading. |
| `vm-setup.lua` | `plugins/slurm/vm-setup.lua` | 1-225 | SPANK lifecycle hooks | Change `--vm` to `--vm-image`, remove multi-step coordination, remove etcd cred propagation. |

## What to drop entirely

- **etcd** (EtcdManager, python-etcd) -- no multi-node coordination needed
- **Cluster.py, Tbon.py** -- no multi-VM orchestration
- **Agent.py, agent/ Go code, protobuf/gRPC** -- no guest agent RPC
- **Networks.py, EthNetwork.py, IBNetwork.py, BridgedNetwork.py** -- no OVS/virtual networking
- **Container.py, Oci.py, Docker.py** -- no container support
- **Run.py, ObjectStore.py** -- not needed for single-VM model

## Dependencies

**Keep**: PyYAML, click
**Drop**: python-etcd, psutil, jsonschema, urllib3, dnspython, ClusterShell, grpcio, pyOpenSSL, protobuf, six
**System tools**: qemu-system-x86_64, qemu-img, genisoimage, hwloc, taskset, ssh, iproute2 (for macvtap), VFIO kernel module

## End-to-end flow

```
srun --vm-image ubuntu-gpu ./my_script.sh
  │
  ▼
SPANK init_post_opt (allocator): set VMOCS_TEMPLATE=ubuntu-gpu in job env
  │
  ▼
SPANK init_post_opt (compute node): call "vmocs internal setup ubuntu-gpu"
  │
  ▼
vmocs internal setup:
  1. Load template "ubuntu-gpu" (inherits "base-ubuntu")
  2. Read SLURM allocation via slurm.py:
     - CPUs: coreset from cgroup cpuset
     - Memory: SLURM_MEM_PER_NODE or cgroup mem limit
     - GPUs: SLURM_STEP_GPUS → map indices to PCI addrs via config
     - NUMA: derive from allocated cores
  3. Create runtime dir: /var/run/vmocs/<job_id>/
  4. Create COW snapshot: qemu-img create -b <base_image> <snapshot>
  5. GPU: for each SLURM-assigned GPU index:
     - Look up PCI addr from gpu-devices config
     - pci_enable_driver(addr, 'vfio-pci') + VFIODev(addr).bind()
  6. Network: create macvtap on host NIC (or TAP on bridge, or SLIRP)
  7. RDMA (if enabled): find/bind IB VF via VFIO (from SLURM GRES or free VF scan)
  8. Generate cloud-init ISO (inject temp SSH keypair + network config)
  9. Build QEMU cmdline (machine, cpu, mem, NUMA, disk, gpu, net, rdma, mounts, qmp)
  10. Fork/exec QEMU → inherits SLURM cgroup automatically
  11. Connect QemuMonitor, bind vCPUs via taskset, cont()
  12. Poll SSH until ready (macvtap: VM IP on host network; SLIRP: localhost:PORT)
  13. Write vm.json metadata to runtime dir
  14. SSH into VM, exec user command, propagate exit code
  │
  ▼
SPANK exit: call "vmocs internal teardown"
  → kill QEMU, unbind VFIO, remove snapshot, clean runtime dir
```

## Resource inheritance from SLURM

The VM is a mirror of the SLURM job allocation. vmocs reads what SLURM assigned and passes it through to QEMU — no device scanning, no "find first free", no allocation logic.

| Resource | SLURM source | How vmocs reads it | What QEMU gets |
|---|---|---|---|
| **CPUs** | `SLURM_JOB_CPUS`, coreset from cgroup | `slurm.py` reads env + cgroup cpuset | `-smp N` + `taskset` to pin vCPUs |
| **Memory** | `SLURM_MEM_PER_NODE` or cgroup mem limit | `slurm.py` reads env or cgroup | `-m <MB>` |
| **GPUs** | GRES: `SLURM_JOB_GPUS` / `SLURM_STEP_GPUS` | `slurm.py` maps GPU indices → PCI addrs via config | `-device vfio-pci,host=ADDR` per GPU |
| **IB/RoCE NICs** | GRES (if configured) or config pool | `slurm.py` reads GRES or `vfio.py` finds VFs | `-device vfio-pci,host=VF_ADDR` |
| **NUMA topology** | Inherited via cgroup cpuset | `slurm.py` reads NUMA node from allocated cores | QEMU NUMA config matches host topology |

What comes from the **user template** instead (not SLURM):
- VM image (OS), machine type, disk model, custom QEMU args
- SSH user, cloud-init user-data
- Mount points (9p/virtiofs shares)
- Whether to do GPU passthrough at all (`gpu: full` / `sriov` / `null`)

### GPU index → PCI address mapping

SLURM assigns GPUs by index (e.g., `SLURM_JOB_GPUS=0,2`). vmocs needs a mapping table in `vmocs.yaml` to convert indices to PCI addresses:

```yaml
gpu-devices:
  # Ordered list — index 0 = first entry, index 1 = second, etc.
  # Must match the order SLURM sees (same as nvidia-smi or rocm-smi order)
  - addr: "0000:41:00.0"
    type: full            # full PF passthrough
    host-driver: amdgpu
  - addr: "0000:61:00.0"
    type: full
    host-driver: amdgpu
  # SR-IOV VFs listed separately since SLURM may manage them as separate GRES
  sriov-vfs:
    - addr: "0000:09:00.1"
      host-driver: amdgpu
    - addr: "0000:09:00.2"
      host-driver: amdgpu
```

Setup: read SLURM GPU indices → look up PCI addrs → `pci_enable_driver(addr, 'vfio-pci')` → `VFIODev(addr).bind()` → QEMU `-device vfio-pci,host=ADDR`
Teardown: `VFIODev(addr).unbind()` → restores `host-driver`

### Multiple VMs on the same node

Because vmocs trusts SLURM's allocation, multi-VM-per-node works automatically:
- Job A gets GPUs 0,1 → vmocs binds PCI addrs for index 0,1
- Job B gets GPU 2 → vmocs binds PCI addr for index 2
- No conflict, no scanning, no locking — SLURM already guaranteed non-overlapping assignments
- Same principle for CPUs, memory, IB VFs

## Networking

### Design goals

1. VMs on different nodes must communicate (MPI over TCP, SSH between VMs)
2. VMs should be reachable from the host network (users SSH in, access services)
3. No OVS dependency — SLURM is one-VM-per-job, no need for per-cluster overlay networks
4. Optional: native RDMA for HPC workloads via IB/RoCE VF passthrough

### Network modes

#### macvtap (default, recommended)

Each VM gets a macvtap device attached to the host's physical NIC. The VM appears as a separate host on the same L2 network — gets its own IP via DHCP or cloud-init static config.

```
Host NIC (eth0) ← macvtap0 → VM-A (job 1)
                ← macvtap1 → VM-B (job 2)
```

- **Cross-node**: VMs on different nodes can communicate directly (same subnet)
- **Multi-VM per node**: Each macvtap is independent, no conflict
- **No infrastructure**: No bridges to pre-configure, no OVS
- **Caveat**: VM cannot talk to its own host via macvtap (kernel limitation). Not a problem — SLURM handles host interaction, and 9p/virtiofs mounts provide filesystem access.
- **Setup**: `ip link add link eth0 name macvtap0 type macvtap mode bridge` → pass fd to QEMU

#### TAP + Linux bridge (alternative)

For environments where macvtap doesn't fit (e.g., need VM↔host communication):

```
Host NIC (eth0) → br0 ← tap0 → VM-A
                      ← tap1 → VM-B
```

- Requires one-time bridge setup on nodes (admin config, not vmocs's job)
- VM gets IP on host network
- VM can talk to its own host

#### user-mode / SLIRP (standalone only)

For `vmocs launch` without SLURM (development, testing). NAT'd behind host, no VM-to-VM connectivity. Uses host port forwarding for SSH:

```
QEMU SLIRP → hostfwd=tcp::PORT-:22
```

Port allocated from `ssh-port-range` using job-id-based offset or flock-based allocation.

#### VFIO NIC passthrough (HPC RDMA)

For MPI workloads needing native InfiniBand or RoCE performance, pass through an SR-IOV VF of the network HCA — same VFIO mechanism as GPUs:

```
Host IB HCA (mlx5_0) → SR-IOV VF → VFIO → VM gets native RDMA
```

- If SLURM manages IB VFs as GRES, vmocs reads the allocation like GPUs
- If not, vmocs scans for free VFs from configured HCA (similar to pcocc's `HostIBNetwork`)
- No PKey management, no OpenSM integration (that's pcocc complexity we drop)

### Network config in vmocs.yaml

```yaml
network:
  mode: macvtap              # "macvtap", "bridge", "user", or "none"
  host-interface: eth0        # Physical NIC for macvtap mode
  bridge: br0                 # Pre-existing bridge for bridge mode
  ssh-port-range: [60222, 60322]  # Only for user mode

  # Optional: IB/RoCE VF passthrough for RDMA
  rdma:
    enabled: false
    device: mlx5_0            # Host IB/RoCE device
    # If SLURM manages as GRES, vmocs reads allocation automatically
    # If not, vmocs finds free VFs from this device
```

## Key design decisions

1. **Trust SLURM, don't reinvent it**: SLURM already allocates CPUs, memory, GPUs (GRES), and network devices. The VM inherits everything SLURM gives the job — vmocs reads the allocation, it never computes its own. What SLURM doesn't control comes from the user via templates (which OS image, VM-specific config like disk model or custom QEMU args). This means multiple VMs on the same node "just work" — each job's VM gets exactly the resources SLURM assigned to that job.
2. **QEMU inherits SLURM cgroup**: Fork/exec from SLURM task process → automatic inheritance. No cgroup management code needed.
3. **No etcd**: State is a JSON file in `/var/run/vmocs/<job_id>/`. No coordination needed — SLURM is the coordinator.
4. **SSH access via temp keypair**: Generate ED25519 keypair per job, inject via cloud-init, destroy on teardown. No user SSH config needed.
5. **Process model**: Unlike pcocc (long-lived process with console proxy), vmocs's `setup` starts QEMU as an orphaned process in the cgroup, SSHes the user command, then exits. `teardown` kills QEMU. Much simpler.

## SPANK plugin (simplified vm-setup.lua)

The pcocc SPANK plugin has complex multi-step coordination (stepid tracking, etcd credential propagation, two-phase init+create). vmocs simplifies this to:

```lua
function slurm_spank_init(spank)
    -- Register --vm-image option with SLURM
    spank:register_option({
        name = "vm-image",
        usage = "Boot a VM with the specified template",
        cb = "option_handler",
        has_arg = 1,
        arginfo = "template_name"
    })
end

function slurm_spank_init_post_opt(spank)
    if not vm_enabled then return SPANK.SUCCESS end
    if spank.context ~= "remote" then
        spank:job_control_setenv("VMOCS_TEMPLATE", vm_option, 1)
    else
        replicate_slurm_vars(spank)
        do_and_log_output("vmocs internal setup " .. vm_option)
    end
end

function slurm_spank_exit(spank)
    if not vm_enabled then return SPANK.SUCCESS end
    if spank.context == "remote" then
        do_and_log_output("vmocs internal teardown")
    end
end
```

## Template system

Reuse pcocc's YAML template inheritance. Example:

```yaml
# confs/templates.yaml
base-ubuntu:
  image: /shared/images/ubuntu-22.04.qcow2
  machine-type: q35
  disk-model: virtio
  mount-points:
    home:
      path: /home
  user-data:
    ssh_authorized_keys:
      - "%{env:VMOCS_SSH_PUBKEY}"

ubuntu-gpu:
  inherits: base-ubuntu
  gpu: full                # enable GPU passthrough; SLURM decides how many
  custom-args:
    - '-cpu'
    - 'host,host-phys-bits=on'

ubuntu-mxgpu:
  inherits: base-ubuntu
  gpu: sriov               # SR-IOV VF passthrough; SLURM decides how many
```

New template fields beyond pcocc's:
- `gpu`: `full`, `sriov`, or `null` — tells vmocs whether to do passthrough (which GPUs and how many is decided by SLURM's GRES allocation, not the template)
- `ssh-user`: username for SSH into VM (default: `root`)
- `ssh-timeout`: seconds to wait for SSH readiness (default: 120)

**Principle**: The template describes *what kind of VM* (OS, machine config, whether GPU passthrough is wanted). SLURM decides *how much* (which CPUs, how much memory, which/how many GPUs). If the template says `gpu: full` but SLURM allocated 0 GPUs, vmocs skips passthrough. If SLURM allocated 4 GPUs, all 4 are passed through.

## Main config (confs/vmocs.yaml)

```yaml
qemu-bin: /usr/bin/qemu-system-x86_64
runtime-dir: /var/run/vmocs

# GPU list — order must match SLURM's GRES index order
# (same order as rocm-smi / nvidia-smi device enumeration)
gpu-devices:
  - addr: "0000:41:00.0"
    type: full
    host-driver: amdgpu
  - addr: "0000:61:00.0"
    type: full
    host-driver: amdgpu
  # SR-IOV VFs (if managed as separate GRES by SLURM)
  sriov-vfs:
    - addr: "0000:09:00.1"
      host-driver: amdgpu
    - addr: "0000:09:00.2"
      host-driver: amdgpu

network:
  mode: macvtap                  # "macvtap", "bridge", "user"
  host-interface: eth0           # for macvtap
  bridge: br0                    # for bridge mode (pre-existing)
  ssh-port-range: [60222, 60322] # for user mode only

  rdma:
    enabled: false
    device: mlx5_0               # host IB/RoCE device for VF passthrough
```

## CLI commands

```
vmocs launch <template>           # Launch VM standalone (no SLURM)
vmocs ssh [--job-id ID]           # SSH into running VM
vmocs list                        # List running VMs
vmocs stop [--job-id ID]          # Stop a VM
vmocs template list               # List available templates
vmocs template show <name>        # Show template details
vmocs internal setup <template>   # Called by SPANK plugin
vmocs internal teardown           # Called by SPANK plugin
```

## Implementation phases

### Phase 1: Core VM launch (no SLURM, no GPU)
Files: `error.py`, `image.py`, `templates.py`, `config.py`, `monitor.py`, `hypervisor.py`, `network.py`, `cli.py`
Network: user-mode (SLIRP) for standalone launch
Milestone: `vmocs launch base-ubuntu` boots a VM and drops into SSH

### Phase 2: SLURM integration + macvtap networking
Files: `slurm.py`, `network.py` (macvtap/bridge), `vm-setup.lua`, CLI `internal` commands
Key: `slurm.py` reads full SLURM allocation (CPUs, memory, NUMA) and passes to hypervisor
Network: macvtap on host NIC — VM gets real IP, cross-node communication works
Milestone: `srun --vm-image base-ubuntu hostname` prints VM hostname; two jobs on same node each get their own VM

### Phase 3: GPU passthrough (SLURM GRES-driven)
Files: `vfio.py`, GPU integration in `hypervisor.py`, GRES→PCI mapping in `slurm.py`
Key: SLURM GRES indices mapped to PCI addresses via config — no device scanning
Milestone: `srun --gres=gpu:2 --vm-image ubuntu-gpu rocm-smi` shows exactly 2 GPUs inside VM

### Phase 4: RDMA + Polish
Files: `vfio.py` (IB VF support), `network.py` (RDMA passthrough)
Plus: list/stop commands, error handling, documentation, packaging
Milestone: MPI job across two nodes, each running a VM with IB VF passthrough

## Testing

- **Unit**: Template parsing/inheritance, QEMU cmdline construction (mock inputs → verify args), config loading, SLURM env parsing
- **Integration**: Boot minimal VM (alpine/cirros) with user-mode networking, verify SSH
- **Hardware**: VFIO bind/unbind + GPU passthrough on real hardware
- **SLURM**: End-to-end test in real SLURM cluster
