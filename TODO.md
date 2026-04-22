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

### Investigate VM boot regression after UEFI feature was added

BIOS VMs boot noticeably slower on the current UEFI-capable codebase compared to
the pre-UEFI baseline. Need to confirm this is a code regression (not hardware
variance) by comparing boot time across two checkpoints:

**Reproduction steps:**
1. `git checkout <pre-UEFI commit>` — launch a BIOS VM, record time-to-SSH.
2. `git checkout HEAD` — launch the same BIOS VM (no UEFI flags), record time-to-SSH.
3. Compare: if (2) is slower, the regression is in the UEFI-era code, not BIOS/UEFI
   firmware differences.

**Likely suspects:**
- Extra QEMU args unconditionally added for UEFI compatibility (e.g. `-machine` flags,
  `-drive` ordering changes, pflash devices).
- Template rendering now touches machine type or firmware paths even for non-UEFI VMs.
- `wait_for_ssh` / blocking-launch timing changes introduced alongside UEFI work.

Pre-UEFI baseline commit: `c19ae6f^` (commit before "feat: UEFI, Windows guest, TPM,
PCI passthrough, SSH hardening").
