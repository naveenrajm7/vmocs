vmocs documentation
===================

**vmocs** (Virtual Machine On Compute through Slurm) launches QEMU/KVM
virtual machines directly or as Slurm jobs.  It combines a standalone command
line interface with a Slurm SPANK plugin so that the VM remains inside the
job's resource limits and lifecycle.

Choose the path that matches what you need:

* Start with :doc:`getting-started` to install vmocs and launch a first VM.
* Use :doc:`direct/index` to manage VMs without a scheduler.
* Use :doc:`slurm/index` to run commands inside VMs allocated by Slurm.
* Use the reference pages when you need the exact command or configuration
  syntax.

.. toctree::
   :maxdepth: 2
   :caption: Start here

   getting-started
   direct/index
   slurm/index

.. toctree::
   :maxdepth: 2
   :caption: Reference

   cli
   conf

.. toctree::
   :maxdepth: 2
   :caption: Internals

   internals/index
