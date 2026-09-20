# VM Lifecycle in vmocs

## Process model

When `vmocs launch` is called, a VM-scoped manager starts the requested
sidecar processes before QEMU:

- **swtpm** (if `tpm: true`) — TPM 2.0 emulator, communicates with QEMU via a Unix socket.
- **virtiofsd** (if `mount-points` contains `virtio-fs` entries) — one process per mount.
- **rocJitsu** (if requested in `emulated-devices`) — vfio-user GPU emulator.
- **rocm-ernic** (if requested in `emulated-devices`) — vfio-user NIC emulator.

QEMU and every sidecar get their own process group/session while retaining the
caller's Slurm cgroup. vmocs preflights the full plan, starts sidecars
transactionally, waits for real Unix sockets, records verified process identities,
and stops process groups in reverse start order.

## Blocking mode (default — Slurm integration)

```
vmocs launch <template>
```

The launcher blocks on `os.waitpid(qemu_pid)`. The Slurm job's lifetime equals the
QEMU process lifetime. When Slurm sends SIGTERM (walltime expiry or `scancel`), the
vmocs SIGTERM handler:

1. Attempt 1 & 2: sends QMP `system_powerdown` (ACPI soft-off), reschedules SIGTERM
   in 10 s if QEMU hasn't exited.
2. Attempt 3: sends QMP `quit` to force QEMU exit; falls back to SIGKILL after 5 s.

The runtime directory (`/tmp/vmocs/<job_id>/`) is removed in the `finally` block after
QEMU exits and all sidecars have been stopped and reaped. Critical sidecar death
also causes the VM to stop.

## Detach mode (interactive use)

```
vmocs launch <template> --detach
```

The launcher returns immediately after printing "VM ready". QEMU and non-vfio-user
sidecars continue running as orphans under init and are recoverable through the
runtime manifest. Use `vmocs list` and `vmocs stop <job_id>` to manage them.
Templates with rocJitsu or rocm-ernic currently reject detach mode until a dedicated
detached supervisor is implemented.

## Guest reboot

A guest reboot (`shutdown /r` on Windows, `reboot` on Linux) sends an ACPI reset
signal. By default QEMU exits on this signal on some machine types (q35 in particular).

To keep the VM alive across reboots, add `-no-shutdown` to `custom-args` in the
template. With `-no-shutdown`, QEMU ignores the guest powerdown/reset signal at the
process level and the VM restarts internally.

**Slurm implication:** with `-no-shutdown`, a guest-initiated `shutdown /s` no longer
exits QEMU and therefore no longer ends the Slurm job. The job ends only when Slurm
sends SIGTERM (walltime / `scancel`). This is acceptable — Slurm, not the guest, owns
the job lifetime.

### Reboot and shutdown with `-no-shutdown`

`-no-shutdown` is an all-or-nothing flag. The two Slurm user scenarios cannot both be
satisfied at once with it:

| Template has `-no-shutdown`? | Guest reboot | Guest shutdown |
|---|---|---|
| No (default) | Job ends ✗ | Job ends ✓ |
| Yes | Job stays ✓ | Job stays ✗ |

To get both columns correct simultaneously, a **QMP event watcher** is needed (see
`TODO.md`). QEMU emits a `RESET` event for reboots and a `SHUTDOWN` event for
shutdowns. A watcher thread can call `mon.quit()` only on `SHUTDOWN`, giving:

| With QMP watcher + `-no-shutdown` | Guest reboot | Guest shutdown |
|---|---|---|
| | Job stays ✓ | Job ends ✓ |

The attached `vmocs run` path implements this QMP watcher: `RESET` preserves the VM
and `SHUTDOWN` ends it. The older bare `vmocs launch` blocking path still relies on
its signal/QMP shutdown loop.

## Why swtpm must be daemonized

swtpm is connected to QEMU via a persistent Unix socket for the entire VM lifetime.
If swtpm dies, QEMU crashes the next time Windows (or any TPM-aware OS) touches the
TPM — typically during a reboot or resume cycle.

Early versions used `subprocess.Popen` + `atexit.register(_try_kill, p)` to start
swtpm. This worked in blocking mode but broke in `--detach` mode: when the vmocs
launcher process exited, Python's atexit handlers ran and killed swtpm. QEMU kept
running (it doesn't notice the dead socket immediately), but the next TPM operation
— triggered by the Windows reboot cycle — caused QEMU to crash.

**Fix:** swtpm is now started with `start_new_session=True` (equivalent to `setsid()`),
placing it in its own session with no registered atexit handler. It survives launcher
exit and lives as long as QEMU does.

```
vmocs launcher ──fork──► QEMU (own pgid, reparented to init on --detach)
                └──fork──► swtpm (own session, start_new_session=True)
```

## Stopping a VM

```
vmocs stop <job_id>
```

Sends QMP `system_powerdown` (ACPI soft-off) and waits up to 60 s for the guest to
flush filesystems cleanly, then falls back to SIGTERM → SIGKILL. Removes the runtime
directory and (if `--save` was given) converts the COW overlay to a standalone qcow2.

After QEMU exits, vmocs explicitly sends TERM and then KILL if needed to every
verified persisted sidecar process group. PID start time and host boot ID checks
protect against signaling a reused PID.

## COW overlay and data persistence

Every VM boots from a copy-on-write (COW) overlay layered over the base image. The
base image is never written. The overlay accumulates all writes made inside the VM.

- **Across reboots:** the overlay persists. Data written before a reboot is visible
  after the reboot.
- **Across `vmocs stop`:** the overlay is deleted with the runtime directory. Changes
  are lost unless `vmocs stop --save <path>` is used to flatten the overlay into a new
  standalone qcow2 first.
- **Across `vmocs launch` runs:** each launch creates a fresh overlay from the same
  base image. Two separate job runs start from identical state.
