# vmocs TODO

## Pending

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
