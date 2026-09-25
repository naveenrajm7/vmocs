# vmocs sidecar architecture

Status: accepted; initial native-process implementation complete

Date: 2026-09-19

## Executive decision

vmocs should treat host helper processes as first-class, VM-scoped resources.
It should not add rocJitsu and rocm-ernic by copying the current `swtpm` and
`virtiofsd` helper functions.

The recommended design has three parts:

1. A **typed sidecar adapter** for each supported feature (`swtpm`,
   `virtiofsd`, `rocjitsu`, and `rocm-ernic`). An adapter converts user intent
   into a process specification, readiness checks, resource requirements, and
   a QEMU command-line fragment. It contains no process-lifecycle code.
2. A **VM-scoped sidecar manager** that starts, observes, records, and stops all
   processes transactionally. QEMU command-line construction becomes a pure
   operation and never calls `Popen`.
3. A **per-VM supervisor** as the eventual lifecycle owner of QEMU and its
   sidecars. In the Slurm path the current blocking `vmocs` process can fill
   this role. Detached mode needs a persistent supervisor process before
   vfio-user sidecars can be considered fully supported.

Use native, host-installed sidecar binaries in production. They inherit the
Slurm job cgroup, run as the job user, and avoid a Docker daemon, container
permissions, `--ipc=host`, and container cleanup. Keep the Stage 1 Compose
stack as a development and integration fixture, not as the vmocs runtime
orchestrator.

For rocJitsu and rocm-ernic, the QEMU contract is **vfio-user over a Unix
socket**, not physical `vfio-pci,host=<BDF>` passthrough. When any vfio-user
device is requested, vmocs must create one shared memfd for guest RAM and bind
it directly to the single `-machine` option. The same memfd can satisfy
virtio-fs and vfio-user together.

## Implementation status

This branch implements Changes 1 and 2 from the incremental plan below:

- pure, typed plans for `swtpm`, `virtiofsd`, rocJitsu, and rocm-ernic;
- transactional startup, Unix-socket and child-liveness readiness, separate
  logs, reverse-order TERM/KILL cleanup, and persisted process identities;
- one machine-bound shared memfd for any combination of virtio-fs and
  vfio-user;
- typed `emulated-devices` and operator-controlled sidecar binary/profile
  configuration;
- QEMU capability, snapshot, machine, binary, profile, shared-directory, and
  socket-path preflight;
- critical-sidecar observation in blocking and attached execution paths; and
- explicit rejection of detached vfio-user VMs until the dedicated detached
  supervisor in Change 3 exists.

Validation on a reference development host used packaged QEMU 11.1.1,
rocJitsu 0.3.0, and rocm-ernic 0.2.0. Both a direct KVM run and a Slurm job
passed the combined TPM + virtio-fs + rocJitsu + rocm-ernic guest checks and
left no runtime or process residue. A direct fault-injection run also
terminated rocJitsu after readiness and verified a nonzero vmocs exit plus
complete QEMU/sidecar cleanup. The packaged ernic in this run advertised
`1dd8:100a`; the earlier Stage 2 build advertised `1022:8001`. This confirms
that the Unix socket plus `vfio-user-pci` attachment is the contract, not a
hard-coded PCI ID.

The Slurm run used QEMU's software fallback because the node's Slurm device
cgroup did not allow `/dev/kvm`. That is a node policy issue, not a sidecar
failure; production enablement should add `/dev/kvm` to the job device policy.

## Terminology and scope

The project names used by the Stage 1/2 experiment are:

- **rocJitsu**: a userspace GPU emulator exposing a vfio-user PCI device.
- **rocm-ernic**: a userspace NIC/RDMA-device emulator exposing a vfio-user
  PCI device.
- **virtiofsd**: the host daemon for a `virtio-fs` directory share. The 9p
  sharing path does not use a sidecar.
- **swtpm**: the TPM 2.0 emulator. It is enabled by `tpm: true`; UEFI alone
  does not imply a TPM.

This proposal covers process orchestration and QEMU attachment. Guest driver
enablement (`amdgpu`, `/dev/kfd`, ionic/RDMA, and `ibv_devices`) is separate.
The Stage 3 attachment milestone is that the emulated devices are present and
their PCI configuration is readable.

## Evidence reviewed

This design is based on the following code and experiment artifacts:

- [`lib/vmocs/hypervisor.py`](../lib/vmocs/hypervisor.py): current `swtpm`,
  `virtiofsd`, physical VFIO, machine, and memory command-line construction.
- [`lib/vmocs/launch.py`](../lib/vmocs/launch.py): QEMU launch, late metadata
  creation, stop, and limited PID-file cleanup.
- [`lib/vmocs/cli.py`](../lib/vmocs/cli.py) and
  [`lib/vmocs/session.py`](../lib/vmocs/session.py): blocking, detached, and
  attached-session lifecycles.
- [`plugins/slurm/spank_vmocs.c`](../plugins/slurm/spank_vmocs.c): the job-user
  launch path inside the Slurm cgroup and best-effort exit cleanup.
- [`development/vmocs-vm-lifecycle.md`](vmocs-vm-lifecycle.md): current
  lifecycle intent.
- Private bring-up notes for the proven rocJitsu + rocm-ernic experiment and
  Stage 3 work.
- The checked-out `qemu-minimal` commit `5d6868914873757ff1c51dec3ca95a3fa0b2e9d9`,
  especially `qemu-tool/run_vm.py` and
  `compose/vfio-user-ernic-rocjitsu-vm/docker-compose.yml`.
- The saved Stage 2 QEMU command line and sidecar logs from the private
  bring-up workspace.

External protocol references are listed at the end of this document.

## Baseline behavior before this branch

| Feature | Host process | Start point | Readiness | Persisted identity | Explicit cleanup |
|---|---|---|---|---|---|
| 9p share | none | QEMU only | n/a | n/a | n/a |
| virtio-fs share | one `virtiofsd` per mount | `_mount_cmdline()` while building QEMU argv | path exists, 10 s | `virtiofs_<N>.sock.pid` | only through `teardown_vm()` PID glob |
| TPM 2.0 | one `swtpm` | `_tpm_cmdline()` while building QEMU argv | path exists, 10 s | none | none; assumes socket disconnect causes exit |
| Physical PCI/GPU | none | QEMU `vfio-pci,host=<BDF>` | kernel/VFIO errors surface through QEMU | n/a | QEMU owns device |
| rocJitsu | not implemented | n/a | n/a | n/a | n/a |
| rocm-ernic | not implemented | n/a | n/a | n/a | n/a |

Both existing daemons use `start_new_session=True`. That is useful because it
prevents terminal hangup and launcher exit from killing detached helpers. It
does not make the processes lifecycle-managed, and it does not move them out
of their inherited Slurm cgroup.

The normal blocking and attached-session paths remove the runtime directory
directly after QEMU exits. They do not call the PID cleanup loop in
`teardown_vm()`. This works only while each daemon reliably exits after QEMU
disconnects.

## Problems in the current pattern

These issues exist already and become important when the sidecar count grows:

1. **Command construction has side effects.** Unit-level QEMU argument
   generation starts real host processes. Planning cannot be validated before
   mutation, tests must mock `Popen`, and a later argument-building error can
   leak an earlier sidecar.
2. **Startup is not transactional.** If sidecar two, QEMU, QMP, key rotation,
   or SSH startup fails, there is no single rollback owner for everything
   already started.
3. **Readiness checks are too weak.** `os.path.exists()` accepts a regular
   file or stale socket and does not notice that the child exited before the
   deadline. Readiness needs `stat.S_ISSOCK` plus child-liveness checks.
4. **Process identity is incomplete.** The TPM PID is not recorded. A bare PID
   also becomes unsafe after PID reuse; stop must verify that it still denotes
   the process vmocs started.
5. **Metadata is written too late.** `vm.json` appears only after SSH is ready.
   A failure before that point can leave QEMU or a sidecar running without a
   recoverable manifest.
6. **There is no liveness policy.** A TPM can die and crash QEMU only on a
   later access. A virtio-fs mount can fail after boot. A vfio-user process is
   part of a guest PCI device and cannot be assumed to reconnect safely.
7. **Normal cleanup relies on daemon behavior.** Direct runtime-directory
   removal can unlink sockets while a leftover process is still alive.
8. **Detached mode has no owner.** QEMU and its helpers are intentionally
   orphaned. `vmocs stop` can find QEMU from `vm.json`, but there is no process
   observing sidecar death or guaranteeing whole-tree cleanup.
9. **Runtime-directory reuse is unchecked.** `os.makedirs(..., exist_ok=True)`
   can reuse files from an earlier launch with the same job ID.
10. **Logs are missing.** Current sidecar stdout/stderr goes to `/dev/null`, so
    readiness failures have little diagnostic value.

Adding two more ad hoc launch helpers would preserve all ten problems.

## Requirements

The target design should provide:

- deterministic planning and stable device ordering;
- preflight before starting any long-running process;
- start-all-or-rollback behavior;
- explicit readiness deadlines and useful errors;
- exact process ownership, including descendants;
- continuous observation of critical sidecars while QEMU runs;
- QEMU-first shutdown followed by bounded TERM/KILL sidecar cleanup;
- equivalent behavior for CLI, attached sessions, and Slurm;
- persisted state sufficient for `list`, `stop`, and crash recovery;
- one memory configuration that composes virtio-fs and vfio-user;
- typed, operator-controlled integrations rather than arbitrary shell
  commands in user templates;
- multiple instances without socket-name or PCI-order ambiguity;
- separate logs and status for every sidecar.

## Alternatives considered

| Alternative | Benefit | Problem | Decision |
|---|---|---|---|
| Add `_rocjitsu_cmdline()` and `_ernic_cmdline()` beside current helpers | Small patch | Multiplies leaks, mixed concerns, and untestable startup | Reject |
| Allow arbitrary `sidecars:` argv and QEMU args in YAML | Very flexible | No typed validation, unsafe shell-like surface, user templates become orchestration programs | Reject |
| Typed adapters + VM-scoped manager/supervisor | Explicit contracts, testable, incremental, no external daemon | Requires a modest lifecycle refactor | **Adopt** |
| Docker Compose per VM | Reuses Stage 1 | Wrong lifecycle layer for Slurm; daemon/cgroup, IPC, permission, and cleanup complications | Development only |
| systemd transient units | Strong process-group cleanup | Compute-node policy and user-bus availability vary; duplicates Slurm ownership | Optional future runner, not baseline |

## Target architecture

```text
CLI / SPANK
    |
    v
resolve template + operator config
    |
    v
VM planner (pure)
    |-- validates feature combinations
    |-- allocates socket paths and stable IDs
    |-- composes one memory/machine plan
    |-- emits ProcessSpec[] + QEMU argv
    |
    v
per-VM supervisor
    |-- writes state=starting manifest
    |-- SidecarManager.start_all()
    |      |-- swtpm
    |      |-- virtiofsd[0..N]
    |      |-- rocm-ernic[0..N]
    |      `-- rocjitsu[0..N]
    |-- waits for all required sockets
    |-- starts QEMU
    |-- writes state=running manifest
    |-- observes QEMU, sidecars, signals, and QMP
    `-- QEMU shutdown -> sidecars -> files -> state=stopped
```

The core invariant is:

> If a process appears in a VM plan, exactly one VM supervisor owns its whole
> lifecycle, from spawn through reap or verified recovery cleanup.

### Module boundaries

A practical layout is:

```text
lib/vmocs/
  runtime.py                 VmPlan, manifest, process identity, state machine
  supervisor.py              QEMU + sidecar event loop and stop protocol
  sidecars/
    __init__.py              explicit adapter registry
    base.py                  SidecarRequest, ProcessSpec, QemuContribution
    manager.py               spawn/readiness/observe/stop/rollback
    swtpm.py
    virtiofs.py
    rocjitsu.py
    rocm_ernic.py
  hypervisor.py              pure QEMU argument construction only
  launch.py                  image/key/NVRAM preparation + supervisor entry
```

The registry should be explicit Python code, not entry-point discovery. vmocs
ships and reviews every process it can start.

### Pure planning types

The exact Python representation can evolve, but the contracts should be close
to these:

```python
from typing import Mapping, Tuple


@dataclass(frozen=True)
class ProcessSpec:
    name: str
    argv: Tuple[str, ...]
    env: Mapping[str, str]
    sockets: Tuple[Path, ...]
    log_path: Path
    startup_timeout: float
    stop_timeout: float
    critical: bool = True

@dataclass(frozen=True)
class QemuContribution:
    args: Tuple[str, ...]
    requires_shared_guest_memory: bool = False

@dataclass(frozen=True)
class SidecarPlan:
    process: ProcessSpec
    qemu: QemuContribution
```

Adapters are pure: they validate a typed request and return `SidecarPlan`
objects. The manager is the only code allowed to turn a `ProcessSpec` into a
process.

Do not use `shell=True`. Build argv as a list, resolve binaries from
operator-controlled configuration, and keep runtime socket/log paths under the
per-VM directory.

### User-facing configuration

Keep templates feature-oriented. Users should request devices or shares, not
describe host processes.

One possible template syntax is:

```yaml
ubuntu-vfio-user:
  image: /var/lib/vmocs/images/ubuntu-vfio-user.qcow2
  qemu-bin: /opt/vmocs/qemu-vfio/bin/qemu-system-x86_64
  machine-type: q35
  cpu-model: EPYC
  emulated-devices:
    - type: rocm-ernic
    - type: rocjitsu
      profile: mi455x
```

An ordered list permits repeated devices and defines stable attachment order.
It does not promise stable guest BDFs. If stable BDFs become a requirement,
vmocs needs one PCI-address allocator covering all QEMU devices; individual
adapters should not hard-code addresses.

Operator-owned `vmocs.yaml` maps those requests to installed artifacts:

```yaml
sidecars:
  startup-timeout: 60
  stop-timeout: 10
  rocjitsu:
    binary: /opt/vmocs/rocjitsu/bin/rocjitsu
    profiles:
      mi455x: /opt/vmocs/rocjitsu/share/configs/gfx1250_mi455x.json
  rocm-ernic:
    binary: /opt/vmocs/rocm-ernic/bin/rocm-ernic
```

Existing `tpm:` and `mount-points:` syntax can remain. Resolution maps them to
the same internal sidecar plan. Avoid exposing a generic executable or raw
QEMU fragment in the new template schema.

### Lifecycle state machine

```text
PREPARING
    |
    v
PREFLIGHTED -> SIDECARS_STARTING -> SIDECARS_READY -> QEMU_STARTING -> RUNNING
      |               |                  |                |             |
      `---------------+------------------+----------------+-------------'
                                      failure / stop
                                             |
                                             v
                                          STOPPING
                                             |
                                             v
                                   STOPPED or FAILED
```

Recommended launch sequence:

1. Resolve the template into a complete `VmPlan`.
2. Reject a live or unverifiable runtime directory with the same job ID.
3. Create the directory as the job user with mode `0700`.
4. Preflight all files, binaries, feature combinations, and QEMU capabilities.
5. Build the complete QEMU argv without starting anything.
6. Atomically write a versioned `state.json` with `state=starting`.
7. Remove only the plan's known stale socket paths.
8. Start all independent sidecars. Starting them sequentially in stable order
   is sufficient; wait for readiness against a common deadline.
9. A sidecar is ready only when every required path is a Unix socket and its
   process is alive. Do not connect to a single-client vfio-user socket merely
   to probe it.
10. Start QEMU, wait for QMP, continue the VM, and wait for SSH as today.
11. Atomically update `state.json` to `running` with QEMU and sidecar process
    identities.
12. Observe QEMU and all critical sidecars until the VM stops.

At every failure boundary, stop and reap everything already started in reverse
order. Include the last bounded portion of the failing sidecar log in the
error while retaining the full log path.

Recommended stop sequence:

1. Set `state=stopping`; reject new start operations for the runtime.
2. Ask QEMU to power down through QMP, escalating to QMP quit and then signal
   as current policy requires.
3. Wait for QEMU to exit. Sidecars must remain available until QEMU has closed
   its connections.
4. Send SIGTERM to every remaining sidecar process group in reverse start
   order, wait a bounded interval, then SIGKILL remaining groups.
5. Reap children, unlink known sockets, and handle save/overlay work.
6. Set `state=stopped` and remove the runtime directory when policy permits.

### Liveness and restart policy

All four current sidecar types should initially be **critical**. Unexpected
death while QEMU is running should fail the VM and trigger orderly teardown.

Do not automatically restart rocJitsu or rocm-ernic underneath a running QEMU.
Socket recreation does not prove that QEMU can reconnect or that emulated
device state survived. Compose's `restart: on-failure` is useful when cycling
the whole Compose service graph; it is not evidence for transparent, in-place
recovery of a guest PCI device.

A sidecar exit during `STOPPING`, after QEMU closes its socket, is expected and
should not turn a clean VM shutdown into a failure.

### Process identity and manifest

`vm.json` should evolve into a versioned runtime manifest written before the
first long-running process starts. For compatibility it may keep the old name,
but `state.json` better describes its purpose.

Example shape:

```json
{
  "schema": 1,
  "generation": "a random launch UUID",
  "job_id": 12345,
  "state": "running",
  "boot_id": "host boot UUID",
  "supervisor": {"pid": 2000, "start_ticks": 9990},
  "qemu": {"pid": 2004, "pgid": 2004, "start_ticks": 10002},
  "sidecars": [
    {
      "name": "rocm-ernic-0",
      "pid": 2001,
      "pgid": 2001,
      "start_ticks": 9994,
      "sockets": ["vfu/ernic-0.sock"],
      "log": "sidecars/rocm-ernic-0.log"
    }
  ]
}
```

Use an atomic temporary-file + `os.replace()` update. The live manager should
hold `Popen` handles (and pidfds where available). Recovery code must compare
the host boot ID and `/proc/<pid>/stat` start time before signaling a persisted
PID. An executable/argv sanity check adds defense in depth. Never signal an
unverified, reused PID.

Start each long-running child in its own session, record its process group, and
stop the process group rather than only its leader. This catches helper
descendants. `setsid()` does not escape the inherited Slurm cgroup.

### Blocking, attached, and detached modes

The same supervisor logic must serve all entry points:

- **Slurm/blocking launch:** the current `vmocs` process can run the supervisor
  loop and remain the task Slurm waits on.
- **Attached session:** the SSH/QMP session loop and VM supervisor should share
  one lifecycle state instead of each deleting the runtime directory directly.
- **Detached launch:** start a dedicated supervisor, wait on a readiness pipe,
  and return only after it reports `running` or a detailed failure. `vmocs
  stop` should send a command over a runtime control socket so save and cleanup
  are serialized by the owner.

Until the detached supervisor and control path exist, reject `--detach` for
templates containing critical vfio-user sidecars. The primary Stage 3 Slurm
path is blocking, so this need not block initial hardware bring-up.

### QEMU machine and memory composition

Memory configuration is a VM-wide concern and must not be emitted by an
individual sidecar adapter.

Compute:

```text
shared_guest_memory = any(
    contribution.requires_shared_guest_memory for contribution in plan
)
```

When true, emit exactly one memory object and exactly one machine option:

```text
-m <memory_mb>
-object memory-backend-memfd,id=vmocs.ram,size=<memory_mb>M,share=on
-machine type=q35,accel=kvm,memory-backend=vmocs.ram
```

Merge existing machine properties such as `smm=on` into that same `-machine`
value. If KVM is unavailable, select the existing fallback without emitting a
second machine option.

Use this machine-bound form whenever vfio-user is present. It is the form
proved by Stage 2 and is required for the server's guest-memory DMA mappings.
If virtio-fs is also present, it uses the same shared RAM object; do not add the
current second `-numa node,memdev=mem` path.

For an initial Stage 3 template:

- use the tested QEMU build with `vfio-user-pci` support;
- use `q35`;
- use `EPYC`, matching the proven `qemu-tool` command, until `-cpu host` is
  tested explicitly;
- do not emit two `-machine` options;
- do not use physical-device root-port policy for vfio-user devices.

Each vfio-user attachment should be generated with `json.dumps`, not string
interpolation, and match the tested form:

```json
{
  "driver": "vfio-user-pci",
  "id": "vfio-user-rocjitsu-0",
  "rombar": 0,
  "socket": {"path": "/runtime/vfu/rocjitsu-0.sock", "type": "unix"}
}
```

Attach directly to the root bus for the first implementation, as Stage 2 did.
Do not route these devices through `pci-root-port: true`, which currently
belongs to physical AMD GPU passthrough.

### Sidecar-specific contracts

| Adapter | Process argv | Readiness | QEMU contribution | Shared guest RAM |
|---|---|---|---|---|
| `swtpm` | current `swtpm socket ... --tpm2` | `tpm.sock` is a socket, process alive | chardev + tpmdev + `tpm-tis` | no |
| `virtiofsd-N` | current daemon + mount-specific path | `virtiofs-N.sock` is a socket, process alive | chardev + `vhost-user-fs-pci` | yes |
| `rocm-ernic-N` | `rocm-ernic -s <socket>` | `vfu/ernic-N.sock` is a socket, process alive | `vfio-user-pci`, `rombar=0` | yes |
| `rocjitsu-N` | `rocjitsu --config <resolved profile> --vfio-socket <socket>` | `vfu/rocjitsu-N.sock` is a socket, process alive | `vfio-user-pci`, `rombar=0` | yes |

Use zero-based internal IDs consistently unless there is a compatibility need
for the experiment's `-1.sock` names. Socket names are an internal runtime
detail; guest PCI IDs must be discovered from the device rather than hard-coded.
Stage 2 observed `1022:8001` for rocm-ernic and `1002:75c1` for rocJitsu, but
the socket protocol is the stable integration contract.

### Preflight

Preflight must finish before any sidecar starts:

- every selected binary exists, is executable, and can load its runtime
  libraries on the target compute-node OS;
- the rocJitsu profile resolves through an operator allow-list and is readable;
- all shared host paths exist and are directories;
- runtime path lengths fit Unix-socket limits;
- the exact QEMU binary runs and reports `vfio-user-pci` in `-device help`;
- the selected QEMU supports the requested machine type and devices;
- vfio-user uses a compatible `q35` plan and shared memory;
- snapshot and device topology are compatible;
- the guest-memory size plus configured sidecar/QEMU headroom fits policy.

This must be capability-based, not just a QEMU version comparison.

The Bates QEMU 11.1.1 binary was proved on the Ubuntu 24.04 experiment node.
A 2026-09-19 check on the current Rocky Linux 9.4 control host found that the
copied binary does not start there (`GLIBC_2.38` and `libslirp.so.0` are
unavailable). This does not invalidate the experiment, but it demonstrates
that copying a binary between distributions is not a cluster packaging plan.
Build or package the QEMU and sidecars for the compute-node OS, and run the
capability probe on every supported node image.

### Slurm resource ownership

Native children of the job-user `vmocs` process inherit its Slurm cgroup. This
gives the desired accounting and job-end containment even though each process
starts a new session.

A Docker-launched sidecar is created by the Docker daemon and may not remain in
the job's cgroup without explicit runtime integration. It also needs shared
IPC for the tested memfd transfer, a bind-mounted socket directory, container
identity cleanup, and job-user authorization to the daemon. These are strong
reasons not to make Docker the production default.

The SPANK plugin currently subtracts only generic QEMU headroom from the Slurm
memory allocation. rocJitsu and rocm-ernic add host RSS and possibly other
allocations. Operator configuration should therefore provide conservative
per-sidecar memory overhead, and guest-memory calculation should subtract the
sum before QEMU starts. Do not guess a permanent value from the Stage 2 attach
test; measure realistic emulator workloads first.

### Runtime security

- Create each VM runtime directory `0700` as the job user.
- Run native sidecars and QEMU as that same user.
- Keep sockets `0600` where the servers permit it. `chmod 666` was needed only
  because the Stage 2 container created a root-owned socket; it should not be
  normal production behavior.
- Resolve binaries and rocJitsu profiles from operator-owned configuration.
- Do not accept shell text, arbitrary environment expansion, or unreviewed
  QEMU fragments in `emulated-devices`.
- Open one log file per sidecar with non-secret argv/status in the manifest.
- Validate a persisted process identity before signaling it.

### Snapshots

QEMU memory snapshots depend on machine, RAM backend, and device topology. A
snapshot created without vfio-user devices must not be silently restored with
them added. Likewise, TPM and device-emulator state may not be reconstructible
from only the current disk and QEMU memory files.

For the first implementation, reject `snapshot` combined with rocJitsu or
rocm-ernic. A later implementation can store a hash of the resolved machine,
memory backend, sidecar/device list, and relevant sidecar state in the snapshot
metadata and require an exact match on restore.

### Observability

Each process should have:

- a stable logical name;
- PID, process group, verified start identity, start/stop timestamps, and exit
  status in the runtime manifest;
- `sidecars/<name>.log` with stdout and stderr;
- structured supervisor log messages for `starting`, `ready`, `exited`,
  `term`, and `kill`;
- `vmocs list` output that distinguishes `starting`, `running`, `degraded`,
  `stopping`, `failed`, and stale manifests.

A launch error should name the failed sidecar, its exit code or readiness
deadline, expected sockets, and log path. QEMU capability failure should occur
before sidecar startup.

## Incremental implementation plan

### Change 1: establish ownership with existing sidecars

1. Introduce the pure planning types and `SidecarManager`.
2. Move `swtpm` and `virtiofsd` startup out of `build_qemu_cmdline()`.
3. Make QEMU command construction pure.
4. Write the runtime manifest before spawning and record verified identities.
5. Put one `finally`-based cleanup path behind launch, run, SSH failure, QMP
   failure, normal QEMU exit, and explicit stop.
6. Add sidecar logs, socket-type checks, early-exit checks, bounded TERM/KILL,
   and reverse-order rollback.

This change should preserve existing template syntax and behavior.

### Change 2: add vfio-user device support

1. Add typed `emulated-devices` configuration and operator binary/profile
   mapping.
2. Add rocJitsu and rocm-ernic adapters with deterministic socket paths.
3. Add QEMU capability probing.
4. Compose one machine-bound shared memfd for virtio-fs and/or vfio-user.
5. Add JSON `vfio-user-pci` devices with `rombar=0` and no physical-GPU root
   ports.
6. Reject snapshots and detached mode for these devices initially.
7. Package host-native binaries for the actual compute-node distribution.

This is sufficient for the Stage 3 Slurm/blocking attachment milestone.

### Change 3: complete detached supervision

1. Extract the common wait/signal/QMP logic into `VmSupervisor`.
2. Add a readiness pipe between detached parent and supervisor.
3. Add a runtime Unix control socket for stop/save/status commands.
4. Monitor critical sidecars and terminate QEMU on unexpected sidecar death.
5. Make `list` and `stop` prefer the supervisor and use verified-manifest
   recovery only when the supervisor is gone.

### Change 4: harden and generalize

1. Add measured per-adapter resource overhead and Slurm guest-memory policy.
2. Add multiple rocJitsu/rocm-ernic instances after their command-line and
   guest-topology requirements are confirmed.
3. Add a central PCI address allocator only if stable guest BDFs are required.
4. Add snapshot compatibility metadata only after emulator state semantics are
   defined.
5. Consider a container or systemd runner only behind the same `ProcessSpec`
   contract and only for a demonstrated deployment need.

## Test strategy

### Unit tests

- Planning creates deterministic names, socket paths, and attachment order.
- QEMU building starts no processes.
- No-sidecar, TPM-only, virtio-fs-only, vfio-user-only, and combined
  virtio-fs + vfio-user plans each emit the expected fragments.
- A vfio-user plan has exactly one `-machine`, one shared memfd, `share=on`,
  `memory-backend=...` on the machine, and no `-numa node,memdev=...`.
- Physical `vfio-pci,host=` and userspace `vfio-user-pci` stay separate.
- JSON socket paths are escaped correctly.
- Unknown device types/profiles, incompatible snapshots, duplicate IDs, and
  unsupported QEMU capabilities fail during preflight.

### Manager tests with fake executables

Use tiny test helpers that create a Unix socket, delay readiness, exit early,
spawn a child, or ignore SIGTERM. Verify:

- successful readiness;
- a regular file is not accepted as a socket;
- early process exit beats the deadline;
- common-deadline timeout reports the right process and log;
- failure of sidecar N rolls back sidecars 0..N-1;
- process groups receive TERM then KILL and are reaped;
- QEMU-start and SSH-start failures stop all sidecars;
- runtime state updates are atomic;
- stale/reused PIDs are never signaled.

### End-to-end acceptance

On the Stage 3 compute node:

1. `vmocs launch <template>` starts the requested native sidecars inside the
   job cgroup, waits for their sockets, and then starts the selected vfio-user
   QEMU.
2. QMP and SSH become ready.
3. `lspci -nn` shows both current emulated devices. The current IDs may be
   `1022:8001` and `1002:75c1`, but acceptance should identify devices by the
   server configuration/log rather than assume permanent IDs.
4. PCI config space is readable and the devices can be enabled, matching the
   Stage 2 bar. `/dev/kfd` and RDMA verbs are not required for this milestone.
5. Guest shutdown, `vmocs stop`, Slurm cancellation, QEMU startup failure, SSH
   timeout, and injected sidecar death leave no QEMU, sidecar descendants,
   containers, or sockets.
6. The same checks pass with a virtio-fs mount and both vfio-user devices,
   proving that the single shared memory plan composes.

## Open decisions before implementation

1. What install prefixes and packaging mechanism will provide QEMU,
   rocJitsu, rocm-ernic, and their libraries on each compute-node OS?
2. Is the first public template syntax an ordered `emulated-devices` list, or
   are separate `rocjitsu:` and `rocm-ernic:` keys preferred?
3. Is more than one emulator instance required in the first merge?
4. For rocm-ernic, is Stage 3 limited to its loopback backend, or must the first
   version model multi-VM manager/worker networking?
5. What measured host-memory reservation is required for each emulator under
   realistic workloads?

None of these changes the lifecycle architecture. For the narrowest Stage 3
merge, use one instance of each, the tested loopback behavior, native binaries,
and operator-defined paths.

## References

- [QEMU vfio-user device documentation](https://www.qemu.org/docs/master/system/devices/vfio-user.html)
- [QEMU vfio-user protocol](https://www.qemu.org/docs/master/interop/vfio-user.html)
- [QEMU vhost-user backends and shared memory](https://www.qemu.org/docs/master/system/devices/virtio/vhost-user.html)
- [QEMU `-machine memory-backend` and `memory-backend-memfd` options](https://www.qemu.org/docs/master/system/qemu-manpage.html)
- [libvfio-user](https://github.com/nutanix/libvfio-user)
