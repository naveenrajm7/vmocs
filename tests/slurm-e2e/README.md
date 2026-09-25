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
tests/slurm-e2e/run-checkpoint.sh
tests/slurm-e2e/run-checkpoint-cancel.sh
tests/slurm-e2e/run-sidecar-failure.sh
```

`run-checkpoint.sh` performs the Stage-1 agent workflow: it writes a marker to
the VM's primary disk, publishes a thin cold checkpoint, resumes it through a
new overlay, verifies the marker, and checks that all device sidecars are
freshly available after resume. The checkpoint is intentionally disk-only;
virtio-fs content and device/sidecar state are not part of the artifact.
It uses the `checkpoint-e2e` template, which omits rocm-ernic so it can run on
a host where rocJitsu, swtpm, and virtiofsd are available.
`run-checkpoint-cancel.sh` sends `SIGTERM` to QEMU and the attached vmocs
supervisor back-to-back while an agent command is still running, then resumes
the resulting checkpoint. This models Slurm signaling the whole step cgroup
and directly covers the original cancellation-versus-save race.

To include a host VFIO GPU in both the save and resume generations, provide
space-separated BDFs, for example::

    VMOCS_E2E_PCI_DEVICES='0000:03:00.0 0000:03:00.1' \
        tests/slurm-e2e/run-checkpoint.sh

The checked-in YAML files contain portable example paths. For a site-specific
run, copy them to ignored `target-vmocs.local.yaml` and
`target-templates.local.yaml`, update the paths, and select the local config::

    VMOCS_E2E_CONFIG=tests/slurm-e2e/target-vmocs.local.yaml \
        tests/slurm-e2e/run-checkpoint.sh

The second command terminates rocJitsu after the VM reaches `running` and
asserts that vmocs returns a failure and removes QEMU, the remaining sidecars,
and runtime state.

Network-policy acceptance templates are provided in
`network-policy-templates.yaml`.  Install them temporarily as the test user's
`~/.vmocs/templates.yaml` and exercise these profiles through `srun`:

- `network-unrestricted-e2e` provides normal SLIRP egress and an intentional
  wildcard host forward from TCP 61080 to guest TCP 18080 for inbound testing.
- `network-restricted-e2e` uses `restrict=on,ipv6=off`; both DNS-based and
  direct-IP egress attempts must fail.
- `network-allow-example-e2e` keeps the restricted base policy and maps only
  synthetic guest address `10.0.2.100` to fixed `nc` connectors for
  `example.com` HTTP/HTTPS.  Use curl's `--resolve` option so the TLS hostname
  is preserved.  Other direct-IP HTTPS attempts must still fail.
- `network-passt-e2e` exercises QEMU 11's native passt backend, outbound
  connectivity, the private management forward, and an intentional wildcard
  TCP 61081 to guest TCP 18080 inbound mapping. The compute node must have the
  `passt` executable installed.

All four acceptance profiles select QEMU 11 at
`/opt/qemu-vfio/bin/qemu-system-x86_64`; the first three intentionally retain
SLIRP so the security-policy behavior is tested on the latest QEMU too.

The `cmd:` connectors execute on the host as the job user and therefore belong
only in administrator-controlled templates.  The wildcard inbound rule is
test-only and must not be left installed after the acceptance run.

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
