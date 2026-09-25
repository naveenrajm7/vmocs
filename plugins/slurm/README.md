# spank_vmocs — Slurm SPANK plugin for vmocs

`spank_vmocs` integrates [vmocs](../../README.md) with Slurm. A job step such
as

```bash
srun -n1 --pty --vm-image base-ubuntu bash -l
```

boots the selected VM inside the Slurm task cgroup, runs the task command in
the guest over SSH, and tears the VM down when the task ends.

The canonical documentation is part of the main Sphinx manual:

- [Using vmocs with Slurm](../../docs/source/slurm/index.rst)
- [Cluster administrator guide](../../docs/source/slurm/admin-guide.rst)
- [GPU passthrough](../../docs/source/slurm/gpu-passthrough.rst)
- [Plugin option reference](../../docs/source/slurm/reference.rst)
- [Troubleshooting](../../docs/source/slurm/troubleshooting.rst)

## Requirements

- Slurm 23.11 or newer (`spank_prepend_task_argv` is required)
- Slurm development headers from the same release series as the deployment
- vmocs and its configuration installed on every compute node
- the SPANK plugin installed on login/submission and compute nodes

## Package build

RPM systems:

```bash
sudo dnf install rpm-build gcc make slurm-devel
make -C plugins/slurm rpm
sudo dnf install ./plugins/slurm/rpm/RPMS/*/vmocs-slurm-plugin-*.rpm
```

Debian systems:

```bash
sudo apt install build-essential debhelper fakeroot libslurm-dev
make -C plugins/slurm deb
sudo apt install ./plugins/vmocs-slurm-plugin_*.deb
```

Use `slurm-smd-dev` instead of `libslurm-dev` with SchedMD's Debian packages.

## Enable the plugin

The packages install the shared object and
`/usr/share/vmocs/vmocs.conf`. Include the fragment from the cluster's main
`plugstack.conf`:

```text
include /usr/share/vmocs/vmocs.conf
```

Restart `slurmd` on the compute nodes, then verify from a login node:

```bash
srun --help | grep -- --vm-image
srun -N1 -n1 -c2 --mem=4G --vm-image base-ubuntu hostname
```

For a nonstandard vmocs installation, use a site-managed entry rather than
editing the packaged fragment:

```text
optional /usr/lib64/slurm/spank_vmocs.so vmocs_path=/opt/vmocs vmocs_conf=/etc/vmocs/vmocs.yaml
```

Rebuild the plugin whenever Slurm moves to a new `X.YY` release series.

## User options

| Option | Purpose |
|---|---|
| `--vm-image=TEMPLATE` | Boot a VM and enable guest command execution |
| `--vm-save=PATH` | Publish a cold primary-disk checkpoint directory |
| `--vm-resume=PATH` | Cold-boot from a complete checkpoint directory |
| `--vm-attach=auto\|none` | Select automatic SSH attachment or VM-as-job mode |

See the linked Sphinx reference for resource mapping, plugin arguments, GPU
discovery, lifecycle hooks, limitations, and failure diagnosis.
