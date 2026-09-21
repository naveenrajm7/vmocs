.. _slurm-gpu-passthrough:

GPU passthrough
===============

The plugin maps GPUs allocated as Slurm generic resources (GRES) into the VM
through VFIO.  Users request ``--gres=gpu:1``; they do not select a host PCI
address themselves.

Two node configurations are supported:

================  ===========================================================
Configuration     Behavior
================  ===========================================================
Bare VFIO         A physical GPU function (PF) is bound to ``vfio-pci`` and
                  allocated exclusively to one job.
GIM SR-IOV        The AMD GIM driver creates virtual functions (VFs), with one
                  VF allocated to each job.
================  ===========================================================

In either case, Slurm grants the job access to a ``/dev/vfio/<group>`` device.
The plugin probes the numeric VFIO group devices, keeps only the groups the
job can open, resolves every PCI function in those IOMMU groups, and supplies
the resulting BDFs to ``vmocs run --pci``.

.. warning::

   VFIO configuration changes device ownership at the host level.  Prepare a
   maintenance and recovery path before rebinding production GPUs.  A physical
   GPU bound to ``vfio-pci`` is not available to its normal host driver.

Enable IOMMU and VFIO
---------------------

Enable IOMMU support in system firmware and on the kernel command line.  A
typical AMD configuration is::

   amd_iommu=on iommu=pt

For Intel hosts, use ``intel_iommu=on iommu=pt``.  Load the appropriate KVM
module together with ``vfio``, ``vfio_pci``, and ``vfio_iommu_type1``.  Verify
that the node exposes ``/dev/kvm`` and ``/dev/vfio/vfio`` before continuing.

Bind physical GPUs to vfio-pci
------------------------------

This step applies to bare VFIO nodes.  GIM manages VF creation and binding on
SR-IOV nodes, so follow the GIM deployment procedure instead.

Use ``driverctl`` for persistent binding across reboots.  First list the GPU
and any companion functions::

   lspci -d 1002: -D | grep -E 'VGA|Processing|Display|Audio'

Bind every function that belongs to the passthrough IOMMU group::

   sudo driverctl set-override 0000:03:00.0 vfio-pci
   sudo driverctl set-override 0000:03:00.1 vfio-pci

Verify the result::

   lspci -ks 0000:03:00.0 | grep 'Kernel driver'

The expected driver is ``vfio-pci``.  Inspect
``/sys/kernel/iommu_groups/<N>/devices`` and do not pass a group containing a
host device that cannot safely be dedicated to the guest.

Set VFIO device permissions
---------------------------

The job user must pass the normal file permission check before Slurm's device
cgroup can enforce the per-job allocation.  Create a udev rule for the numeric
VFIO group nodes::

   SUBSYSTEM=="vfio", KERNEL!="vfio", GROUP="kvm", MODE="0660"

Save it as ``/etc/udev/rules.d/99-vfio-kvm.rules``, then reload udev::

   sudo udevadm control --reload
   sudo udevadm trigger --subsystem-match=vfio

Add permitted job users to the ``kvm`` group.  New membership takes effect in
a new login session::

   sudo usermod -aG kvm USERNAME

Configure Slurm GRES
--------------------

After the devices are bound, generate the GRES ``File=`` list on each compute
node::

   python3 plugins/slurm/gres-conf-gen.py --check
   python3 plugins/slurm/gres-conf-gen.py

The check form prints the BDF, IOMMU group, and VFIO device mapping.  The
normal form emits a line similar to::

   Name=gpu File=/dev/vfio/61,/dev/vfio/62

Put the generated line in ``/etc/slurm/gres.conf``.  Use node-specific entries
or configuration management when mappings differ between nodes.

The corresponding node declaration must advertise an untyped GPU count that
matches this GRES definition::

   NodeName=gpu-node01 ... Gres=gpu:2

Do not declare a typed GPU such as ``gpu:gfx906:2`` while using an untyped
``Name=gpu`` line.  Enable Slurm device constraints so that only the VFIO group
files assigned to a job can be opened from its cgroup.

Restart the daemons after changing GRES configuration::

   sudo systemctl restart slurmctld slurmd
   sudo scontrol update NodeName=gpu-node01 State=resume

Configure the VM template
-------------------------

Templates used for AMD GPU passthrough must enable a dedicated PCIe root port
for every passed device::

   gpu-ubuntu:
     image: /var/lib/vmocs/images/ubuntu-gpu.qcow2
     machine-type: q35
     pci-root-port: true
     ssh-user: ubuntu

Without ``pci-root-port: true``, Q35 guests with AMD GPUs can fail with a QEMU
PCI interrupt assertion.  The guest image must contain a driver compatible
with the assigned physical GPU or VF.

Validate passthrough
--------------------

Request one GPU and inspect it inside the guest::

   srun -N1 -n1 --gres=gpu:1 --vm-image gpu-ubuntu lspci -nn

For an AMD compute image, use a guest tool such as ``rocm-smi`` for a
functional test::

   srun -N1 -n1 --gres=gpu:1 --vm-image gpu-ubuntu rocm-smi

Also verify that two concurrent jobs cannot receive the same VFIO device and
that jobs without a GPU allocation cannot open the numeric VFIO group devices.

Multi-function and ACS behavior
-------------------------------

Consumer GPUs often expose a display function and an HDMI/DisplayPort audio
function.

* With ACS disabled, both functions may share one IOMMU group.  One
  ``/dev/vfio/<group>`` GRES entry covers the group, and vmocs passes every
  function in that group to the VM.
* With ACS enabled, the functions may have separate groups.  The generator
  selects groups containing a display-class device and excludes an
  audio-only group.  Add a separate GRES deliberately if a guest also needs
  the isolated audio function.

The generator always includes GIM virtual functions because a VF has a
``physfn`` relationship and normally represents a single GPU function.
