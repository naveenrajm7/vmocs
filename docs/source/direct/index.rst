.. _direct-use:

Using vmocs directly
====================

The standalone CLI is useful for preparing images, validating templates, and
running a VM without Slurm.  It uses the same configuration and lifecycle code
as the Slurm plugin.

Inspect templates
-----------------

List the available templates or inspect the fully resolved settings of one
template::

   vmocs template list
   vmocs template show base-ubuntu

User templates from ``~/.vmocs/templates.yaml`` are merged with the system
templates.  Template names must be unique across both files.

Choose a launch mode
--------------------

Use ``launch`` when you want to manage or enter a VM manually::

   vmocs launch base-ubuntu --cores 4 --memory 8192 --ssh

Without ``--ssh``, vmocs prints an SSH command and waits for the VM to exit.
Use ``--detach`` to return immediately and manage the VM with ``list`` and
``stop``::

   vmocs launch base-ubuntu --detach
   vmocs list
   vmocs stop 12345

Use ``run`` when a command should execute inside the guest and the VM should
be cleaned up when that command finishes::

   vmocs run base-ubuntu -- uname -a

``run`` is also the supervisor used by the Slurm plugin.  It forwards a
terminal for interactive input, returns the guest command's exit status, and
shuts the VM down after the session.

Save disk changes
-----------------

The primary guest disk normally uses a disposable copy-on-write overlay.  To
flatten a detached VM's modified overlay into a standalone qcow2 image::

   vmocs stop 12345 --save /shared/images/ubuntu-modified.qcow2

For an attached command, save when the command finishes::

   vmocs run base-ubuntu --save /shared/images/ubuntu-modified.qcow2 -- bash

Only the primary OS disk is included.  Files configured through the
``extra-disks`` template field are attached directly and retain their own
changes independently.

Create a fast-start snapshot
----------------------------

Create a combined memory and disk snapshot::

   vmocs snapshot create base-ubuntu /shared/snapshots/ubuntu-ready

Then reference it from a template::

   ubuntu-ready:
     inherits: base-ubuntu
     snapshot: /shared/snapshots/ubuntu-ready

The next launch restores the saved machine state instead of performing a full
guest boot.

Command reference
-----------------

See :doc:`../cli` for exact command syntax and :doc:`../conf` for all
configuration fields.
