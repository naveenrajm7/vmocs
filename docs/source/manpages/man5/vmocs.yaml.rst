.. _vmocs.yaml(5):

vmocs.yaml
==========

Synopsis
--------

``confs/vmocs.yaml``

Description
-----------

The main vmocs configuration file.  It is read at startup and controls global
settings such as the QEMU binary path, runtime directory, and network
configuration.  A custom path can be specified with ``vmocs --config PATH``.

Fields
------

.. describe:: qemu-bin

   Absolute path to the QEMU system binary.

   Default: ``/usr/bin/qemu-system-x86_64``

.. describe:: runtime-dir

   Directory where per-VM runtime state (PID, SSH port, QMP socket, ephemeral
   keys) is stored.  vmocs creates one subdirectory per running VM.

   Default: ``/tmp/vmocs``

.. describe:: gpu-devices

   List of PCI BDF addresses for GPU devices available for passthrough.
   Currently informational — passthrough is requested per-launch with
   ``vmocs launch --pci``.

   Default: ``[]``

.. describe:: network.mode

   Network backend for guest connectivity.  Currently only ``user`` (QEMU
   user-mode networking with port forwarding) is supported.

   Default: ``user``

.. describe:: network.ssh-port-range

   Two-element list ``[low, high]`` defining the inclusive port range from
   which vmocs allocates a host port for SSH forwarding into the guest.

   Default: ``[60222, 60322]``

Example
-------

.. code-block:: yaml

   qemu-bin: /usr/bin/qemu-system-x86_64
   runtime-dir: /tmp/vmocs

   gpu-devices: []

   network:
     mode: user
     ssh-port-range: [60222, 60322]

See Also
--------

:manpage:`vmocs(1)`, :manpage:`vmocs-templates.yaml(5)`
