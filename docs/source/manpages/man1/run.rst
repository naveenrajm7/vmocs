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

   After guest shutdown, flatten changes to the primary disk into a standalone
   qcow2 image at *PATH*.

Examples
--------

Run a command and return its status::

   vmocs run base-ubuntu -- hostname

Open an interactive login shell::

   vmocs run base-ubuntu -- bash -l

Start an unattached VM and hold the supervisor open::

   vmocs run base-ubuntu --attach none

See Also
--------

:manpage:`vmocs(1)`, :manpage:`vmocs-launch(1)`,
:manpage:`vmocs-stop(1)`, :manpage:`vmocs-templates.yaml(5)`
