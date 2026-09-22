.. _slurm-lifecycle:

Lifecycle and cleanup
=====================

The Slurm task owns the VM lifetime.  A normal command, interactive logout,
guest shutdown, Slurm cancellation, or time limit must all converge on QEMU
termination and runtime-directory cleanup.

SPANK lifecycle
---------------

``slurm_spank_init``
   Registers ``--vm-image``, ``--vm-save``, ``--vm-resume``, and
   ``--vm-attach`` in every
   context where the plugin is loaded.

``slurm_spank_init_post_opt``
   In allocator context, records the selected template in the job-control
   environment.

``slurm_spank_task_init``
   Runs remotely as the job user inside the task cgroup.  It rejects a task
   count other than one, derives the vmocs resource arguments, and prepends
   ``vmocs run`` to the task argv.

``slurm_spank_exit``
   Runs in remote context after the task.  It calls
   ``vmocs stop JOB_ID --if-exists`` as a best-effort fallback.  Cleanup errors
   from this hook do not replace the task's result.

Attached session
----------------

In the default ``--vm-attach=auto`` mode, ``vmocs run`` supervises QEMU and an
SSH child.  It returns the guest command's status, requests an orderly guest
poweroff, escalates through QMP if necessary, optionally saves the primary
disk, and removes the per-job runtime directory.

When the local input is a terminal, a transport failure while QEMU remains
alive causes vmocs to wait for SSH and reconnect.  This allows an interactive
session to survive a guest reboot.  A non-interactive command is never
replayed because doing so could repeat side effects.

QEMU is launched with ``-no-shutdown`` for supervised sessions.  A QMP
``RESET`` therefore leaves QEMU alive during reboot.  A guest ``SHUTDOWN``
event is converted into a QMP quit so the Slurm task can finish.

Unattached session
------------------

With ``--vm-attach=none``, vmocs does not start SSH.  It waits directly for
QEMU and installs signal handling that requests ACPI powerdown before
escalating to QMP quit and, if necessary, a process kill.  This mode exists for
manual or externally managed connections; attached mode is the normal Slurm
workflow.

Runtime state
-------------

The configured runtime directory contains one subdirectory per job ID.  A
typical directory contains:

==================  ========================================================
Path                Purpose
==================  ========================================================
``disk.qcow2``      Temporary overlay for the primary guest disk
``id_ed25519``      Per-launch SSH private key when key injection is enabled
``cloud-init.iso``  Generated cloud-init data in cloud-init boot mode
``qmp.sock``        QEMU Machine Protocol socket
``console.sock``    Guest serial console socket
``qemu.log``        QEMU standard output and error
``vm.json``         PID, job ID, template, SSH, and runtime metadata
==================  ========================================================

Normal cleanup removes this directory.  If saving the disk fails, vmocs keeps
the stopped overlay so an administrator can recover it manually.

Disk ownership
--------------

The template's base image is never modified by a normal launch.  vmocs writes
to a per-job COW overlay and discards it at cleanup unless ``--vm-save`` is
specified. Saving occurs after the guest is stopped and publishes the thin
overlay in an immutable checkpoint directory with a manifest and ``COMPLETE``
marker.

Template ``extra-disks`` are different: vmocs attaches them directly.  Their
writes persist independently and they are not included in VM saves or memory
snapshots.
