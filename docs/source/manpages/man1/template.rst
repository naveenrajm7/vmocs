.. _vmocs-template(1):

vmocs template
==============

Synopsis
--------

.. code-block:: text

   vmocs template list
   vmocs template show NAME

Description
-----------

Inspect the VM templates defined in ``templates.yaml``.

Subcommands
-----------

.. describe:: list

   Print the name (and optional description) of every defined template.

.. describe:: show NAME

   Print all settings for *NAME* after inheritance has been resolved.
   Useful for debugging what QEMU options a template will produce.

Examples
--------

::

   $ vmocs template list
   base-ubuntu
   snap-ubuntu
   windows-packer     Windows 11 GPU driver build — UEFI + Secure Boot ...

   $ vmocs template show snap-ubuntu
   boot-mode            cloud-init
   disk-cache           unsafe
   disk-model           virtio
   image                /tmp/ubuntu-24.04.qcow2
   machine-type         q35
   snapshot             /tmp/ubuntu-snap
   ssh-timeout          180
   ssh-user             ubuntu

See Also
--------

:manpage:`vmocs(1)`, :manpage:`vmocs-templates.yaml(5)`
