.. _slurm-user-guide:

Slurm user guide
================

The vmocs options are SPANK options, so they appear alongside Slurm's own
options after the plugin has been installed.  ``--vm-image`` enables vmocs for
a job step; Slurm commands without it behave normally.

Before starting
---------------

Check that the plugin is available and find the templates published by your
administrator::

   srun --help | grep -- --vm
   srun -N1 -n1 vmocs template list

The second command runs the vmocs CLI on a compute node, so it does not require
vmocs to be installed on the login node.

If ``--vm-image`` is not recognized, see
:ref:`troubleshoot-unrecognized-option` or contact the cluster administrator.

Run an interactive session
--------------------------

Request one task and a pseudo-terminal, then run a login shell::

   srun -n1 --pty --vm-image base-ubuntu bash -l

There is one continuous terminal path from the local terminal, through Slurm
and SSH, to the guest.  Logging out ends the guest command, shuts down the VM,
and completes the Slurm step.

Attached interactive sessions wait for SSH and reconnect after a guest reboot
or temporary transport failure while QEMU is still running.  The reconnect
timeout comes from the template's ``ssh-timeout`` setting.

Run a non-interactive command
-----------------------------

Place the guest command after the Slurm and vmocs options::

   srun -n1 --vm-image base-ubuntu hostname
   srun -n1 --vm-image base-ubuntu bash -lc 'cat /etc/os-release'

Argument boundaries are preserved when vmocs forwards the command through
SSH.  The guest command's exit status becomes the ``srun`` exit status.
Unlike interactive sessions, a non-interactive command is not replayed after
an SSH transport failure.

Request resources
-----------------

Request resources with normal Slurm options::

   srun -n1 -c4 --mem=8G --vm-image base-ubuntu \
       bash -lc 'nproc; free -m'

The guest receives four vCPUs.  Its configured memory is slightly smaller
than the allocation because the plugin reserves host memory for QEMU.  See
:ref:`slurm-resource-mapping` for the exact mapping.

Use an existing allocation
--------------------------

Acquire an allocation first, then start the VM as a step inside it::

   salloc -N1 -n1 -c4 --mem=8G
   srun -n1 --pty --vm-image base-ubuntu bash -l

Place ``--vm-image`` on the ``srun`` step that should execute in the guest.
This keeps it clear which step owns the VM and also allows ordinary host steps
in the same allocation.

Submit a batch workflow
-----------------------

The reliable batch pattern is a normal host-side batch script that launches
an explicit vmocs ``srun`` step.  Put the guest workload on storage visible at
the same path inside the VM, for example through a template mount point::

   #!/bin/bash
   #SBATCH --job-name=vmocs-example
   #SBATCH --ntasks=1
   #SBATCH --cpus-per-task=4
   #SBATCH --mem=8G
   #SBATCH --output=vmocs-%j.out

   srun -n1 --vm-image base-ubuntu /shared/jobs/train.sh --epochs 10

Then submit it normally::

   sbatch submit.sh

Putting ``#SBATCH --vm-image=...`` directly in the batch script asks vmocs to
wrap Slurm's private spool copy of that script.  The private host path is not
normally present inside the guest, so direct wrapping works only when the
script and every required path are deliberately made visible in the guest at
the same locations.  Automatic batch-script staging is not currently
implemented.

Save changes to the primary disk
--------------------------------

Use ``--vm-save`` to flatten the primary disk overlay after the guest command
ends::

   srun -n1 --pty --vm-image base-ubuntu \
       --vm-save /shared/images/base-ubuntu-modified.qcow2 bash -l

The destination is a host path on the compute node and must be writable by the
job user.  The resulting qcow2 file is standalone and can be used as the
``image`` of another template.  Saving does not modify the original image.

Only the primary OS disk is saved.  Template ``extra-disks`` are attached
directly, so their writes are already persistent and are not copied into the
saved image.

Use unattached mode
-------------------

The default ``--vm-attach=auto`` mode runs the Slurm command in the guest.
For debugging or an externally managed session, keep the VM as the job but do
not open SSH automatically::

   srun -n1 --vm-image base-ubuntu --vm-attach=none /bin/true

vmocs prints the SSH connection details and waits for QEMU.  The trailing
Slurm command is not executed in this mode.  The job ends when the guest
exits, the allocation reaches its time limit, or the job is cancelled.

Request a GPU
-------------

On a node configured for vmocs GPU passthrough, request a GPU normally::

   srun -n1 --gres=gpu:1 --vm-image gpu-ubuntu rocm-smi

The plugin discovers only the VFIO device groups Slurm has granted to the job
and passes their PCI functions into the VM.  No vmocs GPU option is required.
GPU availability, driver installation inside the image, and supported GRES
names are cluster-specific; see :doc:`gpu-passthrough` for the administrator
setup.

Current limitations
-------------------

* A vmocs step must contain exactly one Slurm task (``--ntasks=1``).
* Host environment variables are not generally copied into the guest.
* Host paths and the submission working directory are available only when the
  template deliberately shares or otherwise provides them.
* Direct wrapping of Slurm's private batch-spool script requires matching
  guest-visible paths; explicit ``srun`` from a host batch script is preferred.
