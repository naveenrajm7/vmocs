.. _vmocs-run(1):

vmocs run
=========

Synopsis
--------

.. code-block:: text

   vmocs run [OPTIONS] TEMPLATE_NAME [--] [COMMAND]...

Description
-----------

Launch *TEMPLATE_NAME* and, by default, execute *COMMAND* inside the guest over
SSH.  vmocs acts as the supervisor for the entire session: it starts QEMU,
forwards standard input and output, returns the guest command's exit status,
then shuts the VM down and removes its runtime directory.

When standard input is a terminal, vmocs requests a guest pseudo-terminal.
Interactive sessions can reconnect after a guest reboot or a temporary SSH
transport loss.  Non-interactive commands are not replayed after a transport
failure.

Options
-------

.. option:: --cores N

   Number of virtual CPUs.  Default: ``2``.

.. option:: --memory MB

   Guest RAM in megabytes.  Default: ``2048``.

.. option:: --job-id N

   Identifier used for the runtime directory.  Defaults to the vmocs process
   ID.  The Slurm plugin supplies the Slurm job ID.

.. option:: --pci BDF

   Pass a host PCI device through to the guest.  Repeat for multiple devices.

.. option:: --attach MODE

   ``auto`` (the default) executes *COMMAND* through SSH.  ``none`` starts the
   VM and waits for QEMU without opening SSH; in that mode *COMMAND* is not
   executed.

.. option:: --save PATH

   After guest shutdown, atomically publish a cold checkpoint directory at
   *PATH*.  Stage 1 captures primary-disk writes only, not RAM, GPU/device
   state, sidecar state, extra disks, or host-mounted content.

.. option:: --resume CHECKPOINT

   Cold-boot from a complete checkpoint created from the same template.  A new
   writable overlay is created, so the checkpoint remains immutable.

.. option:: --forward-env NAME

   Forward the named host environment variable to the guest command. Repeat
   for multiple variables. Only the name is placed in process arguments; the
   value is sent over a separate, non-interactive SSH connection to a private
   temporary guest file, which is deleted immediately before the command
   executes. POSIX guests use a ``0600`` file in a private ``/dev/shm``
   directory. Windows guests use versioned JSON in a directory protected by a
   non-inheriting ACL for the connected user. The command SSH connection keeps
   its original standard input and terminal behavior.

   Missing or invalid names are errors. Windows names that differ only by case
   are also rejected. At most 16 variables and 64 KiB of serialized data may
   be forwarded. Windows also rejects a final process environment block over
   32,767 UTF-16 code units, including inherited variables. Requires
   ``--attach auto`` and a guest command. Windows forwarding requires an
   explicit executable and cannot be combined with ``--save`` because deletion
   from the guest's persistent filesystem is not secure erasure.

   Windows templates currently require ``boot-mode: vagrant`` and
   ``insert-key: true``. vmocs overwrites the image's bootstrap credential,
   requires a QMP-confirmed full guest reboot, then verifies that the ephemeral
   per-launch key succeeds and the bootstrap key fails. If SSH loses the fixed
   pre-reboot success marker, vmocs proves the ephemeral key and forces a fresh
   QMP reset as the session-revocation boundary. It captures
   and pins the first SSH host key presented through the already-running QEMU
   forward (trust on first use) before any environment value is staged. Once
   Windows forwarding is selected, vmocs refuses a later checkpoint request
   for that VM.

   The Vagrant handoff assumes a trusted compute host: the publicly known
   bootstrap administrator key is reachable briefly before the confirmed
   reboot. Protection against a hostile local co-tenant requires injecting the
   per-launch key before ``sshd`` listens or isolating the QEMU host forward.

Examples
--------

Run a command and return its status::

   vmocs run base-ubuntu -- hostname

Forward selected bootstrap values without placing their values in argv::

   vmocs run base-ubuntu \
       --forward-env SERVICE_URL --forward-env CLAIM_TOKEN \
       -- /opt/service/bootstrap.sh

Open an interactive login shell::

   vmocs run base-ubuntu -- bash -l

Start an unattached VM and hold the supervisor open::

   vmocs run base-ubuntu --attach none

Save and resume an agent workspace::

   vmocs run base-ubuntu --save /shared/checkpoints/step-1 -- agent-step
   vmocs run base-ubuntu --resume /shared/checkpoints/step-1 -- agent-step-2

See Also
--------

:manpage:`vmocs(1)`, :manpage:`vmocs-launch(1)`,
:manpage:`vmocs-stop(1)`, :manpage:`vmocs-templates.yaml(5)`
