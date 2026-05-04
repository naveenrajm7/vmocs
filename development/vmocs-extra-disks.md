# vmocs: Extra Disk Support

## Problem

Users need to attach additional emulated disks (typically NVMe) to their VMs — for example, a scratch disk for data workloads backed by a separate qcow2. In libvirt, users achieve this with raw QEMU passthrough:

```
--qemu-commandline="--drive file=${DISK},format=qcow2,if=none,id=${NAME}-nvme"
--qemu-commandline="--device nvme,serial=${NAME}-nvme,drive=${NAME}-nvme"
```

vmocs already has the QEMU building blocks (`block_cmdline()` dispatches to `_nvme_cmdline()`, `_virtio_blk_cmdline()`, etc.), but the lifecycle only manages a single disk today. Supporting multiple disks introduces complexity around COW overlays, snapshots, VM save, and teardown.

This document compares two approaches and proposes a path forward.

---

## Approach A: Persistent Drives (pcocc model)

Extra disks are **not ephemeral**. The user provides a path to an existing qcow2/raw file. vmocs attaches it directly — no COW overlay, no snapshot management.

### How pcocc does it

pcocc calls this `persistent-drives` (`Templates.py:52`). Template config:

```yaml
my-template:
  image: my-os-image
  persistent-drives:
    - /shared/data/scratch.qcow2:
        mmp: 'no'
        cache: 'unsafe'
```

At QEMU launch (`Hypervisor.py:1395-1415`), pcocc loops over persistent drives and attaches each one sequentially after the primary disk:

- `drive0` = ephemeral OS disk (COW overlay)
- `drive1`, `drive2`, ... = persistent drives (direct file access)

On checkpoint (`cmd.py:769-778`), **only `drive0` is saved**. Persistent drives are excluded — they live on shared storage and survive independently.

### What this looks like in vmocs

```yaml
# confs/templates.yaml
rocm-nvme:
  image: /shared/vmocs/images/rocm-dev/os-disk.qcow2
  extra-disks:
    - file: /shared/data/nvme-scratch.qcow2
      device: nvme
      cache: none
      serial: "SCRATCH-0"
```

### Lifecycle impact

| Operation | Behavior |
|---|---|
| **Launch** | Attach extra disk directly (no COW overlay). User must ensure the file exists. |
| **Snapshot create** | Only save drive0 (OS disk) + memory. Extra disks are not part of the snapshot. |
| **Snapshot restore** | Restore drive0 + memory. Extra disks re-attached at their original paths. The extra disk content must match what QEMU expects from the saved memory state. |
| **VM save (`--save`)** | Only flatten drive0. Extra disks are the user's responsibility. |
| **Teardown** | Kill QEMU. Extra disk files are untouched (user owns them). |

### Pros

- Simple to implement — minimal changes to launch/snapshot/teardown
- No extra COW overlay management
- User has full control over their data disks
- Matches pcocc's proven model

### Cons

- **Snapshot restore is fragile**: the saved memory state references drive1's content at snapshot time. If the user modifies the extra disk between snapshot-create and restore, QEMU will see inconsistent state (memory thinks disk has old data, disk has new data). This can cause filesystem corruption inside the guest.
- User must manage extra disk lifecycle (creation, backup, cleanup)
- No isolation between jobs — two VMs launched from the same template with the same persistent disk will corrupt each other (unless MMP locking is added)

### Snapshot restore caveat (important)

When QEMU saves a memory snapshot, it captures the full device state including dirty page tracking and block driver state for **all** attached drives. On restore:

- drive0 works fine: fresh COW overlay over the same base image = identical block content
- drive1 (persistent): if the file hasn't changed since snapshot-create, it works. If it has changed, QEMU's in-memory block state is stale.

For read-only extra disks (e.g., a pre-built dataset), this is safe. For writable scratch disks, the user must understand that the persistent disk content must not change between snapshot creation and restore.

---

## Approach B: Full Ephemeral (per-disk COW overlays)

Every extra disk gets its own COW overlay, just like the primary OS disk. All disks are snapshotted together. The VM is fully self-contained.

### What this looks like in vmocs

```yaml
rocm-nvme:
  image: /shared/vmocs/images/rocm-dev/os-disk.qcow2
  extra-disks:
    - file: /shared/vmocs/images/rocm-dev/nvme-data.qcow2
      device: nvme
      snapshot: true       # create COW overlay
      cache: none
      serial: "VMOCS-NVME-0"
```

The `snapshot: true` flag (from `vmocs-vm-image.md`) tells vmocs to create a COW overlay for this disk, making it ephemeral. The base image is never modified.

### Lifecycle impact

| Operation | Behavior |
|---|---|
| **Launch** | Create COW overlay per extra disk (`runtime_dir/nvme-0.qcow2`). Attach overlay, not base. |
| **Snapshot create** | Stop VM. Save memory. Flatten **each** overlay into `snap_dir/` (e.g., `disk.qcow2`, `nvme-0.qcow2`). |
| **Snapshot restore** | Create fresh COW overlays for **all** disks over their snapshot bases. Restore memory. Node-names must match (`drive0`, `drive1`, ...). |
| **VM save (`--save`)** | Flatten **each** overlay to the save path (e.g., `save-dir/disk.qcow2`, `save-dir/nvme-0.qcow2`). |
| **Teardown** | Delete `runtime_dir/` — all overlays gone. Base images untouched. |

### Pros

- Fully self-contained: snapshot = memory + all disks. Restore is always consistent.
- Base images are never touched — multiple jobs share the same base safely
- No user-side disk management required
- Snapshot restore is safe: every disk starts from a known-good state

### Cons

- More complex implementation — launch/snapshot/save must loop over all disks
- Flattening multiple overlays during snapshot-create takes longer (proportional to written data per disk)
- VM save produces multiple files — the save path must be a directory, not a single file
- Extra disk changes are ephemeral by default — users who want persistent writes need a different mechanism

---

## Hybrid: Per-Disk `snapshot` Flag

The two approaches are not mutually exclusive. The `extra-disks` schema can support both via a per-disk `snapshot` flag:

```yaml
rocm-hybrid:
  image: /shared/vmocs/images/rocm-dev/os-disk.qcow2
  extra-disks:
    # Ephemeral scratch disk — COW overlay, included in snapshots
    - file: /shared/vmocs/images/rocm-dev/nvme-scratch.qcow2
      device: nvme
      snapshot: true
      cache: none
      serial: "SCRATCH-0"

    # Persistent user data — direct access, NOT in snapshots
    - file: /home/user/my-data.qcow2
      device: nvme
      snapshot: false
      cache: writeback
      serial: "DATA-0"
```

| `snapshot` | COW overlay | In snapshot | In VM save | Isolation |
|---|---|---|---|---|
| `true` | Yes | Yes | Yes | Full (base untouched) |
| `false` | No | No | No | None (direct file access) |

This is the most flexible design but also the most complex to implement and reason about. The snapshot-restore caveat from Approach A applies to any disk with `snapshot: false`.

---

## Comparison Summary

| Concern | A: Persistent | B: Full Ephemeral | Hybrid |
|---|---|---|---|
| Implementation effort | Low | Medium | Medium-High |
| Snapshot consistency | Fragile for extra disks | Fully consistent | Per-disk |
| User disk management | User owns extra disks | vmocs owns all disks | Per-disk choice |
| Multi-job safety | Needs MMP locking | Safe (COW isolation) | Per-disk |
| Save complexity | Single file | Directory of files | Directory of files |
| pcocc precedent | Yes | No | No |

---

## Code Changes Required

### Common to all approaches

1. **`templates.py`**: Add `extra-disks` to `TEMPLATE_SETTINGS` (list of dicts, default `[]`, inheritable)
2. **`hypervisor.py`** (`build_qemu_cmdline`): Accept `extra_disks` parameter, loop over them calling `block_cmdline()` with `drive1`, `drive2`, etc.

### Approach A only

3. **`launch.py`**: Pass extra disk paths directly to `build_qemu_cmdline`. No COW overlay creation for extra disks. No changes to `teardown_vm` save logic.

### Approach B only

3. **`launch.py`**: Create COW overlay per extra disk in `runtime_dir/`. Track overlay-to-base mapping in `vm.json`. On `--save`, flatten each overlay.
4. **`snapshot.py`**: Flatten all overlays during `create_snapshot`. Store all disk images in `snap_dir/` with a manifest mapping node-names to files. On restore, create COW overlays for all disks.

### Hybrid

All of the above, conditioned on the per-disk `snapshot` flag.

---

## Recommendation

Start with **Approach A** (persistent drives). It covers the immediate user need (attach an NVMe data disk) with minimal changes. The QEMU plumbing already exists — the main work is looping over `extra-disks` in `build_qemu_cmdline` and passing the paths through `launch.py`.

The snapshot consistency caveat is acceptable for the current use case: users who create snapshots typically use a fixed base image for all disks. And users who need a writable scratch disk usually don't need it preserved across snapshot restores.

Approach B can be added later by introducing the `snapshot: true` flag on individual extra disks. The template schema is forward-compatible — adding the flag doesn't break existing templates that omit it (default to `false` = Approach A behavior).

---

## pcocc Reference

- Template setting: `Templates.py:52` (`persistent-drives`)
- Drive normalization: `Templates.py:327-356` (`_convert_drives_to_dict`)
- QEMU attachment loop: `Hypervisor.py:1395-1415`
- Block drive metadata: `Cluster.py:163-183` (`block_drives` property)
- Checkpoint save (drive0 only): `cmd.py:769-778`
