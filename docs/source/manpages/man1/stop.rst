.. _vmocs-stop(1):

vmocs stop
==========

Synopsis
--------

.. code-block:: text

   vmocs stop [--save PATH] [--if-exists] JOB_ID

Description
-----------

Stop the running VM identified by *JOB_ID*.  vmocs sends a graceful shutdown
via QMP and cleans up the runtime directory.

Options
-------

.. option:: --save PATH

   Before stopping the VM, flatten the VM disk (including all changes made
   inside the guest) into a new standalone ``qcow2`` image at *PATH*.  The
   resulting image can be used directly as a template ``image:`` for future
   jobs — equivalent to a "save disk" operation.

.. option:: --if-exists

   Exit successfully without output when the VM runtime metadata is already
   gone.  The Slurm plugin uses this for best-effort cleanup.

Examples
--------

Stop a VM::

   vmocs stop 12345

Stop and save disk changes::

   vmocs stop 12345 --save /tmp/ubuntu-modified.qcow2

See Also
--------

:manpage:`vmocs(1)`, :manpage:`vmocs-launch(1)`, :manpage:`vmocs-list(1)`
