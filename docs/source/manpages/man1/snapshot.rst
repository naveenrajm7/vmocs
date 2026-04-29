.. _vmocs-snapshot(1):

vmocs snapshot
==============

Synopsis
--------

.. code-block:: text

   vmocs snapshot create [--cores N] [--memory MB] TEMPLATE_NAME SNAP_DIR

Description
-----------

Boot *TEMPLATE_NAME*, wait for SSH to become available, then save a combined
memory-and-disk snapshot to *SNAP_DIR*.  Subsequent launches that reference
this snapshot directory via the ``snapshot:`` field in ``templates.yaml``
restore in approximately 3–5 seconds instead of performing a full boot.

Subcommands
-----------

.. describe:: create TEMPLATE_NAME SNAP_DIR

   Create a snapshot from *TEMPLATE_NAME* and write it to *SNAP_DIR*.

   .. option:: --cores N

      vCPUs for the snapshot VM.  Default: ``2``.

   .. option:: --memory MB

      RAM in megabytes for the snapshot VM.  Default: ``2048``.

Workflow
--------

1. Create the snapshot::

      vmocs snapshot create base-ubuntu /tmp/ubuntu-snap

2. Add the ``snapshot:`` field to a template in ``templates.yaml``::

      snap-ubuntu:
        inherits: base-ubuntu
        snapshot: /tmp/ubuntu-snap

3. Launch with fast restore::

      vmocs launch snap-ubuntu

See Also
--------

:manpage:`vmocs(1)`, :manpage:`vmocs-launch(1)`, :manpage:`vmocs-templates.yaml(5)`
