# Slurm sidecar end-to-end test

This harness validates the combined TPM, virtio-fs, rocJitsu, and rocm-ernic
sidecar plan. It checks both emulated PCI functions, a guest TPM device, the
virtio-fs share, and runtime cleanup.

Target prerequisites:

- QEMU 11 at `/opt/qemu-vfio/bin/qemu-system-x86_64`
- rocJitsu at `/opt/rocjitsu/bin/rocjitsu`
- rocm-ernic at `/opt/rocm-ernic/bin/rocm-ernic`
- `swtpm` and `virtiofsd`
- the prepared `qemu-minimal.qcow2` image referenced by
  `target-templates.yaml`

Run directly on the compute node as the job user:

```bash
tests/slurm-e2e/run-direct.sh
tests/slurm-e2e/run-sidecar-failure.sh
```

The second command terminates rocJitsu after the VM reaches `running` and
asserts that vmocs returns a failure and removes QEMU, the remaining sidecars,
and runtime state.

For Slurm, install `target-templates.yaml` as the test user's
`~/.vmocs/templates.yaml`, copy `local.conf.example` to the ignored
`local.conf`, then run from a login/controller host:

```bash
tests/slurm-e2e/run-slurm.sh
```

The SPANK plugin must be installed on the submitting host and compute node.
Installing the plugin does not require restarting or reconfiguring
`slurmctld` when the existing `plugstack.conf` entry already points to the
same `.so` path.
