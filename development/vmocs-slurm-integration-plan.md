# vmocs Slurm Integration Plan

## Context

vmocs is a lightweight QEMU/KVM wrapper for HPC jobs. The VM lifecycle (`launch_vm`, `teardown_vm`, `build_qemu_cmdline`) is fully implemented and tested. The goal is to wire vmocs into Slurm's SPANK plugin lifecycle so that `srun --vm-image <template>` transparently boots a VM with Slurm-allocated resources and tears down on exit.

We follow pcocc's integration pattern but simplified: vmocs handles a single VM per Slurm task, has no etcd, no multi-node clusters, and no rank mapping.

---

## What Was Built (slurm-integration branch)

### C SPANK Plugin — `plugins/slurm/spank_vmocs.c` ✅

Minimal C plugin with four hooks:

| Hook | Action |
|------|--------|
| `slurm_spank_init` | Register `--vm-image=TEMPLATE` option |
| `slurm_spank_init_post_opt` | Allocator context: persist `VMOCS_TEMPLATE` into job env via `spank_job_control_setenv` |
| `slurm_spank_task_init` | Fork `vmocs launch <template> --cores N --memory M --job-id J` and waitpid (blocking) |
| `slurm_spank_exit` | Fork `vmocs stop <jobid>` — best-effort cleanup, remote context only |

Memory is read from `SLURM_MEM_PER_NODE` or `SLURM_MEM_PER_CPU × SLURM_CPUS_PER_TASK`, with a headroom deduction (5% or 256 MB, whichever is larger) for QEMU overhead. Supports `vmocs_path=/prefix` plugin arg so the binary does not need to be on `PATH`.

Compiled with `make -C plugins/slurm`, installed to `/usr/lib64/slurm/spank_vmocs.so`.

### Lua Reference Plugin — `plugins/slurm/vm-setup.lua` ✅

Equivalent Lua plugin kept for reference. Requires `spank_lua.so` which is not part of upstream Slurm — the C plugin is used in production.

### Plugin Config — `/etc/slurm/plugstack.conf` ✅

Appended to the existing plugstack alongside pyxis:
```
required  spank_pyxis.so
optional  spank_vmocs.so vmocs_path=/path/to/vmocs/.venv
```

### System Config — `/etc/vmocs/vmocs.yaml` + `/etc/vmocs/templates.yaml` ✅

vmocs config resolution order: `VMOCS_CONF` env var → `confs/vmocs.yaml` in cwd → `/etc/vmocs/vmocs.yaml`. The system-wide config at `/etc/vmocs/` is used when the SPANK plugin runs `vmocs launch` (cwd is not the repo directory).

### CLI cleanup — `lib/vmocs/cli.py` ✅

- Extracted `_block_until_exit(qemu_pid, qmp_socket)` helper with 3-attempt SIGTERM escalation (ACPI powerdown × 2 → QMP quit + SIGKILL fallback)
- `launch` command calls the helper instead of inlining the logic
- Dropped unused `import time`

### QEMU logging — `lib/vmocs/launch.py` ✅

QEMU stdout/stderr redirected to `runtime_dir/qemu.log` instead of `/dev/null`. File is cleaned up by `vmocs stop`. Error message on QMP timeout includes the log path.

---

## End-to-End Flow (as implemented)

```
srun -c2 --mem=4G --vm-image base-ubuntu <user-command>
  │
  ├─ SPANK init: register --vm-image
  ├─ SPANK init_post_opt (allocator): set VMOCS_TEMPLATE=base-ubuntu in job env
  │
  ├─ [on compute node, as job user, inside Slurm cgroup]
  ├─ SPANK task_init:
  │   └─ vmocs launch base-ubuntu --cores 2 --memory 1792 --job-id <N>
  │       ├─ COW overlay over base image
  │       ├─ cloud-init ISO, ephemeral SSH keypair
  │       ├─ fork/exec QEMU (inherits Slurm cgroup)
  │       ├─ QMP connect, cont(), wait for SSH
  │       └─ blocks on waitpid(qemu_pid) ← job stays alive here
  │
  ├─ SPANK exit (on job cancel / QEMU exit):
  │   └─ vmocs stop <N>  — ACPI shutdown, kill QEMU, rm runtime dir
  └─ done
```

Verified on a compute node:
- `srun --vm-image base-ubuntu` → VM boots, `vmocs list` shows it running
- `scancel` → `slurm_spank_exit` calls `vmocs stop`, runtime dir cleaned up
- Port allocation is collision-free across concurrent launches (tested with 2 VMs simultaneously)

---

## Deferred: GPU / VFIO Passthrough

The original plan included `slurm.py` (Slurm env reader), `vfio.py` (sysfs bind/unbind), and a `task_init_privileged` hook for root-level GPU rebinding. These were intentionally deferred — the C plugin handles CPU and memory from Slurm env vars, and GPU support will be added as a follow-on phase.

---

## Open Question: What Does the User Command Do?

When running `srun --vm-image base-ubuntu <user-command>`, the SPANK `task_init` hook blocks with `vmocs launch` for the lifetime of the VM. The user command passed to `srun` is the **Slurm task** that runs after `task_init` returns — but since `task_init` never returns until the VM exits, the user command effectively never runs on the host.

This surfaces a fundamental design choice for how vmocs integrates with Slurm workflows.

### Option A — VM-as-Job (current behavior)

`task_init` boots the VM and blocks. The VM is the job. The user command passed to `srun` is never executed by Slurm — the job's workload is expected to be submitted *into* the VM separately (via SSH, a prolog script, or a job script that SSHs in).

**When this is the right model:**
- Interactive allocations: `salloc --vm-image base-ubuntu` → user gets a Slurm shell, SSHs into the VM manually
- Batch jobs where the job script handles SSH internally
- Persistent VM sessions tied to Slurm's cgroup lifetime

**Current behavior confirmed:** `srun --vm-image base-ubuntu bash -c 'echo hello'` booted the VM successfully (job_id=852, port 60222), but `echo hello` never ran — the job hung until `scancel`.

**What's missing for this option to be usable:**
- A way to SSH into the running VM from outside the job (e.g., `vmocs ssh <job_id>`)
- Or a job script pattern that SSHs in after detecting the VM is ready

### Option B — Execute Command Inside VM

`task_init` boots the VM with `--detach`, then SSHs the user's command into the guest and waits for it to finish. The VM is torn down when the command exits.

```
srun --vm-image base-ubuntu hostname
  → boots VM, SSHs: ssh -i <key> -p <port> ubuntu@127.0.0.1 hostname
  → prints guest hostname
  → tears down VM, job exits
```

**When this is the right model:**
- Transparent VM execution: user writes `srun --vm-image ubuntu-gpu ./train.sh` and the script runs inside the VM as if it were a normal job
- Batch workflows where each job step runs in a fresh isolated VM

**What needs to be built:**
- Plugin passes `--detach` to `vmocs launch` and captures the SSH connection info from `vm.json`
- Plugin then SSHs `<user-command>` into the guest, blocks until exit code is returned
- On exit, plugin calls `vmocs stop`
- The user command and its arguments need to be forwarded correctly (quoting, env vars)

**Tradeoff vs Option A:** More complex to implement correctly (argument quoting, exit code propagation, stdin/stdout forwarding), but far more useful for batch HPC workloads.
