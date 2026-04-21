# vmocs: VM Image System

## Design Goals

1. Users boot directly into a VM with minimal wait — target <5s via memory snapshots
2. Support multi-disk setups (OS disk + NVMe scratch/data disks)
3. Pre-built images stored on shared filesystem, referenced by templates
4. Fast boot via QEMU memory snapshot restore (~2-5s instead of ~15-30s full boot)
5. Base images remain immutable — all VM writes go to ephemeral COW overlays per job

---

## Image Packaging Format

Each VM image is a **directory** containing a manifest and one or more disk images:

```
/shared/vmocs/images/<image-name>/
├── manifest.yaml            # Describes the full VM disk layout
├── os-disk.qcow2            # Primary boot disk
├── nvme-data.qcow2          # Optional secondary NVMe disk(s)
├── snapshot.mem.lzo         # Optional pre-saved memory state (lzop compressed)
└── cloud-init/              # Optional default cloud-init configs
    ├── user-data
    └── meta-data
```

### manifest.yaml Schema

```yaml
name: rocm-dev
version: "1.0"
description: ROCm development environment with NVMe scratch disk
machine-type: q35

disks:
  - name: os
    file: os-disk.qcow2                    # Relative to manifest directory
    device: virtio-blk                     # virtio-blk | virtio-scsi | ide | nvme
    snapshot: true                         # Create COW overlay (ephemeral writes)
    boot: true
    cache: unsafe                          # writeback | none | unsafe | writethrough | directsync

  - name: scratch
    file: nvme-data.qcow2
    device: nvme                           # NVMe emulation
    snapshot: true
    serial: "VMOCS-NVME-0"                # NVMe serial number (visible in guest)
    cache: none

memory-snapshot: snapshot.mem.lzo         # Optional — enables fast restore via -incoming

cloud-init:
  user-data: cloud-init/user-data          # Default cloud-init (can be overridden by template)
  meta-data: cloud-init/meta-data

requirements:
  min-memory: 4096                         # MB
  min-cores: 2
  gpu: optional                            # none | optional | required
```

### Supported Disk Device Types

| Device | QEMU flag | Use case |
|---|---|---|
| `virtio-blk` | `-device virtio-blk-pci,drive=<name>` | Primary OS disk (best performance) |
| `virtio-scsi` | `-device scsi-hd,bus=scsi0.0,drive=<name>` | Generic block device |
| `ide` | `-device ide-hd,drive=<name>` | Legacy compatibility |
| `nvme` | `-device nvme,drive=<name>,serial=<serial>` | NVMe emulation for NVMe-aware workloads |

All device types share the same blockdev backend (`-blockdev driver=qcow2,...`). Only the frontend device line differs.

---

## QEMU Command Line Generation

### Block device backend (reused from pcocc `Hypervisor.py:2455-2466`)

```
-blockdev driver=qcow2,node-name=<name>,cache.direct=<>,file.driver=file,file.filename=<path>,discard=unmap
```

### Frontend device dispatch

Extend pcocc's `qemu_gen_block_cmdline()` (`Hypervisor.py:2363-2371`) with NVMe support:

```python
def qemu_gen_block_cmdline(model, path, name, index, cache, mmp, **kwargs):
    if model == 'virtio-blk':
        return qemu_gen_vblk_cmdline(path, name, index, cache, mmp)
    elif model == 'virtio-scsi':
        return qemu_gen_scsi_cmdline(path, name, index, cache, mmp)
    elif model == 'ide':
        return qemu_gen_ide_cmdline(path, name, index, cache, mmp)
    elif model == 'nvme':
        return qemu_gen_nvme_cmdline(path, name, index, cache, mmp,
                                     serial=kwargs.get('serial', 'VMOCS-NVME'))

def qemu_gen_nvme_cmdline(path, name, index, cache, mmp, serial="VMOCS-NVME"):
    cmd = ['-device', f'nvme,drive={name},serial={serial}-{index}']
    return cmd + qemu_gen_drive_cmdline(path, name, cache, mmp)
```

### Full multi-disk QEMU args example

For a VM with virtio-blk OS disk + NVMe data disk:

```
# Disk controller
-device virtio-scsi-pci,id=scsi0

# Drive 0: OS disk (virtio-blk, COW snapshot over base)
-object iothread,id=ioth-drive0
-device virtio-blk-pci,id=vblk-drive0,drive=drive0,addr=06.0,write-cache=on
-blockdev driver=qcow2,node-name=drive0,cache.direct=off,cache.no-flush=on,\
         file.driver=file,file.filename=/run/vmocs/<jobid>/os-cow.qcow2,discard=unmap

# Drive 1: NVMe data disk (COW snapshot over base)
-device nvme,drive=drive1,serial=VMOCS-NVME-0
-blockdev driver=qcow2,node-name=drive1,cache.direct=on,cache.no-flush=off,\
         file.driver=file,file.filename=/run/vmocs/<jobid>/nvme-cow.qcow2,discard=unmap
```

---

## Boot Strategies

### Strategy 1: Full boot (fallback, ~15-30s)

Used when no memory snapshot exists.

```
1. Create COW snapshot per disk:
   qemu-img create -f qcow2 -F qcow2 -b <base>.qcow2 <cow>.qcow2

2. Generate cloud-init ISO (SSH keypair + network config)

3. Start QEMU with -S (paused)

4. Connect QemuMonitor, bind vCPUs, cont()

5. Wait for SSH readiness (poll port until SSH banner)
```

### Strategy 2: Memory snapshot restore (fast, ~2-5s) ← preferred

Used when `memory-snapshot` exists in manifest. Reuse pcocc's checkpoint restore (`Hypervisor.py:1337-1341`).

```
1. Create COW snapshots per disk (same node-names as when snapshot was taken)

2. Start QEMU with:
   -incoming "exec: lzop -dc /shared/vmocs/images/<name>/snapshot.mem.lzo"

3. VM resumes mid-execution — no BIOS, no kernel boot, no init

4. SSH is already running (was running when snapshot was saved)
```

**Requirements for snapshot restore:**
- All disks must use the same node-names (`drive0`, `drive1`, ...) as when the snapshot was captured
- QEMU version, machine type, and CPU flags must match between snapshot creation and restore
- COW overlays must be empty (no writes yet) — the saved memory state references the base images
- Cloud-init does NOT re-run (already ran during snapshot creation)

### Strategy 3: Direct kernel boot (medium, ~5-15s)

Skip BIOS/bootloader. Template specifies kernel directly (pcocc supports this via `Templates.py:43`):

```yaml
fast-ubuntu:
  image-dir: /shared/vmocs/images/fast-ubuntu/
  kernel: /shared/vmocs/kernels/vmlinuz-5.15
  kernel-args: "root=/dev/vda1 console=ttyS0 quiet"
```

QEMU args: `-kernel <path> -append <args>` — eliminates ~5-10s of BIOS/bootloader time.

### Strategy selection logic

```
if manifest has memory-snapshot and snapshot file exists:
    use Strategy 2 (snapshot restore, ~2-5s)
elif template has kernel:
    use Strategy 3 (direct kernel boot, ~5-15s)
else:
    use Strategy 1 (full boot, ~15-30s)
```

---

## COW Overlay Creation

Near-instant — writes a ~200KB qcow2 header, copies no data:

```bash
qemu-img create -f qcow2 \
  -b /shared/vmocs/images/ubuntu-24.04/os-disk.qcow2 \
  -F qcow2 \
  /run/vmocs/<job_id>/os-cow.qcow2
```

All VM writes go to the overlay. The base image is never touched. On job teardown the overlay is deleted — the node is clean for the next user.

---

## Memory Snapshot Creation (Admin Tool)

```bash
vmocs snapshot create <image-name>
vmocs snapshot create rocm-dev --memory 8192 --cores 4
```

Flow:
1. Boot VM from image using Strategy 1 (full boot)
2. Wait for SSH readiness
3. Optionally run post-boot preparation (sync disks, drop caches)
4. Save memory state via QMP (reused from pcocc `Hypervisor.py:1022-1034`):
   ```json
   {"execute": "migrate", "arguments": {"uri": "exec: lzop -c > snapshot.mem.lzo"}}
   ```
5. Poll `query-migrate` until complete
6. Save `snapshot.mem.lzo` into the image directory
7. Update `manifest.yaml` with `memory-snapshot: snapshot.mem.lzo`
8. Kill VM

Regenerate snapshots when base disk images change, QEMU is upgraded, or machine type changes.

---

## Image Store Layout

```
/shared/vmocs/images/
├── ubuntu-22.04/
│   ├── manifest.yaml
│   ├── os-disk.qcow2
│   └── snapshot.mem.lzo
├── rocm-dev/
│   ├── manifest.yaml
│   ├── os-disk.qcow2
│   ├── nvme-data.qcow2
│   └── snapshot.mem.lzo
└── centos-gpu/
    ├── manifest.yaml
    └── os-disk.qcow2
```

No registry server needed. Templates reference paths directly. Admins manage images as regular files on NFS/Lustre/GPFS.

---

## Official Cloud Image URLs

```bash
# Ubuntu 24.04 LTS
https://cloud-images.ubuntu.com/noble/current/noble-server-cloudimg-amd64.img

# Ubuntu 22.04 LTS
https://cloud-images.ubuntu.com/jammy/current/jammy-server-cloudimg-amd64.img

# Rocky Linux 9
https://download.rockylinux.org/pub/rocky/9/images/x86_64/Rocky-9-GenericCloud.latest.x86_64.qcow2

# CentOS Stream 9
https://cloud.centos.org/centos/9-stream/x86_64/images/CentOS-Stream-GenericCloud-9-latest.x86_64.qcow2

# Fedora 41
https://download.fedoraproject.org/pub/fedora/linux/releases/41/Cloud/x86_64/images/Fedora-Cloud-Base-Generic-41-1.4.x86_64.qcow2

# Debian 12
https://cloud.debian.org/images/cloud/bookworm/latest/debian-12-generic-amd64.qcow2
```

All official cloud images: ~350MB–1GB, support cloud-init, serial console configurable via kernel cmdline.

---

## Template Integration

The template declares **what kind of VM** (OS image, machine config, whether GPU passthrough is wanted). SLURM decides **how much** (which CPUs, how much memory, which GPUs and how many via GRES). GPU count is never set in the template.

### Option A: image-dir reference (recommended for multi-disk)

```yaml
# confs/templates.yaml
rocm-dev:
  image-dir: /shared/vmocs/images/rocm-dev/
  gpu: full          # passthrough type: full | sriov | null
                     # SLURM GRES decides how many GPUs are passed through
  bind-vcpus: true
  mount-points:
    home: { path: /home }
  ssh-user: root
  ssh-timeout: 120
```

### Option B: inline single-disk

```yaml
simple-ubuntu:
  image: /shared/vmocs/images/ubuntu-22.04/os-disk.qcow2
  snapshot: /shared/vmocs/images/ubuntu-22.04/snapshot.mem.lzo
  machine-type: q35
  disk-model: virtio-blk
  ssh-user: root
```

### Option C: inline multi-disk

```yaml
rocm-dev-inline:
  image: /shared/vmocs/images/rocm-dev/os-disk.qcow2
  extra-disks:
    - file: /shared/vmocs/images/rocm-dev/nvme-data.qcow2
      device: nvme
      snapshot: true
      serial: "VMOCS-NVME-0"
      cache: none
  snapshot: /shared/vmocs/images/rocm-dev/snapshot.mem.lzo
  gpu: full          # SLURM GRES decides count
```

### Template inheritance

```yaml
base-rocm:
  image-dir: /shared/vmocs/images/rocm-dev/
  machine-type: q35
  mount-points:
    home: { path: /home }
  ssh-user: root

rocm-gpu:
  inherits: base-rocm
  gpu: full          # full PF passthrough; count from SLURM --gres=gpu:N

rocm-sriov:
  inherits: base-rocm
  gpu: sriov         # SR-IOV VF passthrough; count from SLURM --gres=gpu:N
```

**Principle**: If the template says `gpu: full` but SLURM allocated 0 GPUs, vmocs skips passthrough. If SLURM allocated 4 GPUs, all 4 are passed through. The template never overrides SLURM's allocation.

### Template resolution order

```
1. If template has image-dir → read manifest.yaml from that directory
2. If template has image (string) → single primary disk, check for extra-disks
3. Resolve snapshot / memory-snapshot for fast boot strategy
4. Merge cloud-init: template user-data overrides manifest defaults
5. Apply inherited settings from parent templates (pcocc Templates.py:149-183)
```

---

## Filesystem Sharing

### 9p (default — zero daemon, simple)

```bash
-fsdev local,security_model=mapped-xattr,id=home0,path=/home/user
-device virtio-9p-pci,fsdev=home0,mount_tag=userhome
```

Cloud-init mounts in the VM:
```yaml
mounts:
  - [ userhome, /mnt/home, 9p, "trans=virtio,version=9p2000.L,msize=104857600", "0", "0" ]
```

### virtiofs (higher performance, requires virtiofsd)

```bash
/usr/libexec/virtiofsd \
  --socket-path=/run/vmocs/<job_id>/virtiofs.sock \
  --shared-dir=/home/user \
  --cache=auto

-chardev socket,id=vfs0,path=/run/vmocs/<job_id>/virtiofs.sock
-device vhost-user-fs-pci,chardev=vfs0,tag=userhome
-object memory-backend-memfd,id=mem,size=8G,share=on
-numa node,memdev=mem
```

### Performance comparison

| Metric | 9p | virtiofs |
|---|---|---|
| Sequential read | ~100–150 MB/s | ~250–700 MB/s |
| Setup | Zero (built into QEMU) | Requires virtiofsd daemon |
| Root required | No (mapped-xattr mode) | Yes (virtiofsd) |
| Best for | Job scripts, configs | Large data, I/O-heavy workloads |

---

## Image Import Sources

| Source | Command | Notes |
|---|---|---|
| Cloud image URL | `vmocs image import https://...img --name ubuntu-24.04` | Download official cloud image |
| Local qcow2 | `vmocs image import /path/to/image.qcow2 --name my-custom` | Copy into store |
| Docker image | `vmocs image import docker://ubuntu:24.04 --name ubuntu-docker` | Convert via d2vm |
| Saved VM | `vmocs image import /shared/saved/my-dev.qcow2 --name my-dev` | Re-import saved state |
| Packer build | `packer build template.pkr.hcl && vmocs image import output.qcow2 --name custom` | From Packer |

---

## Future: OCI Registry Distribution

```bash
# Push
oras push registry.example.com/vmocs/rocm-dev:1.0 \
  manifest.yaml:application/vnd.vmocs.manifest.v1+yaml \
  os-disk.qcow2:application/vnd.vmocs.disk.qcow2 \
  nvme-data.qcow2:application/vnd.vmocs.disk.qcow2 \
  snapshot.mem.lzo:application/vnd.vmocs.snapshot.lzo

# Pull (cached locally on compute node)
oras pull registry.example.com/vmocs/rocm-dev:1.0 -o /var/cache/vmocs/rocm-dev/
```

Can be added later without changing the manifest format.

---

## CLI Commands

```bash
vmocs image list
vmocs image show rocm-dev
vmocs image import https://cloud-images.ubuntu.com/.../noble-server-cloudimg-amd64.img --name ubuntu-24.04
vmocs image rm ubuntu-22.04
vmocs image verify rocm-dev
vmocs image init /shared/vmocs/images/my-vm/ --disk os:my-os.qcow2 --disk data:my-data.qcow2:nvme
vmocs snapshot create rocm-dev
vmocs snapshot create rocm-dev --memory 8192 --cores 4
```

---

## pcocc Code Reuse Map

| Feature | pcocc source | Reuse plan |
|---|---|---|
| COW snapshot creation | `Hypervisor.py:1374-1386` | Direct reuse |
| Block device cmdline (virtio/scsi/ide) | `Hypervisor.py:2363-2467` | Direct reuse, add NVMe |
| Memory snapshot restore (`-incoming`) | `Hypervisor.py:1337-1341` | Direct reuse |
| Memory snapshot save (`migrate` QMP) | `Hypervisor.py:1022-1034` | Direct reuse via QemuMonitor |
| Cloud-init ISO generation | `Hypervisor.py:1579-1646` | Direct reuse |
| Template inheritance | `Templates.py:149-183` | Reuse with new fields |
| Template loading | `Templates.py:56-372` | Reuse, add image-dir + extra-disks |

### Files to implement

- **`hypervisor.py`**: Add `qemu_gen_nvme_cmdline()`. Extend cmdline builder to iterate manifest disks. Add memory snapshot detection and `-incoming` support.
- **`templates.py`**: Add `image-dir`, `extra-disks`, `snapshot` fields. Add manifest loading logic.
- **`image.py`**: Add `create_snapshot()` helper. Add `parse_manifest(dir_path) -> dict`.
- **`cli.py`**: Add `vmocs image list|show|verify|import|rm` and `vmocs snapshot create` commands.

---

## pcocc vs vmocs: Image Management Comparison

*Analysis recorded April 2026 — basis for future vmocs image versioning work.*

### What is the same

Both projects use the exact same COW overlay strategy at the QEMU level:

```bash
# pcocc Hypervisor.py:1380-1384
# vmocs image.py:create_cow_overlay
qemu-img create -f qcow2 -F qcow2 -b <base_image> <overlay>
```

QEMU always receives the overlay path, never the base image. The base is always
read-only. On job teardown the overlay is deleted. This is identical in both.

### Where pcocc goes further

| Concern | pcocc | vmocs (current) |
|---|---|---|
| Image identity | URI: `[repo]:name[@revision]` e.g. `myrepo:rocm-dev@5` | Plain file path in template |
| Revisions | Full version history per image; each save creates a new numbered revision | None — `vmocs stop --save` overwrites a single file |
| Revision types | **Layer** (incremental, delta over previous) or **Full** (standalone) | Full only (flattened qcow2) |
| Image store | Content-addressed object store with SHA256 blob tracking | Directory of plain files |
| Image locking | MMP ref-counting lock per image — prevents two VMs from writing the same persistent disk | None |
| Remote fetch | Pulls from Docker/OCI registries via skopeo, caches locally | No |
| Image caching | SHA256-keyed cache; invalidated when content changes | No |

### Why locking matters

pcocc locks base images before booting (`Hypervisor.py:1096-1152`). When two
jobs start from the same base image simultaneously, they each get their own
COW overlay — but pcocc tracks a ref-count so a base image cannot be deleted or
overwritten while any VM is referencing it. vmocs has no equivalent; deleting
or replacing a base image while a VM is running will silently corrupt that VM's
reads once the COW overlay falls through to the (now-changed) backing file.

### Roadmap: image versioning for vmocs

The following can be added incrementally without breaking existing templates:

#### Phase 1 — Revision tracking (no store, just files)

Add a revision suffix to the `vmocs stop --save` output:
```
/shared/vmocs/images/rocm-dev/
  os-disk.qcow2          ← current (symlink)
  os-disk.r1.qcow2
  os-disk.r2.qcow2
  os-disk.r3.qcow2       ← latest
```

`vmocs image revisions <name>` lists them. Templates can pin a revision:
```yaml
rocm-dev:
  image: /shared/vmocs/images/rocm-dev/os-disk.r2.qcow2
```

#### Phase 2 — Image locking

Before creating the COW overlay, write a lock file:
```
/tmp/vmocs-locks/<normalized_image_path>.lock
```
containing `{ job_id, pid, timestamp }`. Increment a ref-count on open,
decrement on teardown. Refuse to delete or overwrite an image with ref-count > 0.
This mirrors pcocc's MMP mechanism (`Hypervisor.py:1096-1152`) but without etcd —
a local JSON file per image is sufficient for single-node use.

#### Phase 3 — Content-addressed store

Move images into a store keyed by SHA256 of the qcow2 content, matching pcocc's
`ObjectStore.py`. This enables deduplication across revisions (delta layers share
unchanged blocks) and makes remote distribution via OCI registries natural (see
the OCI section above).

#### Phase 4 — Remote fetch

Integrate skopeo (or oras for non-container images) to pull image revisions from
a registry, matching pcocc's `Image.py:683-763`. Compute nodes cache pulls under
`/var/cache/vmocs/`.

Phases 1 and 2 are low-risk additions that can be done without changing the
template format or breaking existing workflows.
