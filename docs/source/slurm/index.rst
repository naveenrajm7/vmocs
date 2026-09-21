.. _slurm-plugin:

Using vmocs with Slurm
======================

The vmocs SPANK plugin adds VM options to Slurm.  A command such as

.. code-block:: console

   $ srun -n1 --vm-image base-ubuntu hostname

allocates a normal Slurm task, boots the selected VM inside that task's
cgroup, runs ``hostname`` in the guest over SSH, and destroys the VM when the
command finishes.  Slurm remains responsible for allocation, limits,
accounting, signals, and wall time; vmocs manages the guest lifecycle.

Quick start for users
---------------------

The plugin and at least one template must already be installed by a cluster
administrator.  Confirm that the options are available::

   srun --help | grep -- --vm-image

Open an interactive shell inside a VM::

   srun -n1 --pty --vm-image base-ubuntu bash -l

Run a single non-interactive command::

   srun -n1 --vm-image base-ubuntu uname -a

Request CPU and memory as usual; the plugin maps the allocation into the
guest::

   srun -n1 -c4 --mem=8G --vm-image base-ubuntu nproc

Seamless guest sessions currently require exactly one Slurm task.  See the
:doc:`user-guide` for allocations, batch jobs, saved images, and current
limitations.

Choose your guide
-----------------

**Cluster users**
   Follow the :doc:`user-guide` for ``srun``, ``salloc``, ``sbatch``,
   interactive sessions, resources, and saving guest changes.

**Cluster administrators**
   Follow the :doc:`admin-guide` to build, install, configure, and upgrade the
   plugin.  GPU nodes also require the :doc:`gpu-passthrough` setup.

**Operators and developers**
   Use the :doc:`reference` for exact options and resource mappings, the
   :doc:`troubleshooting` guide for common failures, and the
   :doc:`../internals/index` section for implementation details.

.. toctree::
   :maxdepth: 2

   user-guide
   admin-guide
   gpu-passthrough
   reference
   troubleshooting
