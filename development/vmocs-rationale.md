# vmocs: Project Rationale

**vmocs** (ವಿಮೋಕ್ಷ) exists because no existing tool lets a SLURM user request a VM the same way they request a container — with `srun --vm-image` — while keeping the VM fully inside SLURM's cgroup, GPU allocation, and accounting.

---

## What Was Evaluated

### pcocc (CEA) — closest existing tool
Production-proven at CEA's EXA1 supercomputer (672 Grace Hopper nodes). Uses QEMU/KVM and has a Lua SPANK plugin.

**Why it doesn't fit:**
- Not SLURM-native — users run `pcocc alloc` / `pcocc run`, not `srun`/`sbatch`. It wraps SLURM rather than integrating into it.
- Heavy infrastructure: requires etcd, Open vSwitch, and OpenSM on every node.
- Multi-VM orchestration (Cluster.py, Tbon.py, gRPC agent) adds complexity we don't need for the single-VM-per-job model.
- Tiny community (52 stars), sparse documentation, tightly coupled to CEA's patterns.

**What we take from it:** The QEMU command-line construction, VFIO passthrough, template inheritance system, and QMP monitor code are battle-tested and worth reusing (~40-50% of `Hypervisor.py`, `Templates.py`, `Image.py`).

---

### Libvirt + SPANK Plugin
Libvirt is the industry-standard VM management layer with rich Python bindings.

**Why it doesn't fit:**
- **Critical cgroup conflict:** Libvirt moves QEMU into `machine.slice`, removing it from SLURM's cgroup hierarchy. The VM escapes SLURM's resource accounting and limits.
- Workarounds (custom domain XML partition, disabling libvirt cgroup management) are fragile and require coordination between two independent daemons.
- Requires `libvirtd` running on every compute node as a privileged service.

---

### Vagrant + SPANK Plugin
Vagrant with the vagrant-libvirt provider.

**Why it doesn't fit:**
- Designed for developer workstations, not HPC — no concept of job boundaries or SLURM allocations.
- Same libvirt cgroup escape problem, plus Vagrant adds another indirection layer.
- Ruby dependency on every compute node.
- Would require so much wrapping that Vagrant's value is entirely bypassed.

---

### Firecracker / Cloud-Hypervisor MicroVMs
Extremely fast boot (<125ms), process-based (inherits parent cgroup naturally), purpose-built for multi-tenant isolation.

**Why it doesn't fit:**
- No GPU passthrough support — Firecracker strips all PCI passthrough by design.
- Designed for serverless/container-like workloads, not HPC.

---

### Slurm-V (Ohio State, Euro-Par 2016) — academic prior art
The closest design to vmocs. Extended SLURM with a SPANK plugin-based VM lifecycle: VM Configuration Reader, VM Launcher, VM Reclaimer.

**What it validates:**
- SPANK plugin approach is viable for VM lifecycle management.
- 2.64x faster boot via memory snapshot scheme (which vmocs adopts).
- SR-IOV and IVShmem for MPI across VMs confirmed working.

**Why not adopt it directly:** Academic prototype, no maintained codebase, no GPU passthrough design.

---

## The Gap

| Capability | pcocc | libvirt+SPANK | Vagrant | Firecracker | **vmocs** |
|---|---|---|---|---|---|
| `srun --vm-image` UX | No | Yes | Yes | Yes | **Yes** |
| Stays in SLURM cgroup | Yes | No | No | Yes | **Yes** |
| GPU passthrough | Unclear | Fragile | Fragile | **No** | **Yes** |
| No extra infrastructure | No (etcd/OVS) | No (libvirtd) | No (libvirtd) | Yes | **Yes** |
| Single-VM-per-job model | No (multi-VM) | Yes | Yes | Yes | **Yes** |

---

## The Approach

**Direct QEMU + SPANK Plugin**, extracting the proven machinery from pcocc and dropping everything else.

- QEMU is forked from within SLURM's `task_init` hook → inherits the job's cgroup automatically, zero cgroup management code.
- SLURM allocates CPUs, memory, GPUs (GRES) → vmocs reads the allocation and passes it through to QEMU. No resource scheduling logic.
- State is a JSON file in `/run/vmocs/<job_id>/`. No etcd, no daemon, no coordination.
- One VM per SLURM job. Multiple jobs on the same node each get their own VM with non-overlapping resources — SLURM already guarantees this.

This is the same architecture Pyxis uses for containers (fork within `task_init`, inherit cgroup, no cgroup management), applied to VMs.
