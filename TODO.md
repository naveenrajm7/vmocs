# vmocs TODO

## Pending

### Slurm GPU passthrough via VFIO or GIM/SR-IOV

Enable `--gres=gpu:N` to transparently pass allocated GPUs into the VM via VFIO
(exclusive full passthrough) or AMD GIM SR-IOV virtual functions (shared).

---

**How Slurm exposes the allocation:**

`--gres=gpu:1` sets `SLURM_STEP_GPUS=0` (index of the allocated GPU). For VM
passthrough we need the PCI BDF (`0000:41:00.0`), not a device file.

---

**Chosen design: static driver + manual `gres.conf`**

Driver binding is a node configuration decision, not a per-job decision. The
admin binds GPUs to `vfio-pci` or enables GIM VFs once at node setup, then
declares the resulting device files in `gres.conf`:

```ini
# VFIO-bound GPU (full exclusive passthrough)
Name=gpu File=/dev/vfio/0

# GIM virtual functions (shared passthrough, one VF per job)
Name=gpu File=/dev/dri/renderD192,/dev/dri/renderD193
```

Slurm allocates a device file to the job, sets `SLURM_STEP_GPUS`, and restricts
the cgroup. The plugin reads the allocated device file, follows the sysfs symlink
to get the BDF, and passes `--pci <BDF>` to `vmocs launch`. No runtime driver
rebinding, no privileged hook, no restore-on-exit logic.

**Plugin job is trivial:**
```
SLURM_STEP_GPUS=0
→ device file from gres.conf: /dev/vfio/0
→ /sys/class/.../device → 0000:41:00.0
→ vmocs launch base-ubuntu --pci 0000:41:00.0
```

**Tradeoff:** GPU role is static — a VFIO-configured GPU cannot simultaneously
run host compute jobs. Acceptable for HPC nodes with fixed roles.

---

**Alternative: dynamic driver rebind per job**

For nodes where GPUs need to serve both host compute and VM passthrough at
different times, the plugin can rebind the driver at job start and restore it at
exit. This requires a privileged `slurm_spank_task_init_privileged` hook:

1. Unbind GPU from `amdgpu`: write BDF to `.../driver/unbind`
2. Bind to `vfio-pci`: write `vfio-pci` to `.../driver_override`, then `.../bind`
3. Save original driver to `runtime_dir/vfio_state.json`
4. `vmocs launch --pci <BDF>`
5. On exit: reverse — unbind vfio-pci, restore original driver

More flexible but more complex: requires root in the plugin, careful cleanup on
failure, and the GPU must be idle (no host processes using it) when the job starts.

---

**What needs to be built (static design):**

| Component | File | Action |
|-----------|------|--------|
| GPU index → device file → BDF | `lib/vmocs/slurm.py` | `gpu_pci_addresses()`: read `SLURM_STEP_GPUS`, resolve device file from gres assignment, follow sysfs symlink to BDF |
| SPANK plugin | `plugins/slurm/spank_vmocs.c` | Read `SLURM_STEP_GPUS`, append `--pci <BDF>` per GPU to the `vmocs launch` command |

For GIM: VF device files are pre-created by the GIM driver and listed in
`gres.conf` — no extra vmocs code needed beyond reading `SLURM_STEP_GPUS`.

### QMP event watcher — reboot-survives, shutdown-ends-job

Currently `-no-shutdown` is an all-or-nothing flag: either both guest reboot and guest
shutdown keep QEMU alive, or neither does. Slurm users need the split behaviour:

- Guest reboot (`shutdown /r`) → job stays, VM comes back
- Guest shutdown (`shutdown /s`) → job ends cleanly

QEMU emits different QMP events for each: `RESET` for a reboot, `SHUTDOWN` for a
shutdown. A small watcher thread in the blocking launch path can act on them:

- `RESET` event → do nothing (QEMU resets the VM internally)
- `SHUTDOWN` event → call `mon.quit()` → QEMU exits → `waitpid` returns → job ends

Implementation sketch:
1. Start a daemon thread after `mon.cont()` that opens a second QMP connection and
   reads events in a loop.
2. On `SHUTDOWN` event, call `mon.quit()` and set an event so the main thread knows
   to skip the `waitpid` cleanup (QEMU will have already exited).
3. `-no-shutdown` remains in the template so QEMU doesn't self-exit before the watcher
   sees the event.

This is only needed in blocking mode. Detach mode has no `waitpid` so job lifetime is
unaffected by guest-initiated shutdown regardless.

### Improve VM boot time

Current baseline on this machine (HEAD, `5c9dcc8`):

| Template        | Boot mode   | Time to SSH |
|-----------------|-------------|-------------|
| `base-ubuntu`   | cloud-init  | ~5m 30s     |
| `debian-vagrant`| vagrant BIOS| ~6m 14s     |
| `debian-vagrant-uefi` | vagrant UEFI | ~17m 34s |

UEFI boot is ~3x slower than BIOS — likely OVMF POST overhead, not a code issue.
BIOS/cloud-init times (~5-6m) may also be hardware-dependent; revisit on a different
machine before optimising.

**Possible directions:**
- OVMF: try `OVMF_CODE_4M.ms.fd` (pre-enrolled keys) or disable Secure Boot to
  reduce POST time.
- Snapshot restore (`vmocs snapshot`) already exists for cloud-init — measure vs
  cold boot to confirm it is worth using by default.
- Profile guest boot with `systemd-analyze` to see if time is in firmware, kernel,
  or userspace.
