.. _slurm-troubleshooting:

Troubleshooting the Slurm plugin
================================

Start with the client-visible option, then follow the failure to the compute
node.  vmocs prints user-facing errors to the job's standard error; QEMU output
is stored under the configured runtime directory.

.. _troubleshoot-unrecognized-option:

``--vm-image`` is unrecognized
------------------------------

Run::

   srun --help | grep -- --vm-image

If no option appears:

* install the plugin on the login or submission node, not only the compute
  nodes;
* confirm that its ``plugstack.conf`` includes the vmocs fragment;
* inspect client and Slurm daemon logs for a plugin-load warning; and
* confirm the plugin was built against the deployed Slurm ``X.YY`` release
  series.

Because the packaged plugstack entry is ``optional``, an incompatible or
missing shared object produces a warning but does not prevent ordinary Slurm
commands from starting.

The option appears but the VM does not start
--------------------------------------------

Deploy the plugin, vmocs CLI, configuration, templates, images, and required
host tools on every eligible compute node.  Confirm that ``vmocs_path`` and
``vmocs_conf`` name paths valid on compute nodes and restart ``slurmd`` after
replacing the plugin.

``seamless guest sessions currently require --ntasks=1``
---------------------------------------------------------

Set the step task count explicitly::

   srun -n1 --vm-image base-ubuntu hostname

Increasing CPUs per task with ``-c`` does not increase the task count and is
the supported way to give the VM more vCPUs.

QEMU fails before SSH is ready
------------------------------

Find the job runtime directory and inspect ``qemu.log``::

   vmocs list
   less /var/run/vmocs/JOB_ID/qemu.log

Common causes include an unreadable image, an unavailable QEMU binary,
insufficient ``/dev/kvm`` permission, an invalid custom QEMU argument, a busy
SSH forwarding port range, or a VFIO device that cannot be attached.

The VM times out waiting for SSH
--------------------------------

Confirm that:

* the guest image runs an SSH server;
* ``ssh-user`` matches an account in the image;
* a cloud-init image uses ``boot-mode: cloud-init``;
* a Vagrant-compatible image uses ``boot-mode: vagrant``;
* the image accepts the selected key-injection method; and
* ``ssh-timeout`` is long enough for the image's first boot.

Inspect the serial socket or ``qemu.log`` for guest boot errors.  Increasing
the timeout does not fix a wrong SSH user or an image without cloud-init.

CPU or memory does not match the request
----------------------------------------

Use ``--cpus-per-task`` rather than only requesting a total job CPU count::

   srun -n1 --cpus-per-task=4 --mem=8G --vm-image base-ubuntu \
       bash -lc 'nproc; free -m'

The plugin reads ``SLURM_CPUS_PER_TASK``, ``SLURM_MEM_PER_NODE``, and
``SLURM_MEM_PER_CPU``.  It reserves memory for QEMU, and the guest consumes
additional memory internally.  See :ref:`slurm-resource-mapping`.

The allocated GPU is missing in the guest
-----------------------------------------

On the compute node, verify all of the following:

* the GPU or VF is bound to ``vfio-pci``;
* ``gres-conf-gen.py --check`` reports the expected BDF and group;
* Slurm's GRES entry uses ``File=/dev/vfio/<group>``;
* device constraints allow the allocated group and deny unallocated groups;
* the job user can open the group node through its ``kvm`` group membership;
* the template sets ``pci-root-port: true`` for AMD GPUs; and
* the guest contains the appropriate device driver.

Test the allocation from inside a job before involving QEMU::

   srun -n1 --gres=gpu:1 ls -l /dev/vfio

Permission denied for KVM or VFIO
---------------------------------

Check ``/dev/kvm``, ``/dev/vfio/vfio``, and the allocated numeric VFIO group.
The job user's login session must include the ``kvm`` group, and the udev rule
must grant group access to numeric VFIO devices.  File permissions and Slurm's
device cgroup are both required.

QEMU reports a PCI IRQ assertion for an AMD GPU
------------------------------------------------

Set the template field::

   pci-root-port: true

This gives every passed function a dedicated Q35 PCIe root port and avoids the
interrupt-routing failure seen when affected AMD devices are attached
directly.

A batch script is not found inside the guest
---------------------------------------------

Slurm's private spool copy of a submitted script is a host file and is not
automatically staged into the VM.  Use a host-side batch script containing an
explicit ``srun --vm-image ... /shared/path/job.sh`` step, and expose that
shared path inside the guest through the template.  See
:ref:`slurm-user-guide` for an example.

A cancelled job leaves runtime state
------------------------------------

List recorded VMs and ask vmocs to clean up the job ID::

   vmocs list
   vmocs stop JOB_ID --if-exists

Check that the plugin exit hook can resolve the same vmocs binary and
configuration as the task-init hook.  If QEMU is still alive, inspect Slurm's
signal handling and ``qemu.log`` before removing runtime files manually.
