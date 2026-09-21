# vmocs

vmocs — **V**irtual **M**achine **O**n **C**ompute through **S**LURM — is a lightweight QEMU wrapper and SLURM plugin, inspired by [pcocc (Peacock)](https://github.com/cea-hpc/pcocc), that allows users to run Virtual Machines through the `srun` command.

The name is pronounced "vimoks", after [ವಿಮೋಕ್ಷ](https://alar.ink/dictionary/kannada/english/%E0%B2%B5%E0%B2%BF%E0%B2%AE%E0%B3%8B%E0%B2%95%E0%B3%8D%E0%B2%B7) (Kannada for liberation, an untying, unbounding) — fitting for a tool built to free users to run kernel-space workloads without interfering with other users or jobs.

## Documentation

- [Getting started](docs/source/getting-started.rst)
- [Using vmocs directly](docs/source/direct/index.rst)
- [Using vmocs with Slurm](docs/source/slurm/index.rst)
- [CLI and configuration reference](docs/source/index.rst)

## Benefits

* Seamlessly execute the user's task in a virtual machine.
* Simple command-line interface that wraps QEMU
* Fast VM load with support for snapshots.
* Allows users to bring their own OS + kernel, even Windows to Slurm.
* Share VMs via templates

## Installation

### Prerequisites

```
qemu-system-x86_64   # QEMU (with KVM support)
qemu-img             # image inspection and COW overlay creation
genisoimage          # cloud-init ISO generation (cloud-init mode only)
ssh / ssh-keygen     # key generation and connectivity checks
lzop                 # memory snapshot compression (snapshot mode only)
swtpm                # TPM 2.0 emulation (only when tpm: true)
virtiofsd            # VirtioFS daemon (only when mount-points use type: virtio-fs)
```

Install on Debian/Ubuntu:
```bash
apt install qemu-system-x86 qemu-utils genisoimage openssh-client lzop swtpm virtiofsd
```

### Setup

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
