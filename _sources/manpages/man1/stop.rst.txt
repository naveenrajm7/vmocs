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

   Stop the guest and atomically publish a cold primary-disk checkpoint
   directory at *PATH*.  Resume it with ``vmocs run TEMPLATE --resume PATH``.

.. option:: --if-exists

   Exit successfully without output when the VM runtime metadata is already
   gone.  The Slurm plugin uses this for best-effort cleanup.

Examples
--------

Stop a VM::

   vmocs stop 12345

Stop and save disk changes::

   vmocs stop 12345 --save /shared/checkpoints/agent-step-1

See Also
--------

:manpage:`vmocs(1)`, :manpage:`vmocs-launch(1)`, :manpage:`vmocs-list(1)`
