.. _vmocs-launch(1):

vmocs launch
============

Synopsis
--------

.. code-block:: text

   vmocs launch [OPTIONS] TEMPLATE_NAME

Description
-----------

Launch a VM from *TEMPLATE_NAME*.  The template is resolved from
``templates.yaml`` (with inheritance), then QEMU is started with the
appropriate options.

After the VM is ready, vmocs prints the SSH connection string and — by default
— **blocks** until QEMU exits.  Blocking is required for correct SLURM job
containment: when SLURM sends ``SIGTERM`` to the job step, vmocs intercepts it
and performs a graceful shutdown sequence before exiting.

Options
-------

.. option:: --cores N

   Number of virtual CPUs.  Default: ``2``.

.. option:: --memory MB

   RAM in megabytes.  Default: ``2048``.

.. option:: --job-id N

   Job identifier used to name the runtime directory and look up the VM with
   ``vmocs list`` and ``vmocs stop``.  Defaults to the current process PID.

.. option:: --pci BDF

   Pass through a host PCI device to the VM.  *BDF* is the PCI bus–device–
   function address in the form ``DDDD:BB:DD.F`` (e.g. ``0000:03:00.0``).
   Repeat the flag to pass through multiple devices.

.. option:: --detach

   Return immediately after the VM is ready.  QEMU continues running in the
   background.  **Do not use inside a blocking SLURM job step** — the job
   will exit while the VM is still running.

.. option:: --ssh

   Open an interactive SSH session to the VM after boot.  vmocs shuts the VM
   down cleanly when the session ends.

Shutdown Behaviour
------------------

When vmocs receives ``SIGTERM`` or ``SIGINT`` (and ``--detach`` is not set):

1. Sends ACPI powerdown via QMP and reschedules ``SIGTERM`` in 10 seconds.
2. On the second signal, repeats step 1.
3. On the third signal, sends ``quit`` via QMP.  If QMP fails, force-kills
   QEMU after 5 seconds.

Examples
--------

Basic launch::

   vmocs launch base-ubuntu

Launch with 8 cores, 16 GB RAM, and immediate SSH::

   vmocs launch base-ubuntu --cores 8 --memory 16384 --ssh

Pass through a GPU::

   vmocs launch base-ubuntu --pci 0000:03:00.0 --pci 0000:03:00.1

Background launch (for scripting)::

   vmocs launch base-ubuntu --detach

See Also
--------

:manpage:`vmocs(1)`, :manpage:`vmocs-stop(1)`, :manpage:`vmocs-templates.yaml(5)`
