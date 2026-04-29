.. _vmocs(1):

vmocs
=====

Synopsis
--------

.. code-block:: text

   vmocs [--config PATH] [--version] <command> [<args>]

Description
-----------

**vmocs** is a lightweight QEMU/KVM wrapper for launching and managing virtual
machines inside SLURM jobs.  It reads VM templates from a YAML configuration
file and handles SSH key injection, port allocation, QMP lifecycle management,
and optional memory snapshots for fast restores.

Global Options
--------------

.. option:: --config PATH

   Path to ``vmocs.yaml``.  Defaults to ``confs/vmocs.yaml`` relative to the
   working directory.

.. option:: --version

   Print the vmocs version and exit.

Commands
--------

.. describe:: launch

   Launch a VM from a template.  See :manpage:`vmocs-launch(1)`.

.. describe:: list

   List running VMs.  See :manpage:`vmocs-list(1)`.

.. describe:: stop

   Stop a running VM.  See :manpage:`vmocs-stop(1)`.

.. describe:: template

   List and inspect VM templates.  See :manpage:`vmocs-template(1)`.

.. describe:: snapshot

   Create VM snapshots for fast boot.  See :manpage:`vmocs-snapshot(1)`.

Files
-----

``confs/vmocs.yaml``
   Main configuration file.  See :manpage:`vmocs.yaml(5)`.

``confs/templates.yaml``
   VM template definitions.  See :manpage:`vmocs-templates.yaml(5)`.

See Also
--------

:manpage:`vmocs-launch(1)`, :manpage:`vmocs-list(1)`, :manpage:`vmocs-stop(1)`,
:manpage:`vmocs-template(1)`, :manpage:`vmocs-snapshot(1)`,
:manpage:`vmocs.yaml(5)`, :manpage:`vmocs-templates.yaml(5)`
