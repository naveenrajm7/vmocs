.. _getting-started:

Getting started
===============

This guide installs the ``vmocs`` command and launches one VM directly.  A
cluster administrator who is deploying the Slurm plugin should complete this
setup on the compute nodes, then continue with the
:doc:`slurm/admin-guide`.

Requirements
------------

vmocs requires Python 3.8 or newer and an x86-64 host with QEMU.  KVM is
strongly recommended; without access to ``/dev/kvm``, QEMU falls back to
software emulation.

Install the host tools appropriate for the features you use:

=========================  ================================================
Tool                       Purpose
=========================  ================================================
``qemu-system-x86_64``      Run the guest
``qemu-img``                Create overlays and saved images
``ssh`` and ``ssh-keygen``  Connect to the guest and create ephemeral keys
``genisoimage``             Inject SSH configuration in cloud-init mode
``lzop``                    Create and restore memory snapshots
``swtpm``                   Provide a TPM when a template sets ``tpm: true``
``virtiofsd``               Serve ``virtio-fs`` mount points
=========================  ================================================

For example, on Debian or Ubuntu::

   sudo apt install qemu-system-x86 qemu-utils genisoimage openssh-client \
       lzop swtpm virtiofsd

Install vmocs
-------------

Create a virtual environment and install the checkout in editable mode::

   git clone https://github.com/naveenrajm7/vmocs.git
   cd vmocs
   python3 -m venv .venv
   .venv/bin/pip install -e .
   .venv/bin/vmocs --version

The examples below assume that ``vmocs`` is on ``PATH``.  Activate the virtual
environment or replace ``vmocs`` with ``.venv/bin/vmocs``.

Configure vmocs
---------------

vmocs needs a global configuration file and at least one VM template.  When
run from the repository, the example files under ``confs/`` are discovered
automatically.  A system installation normally uses
``/etc/vmocs/vmocs.yaml`` and ``/etc/vmocs/templates.yaml``.

A minimal global configuration is::

   # /etc/vmocs/vmocs.yaml
   qemu-bin: /usr/bin/qemu-system-x86_64
   runtime-dir: /var/run/vmocs
   templates: /etc/vmocs/templates.yaml

Create a template that names an existing qcow2 image::

   # /etc/vmocs/templates.yaml
   base-ubuntu:
     description: Ubuntu base image
     image: /var/lib/vmocs/images/ubuntu-24.04.qcow2
     boot-mode: cloud-init
     ssh-user: ubuntu
     ssh-timeout: 180

The image must contain an SSH server.  In ``cloud-init`` mode it must also
support cloud-init.  See :manpage:`vmocs-templates.yaml(5)` for the complete
template schema and the alternative ``vagrant`` boot mode.

Launch the first VM
-------------------

Check that the template loads, then open a guest shell::

   vmocs template list
   vmocs launch base-ubuntu --ssh

vmocs creates a copy-on-write overlay, injects an ephemeral SSH key, starts
QEMU, and waits for SSH.  Ending the SSH session shuts the VM down and removes
its temporary runtime state; the base image is not modified.

Next steps
----------

* :doc:`direct/index` covers direct launches, attached commands, snapshots,
  and saving disk changes.
* :doc:`slurm/index` introduces the Slurm workflow for users.
* :doc:`slurm/admin-guide` covers cluster-wide plugin deployment.
