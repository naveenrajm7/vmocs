.. _slurm-plugin-reference:

Slurm plugin reference
======================

User options
------------

The plugin registers these options with Slurm clients and remote job steps:

.. option:: --vm-image TEMPLATE

   Enable vmocs and boot the named template.  The template must be available
   through the compute node's vmocs configuration.

.. option:: --vm-save PATH

   After guest shutdown, publish a cold primary-disk checkpoint directory at
   the host path *PATH*.  The destination must not already exist.  Meaningful
   only with ``--vm-image``.

.. option:: --vm-resume CHECKPOINT

   Cold-boot from a complete vmocs checkpoint.  The checkpoint must have been
   created from the same template passed through ``--vm-image``.  Resume uses
   a new writable overlay and never mutates *CHECKPOINT*.

.. option:: --vm-attach MODE

   Select ``auto`` (the default) to run the Slurm task command inside the guest
   over SSH.  Select ``none`` to boot the VM, print connection details, and
   wait for QEMU without executing the task command in the guest.

All current modes require a job step with exactly one task.  The plugin
returns an error when Slurm reports a larger task count.

.. _slurm-plugin-arguments:

Plugin arguments
----------------

Plugin arguments follow the shared-object path in ``plugstack.conf``.  They
are cluster configuration, not options users pass to ``srun``.

.. describe:: vmocs_path=PREFIX

   Execute ``PREFIX/bin/vmocs``.  Without this argument, the plugin executes
   ``vmocs`` through ``PATH``.

.. describe:: vmocs_conf=PATH

   Add ``--config PATH`` to every ``vmocs run`` and cleanup invocation.
   Without it, the normal vmocs configuration resolution rules apply.

Example::

   optional /usr/lib64/slurm/spank_vmocs.so vmocs_path=/opt/vmocs vmocs_conf=/etc/vmocs/vmocs.yaml

.. _slurm-resource-mapping:

Resource mapping
----------------

CPU and memory
~~~~~~~~~~~~~~

.. list-table::
   :header-rows: 1
   :widths: 36 38 26

   * - Slurm request
     - Source
     - vmocs argument
   * - ``-c N`` / ``--cpus-per-task=N``
     - ``SLURM_CPUS_PER_TASK``
     - ``--cores N``
   * - ``--mem=SIZE``
     - ``SLURM_MEM_PER_NODE``
     - ``--memory MB``
   * - ``--mem-per-cpu=SIZE``
     - ``SLURM_MEM_PER_CPU`` times cores
     - ``--memory MB``

If ``SLURM_CPUS_PER_TASK`` is unavailable, the plugin passes one core.  If no
supported Slurm memory variable is available, it omits ``--memory`` and
``vmocs run`` uses its 2048 MB default.

Before passing memory to QEMU, the plugin subtracts the greater of five
percent or 256 MB for host-side QEMU overhead.  The result is clamped to a
minimum of 512 MB.  For example, a 4096 MB allocation produces a 3840 MB QEMU
configuration.  Guest firmware and the kernel consume additional memory, so
tools inside the guest report a smaller usable total.

GPU devices
~~~~~~~~~~~

Slurm assigns GPU GRES entries whose ``File=`` values are numeric
``/dev/vfio/<group>`` nodes.  From inside the job's device cgroup, the plugin:

1. attempts to open every numeric VFIO group node;
2. keeps only the nodes allowed to the job;
3. enumerates the PCI BDFs in each permitted IOMMU group; and
4. appends one ``--pci BDF`` argument for each function.

This deliberately uses the device cgroup as the allocation source of truth;
the plugin does not select the first free GPU or maintain a separate
GPU-index map.  See :doc:`gpu-passthrough` for node configuration.

Command transformation
----------------------

For an attached task resembling::

   srun -n1 -c4 --mem=8G --vm-image base-ubuntu hostname

the task-init hook prepends an argv sequence equivalent to::

   vmocs run base-ubuntu --cores 4 --memory 7783 --job-id JOB_ID \
       --attach auto -- hostname

The exact memory value follows the headroom calculation.  Any configured
``--config``, save path, and detected PCI devices are inserted before the
``--`` separator.  Slurm then executes vmocs as the actual task process.

Lifecycle hooks
---------------

.. list-table::
   :header-rows: 1
   :widths: 40 60

   * - Hook
     - Action
   * - ``slurm_spank_init``
     - Register the four Slurm options.
   * - ``slurm_spank_init_post_opt``
     - Persist the selected template in the job environment in allocator
       context.
   * - ``slurm_spank_task_init``
     - Validate one task and prepend ``vmocs run`` to the original task argv.
   * - ``slurm_spank_exit``
     - Run ``vmocs stop JOB_ID --if-exists`` as best-effort remote cleanup.

The primary cleanup belongs to ``vmocs run``.  The exit hook is a fallback and
does not turn a cleanup failure into a failed Slurm job.
