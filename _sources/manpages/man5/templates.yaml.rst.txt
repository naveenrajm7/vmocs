.. _vmocs-templates.yaml(5):

templates.yaml
==============

Synopsis
--------

``confs/templates.yaml``

Description
-----------

Defines named VM templates.  Each template is a YAML mapping of settings.
Templates can inherit from another template via the ``inherits:`` field;
child settings override parent settings.

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

   Username for the initial SSH connection.

   Typical values: ``ubuntu`` (cloud-init images), ``vagrant`` (Vagrant boxes).

.. describe:: ssh-timeout

   Maximum seconds to wait for SSH to become available after QEMU starts.
   Default: ``180``.

.. describe:: machine-type

   QEMU machine type passed to ``-machine``.  Examples: ``q35``,
   ``pc-q35-8.2``.

.. describe:: disk-model

   QEMU disk controller model.  Examples: ``virtio``, ``virtio-scsi``.

.. describe:: disk-cache

   QEMU disk cache mode (``-drive cache=``).  Examples: ``unsafe``,
   ``writeback``, ``none``.

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

   Guest display backend.  ``vnc`` enables a VNC server; omit for no display
   (headless).

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

   Boolean.  Whether to inject the SSH key via cloud-init or Vagrant mechanism.
   Set to ``false`` when using a pre-provisioned static SSH key.

.. describe:: ssh-key

   Absolute path to a pre-existing private key to use for SSH instead of
   generating an ephemeral key.  Only used when ``insert-key: false``.

.. describe:: pci-root-port

   Boolean.  Add a PCI root port device.  Needed for hot-plug or certain
   PCI passthrough configurations.

.. describe:: mount-points

   Map of host directories to expose to the guest via ``virtio-fs``.  Each
   entry has a name (used as the mount tag), a ``path``, and a ``type``
   (currently ``virtio-fs``).

   Example::

      mount-points:
        home:
          path: /home/user
          type: virtio-fs

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
