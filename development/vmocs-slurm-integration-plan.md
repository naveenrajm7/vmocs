# vmocs Slurm Integration Plan

## Context

vmocs is a lightweight QEMU/KVM wrapper for HPC jobs. The VM lifecycle (`launch_vm`, `teardown_vm`, `build_qemu_cmdline`) is fully implemented and tested. Now we need to wire vmocs into Slurm's SPANK plugin lifecycle so that running `srun --vm-image ubuntu-gpu ./script.sh` transparently boots a VM with Slurm-allocated resources, runs the user's command inside it, and tears down on exit.

We follow pcocc's integration pattern (`plugins/slurm/vm-setup.lua` + `Batch.py`) but simplified: vmocs handles a single VM per Slurm task, has no etcd, no multi-node clusters, and no rank mapping.

---

## Files to Create/Modify

| File | Action | Purpose |
|------|--------|---------|
| `lib/vmocs/slurm.py` | Create | Read Slurm env vars, map GPU indices to PCI addrs |
| `lib/vmocs/vfio.py` | Create | VFIO bind/unbind via sysfs |
| `lib/vmocs/cli.py` | Modify | Add `internal` subgroup (setup, teardown, setup-vfio) |
| `plugins/slurm/vm-setup.lua` | Create | Lua SPANK plugin |
| `plugins/slurm/vmocs.conf` | Create | Slurm plugstack.conf.d entry |
| `tests/test_slurm.py` | Create | Unit tests for slurm.py |
| `tests/test_vfio.py` | Create | Unit tests for vfio.py |

---

## Phase 1: `lib/vmocs/slurm.py` — Slurm Resource Reader

Pure functions to read Slurm environment and translate to `launch_vm()` parameters.

**Functions:**

1. **`job_id() -> int`** — `int(os.environ['SLURM_JOB_ID'])`. Raise `VmocsError` if unset.

2. **`num_cores() -> int`** — `int(os.environ.get('SLURM_CPUS_PER_TASK', '1'))`.
   Reference: pcocc `Batch.py:1926`.

3. **`memory_mb() -> int`** — Three-tier fallback:
   - `SLURM_MEM_PER_NODE` env var (in MB, direct)
   - `scontrol show jobid=$SLURM_JOB_ID` → parse `MinMemoryCPU=XM`/`MinMemoryCPU=XG` or `MinMemoryNode=XM`/`MinMemoryNode=XG` (same regex as pcocc `Batch.py:1894-1914`)
   - cgroup v2: read `/sys/fs/cgroup/memory.max`, convert bytes→MB
   - Subtract headroom (256 MB or 5%, whichever is larger) for QEMU overhead

4. **`gpu_indices() -> list[int]`** — Parse `SLURM_STEP_GPUS` first (more precise), then `SLURM_JOB_GPUS`. Split on commas, return sorted int list. Empty list if neither set.

5. **`map_gpu_indices_to_pci(indices, gpu_devices) -> list[str]`** — Index into the ordered `gpu-devices` list from `vmocs.yaml`. Raise `VmocsError` if index out of range.

**Tests:** `tests/test_slurm.py` — mock `os.environ` and subprocess output for each fallback path.

---

## Phase 2: `lib/vmocs/vfio.py` — VFIO Bind/Unbind

Two functions operating on sysfs. Runs as root (called from `task_init_privileged`).

1. **`bind_vfio(pci_addr) -> str|None`** — Returns original driver name.
   - Read current driver: `os.readlink('/sys/bus/pci/devices/{addr}/driver')` → `basename`
   - Unbind: write `addr` to `.../driver/unbind`
   - Override: write `vfio-pci` to `.../driver_override`
   - Bind: write `addr` to `/sys/bus/pci/drivers/vfio-pci/bind`

2. **`unbind_vfio(pci_addr, restore_driver)`** — Reverse the bind.
   - Unbind from vfio-pci
   - Clear `driver_override`
   - If `restore_driver` is not None, rebind to original

Wrap `OSError` with `HypervisorError` including the PCI address and a note about root requirement on EPERM.

**Tests:** `tests/test_vfio.py` — mock sysfs reads/writes, verify correct paths and order.

---

## Phase 3: CLI `internal` Subgroup in `lib/vmocs/cli.py`

Add a hidden Click group for commands that the SPANK plugin calls. These are not user-facing.

### Refactor first: Extract SIGTERM handler

The existing `launch` command (lines 149-211) has SIGTERM handler + waitpid logic. Extract into a shared helper:

```python
def _block_until_exit(meta):
    """Install SIGTERM handler with ACPI-powerdown escalation, then waitpid."""
```

Both `launch` and `internal setup` use this.

### Commands:

#### `vmocs internal setup <template_name>`
Called from SPANK `task_init` (job user, inside Slurm cgroup).

1. `slurm.job_id()`, `slurm.num_cores()`, `slurm.memory_mb()`
2. `slurm.gpu_indices()` → `slurm.map_gpu_indices_to_pci(indices, cfg.gpu_devices)`
3. `launch_vm(cfg, tpl, cores, memory_mb, job_id, pci_devices=pci_addrs)`
4. `_block_until_exit(meta)` — blocks until QEMU exits
5. Clean up runtime dir on exit

#### `vmocs internal teardown`
Called from SPANK `exit` hook.

1. Read `SLURM_JOB_ID` → find runtime dir
2. If `vfio_state.json` exists, call `vfio.unbind_vfio()` for each device (skip with warning if not root)
3. `teardown_vm(job_id, runtime_base=cfg.runtime_dir)`

#### `vmocs internal setup-vfio <template_name>`
Called from SPANK `task_init_privileged` (root).

1. `slurm.gpu_indices()` → `slurm.map_gpu_indices_to_pci()`
2. For each PCI addr: `vfio.bind_vfio(addr)`
3. Write `{addr: original_driver}` to `/run/vmocs/<job_id>/vfio_state.json`

---

## Phase 4: `plugins/slurm/vm-setup.lua` — SPANK Plugin

Lua SPANK plugin following pcocc's `vm-setup.lua` pattern but without stepid tracking or multi-step coordination.

**Hooks:**

| Hook | Action |
|------|--------|
| `slurm_spank_init` | Register `--vm-image` option |
| `option_handler` | Set `vm_enabled=true`, `vm_option=optarg` |
| `slurm_spank_init_post_opt` | Allocator: `job_control_setenv("VMOCS_TEMPLATE", vm_option)`; Remote: `replicate_slurm_vars()` |
| `slurm_spank_task_init_privileged` | `vmocs internal setup-vfio <template>` |
| `slurm_spank_task_init` | `vmocs internal setup <template>` (blocks until VM exits) |
| `slurm_spank_exit` | `vmocs internal teardown` (remote context only) |

**Helper functions** (from pcocc):
- `do_and_log_output(cmd)` — Execute via `io.popen`, log lines
- `setenv(name, val)` — `posix.setenv` wrapper
- `replicate_var(spank, slurm_name, env_name)` — `spank:get_item` → `setenv`
- `replicate_env(spank, slurm_name, env_name)` — `spank:getenv` → `setenv`
- `replicate_slurm_vars(spank)` — Replicate: `SLURM_JOB_ID`, `SLURM_JOB_UID`, `SLURM_CPUS_PER_TASK`, `SLURM_MEM_PER_NODE`, `SLURM_STEP_GPUS`, `VMOCS_TEMPLATE`

**Omitted vs pcocc:** No `job_prolog`/`job_epilog` (no stepid tracking needed). No pcocc_path discovery (vmocs assumed on PATH or configured via plugin args).

---

## Phase 5: `plugins/slurm/vmocs.conf` — Plugin Installation

Single line:
```
optional /usr/lib64/slurm/spank_lua.so /etc/slurm/lua.d/vm-setup.lua
```

Installed to `/etc/slurm/plugstack.conf.d/vmocs.conf` on compute nodes.

---

## Implementation Order

```
Phase 1 (slurm.py) ──┐
                      ├── Phase 3 (cli.py internal) ── Phase 4 (Lua) ── Phase 5 (conf)
Phase 2 (vfio.py) ───┘
```

Phases 1 and 2 are independent and can be done in parallel.

---

## End-to-End Flow

```
srun --vm-image ubuntu-gpu -c4 --mem=8G --gres=gpu:1 ./my_script.sh
  │
  ├─ SPANK init: register --vm-image
  ├─ SPANK init_post_opt (allocator): set VMOCS_TEMPLATE=ubuntu-gpu in job env
  │
  ├─ [on compute node]
  ├─ SPANK init_post_opt (remote): replicate SLURM_JOB_ID, SLURM_CPUS_PER_TASK, etc.
  ├─ SPANK task_init_privileged (root):
  │   └─ vmocs internal setup-vfio ubuntu-gpu
  │       └─ bind GPU 0 → vfio-pci, write vfio_state.json
  ├─ SPANK task_init (job user, inside Slurm cgroup):
  │   └─ vmocs internal setup ubuntu-gpu
  │       ├─ Read: cores=4, mem=8192MB(-headroom), gpu=[0000:41:00.0]
  │       ├─ launch_vm(cfg, tpl, 4, 7936, job_id, pci_devices=['0000:41:00.0'])
  │       │   ├─ COW overlay, cloud-init ISO, fork/exec QEMU (inherits cgroup)
  │       │   ├─ QMP connect, cont(), wait SSH
  │       │   └─ Return meta
  │       └─ waitpid(qemu_pid) — blocks until VM exits
  │
  ├─ SPANK exit (remote):
  │   └─ vmocs internal teardown
  │       ├─ kill QEMU, rm runtime dir
  │       └─ unbind vfio-pci, restore amdgpu
  └─ done
```

---

## Verification

1. **Unit tests**: `pytest tests/test_slurm.py tests/test_vfio.py` — mock-based, no Slurm needed
2. **Manual test without Slurm**: `vmocs internal setup ubuntu-base` with `SLURM_JOB_ID`, `SLURM_CPUS_PER_TASK`, `SLURM_MEM_PER_NODE` set manually in env — verify it reads them and launches a VM
3. **Manual test with Slurm**: `srun --vm-image ubuntu-base -c2 --mem=4G hostname` — verify VM boots, SSH works, QEMU inherits cgroup, cleanup on exit
4. **GPU test**: `srun --vm-image ubuntu-gpu --gres=gpu:1 nvidia-smi` — verify VFIO bind/unbind and GPU visible in guest
