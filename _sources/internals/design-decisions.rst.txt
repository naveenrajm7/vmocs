.. _design-decisions:

Design decisions
================

This page records the major current design choices.  It describes the
implemented system rather than possible future work.

Use a SPANK plugin
------------------

SPANK makes VM execution part of the ordinary ``srun`` lifecycle.  Slurm
continues to select nodes, enforce limits, deliver signals, account usage, and
apply the time limit.  vmocs does not need a parallel scheduler or a custom
allocation command.

Use C for the production plugin
-------------------------------

The repository retains a Lua prototype, but the deployed plugin is C.  A C
plugin uses Slurm's native interface without adding a Lua SPANK runtime to
every client and compute node.  The trade-off is that it must be rebuilt for
each Slurm release series.

Launch QEMU directly
--------------------

Direct fork/exec gives QEMU the Slurm task's cgroup by inheritance.  Routing
the launch through libvirt or another system service risks moving QEMU into a
service-owned cgroup and breaking Slurm containment and accounting.  The
trade-off is that vmocs owns QMP, process supervision, and cleanup itself.

Treat Slurm as the resource authority
-------------------------------------

The plugin does not keep its own pool of CPUs, memory, or GPUs.  CPU and memory
come from Slurm's task environment.  GPU selection comes from the VFIO device
nodes allowed by Slurm's device cgroup.  Avoiding a second allocator prevents
conflicting ownership decisions.

Reserve memory outside the guest
--------------------------------

QEMU needs host memory in addition to guest RAM.  The plugin therefore
subtracts the greater of five percent or 256 MB from a Slurm memory allocation
before configuring the guest, with a 512 MB lower bound.  This is a simple,
predictable policy; clusters with workloads that need different overhead must
account for that when choosing Slurm memory requests.

Execute guest commands over SSH
-------------------------------

SSH works across supported Linux, Windows, cloud-init, and Vagrant-style
images without requiring a vmocs-specific agent inside the guest.  It also
provides terminal forwarding and an exit status.  The trade-off is that host
paths and arbitrary environment variables do not automatically exist inside
the VM.

Keep the primary image immutable
--------------------------------

Every launch creates a qcow2 overlay over the template image.  Jobs start from
a known base, concurrent jobs do not write the same OS disk, and cleanup is
normally a directory removal.  ``--vm-save`` is an explicit opt-in operation
that converts the overlay to a new independent image.

Reject multi-task steps
-----------------------

Runtime state currently uses the Slurm job ID and represents one VM.  Starting
multiple tasks would collide on that state and make VM ownership ambiguous.
The plugin rejects task counts other than one instead of allowing partial or
unsafe behavior; users scale the single VM with ``--cpus-per-task``.

Load the plugin as optional
---------------------------

The packaged plugstack fragment uses ``optional`` so a vmocs deployment issue
does not prevent non-VM Slurm workloads.  The operational trade-off is that an
incompatible plugin can be noticed first as an unrecognized option.  The
administrator validation procedure therefore checks option registration from
a login node after every deployment.
