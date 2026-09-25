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
configuration.

The configuration file is resolved in the following order:

1. ``--config PATH`` command-line option
2. ``VMOCS_CONF`` environment variable
3. ``confs/vmocs.yaml`` relative to the current working directory
4. ``/etc/vmocs/vmocs.yaml`` (system-wide fallback)

Fields
------

.. describe:: qemu-bin

   Path to the QEMU system binary.  Can be an absolute path or a bare name
   resolved via ``PATH``.

   Default: ``qemu-system-x86_64``

.. describe:: runtime-dir

   Directory where per-VM runtime state (PID, SSH port, QMP socket, ephemeral
   keys) is stored.  vmocs creates one subdirectory per running VM.

   Default: ``/var/run/vmocs``

.. describe:: templates

   Path to the system-wide templates file.  When set, vmocs loads VM templates
   from this path instead of looking next to ``vmocs.yaml``.

   The full resolution order for the templates path is:

   1. ``VMOCS_TEMPLATES`` environment variable (highest priority)
   2. This ``templates:`` key
   3. Same directory as ``vmocs.yaml`` (default fallback)

   User templates at ``~/.vmocs/templates.yaml`` are always merged on top,
   regardless of which path is used for system templates.

   Example::

      templates: /etc/vmocs/templates.yaml

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

.. describe:: sidecars

   Operator-owned settings for VM-scoped host processes.  ``startup-timeout``
   controls how long vmocs waits for Unix-socket readiness, and
   ``stop-timeout`` controls the graceful termination interval.  Both are in
   seconds and can also be overridden within an individual sidecar mapping.

   ``swtpm.binary``, ``virtiofsd.binary``, ``rocjitsu.binary``, and
   ``rocm-ernic.binary`` select installed executables.  The
   ``rocjitsu.profiles`` mapping is an allow-list from template profile names
   to readable rocJitsu JSON configuration files.  User templates cannot
   provide arbitrary executable paths or process arguments.

   Example::

      sidecars:
        startup-timeout: 60
        stop-timeout: 10
        swtpm:
          binary: /usr/bin/swtpm
        virtiofsd:
          binary: /usr/libexec/virtiofsd
        rocjitsu:
          binary: /opt/rocjitsu/bin/rocjitsu
          profiles:
            mi455x: /opt/rocjitsu/share/rocjitsu/configs/gfx1250_mi455x.json
        rocm-ernic:
          binary: /opt/rocm-ernic/bin/rocm-ernic

Example
-------

.. code-block:: yaml

   qemu-bin: /usr/bin/qemu-system-x86_64
   runtime-dir: /tmp/vmocs
   templates: /etc/vmocs/templates.yaml

   gpu-devices: []

   network:
     mode: user
     ssh-port-range: [60222, 60322]

   sidecars:
     startup-timeout: 60
     stop-timeout: 10

See Also
--------

:manpage:`vmocs(1)`, :manpage:`vmocs-templates.yaml(5)`
