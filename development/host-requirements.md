# vmocs Host Requirements

Baseline configuration required on every compute node that will run vmocs VMs.
Reference baseline: Ubuntu 24.04, kernel 6.8, and an x86_64 host with hardware
virtualization and IOMMU support.

---

## 1. CPU & Firmware

| Requirement | Value |
|---|---|
| Architecture | x86_64 with AMD-V or Intel VT-x |
| IOMMU | AMD-Vi (`amd_iommu=on`) or Intel VT-d — must be enabled in BIOS/UEFI |
| IOMMU passthrough mode | `iommu=pt` (kernel cmdline) |

**Kernel cmdline (minimum):**
```
amd_iommu=on iommu=pt
# Intel equivalent: intel_iommu=on iommu=pt
```

---

## 2. Kernel Modules

All must be loaded (add to `/etc/modules` or a `/etc/modules-load.d/vmocs.conf`):

```
kvm
kvm_amd        # or kvm_intel on Intel hosts
vfio
vfio_pci
vfio_pci_core
vfio_iommu_type1
iommufd
```

Verify with: `lsmod | grep -E 'kvm|vfio'`

---

## 3. Required Packages

```bash
apt install -y \
    qemu-system-x86     # >= 8.2; provides qemu-system-x86_64 \
    virtiofsd           # for host↔VM filesystem sharing \
    libvirt-daemon      # optional, for libvirt group / mgmt tooling
```

Binaries confirmed on reference host:
- `/usr/bin/qemu-system-x86_64` — QEMU 8.2.2
- `/usr/libexec/virtiofsd` and `/usr/lib/qemu/virtiofsd`

---

## 4. Device Node Permissions

### `/dev/kvm`
```
crw-rw---- 1 root kvm 10, 232  /dev/kvm
```
Users need to be in the **`kvm`** group.

### `/dev/vfio/vfio` (control node)
```
crw-rw-rw- 1 root root 10, 196  /dev/vfio/vfio
```
World-writable by default — no group membership needed.

### `/dev/vfio/<group_id>` (per-device IOMMU group nodes)
```
crw-rw---- 1 root kvm 510, N  /dev/vfio/<N>
```
Also owned by group **`kvm`** via udev rule. Users need **`kvm`** group.

**udev rule** — create `/etc/udev/rules.d/99-vfio.rules`:
```
SUBSYSTEM=="vfio", GROUP="kvm", MODE="0660"
```
Reload with: `udevadm control --reload-rules && udevadm trigger`

---

## 5. User Group Membership

Each user that will launch vmocs VMs must belong to:

| Group | Purpose |
|---|---|
| `kvm` | Access `/dev/kvm` and `/dev/vfio/<N>` |
| `libvirt` | Optional — only if using libvirt management |
| `render` | Access render nodes (GPU compute, optional) |
| `video` | Access display/GPU device nodes (optional) |

**Add a user:**
```bash
usermod -aG kvm,libvirt <username>
# logout/login or: newgrp kvm
```

**Verify:**
```bash
groups <username>   # must include kvm
```

---

## 6. PCI Passthrough — VFIO Binding

Devices to be passed through must:

1. Be in their own IOMMU group (no sharing with non-passthrough devices).
2. Have their driver replaced with `vfio-pci` **before** QEMU starts.

**Reference host — passthrough devices:**

| IOMMU Group | PCI Address | Device | Driver |
|---|---|---|---|
| 63 | `0000:03:00.0` | AMD Navi 31 RX 7900 XTX (GPU) | `vfio-pci` |
| 64 | `0000:03:00.1` | AMD Navi 31 HDMI/DP Audio | `vfio-pci` |

Each function is in its own IOMMU group — clean for unprivileged passthrough.

**Bind a device to vfio-pci at boot** via `/etc/modprobe.d/vfio.conf`:
```
options vfio-pci ids=1002:744c,1002:ab30
# replace with your device vendor:device IDs (lspci -nn)
```

Or bind dynamically:
```bash
echo "vfio-pci" > /sys/bus/pci/devices/0000:03:00.0/driver_override
echo "0000:03:00.0" > /sys/bus/pci/drivers/vfio-pci/bind
```

---

## 7. Hugepages (Optional but Recommended)

Not configured on the reference host (`HugePages_Total: 0`). For production, pre-allocate to reduce VM memory latency:

```bash
# /etc/sysctl.d/vmocs-hugepages.conf
vm.nr_hugepages = 1024   # adjust: 1024 × 2 MiB = 2 GiB reserved
```

Apply: `sysctl -p /etc/sysctl.d/vmocs-hugepages.conf`

---

## 8. vfio-user Emulated Devices

rocJitsu and rocm-ernic are userspace PCI device servers.  Unlike physical
VFIO passthrough, this path does **not** require a host BDF, an IOMMU group, or
binding hardware to `vfio-pci`.  It does require:

- a QEMU build whose `-device help` contains `vfio-user-pci`;
- native `rocjitsu` and/or `rocm-ernic` executables installed on every
  eligible compute node;
- a readable rocJitsu profile such as `gfx1250_mi455x.json`;
- `q35` and one shared `memory-backend-memfd` bound through
  `-machine ...,memory-backend=...`.

Ubuntu 24.04's QEMU 8.2 package does not provide the needed device in the
validated environment.  Use the packaged QEMU 11 build and verify the actual
binary on each node:

```bash
/opt/qemu-vfio/bin/qemu-system-x86_64 -device help | grep vfio-user-pci
```

The servers and QEMU run as the Slurm job user and inherit the job cgroup.
Their sockets and logs live under the per-VM runtime directory.

---

## 9. Ansible Playbook Checklist

Tasks for a `vmocs-host-baseline` role:

- [ ] Set kernel cmdline (`amd_iommu=on iommu=pt`) via grub/systemd-boot and reboot
- [ ] Install packages: `qemu-system-x86`, `virtiofsd`, `libvirt-daemon`
- [ ] Load kernel modules: `kvm_amd`, `vfio_pci`, `vfio_iommu_type1`, `iommufd`
- [ ] Deploy `/etc/udev/rules.d/99-vfio.rules` and reload udev
- [ ] Add compute users to `kvm` (and optionally `libvirt`, `render`) groups
- [ ] Deploy `/etc/modprobe.d/vfio.conf` with passthrough device IDs
- [ ] (Optional) Configure hugepages via sysctl

---

## 10. Quick Verification Script

Run on a candidate host to check readiness:

```bash
#!/bin/bash
echo "=== IOMMU ===" && grep -o 'iommu=[^ ]*\|amd_iommu=[^ ]*\|intel_iommu=[^ ]*' /proc/cmdline
echo "=== KVM device ===" && ls -la /dev/kvm
echo "=== VFIO devices ===" && ls -la /dev/vfio/
echo "=== Modules ===" && lsmod | grep -E 'kvm|vfio'
echo "=== User groups ===" && groups
echo "=== QEMU ===" && qemu-system-x86_64 --version
echo "=== virtiofsd ===" && (virtiofsd --version 2>&1 || find /usr -name virtiofsd 2>/dev/null | head -1)
```
