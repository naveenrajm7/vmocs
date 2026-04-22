.. _vmocs-list(1):

vmocs list
==========

Synopsis
--------

.. code-block:: text

   vmocs list

Description
-----------

List all VMs that have a runtime directory under ``runtime-dir`` (as configured
in ``vmocs.yaml``).  For each VM, vmocs checks whether the recorded QEMU PID is
still alive and reports the status accordingly.

Output columns:

``job_id``
   The job identifier passed to ``vmocs launch --job-id`` (or PID if
   omitted).

``pid``
   The QEMU process PID.

``template``
   The template name used to launch the VM.

``ssh_port``
   The host port forwarded to the guest SSH port (22).

``status``
   ``running`` if the QEMU process is alive, ``dead`` otherwise.

Examples
--------

::

   $ vmocs list
   job_id=12345  pid=98765  template=base-ubuntu  ssh_port=60222  status=running

See Also
--------

:manpage:`vmocs(1)`, :manpage:`vmocs-launch(1)`, :manpage:`vmocs-stop(1)`
