# vmocs: SLURM Integration

## Overview

vmocs integrates with SLURM via the SPANK plugin API — the same mechanism Pyxis uses for containers. The SPANK plugin intercepts the job lifecycle to start a VM before the user's command runs and tear it down on exit. SLURM handles all resource allocation; vmocs only reads what SLURM already decided.

---

## SPANK Hook Execution Order

Full execution tree showing where vmocs hooks fire:

```
slurmd
  +-> init()
  +-> job_prolog()
  |   +-> slurmstepd
  |        +-> init()
  |         -> process spank options
  |         -> init_post_opt()                ← vmocs: validate --vm-image, resolve template
  |        +-> drop privileges
  |        +-> user_init()
  |        +-> for each task
  |        |       +-> fork()
  |        |       +-> reclaim privileges
  |        |       +-> task_init_privileged() ← vmocs: VFIO bind (needs root)
  |        |       +-> drop privileges
  |        |       +-> task_init()            ← vmocs: create + start + wait + SSH
  |        |       +-> task_post_fork()
  |        +-> for each task
  |        |       +-> wait()
  |        |       +-> task_exit()
  |        +-> exit()                         ← vmocs: stop + destroy + VFIO unbind
  +-> job_epilog()
  +-> slurmd_exit()
```

### Hook responsibilities

| Hook | Context | vmocs action |
|---|---|---|
| `init` | local + remote | Register `--vm-image` option |
| `init_post_opt` | local | Validate template name, check it exists on shared FS |
| `init_post_opt` | allocator | Set `VMOCS_TEMPLATE` in job env |
| `init_post_opt` | remote | Replicate SLURM vars into env |
| `task_init_privileged` | remote (root) | VFIO bind GPU/IB devices; create runtime dir |
| `task_init` | remote (job user) | `vmocs internal setup` — COW, QEMU, wait, SSH |
| `exit` | remote | `vmocs internal teardown` — kill QEMU, VFIO unbind, cleanup |

### Plugin contexts

| Context | Loaded by | Purpose |
|---|---|---|
| `local` | `srun` | Client-side option parsing |
| `remote` | `slurmstepd` | Compute node, within job step — where VMs actually run |
| `allocator` | `sbatch` / `salloc` | Allocation-time env var propagation |
| `slurmd` | `slurmd` daemon | Daemon-level initialization |
| `job_script` | job prolog/epilog | Separate address space |

---

## vmocs SPANK Plugin (Lua)

Using Lua via `slurm-spank-lua` for prototyping; C for production.

```lua
local vm_enabled = false
local vm_option  = nil

function slurm_spank_init(spank)
    spank:register_option({
        name    = "vm-image",
        usage   = "Boot a VM with the specified template name",
        cb      = "option_handler",
        has_arg = 1,
        arginfo = "template_name"
    })
end

function option_handler(val, optarg, is_remote)
    vm_enabled = true
    vm_option  = optarg
    return SPANK.SUCCESS
end

function slurm_spank_init_post_opt(spank)
    if not vm_enabled then return SPANK.SUCCESS end

    if spank.context == "allocator" then
        spank:job_control_setenv("VMOCS_TEMPLATE", vm_option, 1)
    elseif spank.context == "remote" then
        replicate_slurm_vars(spank)
    end

    return SPANK.SUCCESS
end

function slurm_spank_task_init_privileged(spank)
    if not vm_enabled then return SPANK.SUCCESS end
    do_and_log_output("vmocs internal setup-vfio " .. vm_option)
    return SPANK.SUCCESS
end

function slurm_spank_task_init(spank)
    if not vm_enabled then return SPANK.SUCCESS end
    do_and_log_output("vmocs internal setup " .. vm_option)
    return SPANK.SUCCESS
end

function slurm_spank_exit(spank)
    if not vm_enabled then return SPANK.SUCCESS end
    if spank.context == "remote" then
        do_and_log_output("vmocs internal teardown")
    end
    return SPANK.SUCCESS
end
```

---

## Comparison with Pyxis (Reference Model)

Pyxis (NVIDIA's SPANK plugin for enroot containers) is the primary design reference for vmocs.

### Hook mapping

| Hook | Pyxis (containers) | vmocs (VMs) |
|---|---|---|
| `init` / `init_post_opt` | Parse `--container-*` args | Parse `--vm-image`, register options |
| `task_init_privileged` | Namespace setup | VFIO bind GPU/IB devices |
| `task_init` | Import image, create container, enter namespaces | Create COW overlay, start QEMU, wait SSH |
| `task_post_fork` | Coordination from parent after fork | (unused) |
| `exit` | Export/save containers, `enroot remove -f` | Stop QEMU, VFIO unbind, rm overlay |

### Component parallel

| Pyxis/enroot | vmocs | Purpose |
|---|---|---|
| Pyxis SPANK plugin | vmocs SPANK plugin | Hooks into SLURM job lifecycle |
| enroot CLI | vmocs CLI | Image + lifecycle management |
| `--container-image` | `--vm-image` | Specify base image/template |
| `--container-save` | `--vm-save` | Export state after job |
| squashfs images | qcow2 image directories | Image format |
| `enroot import` | `vmocs image import` | Pull/download images |
| `enroot create` | COW overlay creation | Create instance from image |
| `enroot start` | `vmocs internal setup` | Launch the runtime |
| `enroot remove` | `vmocs internal teardown` | Clean up |

### Key insight from Pyxis

Pyxis does **not** manage cgroups itself. The `task_init` callback runs within the SLURM-managed cgroup, so resources are automatically accounted to the job. vmocs uses the same principle — QEMU is forked from within `task_init`, inheriting the job's cgroup automatically. No cgroup management code needed.

### Multi-task coordination

Pyxis uses shared memory with atomic counters and pthread mutex to coordinate multiple tasks sharing the same container. vmocs does not need this — each SLURM task gets its own VM, and SLURM's allocation already guarantees non-overlapping GPU/CPU/memory assignments across jobs.

---

## Cgroup Integration

### Why cgroup correctness matters

SLURM tracks resource usage (CPU time, memory high-water, GPU utilization) via the cgroup hierarchy. If QEMU escapes to a different cgroup (e.g., libvirt's `machine.slice`), SLURM's accounting is wrong and resource limits are not enforced on the VM.

### The cgroup v2 hierarchy

```
/sys/fs/cgroup/system.slice/slurmstepd.scope/
  job_<JOBID>/
    step_<STEPID>/
      task_<TASKID>/     ← leaf node — QEMU process lives here
```

Because vmocs forks QEMU from within `task_init` (which runs inside this leaf cgroup), the QEMU process automatically inherits the job's cgroup. No hooks, no process migration, no coordination needed.

### cgroup v2 constraints

| Rule | Implication for vmocs |
|---|---|
| **No internal process rule** | All processes must live on leaf cgroups. QEMU must sit at `task_<TASKID>/` or a child leaf — never at an intermediate node. |
| **Single-writer rule** | systemd expects to be the sole cgroup writer; SLURM gets delegated control via `Delegate=yes`. vmocs must never write to cgroups itself. |
| **BPF device control** | GPU device access under cgroup v2 requires BPF programs, not device files. SLURM configures this via `cgroup_plugin=autodetect`. |

### The libvirt cgroup conflict (why vmocs avoids libvirt)

If libvirt were used, it would move the QEMU process into `machine.slice/machine-<name>.scope`, removing it from SLURM's cgroup hierarchy. The VM would escape SLURM's resource accounting and limits would not be enforced.

| Option | Mechanism | Trade-off |
|---|---|---|
| **Direct QEMU (vmocs approach)** | Process inherits parent cgroup automatically | Must manage VM lifecycle ourselves |
| Custom libvirt partition | `<resource><partition>/slurm/...</partition></resource>` in domain XML | Fragile, requires libvirt+SLURM coordination |
| Disable libvirt cgroup mgmt | Set controllers to `[]` in `/etc/libvirt/qemu.conf` | Lose libvirt resource management entirely |
| KubeVirt approach | Hook-based process migration after libvirt starts QEMU | Proven at scale but complex |

**vmocs uses direct QEMU — cgroup correctness is automatic.**

---

## Resource Inheritance from SLURM

The VM mirrors the SLURM job allocation. vmocs reads what SLURM assigned and passes it through to QEMU — no device scanning, no "find first free", no allocation logic of its own.

| Resource | SLURM source | How vmocs reads it | What QEMU gets |
|---|---|---|---|
| **CPUs** | `SLURM_JOB_CPUS`, coreset from cgroup | `slurm.py` reads env + cgroup cpuset | `-smp N` + `taskset` to pin vCPUs |
| **Memory** | `SLURM_MEM_PER_NODE` or cgroup mem limit | `slurm.py` reads env or cgroup | `-m <MB>` |
| **GPUs** | `SLURM_JOB_GPUS` / `SLURM_STEP_GPUS` (GRES indices) | `slurm.py` maps indices → PCI addrs via config | `-device vfio-pci,host=ADDR` per GPU |
| **IB/RoCE NICs** | GRES (if configured) or config pool | `slurm.py` reads GRES or `vfio.py` finds VFs | `-device vfio-pci,host=VF_ADDR` |
| **NUMA topology** | Inherited via cgroup cpuset | `slurm.py` reads NUMA node from allocated cores | QEMU NUMA config matches host topology |

What comes from the **template** instead (not SLURM):
- VM image (OS), machine type, disk model, custom QEMU args
- SSH user, cloud-init user-data
- Mount points (9p/virtiofs shares)
- Whether GPU passthrough is wanted at all (`gpu: full` / `sriov` / `null`) — SLURM decides *how many*

### GPU index → PCI address mapping

SLURM assigns GPUs by index (e.g., `SLURM_JOB_GPUS=0,2`). vmocs maps these to PCI addresses via `vmocs.yaml`:

```yaml
gpu-devices:
  # Ordered list — index 0 = first entry, must match SLURM's enumeration order
  - addr: "0000:41:00.0"
    type: full
    host-driver: amdgpu
  - addr: "0000:61:00.0"
    type: full
    host-driver: amdgpu
  sriov-vfs:
    - addr: "0000:09:00.1"
      host-driver: amdgpu
    - addr: "0000:09:00.2"
      host-driver: amdgpu
```

Setup: read SLURM GPU indices → look up PCI addrs → `pci_enable_driver(addr, 'vfio-pci')` → `VFIODev(addr).bind()` → QEMU `-device vfio-pci,host=ADDR`

Teardown: `VFIODev(addr).unbind()` → restore `host-driver`

### Multiple VMs on the same node

Because vmocs trusts SLURM's allocation, multiple VMs per node work automatically:
- Job A gets GPUs 0,1 → vmocs binds PCI addrs for indices 0,1
- Job B gets GPU 2 → vmocs binds PCI addr for index 2
- No conflict, no scanning, no locking — SLURM already guaranteed non-overlapping assignments

---

## Complete End-to-End Flow

```
srun --vm-image ubuntu-gpu ./my_script.sh
  │
  ▼
srun (local context)
  └─ init(): register --vm-image option
  └─ init_post_opt(): validate template name

slurmstepd (remote context, compute node)
  └─ init_post_opt():
      ├─ set VMOCS_TEMPLATE=ubuntu-gpu in job env  [allocator path]
      └─ replicate SLURM vars into env             [remote path]

  └─ task_init_privileged() [root]:
      ├─ For each SLURM-assigned GPU index:
      │   ├─ Look up PCI addr from gpu-devices config
      │   ├─ pci_enable_driver(addr, 'vfio-pci')
      │   └─ VFIODev(addr).bind()
      └─ mkdir /run/vmocs/<job_id>/

  └─ task_init() [job user, inside SLURM cgroup]:
      └─ vmocs internal setup ubuntu-gpu:
          1.  Load template "ubuntu-gpu" (inherits "base-ubuntu")
          2.  Read SLURM allocation: CPUs (coreset), memory, NUMA, GPU PCI addrs
          3.  Create COW snapshots for each disk in manifest
          4.  Generate cloud-init ISO (inject temp SSH keypair + network config)
          5.  Build QEMU cmdline (machine, cpu, mem, NUMA, disks, gpu, net, mounts, qmp)
          6.  Fork/exec QEMU → inherits SLURM cgroup automatically
          7.  Connect QemuMonitor, bind vCPUs via taskset, cont()
          8.  Poll SSH until ready
          9.  Write vm.json to /run/vmocs/<job_id>/
          10. SSH into VM, exec user command, propagate exit code
  │
  ▼
SPANK exit:
  └─ vmocs internal teardown:
      1. Kill QEMU (SIGTERM → wait 10s → SIGKILL)
      2. VFIODev(addr).unbind() for each GPU → restore host-driver
      3. Delete COW snapshots and cloud-init ISO
      4. Remove /run/vmocs/<job_id>/
```

---

## Windows VMs on SLURM

Windows VMs require node exclusivity — one user per node at a time. SLURM provides this natively.

### Dedicated partition

```
# slurm.conf
PartitionName=windows \
  Nodes=winnode[1-4] \
  OverSubscribe=NO \
  Default=NO \
  MaxTime=8:00:00 \
  State=UP
```

`OverSubscribe=NO` is the key — SLURM never assigns two jobs to the same node in this partition.

### GPU as the gating resource

Windows nodes have one GPU. GPU is an exclusive SLURM resource — once allocated, no other job can get it. Requiring GPU naturally enforces single-user access:

```bash
salloc --partition=windows --gres=gpu:1 --nodes=1
```

### Node tagging for discoverability

```
# slurm.conf
NodeName=winnode[1-4] \
  Gres=gpu:amd:1 \
  Feature=windows,gpu-passthrough \
  CPUs=32 RealMemory=512000 State=UNKNOWN
```

```bash
sinfo -p windows
sinfo --format="%N %f %G" -p windows
```

### SLURM vs vmocs responsibility split

| SLURM | vmocs SPANK |
|---|---|
| Allocates node exclusively to user | Starts virtiofsd as the job user |
| Enforces one user per node via OverSubscribe=NO | Creates per-job COW overlay from base.qcow2 |
| Tracks job lifetime | Starts and destroys the VM |
| Runs prolog/epilog | Resets overlay on job exit (next user gets clean base) |

---

## SPANK Plugin Language Options

| Language | Pros | Cons | Recommendation |
|---|---|---|---|
| **Lua** (via slurm-spank-lua) | Fast iteration, no recompile, full SPANK API | Runtime dependency | **Prototyping** |
| **C** (native `.so`) | Maximum performance, no runtime deps | Recompile per major SLURM release | **Production** |
| Python | Familiar | No official SPANK binding exists | Avoid |

---

## References

### Projects

- [NVIDIA/pyxis](https://github.com/NVIDIA/pyxis) — SPANK plugin for enroot containers (primary design reference)
- [cea-hpc/pcocc](https://github.com/cea-hpc/pcocc) — Private Cloud on Compute Cluster (QEMU machinery reference)
- [stanford-rc/slurm-spank-lua](https://github.com/stanford-rc/slurm-spank-lua) — Lua SPANK plugin support

### SLURM Documentation

- [SLURM SPANK API](https://slurm.schedmd.com/spank.html)
- [SLURM Cgroups](https://slurm.schedmd.com/cgroups.html)
- [SLURM cgroup v2](https://slurm.schedmd.com/cgroup_v2.html)
- [SLURM GRES (GPU scheduling)](https://slurm.schedmd.com/gres.html)

### Cgroup Documentation

- [Linux Kernel cgroup v2](https://docs.kernel.org/admin-guide/cgroup-v2.html)
- [Libvirt Cgroups](https://libvirt.org/cgroups.html)
- [KubeVirt Housekeeping Cgroup](https://github.com/kubevirt/kubevirt/pull/8233) — example of libvirt + external cgroup management at scale

### Papers

- [Slurm-V: Extending Slurm for Building Efficient HPC Cloud (Euro-Par 2016)](https://link.springer.com/chapter/10.1007/978-3-319-43659-3_26) — closest prior art; validates SPANK plugin approach, documents SR-IOV and IVShmem for MPI, achieves 2.64x faster boot via snapshots
- [CEA SLUG '19: VMs and Containers for a Slurm-based Development Cluster](https://slurm.schedmd.com/SLUG19/CEA.pdf) — pcocc in production at CEA EXA1 supercomputer
- [SLUG '15: SR-IOV/IVShmem in MVAPICH2 on Slurm](https://slurm.schedmd.com/SLUG15/mv2_virt_slug_luxi_osu.pdf) — IB VF passthrough reference
