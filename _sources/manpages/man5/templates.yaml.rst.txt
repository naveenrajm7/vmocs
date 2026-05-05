.. _vmocs-templates.yaml(5):

templates.yaml
==============

Synopsis
--------

``/etc/vmocs/templates.yaml``, ``~/.vmocs/templates.yaml``

Description
-----------

Defines named VM templates.  Each template is a YAML mapping of settings.
Templates can inherit from another template via the ``inherits:`` field;
child settings override parent settings.

Template Locations
------------------

vmocs loads templates from two sources, in order:

1. **System templates** (required) — the path is resolved as follows:

   a. ``VMOCS_TEMPLATES`` environment variable
   b. ``templates:`` key in ``vmocs.yaml``
   c. Same directory as ``vmocs.yaml`` (fallback)

2. **User templates** (optional) — ``~/.vmocs/templates.yaml``.
   Loaded silently if present; skipped without error if absent.
   When user templates are found, vmocs prints a notice to stderr::

      Note: merging 2 user template(s) from /home/user/.vmocs/templates.yaml

Template names must be unique across both files.  Defining the same name in
both system and user templates is an error.

Cluster deployments
~~~~~~~~~~~~~~~~~~~

For a shared cluster, set the system templates path in ``vmocs.yaml``::

   templates: /cluster/vmocs/config/templates.yaml

All users on the cluster then share those templates automatically.  Individual
users can add personal templates in ``~/.vmocs/templates.yaml`` without
modifying the cluster file.

To override the path at runtime without editing ``vmocs.yaml``::

   VMOCS_TEMPLATES=/other/path/templates.yaml vmocs template list

Fields
------

.. describe:: inherits

   Name of a parent template.  All parent settings are copied into the child;
   any field defined in the child overrides the corresponding parent field.

.. describe:: description

   Human-readable description shown by ``vmocs template list``.

.. describe:: image

   Absolute path to the base ``qcow2`` disk image.  vmocs creates a temporary
   overlay on top of this image at launch time so the original is never
   modified.

.. describe:: image-dir

   Directory containing ``qcow2`` images.  vmocs uses the first ``*.qcow2``
   file found.  Mutually exclusive with ``image``; ``image`` takes precedence
   when a snapshot is not in use.

.. describe:: boot-mode

   How the guest is configured for SSH access:

   ``cloud-init`` *(default)*
     vmocs generates an ephemeral ED25519 key pair, writes a cloud-init
     ISO (``meta-data`` + ``user-data``), and attaches it as a CD-ROM drive.
     Requires a cloud-init-enabled guest image.

   ``vagrant``
     vmocs injects the standard Vagrant insecure public key and then
     rotates it to an ephemeral key after first SSH contact.  Works for
     Windows, BSD, or any image following the Vagrant convention.

.. describe:: ssh-user

   Username for the initial SSH connection.  Default: ``root``.

   Typical values: ``ubuntu`` (cloud-init images), ``vagrant`` (Vagrant boxes).

.. describe:: ssh-timeout

   Maximum seconds to wait for SSH to become available after QEMU starts.
   Default: ``120``.

.. describe:: qemu-bin

   Override the global ``qemu-bin`` path for this template.  Useful when
   different templates require different QEMU versions.

.. describe:: machine-type

   QEMU machine type passed to ``-machine``.  Default: ``q35``.
   Examples: ``q35``, ``pc-q35-8.2``.

.. describe:: disk-model

   QEMU disk controller model.  Valid values: ``virtio`` *(default)*,
   ``virtio-scsi``, ``ide``, ``nvme``.

.. describe:: disk-cache

   QEMU disk cache mode.  Valid values: ``unsafe`` *(default)*,
   ``writeback``, ``none``, ``writethrough``, ``directsync``.

.. describe:: firmware

   Path to a UEFI firmware image (``OVMF_CODE``).  When set, vmocs passes
   ``-drive if=pflash`` for both the code and vars images, enabling UEFI boot.

.. describe:: firmware-vars-template

   Path to the UEFI variable store template (``OVMF_VARS``).  vmocs copies
   this file to the runtime directory at launch so each VM gets its own
   writable variable store.

.. describe:: snapshot

   Path to a snapshot directory created by ``vmocs snapshot create``.  When
   set, vmocs restores the VM from the saved memory and disk state instead of
   performing a full boot (~3–5 s vs. full boot time).

.. describe:: display

   Guest display backend.  Default: ``none`` (headless).  Set to ``vnc`` to
   enable a VNC server.

.. describe:: vnc-port

   VNC display port number.  Only used when ``display: vnc``.

.. describe:: clock-offset

   Guest clock offset: ``utc`` (default) or ``localtime``.  Use
   ``localtime`` for Windows guests.

.. describe:: hyperv

   Boolean.  Enable Hyper-V enlightenments (``-cpu ...,hv_*``).  Improves
   performance for Windows guests.

.. describe:: smm

   Boolean.  Enable System Management Mode (required for Secure Boot with OVMF
   ``*.ms.fd`` firmware).

.. describe:: tpm

   Boolean.  Attach a software TPM 2.0 device (``swtpm``).  Required for
   Windows 11.

.. describe:: insert-key

   Boolean.  Default: ``true``.  Whether to inject the SSH key via cloud-init
   or Vagrant mechanism.  Set to ``false`` when using a pre-provisioned static
   SSH key.

.. describe:: ssh-key

   Absolute path to a pre-existing private key to use for SSH instead of
   generating an ephemeral key.  Only used when ``insert-key: false``.

.. describe:: pci-root-port

   Boolean.  Add a PCI root port device per PCI passthrough device.  Needed
   for AMD GPUs and any device sensitive to PCIe topology.

.. describe:: extra-hostfwd

   List of extra QEMU ``hostfwd`` entries appended to the user-mode network
   device.  Each entry uses the QEMU syntax
   ``protocol::hostport-:guestport``.  Default: ``[]``.

   Example::

      extra-hostfwd:
        - tcp::3389-:3389
        - tcp::5985-:5985

.. describe:: pci-roms

   Mapping of ``vendor:device`` PCI ID pairs to ROM file paths.  When a PCI
   passthrough device matches a key in this mapping, vmocs passes
   ``romfile=<path>`` to the ``vfio-pci`` device.  Only applied to
   display-class (``0x03xx``) devices.  Default: ``{}``.

   Required for APU/iGPU passthrough where the GPU has no on-board vBIOS ROM
   and QEMU must supply one externally.

   Example::

      pci-roms:
        1002:1586: /cluster/vmocs/roms/vbios_1002_1586.bin

.. describe:: gpu

   GPU passthrough mode: ``full`` (whole-device VFIO), ``sriov``
   (SR-IOV virtual function), or omit for no GPU passthrough.

.. describe:: bind-vcpus

   Boolean.  Pin guest vCPUs to host CPUs.  Default: ``false``.

.. describe:: user-data

   Custom cloud-init user-data string.  When set, vmocs uses this instead of
   generating its own user-data.  Only applies to ``cloud-init`` boot mode.

.. describe:: kernel

   Path to a Linux kernel image for direct kernel boot (bypasses BIOS/UEFI).
   When set, vmocs passes ``-kernel`` and a default ``-append`` with
   ``console=ttyS0 root=/dev/vda1`` unless ``custom-args`` contains
   ``-append``.

.. describe:: mount-points

   Map of host directories to share with the guest.  Each entry has a name
   (used as the mount tag inside the guest), a ``path``, and an optional
   ``type``.

   Supported types:

   ``virtio-9p`` *(default)*
     Built into QEMU, no extra daemon required.  Supports ``readonly: true``.

   ``virtio-fs``
     Uses ``virtiofsd`` as a sidecar daemon.  Higher performance but requires
     shared memory backing and does not support read-only mounts.

   Full syntax::

      mount-points:
        home:
          path: /home/user
          type: virtio-fs
        scratch:
          path: /tmp/scratch
          type: virtio-9p
          readonly: true

   Shorthand (defaults to ``virtio-9p``)::

      mount-points:
        home: /home/user

.. describe:: extra-disks

   List of additional persistent disks to attach to the VM.  Each entry is a
   mapping with the following keys:

   ``file`` *(required)*
     Absolute path to an existing ``qcow2`` or ``raw`` disk image.  The file
     must exist before launch.  vmocs attaches it directly — no COW overlay is
     created, so writes are persistent and the user owns the file's lifecycle.

   ``device`` *(optional)*
     QEMU disk controller model for this disk: ``virtio``, ``virtio-scsi``,
     ``ide``, or ``nvme``.  Defaults to the template's ``disk-model``.

     .. note::

        The ``nvme`` device requires full upstream QEMU
        (``qemu-system-x86_64``).  RHEL/CentOS ``qemu-kvm`` may not include
        it.  Check with ``qemu -device help | grep nvme``.

   ``cache`` *(optional)*
     Cache mode for this disk.  Same values as ``disk-cache``.  Defaults to
     the template's ``disk-cache``.

   ``serial`` *(optional)*
     Serial number string for the disk device.  Only meaningful for ``nvme``
     devices.  Defaults to ``VMOCS-NVME-<index>``.

   Extra disks appear as sequential block devices inside the guest (``vdb``,
   ``vdc``, … for virtio; ``/dev/nvme0n1``, ``/dev/nvme1n1`` for NVMe).  On
   teardown, extra disk files are left untouched.

   Extra disks are **not included in snapshots or VM saves** — only the
   primary OS disk (``drive0``) is managed by vmocs.

   Example::

      extra-disks:
        - file: /shared/data/scratch.qcow2
          device: nvme
          cache: none
          serial: SCRATCH-0
        - file: /home/user/data.qcow2
          device: virtio
          cache: writeback

.. describe:: custom-args

   List of raw QEMU arguments appended verbatim to the QEMU command line.
   Use for options that have no dedicated template field.

Examples
--------

Minimal cloud-init template::

   base-ubuntu:
     image: /tmp/ubuntu-24.04.qcow2
     machine-type: q35
     disk-model: virtio
     disk-cache: unsafe
     ssh-user: ubuntu
     ssh-timeout: 180

Inheritance and fast restore::

   snap-ubuntu:
     inherits: base-ubuntu
     snapshot: /tmp/ubuntu-snap

Extra persistent disk::

   rocm-nvme:
     inherits: base-ubuntu
     extra-disks:
       - file: /shared/data/scratch.qcow2
         device: nvme
         cache: none
         serial: SCRATCH-0

UEFI Windows guest with TPM::

   windows-vm:
     image: /tmp/win11.qcow2
     boot-mode: vagrant
     ssh-user: vagrant
     ssh-timeout: 300
     machine-type: pc-q35-8.2
     disk-model: virtio-scsi
     firmware: /usr/share/OVMF/OVMF_CODE_4M.ms.fd
     firmware-vars-template: /usr/share/OVMF/OVMF_VARS_4M.ms.fd
     clock-offset: localtime
     hyperv: true
     smm: true
     tpm: true

See Also
--------

:manpage:`vmocs(1)`, :manpage:`vmocs-template(1)`, :manpage:`vmocs.yaml(5)`
