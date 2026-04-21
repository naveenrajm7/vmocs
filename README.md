# vmocs

A lightweight QEMU/KVM wrapper for launching and managing virtual machines,
extracted and simplified from [pcocc](https://github.com/cea-hpc/pcocc).
No libvirt, no agents — just fork/exec QEMU with user-mode networking.

## Features

- **Two first-class boot modes** — cloud-init or vagrant convention
- **Fast restore** — memory snapshots bring a VM up in ~2s vs ~3min cold boot
- **Ephemeral keys** — per-launch ED25519 keypairs; vagrant insecure key is
  rotated out the moment the VM becomes reachable
- **Concurrent launches** — port allocation is collision-safe across parallel jobs
- **Disk save** — flatten a running VM's COW overlay to a standalone image

## Prerequisites

```
qemu-system-x86_64   # QEMU (with KVM support)
qemu-img             # image inspection and COW overlay creation
genisoimage          # cloud-init ISO generation (cloud-init mode only)
ssh / ssh-keygen     # key generation and connectivity checks
lzop                 # memory snapshot compression (snapshot mode only)
```

Install on Debian/Ubuntu:
```bash
apt install qemu-system-x86 qemu-utils genisoimage openssh-client lzop
```

## Setup

```bash
git clone <repo>
cd vmocs
python3 -m venv .venv
.venv/bin/pip install -e .
```

Verify:
```bash
.venv/bin/vmocs --version
```

## Configuration

Global config: `confs/vmocs.yaml`

```yaml
qemu-bin: /usr/bin/qemu-system-x86_64
runtime-dir: /tmp/vmocs          # per-job dirs created here

network:
  ssh-port-range: [60222, 60322] # ports scanned in order, first free wins
```

VM templates: `confs/templates.yaml`

```yaml
# cloud-init mode (default) — image must support cloud-init
base-ubuntu:
  image: /path/to/ubuntu-24.04.qcow2
  ssh-user: ubuntu
  ssh-timeout: 180

# vagrant mode — image must have vagrant user + insecure public key pre-installed
debian-vagrant:
  image: /path/to/box.img
  boot-mode: vagrant
  ssh-user: vagrant
  ssh-timeout: 120
```

### Boot mode contract

| mode | image requirement | how the key reaches the VM |
|------|------------------|---------------------------|
| `cloud-init` (default) | cloud-init installed in image | ISO cdrom injected at boot |
| `vagrant` | vagrant user + Vagrant insecure public key pre-installed | SSH in with insecure key post-boot, overwrite `authorized_keys` with ephemeral key |

In both modes vmocs generates a fresh ED25519 keypair per launch and stores
it under `runtime-dir/<job_id>/id_ed25519`.

---

## End-to-end tests

### 1. Inspect a template

```bash
.venv/bin/vmocs template list
.venv/bin/vmocs template show base-ubuntu
.venv/bin/vmocs template show debian-vagrant
```

Expected: `boot-mode` field shows `cloud-init` or `vagrant` respectively.

---

### 2. Single VM — cloud-init

```bash
.venv/bin/vmocs launch base-ubuntu --cores 2 --memory 1024
```

Expected output:
```
Launching VM from template 'base-ubuntu' (2 cores, 1024 MB)...
VM ready  job_id=<N>  ssh -i /tmp/vmocs/<N>/id_ed25519 -p 60222 ubuntu@127.0.0.1
```

Connect:
```bash
ssh -i /tmp/vmocs/<N>/id_ed25519 -o StrictHostKeyChecking=no -p 60222 ubuntu@127.0.0.1
```

What this verifies:
- Cloud-init ISO is generated and injected
- Ephemeral ED25519 key is written into the VM by cloud-init
- SSH becomes reachable within `ssh-timeout` seconds

Stop:
```bash
.venv/bin/vmocs stop <N>
```

---

### 3. Single VM — vagrant (with key rotation check)

```bash
.venv/bin/vmocs launch debian-vagrant --cores 2 --memory 1024
```

Expected output:
```
Launching VM from template 'debian-vagrant' (2 cores, 1024 MB)...
VM ready  job_id=<N>  ssh -i /tmp/vmocs/<N>/id_ed25519 -p 60222 vagrant@127.0.0.1
```

Note: the key path is `/tmp/vmocs/<N>/id_ed25519` (ephemeral), not the bundled
Vagrant insecure key — the rotation has already happened.

**Verify ephemeral key works:**
```bash
ssh -i /tmp/vmocs/<N>/id_ed25519 -o StrictHostKeyChecking=no \
    -p 60222 vagrant@127.0.0.1 \
    'whoami && cat ~/.ssh/authorized_keys'
```

Expected: logged in as `vagrant`, `authorized_keys` contains one ED25519 entry
(the ephemeral key) and nothing else.

**Verify insecure key is rejected:**
```bash
ssh -i lib/vmocs/keys/vagrant_insecure_key -o StrictHostKeyChecking=no \
    -o ConnectTimeout=3 -p 60222 vagrant@127.0.0.1 'echo should not reach here'
echo "exit: $?"
```

Expected: `Permission denied (publickey)` and `exit: 255`.

Stop:
```bash
.venv/bin/vmocs stop <N>
```

---

### 4. Concurrent launch — collision test

The critical real-world scenario: two jobs arrive on the same node simultaneously.
Port allocation must not collide and both VMs must become reachable.

```bash
.venv/bin/vmocs launch base-ubuntu   --cores 2 --memory 1024 > /tmp/vm1.out 2>&1 &
.venv/bin/vmocs launch debian-vagrant --cores 2 --memory 1024 > /tmp/vm2.out 2>&1 &
wait
cat /tmp/vm1.out
cat /tmp/vm2.out
```

Expected: two `VM ready` lines with **different ports** (e.g. 60222 and 60223).

SSH into both simultaneously to confirm no cross-contamination:
```bash
# Replace ports with what was actually assigned
ssh -i /tmp/vmocs/<N1>/id_ed25519 -o StrictHostKeyChecking=no \
    -p <PORT1> ubuntu@127.0.0.1 'echo "VM1: $(uname -n) / $(whoami)"' 2>/dev/null

ssh -i /tmp/vmocs/<N2>/id_ed25519 -o StrictHostKeyChecking=no \
    -p <PORT2> vagrant@127.0.0.1 'echo "VM2: $(uname -n) / $(whoami)"' 2>/dev/null
```

Expected:
```
VM1: vmocs / ubuntu
VM2: debian / vagrant
```

Stop both:
```bash
.venv/bin/vmocs stop <N1>
.venv/bin/vmocs stop <N2>
```

---

### 5. List running VMs

```bash
.venv/bin/vmocs list
```

Shows all VMs with active `vm.json` under `runtime-dir`, including job_id, pid,
template name, SSH port, and QEMU process status.

---

### 6. Save VM disk state

To export a running VM's disk as a standalone image (persists all changes made
inside the VM):

```bash
.venv/bin/vmocs stop <N> --save /path/to/output.qcow2
```

vmocs sends an ACPI shutdown so the guest flushes filesystems cleanly, then
flattens the COW overlay into a self-contained qcow2.

---

## Unit tests

```bash
.venv/bin/pytest tests/ -v
```

---

## Runtime layout

```
/tmp/vmocs/<job_id>/
  disk.qcow2       # COW overlay (backed by template image, discarded on stop)
  id_ed25519       # ephemeral SSH private key
  id_ed25519.pub
  cloud-init.iso   # cloud-init mode only
  qmp.sock         # QMP monitor socket
  console.sock     # serial console socket
  vm.json          # metadata: pid, port, key_path, boot_mode, ...
```
