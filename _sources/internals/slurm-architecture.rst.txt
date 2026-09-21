.. _slurm-architecture:

Slurm integration architecture
==============================

vmocs separates resource allocation from VM lifecycle management.  Slurm
selects the node and grants CPU, memory, time, and devices.  The SPANK plugin
translates that allocation into ``vmocs run`` arguments.  vmocs starts QEMU
inside the task and connects the original task command to the guest over SSH.

Components
----------

=======================  ===================================================
Component                Responsibility
=======================  ===================================================
Slurm client             Parse job and vmocs options and submit the request
Slurm controller         Allocate nodes and scheduled resources
``slurmstepd``            Create the task cgroup and invoke remote SPANK hooks
``spank_vmocs.so``        Map the allocation and prepend the vmocs supervisor
``vmocs run``             Start QEMU, attach through SSH, and clean up
QEMU                     Run the guest inside the inherited task cgroup
=======================  ===================================================

Attached task flow
------------------

.. code-block:: text

   srun -n1 -c4 --mem=8G --vm-image ubuntu hostname
      |
      +-- Slurm allocates one task and creates its cgroup
      |
      +-- SPANK task_init reads CPU/memory and permitted VFIO devices
      |      and prepends: vmocs run ubuntu ... -- hostname
      |
      +-- Slurm executes vmocs as the task process
             |
             +-- create a primary-disk COW overlay
             +-- create an ephemeral SSH identity
             +-- fork/exec QEMU inside the task cgroup
             +-- wait for QMP and SSH readiness
             +-- execute hostname in the guest through SSH
             +-- return the guest status and shut QEMU down

No host shell is used to construct the prepended command.  The C plugin builds
an argv vector and ``vmocs run`` preserves the original argument boundaries
when it crosses the required SSH remote-shell boundary.

Cgroup containment
------------------

The plugin's remote task-init hook runs after Slurm has placed the task in its
cgroup.  ``vmocs run`` forks QEMU from there, so QEMU inherits the same CPU,
memory, and device constraints.  vmocs does not create, move, or modify Slurm
cgroups.

This is also why vmocs launches QEMU directly rather than through libvirt.  A
separate service manager could move QEMU into another hierarchy and separate
its resource usage from the Slurm task.

GPU allocation
--------------

GPU passthrough uses Slurm's device cgroup rather than a second allocator.
The cluster represents each schedulable GPU or VF as a numeric VFIO group
device in ``gres.conf``.  The plugin tries to open those devices from the job;
only groups allowed by the task cgroup succeed.  It then passes all PCI
functions in the permitted IOMMU group to QEMU.

This design supports both a physical function dedicated to one job and GIM
SR-IOV virtual functions, while leaving exclusivity and accounting with
Slurm.

Configuration boundary
----------------------

The plugin knows only enough to locate ``vmocs``, select a template, and map
the Slurm allocation.  Image paths, guest boot mode, SSH identity behavior,
firmware, disks, mounts, and QEMU details remain in ``vmocs.yaml`` and
``templates.yaml``.  The same templates can therefore be tested with the
standalone CLI before being exposed through Slurm.
