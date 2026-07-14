# vmocs end-to-end tests

Manual test scenarios covering both boot modes, key rotation, and concurrent
launch safety. Run these after any change to `launch.py`, `hypervisor.py`, or
`monitor.py`.

## Prerequisites

Images used in these tests:

| Template | Image | Boot mode |
|---|---|---|
| `base-ubuntu` | `/tmp/ubuntu-24.04.qcow2` | cloud-init |
| `debian-vagrant` | `/var/lib/vmocs/images/debian.box` | vagrant |

Both templates are defined in `confs/templates.yaml`.

---

## 1. Inspect a template

```bash
.venv/bin/vmocs template list
.venv/bin/vmocs template show base-ubuntu
.venv/bin/vmocs template show debian-vagrant
```

Expected: `boot-mode` field shows `cloud-init` or `vagrant` respectively.

---

## 2. Single VM — cloud-init

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

## 3. Single VM — vagrant (with key rotation check)

```bash
.venv/bin/vmocs launch debian-vagrant --cores 2 --memory 1024
```

Expected output:
```
Launching VM from template 'debian-vagrant' (2 cores, 1024 MB)...
VM ready  job_id=<N>  ssh -i /tmp/vmocs/<N>/id_ed25519 -p 60222 vagrant@127.0.0.1
```

Note: key path is `/tmp/vmocs/<N>/id_ed25519` (ephemeral) — the Vagrant insecure
key was already rotated out before `vmocs launch` returned.

**Verify ephemeral key works:**
```bash
ssh -i /tmp/vmocs/<N>/id_ed25519 -o StrictHostKeyChecking=no \
    -p 60222 vagrant@127.0.0.1 \
    'whoami && cat ~/.ssh/authorized_keys'
```

Expected: logged in as `vagrant`, `authorized_keys` contains exactly one ED25519
entry (the ephemeral key) and nothing else.

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

## 4. Concurrent launch — port collision test

The critical real-world scenario: two jobs arrive on the same node simultaneously.
Port allocation must not collide and both VMs must become reachable independently.

```bash
.venv/bin/vmocs launch base-ubuntu    --cores 2 --memory 1024 > /tmp/vm1.out 2>&1 &
.venv/bin/vmocs launch debian-vagrant --cores 2 --memory 1024 > /tmp/vm2.out 2>&1 &
wait
cat /tmp/vm1.out
cat /tmp/vm2.out
```

Expected: two `VM ready` lines with **different ports** (e.g. 60222 and 60223).

SSH into both simultaneously to confirm no cross-contamination:
```bash
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

## 5. List running VMs

```bash
.venv/bin/vmocs list
```

Shows all VMs with active `vm.json` under `runtime-dir`, including job_id, pid,
template name, SSH port, and QEMU process status.

---

## 6. Save VM disk state

To export a running VM's disk as a standalone image (persists all changes made
inside the VM):

```bash
.venv/bin/vmocs stop <N> --save /path/to/output.qcow2
```

vmocs sends an ACPI shutdown so the guest flushes filesystems cleanly, then
flattens the COW overlay into a self-contained qcow2.

---

## 7. Slurm resource inheritance

Verifies that the VM receives the CPU count and memory that Slurm allocated,
with the expected headroom deduction for QEMU overhead.

Launch a VM via `srun` with explicit resources in the background, wait for it
to appear in `vmocs list`, then SSH in to inspect what the guest sees:

```bash
srun -c 4 --mem=4G --vm-image base-ubuntu sleep infinity > /tmp/vmocs-srun.out 2>&1 &

# Wait for VM to be ready
until vmocs list 2>/dev/null | grep -q running; do sleep 3; done
vmocs list
```

Once running, read the job ID and SSH port from `vmocs list`, then:

```bash
KEY=/tmp/vmocs/<N>/id_ed25519

ssh -i $KEY -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
    -p <PORT> ubuntu@127.0.0.1 'nproc && free -m'
```

Expected:
- `nproc` → `4`
- `free -m` total → ~3663 MB (4096 MB minus headroom: max(5%, 256 MB) = 256 MB
  deducted by the SPANK plugin before passing `--memory` to vmocs, plus ~177 MB
  consumed by the guest kernel/firmware)

Cancel when done:

```bash
scancel <JOBID>
```

---

## 8. Extra persistent disk

Verifies that an additional persistent disk is visible and writable inside the guest.

**Setup:** Create a blank disk image and a template that references it:

```bash
qemu-img create -f qcow2 /tmp/extra-disk-test.qcow2 1G
```

Template entry (add to `templates.yaml`):
```yaml
test-extra-disk:
  inherits: base-ubuntu
  extra-disks:
    - file: /tmp/extra-disk-test.qcow2
      device: virtio       # use 'nvme' if your QEMU build supports it
      cache: none
```

**Launch:**
```bash
.venv/bin/vmocs launch test-extra-disk --cores 2 --memory 2048 --detach
```

**Verify disk is visible:**
```bash
ssh -i /tmp/vmocs/<N>/id_ed25519 -o StrictHostKeyChecking=no \
    -p <PORT> ubuntu@127.0.0.1 'lsblk'
```

Expected: `vdb` (or `nvme0n1` for NVMe) appears as a 1G disk alongside `vda`.

**Verify disk is writable:**
```bash
ssh -i /tmp/vmocs/<N>/id_ed25519 -o StrictHostKeyChecking=no \
    -p <PORT> ubuntu@127.0.0.1 \
    'sudo mkfs.ext4 /dev/vdb && sudo mkdir -p /mnt/extra && sudo mount /dev/vdb /mnt/extra && echo "hello" | sudo tee /mnt/extra/test.txt && cat /mnt/extra/test.txt'
```

Expected: `hello` printed — disk formats, mounts, and reads/writes successfully.

**Verify persistence:** Stop the VM, relaunch from the same template, and confirm
the data written above survives (no COW overlay — writes go directly to the file):

```bash
.venv/bin/vmocs stop <N>
.venv/bin/vmocs launch test-extra-disk --cores 2 --memory 2048 --detach
ssh ... ubuntu@127.0.0.1 'sudo mount /dev/vdb /mnt/extra && cat /mnt/extra/test.txt'
```

Expected: `hello` — the file persisted across VM restarts.

Stop:
```bash
.venv/bin/vmocs stop <N>
```

---

## Runtime layout (for reference)

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
