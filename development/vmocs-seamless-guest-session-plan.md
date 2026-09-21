# Seamless Guest Sessions

> **Implementation status:** The attached-session design is implemented. See
> the current [Slurm user guide](../docs/source/slurm/user-guide.rst) and
> [lifecycle documentation](../docs/source/internals/lifecycle.rst). Remaining
> limitations in this file are planning context, not the public reference.

## Goal

Make a Slurm task enter the VM directly instead of printing an SSH command that
the user must run from the compute node.

```bash
# Interactive guest shell
srun -n1 --pty --vm-image ubuntu bash -l

# Non-interactive guest command
srun -n1 --vm-image ubuntu hostname
```

The Slurm allocation remains the owner of the VM. Slurm continues to provide
resource allocation, cgroup containment, GPU/VFIO access, signal delivery,
stdio forwarding, accounting, and the wall-time limit. vmocs owns VM startup,
the SSH connection, guest-command status, and VM teardown.

## User-visible modes

### Attached mode (default)

The SPANK plugin prepends a vmocs supervisor to the task argv. The supervisor
boots the VM and runs the original Slurm command in the guest over SSH.

```text
srun --vm-image ubuntu bash -l
  -> vmocs run ... ubuntu -- bash -l
  -> boot QEMU
  -> SSH to the guest
  -> return the guest command's exit status to Slurm
  -> stop the VM
```

When stdin is a terminal, vmocs requests a guest PTY. With `srun --pty`, this
creates one terminal path from the user's terminal, through Slurm, through SSH,
to the guest shell.

### Unattached mode

`--vm-attach=none` retains the VM-as-job workflow. vmocs boots the VM, prints
connection information, and waits for QEMU. No automatic SSH session is opened.
The job ends on guest termination, cancellation, or its Slurm time limit.

This is an escape hatch for debugging and externally managed sessions; attached
mode is the primary workflow.

## Lifecycle contract

| Event | Result |
|---|---|
| Guest command or shell exits normally | Stop the VM and return its status to Slurm |
| `scancel`, wall-time expiry, SIGTERM | Request guest shutdown, then force QEMU down if necessary |
| Guest powers off | QEMU exits and the Slurm task ends |
| SSH transport fails while QEMU remains alive | Wait for SSH and reconnect within a bounded window |
| Guest reboot | Keep QEMU and the Slurm job alive; reconnect SSH after boot |

Reliable reboot handling requires templates to keep QEMU alive across guest
reset/power events (`-no-shutdown`) and a QMP event watcher that distinguishes
`RESET` from `SHUTDOWN`. On `RESET`, the supervisor reconnects. On `SHUTDOWN`,
it asks QEMU to quit so the Slurm task can finish.

## Slurm integration

Slurm 23.11 and newer provides `spank_prepend_task_argv()`. In
`slurm_spank_task_init`, the plugin prepends:

```text
vmocs [--config PATH] run [resource options] TEMPLATE --
```

to the task's original argv. This avoids shell quoting and allows Slurm to exec
the vmocs supervisor as the real task. QEMU is then forked from inside the task
cgroup and inherits the task's CPU, memory, and device constraints.

The initial implementation supports exactly one Slurm task. A later extension
can use `<job-id>.<step-id>.<global-task-id>` runtime identities for one VM per
task.

## vmocs implementation

Add a `vmocs run` command that:

1. Validates the template and launches the VM.
2. In attached mode, constructs an SSH command using the generated key and
   original task argv.
3. Requests a TTY only when local stdin is a TTY.
4. Preserves argument boundaries by POSIX-shell quoting the remote argv.
5. Returns the SSH/guest exit code (`255` remains a transport failure).
6. Cleans up QEMU and the runtime directory in a `finally` path.
7. In unattached mode, waits for QEMU without opening SSH.

The existing `vmocs launch` command remains available for standalone and manual
VM management.

## Environment and files

SSH does not automatically reproduce the host environment or filesystem. The
first version forwards a small, explicit allowlist of Slurm identity variables.
Working-directory execution is enabled only when the submit directory is
available at the same path in the guest (for example through virtio-fs).

Arbitrary host executables and Slurm's private batch-spool script are not
automatically present in the VM. Transparent `sbatch` support therefore needs a
follow-up staging mechanism or a documented shared-filesystem contract.

## Safety and compatibility

- Require Slurm 23.11 or newer for transparent argv wrapping.
- Reject multi-task steps initially instead of colliding on a job-ID runtime
  directory.
- Build subprocesses as argv arrays; do not invoke `/bin/sh -c` in the plugin.
- Keep `slurm_spank_exit` as best-effort cleanup after the supervisor's primary
  cleanup path.
- Preserve `--vm-save` only after shutdown and before deleting the overlay.

## Delivery phases

1. Add the `vmocs run` supervisor, attached/unattached modes, PTY forwarding,
   exit-status propagation, cleanup, and unit tests.
2. Change the C SPANK plugin to prepend the supervisor to the original task.
3. Add bounded SSH reconnection and QMP reset/shutdown event handling.
4. Add working-directory and selected-environment forwarding.
5. Add batch-script staging and, if needed, one-VM-per-task identities.
