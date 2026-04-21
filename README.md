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

## Testing

Unit tests:
```bash
.venv/bin/pytest tests/ -v
```

End-to-end test scenarios (boot modes, key rotation, concurrent launch):
see [`tests/e2e.md`](tests/e2e.md).
