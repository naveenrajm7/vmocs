# vmocs TODO

## Pending

### Slurm GPU passthrough via VFIO or GIM/SR-IOV

**Status: implemented, end-to-end test pending (node reboot required — GPU wedged
after failed passthrough attempt; `lspci` shows `rev ff / Unknown header type 7f`).**

Enable `--gres=gpu:N` to transparently pass allocated GPUs into the VM via VFIO
(exclusive full passthrough) or AMD GIM SR-IOV virtual functions (shared, one VF
per job).

**What has been built:**

| Component | File | Status |
|-----------|------|--------|
| VFIO device → BDF discovery | `plugins/slurm/spank_vmocs.c` `collect_pci_args()` | Done |
| `--pci <BDF>` injection into launch | `plugins/slurm/spank_vmocs.c` `slurm_spank_task_init()` | Done |
| gres.conf generation script | `plugins/slurm/gres-conf-gen.py` | Done |
| Node file permissions | udev rule `99-vfio-kvm.rules` + user in `kvm` group | Done (node-level setup) |

**Node setup steps (one-time per node):**
```bash
# 1. Bind GPUs to vfio-pci (driverctl persists across reboots)
driverctl set-override 0000:03:00.0 vfio-pci
driverctl set-override 0000:03:00.1 vfio-pci

# 2. udev rule so kvm group members can open vfio devices
echo 'SUBSYSTEM=="vfio", KERNEL!="vfio", GROUP="kvm", MODE="0660"' \
    > /etc/udev/rules.d/99-vfio-kvm.rules
udevadm control --reload && udevadm trigger --subsystem-match=vfio

# 3. Add job users to kvm group
usermod -aG kvm <user>

# 4. Generate gres.conf File= line
python3 plugins/slurm/gres-conf-gen.py >> /etc/slurm/gres.conf

# 5. Update slurm-nodes.conf: change gres=gpu:<type>:N → gres=gpu:N
# 6. Restart slurmctld + slurmd
```

**Template requirement:** templates used with GPU passthrough must set
`pci-root-port: true` — without it QEMU crashes with IRQ assertion failure
(`pci_irq_handler: 0 <= irq_num && irq_num < PCI_NUM_PINS`) on AMD GPUs.

**Remaining: end-to-end passthrough test**

Verify after node reboot:
```bash
srun --gres=gpu:1 --cpus-per-task=2 --mem=4G --vm-image=base-ubuntu hostname
# while running:
ssh -i /tmp/vmocs/<jobid>/id_ed25519 -p <ssh_port> ubuntu@localhost lspci
# expect: AMD GPU visible inside VM
```

---

**How Slurm exposes the allocation:**

`--gres=gpu:1` allocates one GPU device file from `gres.conf File=` to the job,
adds it to the job's cgroup device whitelist, and sets `SLURM_STEP_GPUS` to the
ordinal index within the File= list. The cgroup is the source of truth — only the
allocated `/dev/vfio/<N>` is accessible inside the task. `SLURM_STEP_GPUS` is not
used in the plugin; the cgroup filter is sufficient.

---

**How both cases reduce to the same device files**

**Case 1 — bare VFIO (full GPU exclusive passthrough):**
- Admin binds each GPU's PF to `vfio-pci` at node setup
- Kernel creates `/dev/vfio/<iommu_group>` per GPU
- PF has no `physfn` symlink; driver symlink resolves to `vfio-pci`

**Case 2 — AMD GIM SR-IOV (one VF per job, GPU shared across jobs):**
- GIM driver (`gim` module) manages the PF (device id `75a3` on MI300-series)
- GIM creates one VF per physical GPU (device id `75b3`), then binds each VF to
  `vfio-pci`
- Kernel creates `/dev/vfio/<iommu_group>` per VF — identical file layout to
  bare VFIO
- VF is identified in sysfs by the presence of a `physfn` symlink pointing to its PF

In both cases the device file Slurm tracks is `/dev/vfio/<N>`. No DRI files
(`/dev/dri/renderD*`) are involved — the GPU (or VF) is not bound to `amdgpu`.

---

**Example mapping on an 8-GPU GIM SR-IOV node:**

```
BDF (VF)        iommu group   device file
0000:05:02.0    194           /dev/vfio/194
0000:15:02.0    190           /dev/vfio/190
0000:65:02.0    192           /dev/vfio/192
0000:75:02.0    193           /dev/vfio/193
0000:85:02.0    196           /dev/vfio/196
0000:95:02.0    191           /dev/vfio/191
0000:e5:02.0    195           /dev/vfio/195
0000:f5:02.0    197           /dev/vfio/197
```

IOMMU group numbers are not in BDF order — always derive them from sysfs.

---

**Chosen design: static driver + auto-generated `gres.conf`**

Driver binding is a node configuration decision, not a per-job decision. The admin
runs the helper script once at node setup to generate the `gres.conf` File= line:

```bash
# Run on the node to generate the gres.conf File= line
python3 -c "
from pathlib import Path
devs = []
for bdf in Path('/sys/bus/pci/devices').iterdir():
    # VF: has physfn symlink (bare VFIO PFs don't)
    is_vf = (bdf / 'physfn').exists()
    drv = bdf / 'driver'
    if not drv.exists() or Path(drv).resolve().name != 'vfio-pci': continue
    # For bare VFIO PFs: filter to display class (0x03xx), skip audio (0x04xx)
    if not is_vf:
        cls = (bdf / 'class').read_text().strip()
        if not cls.startswith('0x03'): continue
    grp = Path(bdf / 'iommu_group').resolve().name
    dev = f'/dev/vfio/{grp}'
    if Path(dev).exists(): devs.append(dev)
print('Name=gpu File=' + ','.join(sorted(devs)))
"
```

Example output for the GIM node above:
```ini
Name=gpu File=/dev/vfio/190,/dev/vfio/191,/dev/vfio/192,/dev/vfio/193,/dev/vfio/194,/dev/vfio/195,/dev/vfio/196,/dev/vfio/197
```

Slurm allocates one device file to the job, restricts the cgroup, and the plugin
does the rest. No runtime rebinding, no privileged hook, no restore-on-exit logic.

---

**Plugin flow:**

```
Slurm allocates /dev/vfio/194 → cgroup whitelists it
plugin (task_init) scans /dev/vfio/* numeric entries
tries to open each → /dev/vfio/194 succeeds, others → EPERM
extract iommu group number: 194
collect ALL BDFs in /sys/kernel/iommu_groups/194/devices/
→ pass each as --pci to vmocs launch
vmocs launch <template> --pci 0000:05:02.0 [--pci 0000:05:02.1 ...]
```

**Consumer card edge case — multiple BDFs per IOMMU group:**

Consumer GPUs (e.g., RX 7900 XTX) expose two PCI functions on the same slot:
- `03:00.0` — VGA/display controller (`0x03xx` class)
- `03:00.1` — HDMI/DP audio device (`0x04xx` class)

Whether these land in the same IOMMU group or separate ones depends on whether
ACS (Access Control Services) is enabled on the host:

**ACS off (default on most systems) — same group:**
```
03:00.0  →  group 63  →  /dev/vfio/63
03:00.1  →  group 63  →  /dev/vfio/63   ← same file
```
gres.conf is one entry: `Name=gpu File=/dev/vfio/63`

Slurm allocates `/dev/vfio/63`. The plugin collects **all** BDFs in group 63 and
passes them all to `--pci`. Both functions appear in the VM. This is required for
Windows GUI VMs — omitting the audio function can cause the guest to hang on boot.

**ACS on — separate groups:**
```
03:00.0  →  group 63  →  /dev/vfio/63
03:00.1  →  group 64  →  /dev/vfio/64
```
Slurm has no way to allocate two device files as one atomic GPU unit. Options:
- **Compute only:** list only `/dev/vfio/63` in gres.conf, plugin passes `03:00.0`
  alone. Audio is not available in the VM but compute works fine.
- **Full GUI (Windows):** list both files, allocate with `--gres=gpu:1,gpu:audio:1`
  using a separate `Type=audio` gres entry. More admin overhead.

For Instinct/GIM nodes this edge case does not apply — each VF is a single-function
device in its own IOMMU group, so the group always contains exactly one BDF.

**Plugin rule:** pass ALL BDFs in the accessible IOMMU group to `--pci`, without
filtering by PCI class. The `0x03xx` class filter is only used in `gres-conf-gen.py`
to identify which groups represent GPUs (not audio-only or other devices).

---

**What needs to be built (static design):**

| Component | File | Action |
|-----------|------|--------|
| Accessible VFIO device → BDF | `lib/vmocs/slurm.py` | `gpu_pci_addresses()`: scan `/dev/vfio/*`, try-open each, follow iommu_group sysfs to BDF |
| SPANK plugin | `plugins/slurm/spank_vmocs.c` | Call `gpu_pci_addresses()` equivalent in C, append `--pci <BDF>` per GPU to `vmocs launch` |
| gres.conf helper | `plugins/slurm/gres-conf-gen.py` | Script admins run once at node setup to print the `File=` line |

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
failure, and the GPU must be idle when the job starts. Not needed for GIM nodes
where VFs are always vfio-pci bound.

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

### Fix --vm-save race condition (Slurm)

`--vm-save` via `srun --vm-image ... --vm-save <path>` produces a sparse, unusable image.

**Root cause:** Two cleanup paths race after job cancellation:

1. `scancel` → SIGTERM → `_block_until_exit` in `vmocs launch` → ACPI shutdown → QEMU exits →
   `finally: shutil.rmtree(runtime_dir)` — deletes `disk.qcow2` (the COW overlay)
2. `slurm_spank_exit` → `vmocs stop <job_id> --save <path>` → `teardown_vm` reads `vm.json` →
   calls `convert_standalone(meta['overlay'], save_path)` — but the overlay is already deleted
   (or was partially flushed when SIGTERM killed QEMU mid-write)

**Observed symptom:** Saved image is ~175 MB vs ~600 MB for the original. `qemu-img check`
shows only 2.50% clusters allocated (1434 vs 29256). The image has no backing file and passes
structural checks, but launching a VM from it results in QEMU starting (process visible) with
the QMP socket created, yet the QMP greeting banner is never sent — VM hangs before any guest
code runs.

**Why qemu-img convert is not to blame:** Manual `qemu-img convert` on an intact COW overlay
correctly traverses the backing chain and produces a full standalone image. The race means
`convert_standalone` is called on an already-deleted overlay (gets ENOENT), or QEMU was killed
before flushing dirty cache pages to the overlay, so only the sparsely-written COW delta is
present at convert time.

**Fix direction:** When `--save` is requested, `vmocs launch` must not delete `runtime_dir` in
its finally block — it should leave the overlay intact for `vmocs stop --save` to convert. One
approach: write a `save_path` field into `vm.json` at launch time; in the finally block, skip
`shutil.rmtree` if that field is set; let `vmocs stop` own the full cleanup after conversion.

**Validation:** Launch a VM from the saved qcow2 as a template image — it must boot successfully
and SSH must become reachable within the normal timeout.

---

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
