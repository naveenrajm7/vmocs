.. _slurm-admin-guide:

Cluster administrator guide
===========================

Deploy both the vmocs CLI and its SPANK plugin.  The CLI runs on compute
nodes; the plugin must be available wherever Slurm parses its options as well
as wherever the job step runs.

Requirements
------------

* Slurm 23.11 or newer, which provides ``spank_prepend_task_argv()``.
* Build headers matching the deployed Slurm release series.
* A working vmocs installation and QEMU/KVM setup on every compute node.
* Identical vmocs system configuration and templates, or shared configuration
  paths, on the compute nodes.
* The plugin installed on login/submission nodes and compute nodes.

Complete :doc:`../getting-started` on a representative compute node before
integrating Slurm.  For GPU nodes, also complete :doc:`gpu-passthrough`.

Build a package
---------------

Build the plugin against the same Slurm release series used by the cluster.

RPM systems
~~~~~~~~~~~

On RHEL, Rocky Linux, AlmaLinux, or SLES::

   sudo dnf install rpm-build gcc make slurm-devel
   make -C plugins/slurm rpm
   sudo dnf install ./plugins/slurm/rpm/RPMS/*/vmocs-slurm-plugin-*.rpm

The RPM release records the build's Slurm series, for example ``sl2411``, and
depends on the matching ``libslurm``.

Debian systems
~~~~~~~~~~~~~~

On Debian or Ubuntu::

   sudo apt install build-essential debhelper fakeroot libslurm-dev
   make -C plugins/slurm deb
   sudo apt install ./plugins/vmocs-slurm-plugin_*.deb

When using SchedMD's packages, install ``slurm-smd-dev`` instead of
``libslurm-dev``.  The Debian package does not encode the Slurm release series;
administrators must keep the build and deployed series aligned.

Install without a package
-------------------------

``make install`` honors ``prefix``, ``libdir``, ``datarootdir``, and
``DESTDIR``::

   # /usr/local/lib/slurm/spank_vmocs.so by default
   sudo make -C plugins/slurm install

   # Or install beside distribution-provided Slurm plugins
   sudo make -C plugins/slurm install prefix=/usr libdir=/usr/lib64

The command also writes a plugstack fragment under
``<datadir>/vmocs/vmocs.conf``.  ``make uninstall`` removes the two installed
files but does not edit the cluster's ``plugstack.conf``.

Enable the plugin
-----------------

Packages install the shared object in the distribution's Slurm plugin
directory and a ready-made fragment at ``/usr/share/vmocs/vmocs.conf``.  Add
the fragment to the main plugstack configuration::

   include /usr/share/vmocs/vmocs.conf

The shipped entry is ``optional`` so an installation problem does not stop
unrelated Slurm jobs.  It also means plugin load failures may appear only as a
daemon or client warning, followed by ``--vm-image`` being unrecognized.

Install the plugin and plugstack configuration on:

* login and submission nodes, where ``srun``, ``salloc``, and ``sbatch`` parse
  the option; and
* compute nodes, where ``slurmstepd`` wraps the job task and launches vmocs.

Keep the plugstack content consistent across those systems.  Restart
``slurmd`` on each compute node after enabling or replacing the plugin::

   sudo systemctl restart slurmd

Configure plugin arguments
--------------------------

Do not edit the packaged fragment because upgrades overwrite it.  Replace its
``include`` with a site-managed entry that uses the absolute installed path::

   optional /usr/lib64/slurm/spank_vmocs.so vmocs_path=/opt/vmocs vmocs_conf=/etc/vmocs/vmocs.yaml

On Debian, the library path is normally
``/usr/lib/<multiarch-triplet>/slurm/spank_vmocs.so``.  The two optional
arguments are:

``vmocs_path=PREFIX``
   Run ``PREFIX/bin/vmocs`` instead of resolving ``vmocs`` from ``PATH``.

``vmocs_conf=PATH``
   Pass ``--config PATH`` to every vmocs invocation.

See :ref:`slurm-plugin-arguments` for the exact reference.

Install the vmocs configuration
-------------------------------

Place ``vmocs.yaml`` and ``templates.yaml`` on every compute node or use a
shared path.  A typical system configuration is::

   qemu-bin: /usr/bin/qemu-system-x86_64
   runtime-dir: /var/run/vmocs
   templates: /etc/vmocs/templates.yaml

The runtime directory must be node-local and writable by job users.  If it is
under ``/run``, recreate it at boot with a ``tmpfiles.d`` rule or an equivalent
site mechanism.  Choose ownership and permissions that allow jobs to create
their state while preventing users from modifying one another's directories.
The image files and any template resources must be readable on every eligible
compute node.  Publish templates with stable names because users pass the name
through ``--vm-image``.

Validate the deployment
-----------------------

From a login node, verify option registration::

   srun --help | grep -- --vm-image

Then run a single-task smoke test on a configured compute node::

   srun -N1 -n1 -c2 --mem=4G --vm-image base-ubuntu hostname

Confirm that the hostname is from the guest and the command returns.  Then
check the selected compute node for leftover runtime state::

   srun -N1 -n1 -w COMPUTE_NODE vmocs list

Upgrade compatibility
---------------------

``spank_vmocs.so`` records the ``SLURM_VERSION_NUMBER`` from the headers used
to build it.  Slurm accepts micro releases within the same ``X.YY`` series but
rejects the plugin across release series with an
``Incompatible Slurm plugin version`` message.

For every Slurm ``X.YY`` upgrade:

1. Install the matching development headers.
2. Rebuild the RPM, Debian package, or shared object.
3. Deploy it to login/submission and compute nodes.
4. Restart ``slurmd`` on the compute nodes.
5. Repeat the option-registration and smoke tests.

Keep the plugin package version in sync between ``VMOCS_VER`` in
``plugins/slurm/Makefile`` and the top entry of
``plugins/slurm/debian/changelog`` when making a release.
