# Durable VM Save and Resume

Status: Stage 1 implemented; Stages 2-5 planned
Updated: 2026-09-21

## Goal

Make `--vm-save` a dependable checkpoint contract for agentic jobs:

- a normal task exit produces a resumable VM;
- `scancel` and wall-time expiry do not race checkpoint creation against cleanup;
- a partial export is never mistaken for a valid checkpoint;
- save failure preserves the source state for retry or manual recovery;
- restore either resumes with the promised semantics or fails before boot with a
  precise incompatibility error.

The primary contract should be **durable cold resume**: preserve all managed
persistent VM state and boot it again later. Exact CPU/RAM/device continuation is
a separate, opt-in **suspended resume** contract because it cannot work for every
device configuration, especially arbitrary VFIO devices.

## Implementation status and scope

Stage 1 implements the smallest useful Enroot-like workflow:

```text
vmocs run TEMPLATE --save CHECKPOINT -- agent-command
vmocs run TEMPLATE --resume CHECKPOINT -- next-agent-command

srun --vm-image=TEMPLATE --vm-save=CHECKPOINT agent-command
srun --vm-image=TEMPLATE --vm-resume=CHECKPOINT next-agent-command
```

The first command cleanly stops QEMU and atomically publishes a thin bundle
containing the primary qcow2 overlay. The second command cold-boots using that
saved disk as an immutable base and creates a fresh per-job overlay. This makes
checkpoint generations composable without modifying an earlier checkpoint.

The Stage-1 artifact contains `disk.qcow2`, `manifest.json`, and a `COMPLETE`
marker. Resume rejects missing/incomplete markers, invalid qcow2 chains, and a
template-name mismatch. Save intent and checkpoint identity are written to
`vm.json` before QEMU starts, so the SPANK exit hook can retry a save left by an
interrupted supervisor. A save error retains the stopped runtime overlay.

Stage 1 captures only writes to the primary guest disk. It intentionally does
**not** capture RAM, running processes, CPU state, GPU/VFIO state, swtpm state,
UEFI variables, extra disks, or virtio-fs/host-mounted content. Resume is a
normal boot with newly created GPU and sidecar connections. Agents must put
their durable workspace on the primary disk and flush application state before
exit. Cancellation is best-effort within Slurm's TERM-to-KILL interval; the
persistent staging/worker design in Stage 3 is required for a hard guarantee.

This is progressive for disk persistence: Stage 2 expands the same cold bundle
and manifest. Exact continuation of RAM/process/device state is a separate,
capability-gated feature (Stage 5), not an automatic upgrade of disk resume.

### Stage-1 validation (2026-09-21)

- Python suite: 61 passed, with 3 dependency-gated skips on the development
  host.
- SPANK smoke test: built with `-Wall -Werror`; Slurm exposed `--vm-image`,
  `--vm-save`, `--vm-resume`, and `--vm-attach`.
- Reference VFIO host normal flow: saved a primary-disk marker, resumed it,
  recreated rocJitsu/swtpm/virtiofsd, and removed both runtime directories.
- Cancellation flow: sent `SIGTERM` to QEMU and the attached supervisor while
  the guest command was still running; the supervisor returned 143, published
  a complete checkpoint, and the resumed VM recovered the marker. This also
  found and fixed a QMP-disconnect waiter that could otherwise block cleanup.
- Physical-device flow: repeated save/resume with the RX 7900 XTX VGA and audio
  functions (`0000:03:00.0` and `0000:03:00.1`) attached through VFIO; both
  generations observed the physical GPU and the fresh vfio-user device.

## Executive decision

Do not make a long `qemu-img convert` part of Slurm teardown. Saving is split
into two transactions:

1. **Seal**: stop writes, make the working state durable, record a complete
   manifest, and relinquish QEMU/VFIO resources. This must be fast enough to fit
   inside Slurm's `KillWait` window.
2. **Export**: turn the sealed state into a portable standalone image, if the
   user requested one. This is restartable and may continue outside the job's
   kill domain.

The sealed checkpoint bundle, not the portable export, is the source of truth.
This avoids copying a many-gigabyte base disk during the cancellation critical
path.

```text
Slurm task
  `- vmocs run (single lifecycle owner)
       |- SSH child
       |- QMP connection
       `- QEMU + sidecars
              |
     normal exit / SIGTERM / save request
              v
     quiesce -> stop writes -> flush -> stop QEMU -> SEAL
              |                                      |
              |                          task may now terminate
              v                                      v
     checkpoint worker -----------------------> EXPORT + VERIFY
                                                       |
                                             atomic publish as COMPLETE
```

`slurm_spank_task_exit`/`slurm_spank_exit` are recovery triggers only. They must
never be the sole owner of a checkpoint and must never delete a runtime directory
that contains a pending save.

## Semantics: two kinds of resume

### 1. Cold checkpoint (required baseline)

The guest is shut down or stopped, its managed disk state is sealed, and the
next job boots from that state. This is analogous to Enroot exporting a modified
root filesystem: filesystem state persists, but host process state does not.

This mode supports GPU/VFIO jobs and is the recommended mode for agents. The
agent should write its own durable progress to disk, then exit or request a
checkpoint. On an uncooperative cancellation, the guarantee is crash-consistent
storage, not preservation of unflushed application memory.

The complete cold-checkpoint target (Stage 2 and later) includes:

- the primary disk overlay and immutable base-image identity;
- every disk declared as managed by vmocs;
- UEFI variable storage;
- swtpm state when TPM is enabled;
- the SSH credential needed to reach the already-initialized guest, stored with
  user-only permissions and rotated after a successful resume where possible;
- a manifest describing QEMU, the template, resources, devices, external
  dependencies, consistency level, and integrity hashes.

Directly attached extra disks and host mounts are external dependencies. They
are recorded and validated, not copied silently.

### 2. Suspended checkpoint (opt-in)

QEMU migration-to-file captures CPU, RAM, and migratable device state in
addition to the cold-checkpoint files. Restore continues from the paused point
rather than booting the guest.

This mode must be capability-gated and must never silently fall back to cold
resume. Initially reject it when the VM has:

- a VFIO device unless the exact device/driver stack advertises and passes a
  tested migration capability;
- a virtio-fs/vhost-user backend without a proven state-transfer path;
- a writable external disk whose contents cannot be pinned to the checkpoint;
- a QEMU machine/device configuration that differs from the saved manifest.

The first supported target should be a software-only VM with managed qcow2
disks. TPM can be added only after swtpm state save/restore is tested end to end.

QEMU requires matching device topology for migration restore. With the current
`-cpu host`, the first implementation should require the same CPU compatibility
class and QEMU machine version; it should also require the saved vCPU and RAM
sizes. Cross-version and cross-node compatibility can be widened later.

## Pre-Stage-1 implementation audit

This table records the behavior that motivated Stage 1. It is retained as
design history; the attached signal handler, atomic thin bundle, persisted save
intent, `COMPLETE` validation, and cold resume path are now implemented.

| Area | Current behavior | Problem |
|---|---|---|
| Slurm option | `--vm-save` is prepended to `vmocs run` | Good plumbing, but the save intent is not propagated for every allocator workflow and is not durable before launch |
| Attached mode | `run_attached_session()` converts the overlay in `finally` | Default `SIGTERM` terminates Python; the attached path installs no signal handler, so `finally` is not a cancellation protocol |
| Unattached mode | `_block_until_exit()` handles `SIGTERM`, then converts | Conversion can exceed `KillWait` and receive `SIGKILL` |
| SPANK exit | runs `vmocs stop --if-exists` without the save path | It can delete the only overlay after the supervisor was interrupted |
| Save content | flattens only `disk.qcow2` | Loses UEFI NVRAM, TPM state, managed extra disks, and all restore compatibility metadata |
| Output safety | converts directly to the requested path | A killed conversion leaves a plausible-looking truncated output; there is no atomic publish or `COMPLETE` marker |
| QMP ownership | session watcher holds QMP while stop/cleanup paths can open another monitor | Lifecycle operations can race and QMP availability depends on which path won |
| Disk policy | default cache mode is `unsafe` | A durability feature must not rely on ignored flush requests |
| Fast snapshot | memory migration and disk conversion are separate ad hoc steps | No bundle transaction, compatibility manifest, file `fsync`, or complete restore validation |
| Extra disks | attached directly and intentionally excluded from save | A resumed VM may observe external disk contents from a different point in time |

The old TODO diagnosis—launcher cleanup beating SPANK save—has partly been
superseded by `vmocs run` owning normal cleanup. The cancellation failure is
still present in a more direct form: attached mode has no termination state
machine, and any in-job conversion is bounded by Slurm's kill window.

## Lifecycle owner and state machine

Refactor `vmocs run` into the only process allowed to drive QMP, QEMU shutdown,
checkpoint sealing, and runtime deletion. Signal handlers only write a wakeup
byte/set a flag; all work happens in the main event loop.

Use an atomic on-disk journal, not only in-memory flags:

```text
CREATING -> RUNNING -> SAVE_REQUESTED -> QUIESCING -> STOPPING
        -> STOPPED -> SEALED -> EXPORTING -> COMPLETE
                                         `-> EXPORT_FAILED
                    `-> SEAL_FAILED
```

Every transition is written as `state.json.tmp`, `fsync`ed, renamed over
`state.json`, and followed by a parent-directory `fsync`. The journal contains
the requested destination and mode before QEMU starts, so recovery does not
depend on SPANK retaining command-line state.

Required invariants:

1. The final destination is absent or contains a `COMPLETE` manifest; never
   expose a half-written checkpoint under its final name.
2. No code removes the source runtime/staging data before `SEALED`; no code
   removes the last sealed copy before export is verified.
3. Repeating `seal`, `enqueue`, or `finalize` after a crash is idempotent.
4. Exactly one owner holds the checkpoint lock for a job/step/task identity.
5. Save failure leaves an actionable `FAILED` record and recoverable source.
6. Resume accepts only `COMPLETE` checkpoints and validates all dependencies.

Use `<job-id>.<step-id>.<task-id>` rather than job ID alone for runtime identity.
Record a PID start time or use a pidfd while the supervisor is alive so a reused
PID cannot be mistaken for QEMU during recovery.

Add a `checkpoint-staging-dir` distinct from the disposable runtime directory.
For a save-enabled job, create its save-owned writable files (disk overlays,
NVRAM, TPM state, and credentials) in a per-user `.inprogress` directory there
*before QEMU starts*. The runtime directory may still hold sockets and transient
logs. Sealing then freezes/renames metadata around files already in persistent
staging; it never copies a virtual disk during the cancellation window. The
privileged setup path must create the per-user staging root with safe ownership
and permissions.

## Save sequences

### Normal completion

1. The guest command exits and the supervisor records `SAVE_REQUESTED`.
2. Ask the guest to shut down cleanly. A future QEMU guest-agent channel may
   issue sync/fs-freeze; do not make guest-agent availability mandatory.
3. On timeout, pause QEMU, issue block flushes for all managed disks, then quit
   QEMU. Report whether the result is clean or crash-consistent.
4. Stop sidecars and seal disk/NVRAM/TPM/key material into a staging bundle.
5. Submit export to the checkpoint worker. For a normal exit, keep the Slurm
   task alive while waiting so save failure can affect the command result.
6. Verify and atomically publish the destination, then allow runtime cleanup.

Save-enabled jobs must use a flush-honoring disk cache (`writeback`, `none`,
`writethrough`, or `directsync`). Reject `unsafe` during preflight rather than
claiming durability that the configuration defeats. Changing the global default
from `unsafe` should be a separately called-out compatibility change.

### `scancel` or wall-time SIGTERM

1. The supervisor's `SIGTERM` handler wakes the event loop; it does not exit.
2. Stop/reap the SSH child and record the termination reason.
3. Attempt bounded clean shutdown. Reserve most of `KillWait` for QEMU exit and
   sealing, not flattening. If clean shutdown cannot fit, pause/flush/quit and
   label the checkpoint crash-consistent.
4. Seal the thin bundle and enqueue export. Once QEMU exits, VFIO resources are
   released and the job process may end even if portable export is still active.
5. If the supervisor is killed at any point, `slurm_spank_task_exit` asks the
   worker to reconcile the journal. It does not call the destructive generic
   `stop` path.

Slurm signals all job steps with `SIGTERM`, waits `KillWait`, then uses
`SIGKILL`. QEMU may therefore be exiting concurrently with the supervisor. The
recovery path must tolerate QEMU already being gone and validate/repair qcow2
before publishing a crash-consistent checkpoint.

An unconditional `SIGKILL`, host crash, or storage loss cannot guarantee the
latest in-memory state. The contract in those cases is: retain whatever sealed
or recoverable source exists, never publish it as complete without validation,
and surface the failure.

### Explicit suspended save

This is cooperative and should be requested before cancellation, for example by
`vmocs checkpoint JOB --mode=suspend`, a future guest control channel, or a
Slurm pre-time-limit `USR1` notification with enough lead time.

1. Validate the device/resource compatibility allowlist before launch and again
   before saving.
2. Pause the VM with QMP `stop`.
3. Flush every managed block node.
4. Migrate to a direct `file:` URI and poll `query-migrate` to completion.
5. `fsync` the migration file and its directory. QEMU documents that file
   migration does not flush cached file data/metadata at completion.
6. Quit QEMU, stop sidecars, seal the matching disk and device-sidecar state,
   then atomically publish the bundle.

Do not use a shell-interpolated `exec:lzop > PATH` URI for user-provided paths.
Use a direct file channel (or a pre-opened file descriptor where supported),
then compress as a separate restartable export step if desired.

## Checkpoint bundle

Stage 1 uses the deliberately small layout:

```text
checkpoint/
  disk.qcow2       # thin overlay; immutable after publication
  manifest.json    # scope, backing path, resources, exclusions
  COMPLETE         # contains checkpoint ID
```

The future complete-cold/suspended layout is:

Recommended on-disk layout:

```text
checkpoint.vmocs.partial.<uuid>/
  manifest.json
  state.json
  disks/
    drive0.overlay.qcow2
    ...
  nvram.fd                  # when applicable
  tpm/                      # when applicable
  ssh/id_ed25519            # mode 0600; bundle directory mode 0700
  memory.state              # suspended mode only
  logs/
    save.log
```

The complete manifest includes:

- schema version, checkpoint ID, timestamps, uid/gid, and Slurm job/step/task;
- mode (`cold` or `suspend`) and consistency (`clean`, `filesystem`, or
  `crash`), plus the event that triggered it;
- QEMU binary/version, machine type, CPU contract, RAM/vCPU sizes, and a
  normalized device-topology fingerprint;
- template name and resolved-template digest;
- each managed disk's node name, format, virtual size, backing image identity,
  and checksum/validation result;
- NVRAM, TPM, and memory-state metadata;
- external disks and mounts with identity information and resume policy;
- export/verification results and artifact hashes.

Publish with an atomic directory rename on the destination filesystem after all
files and the parent directory are durable. Portable single-qcow2 output is a
derived artifact and should be written to `PATH.partial.<uuid>`, checked with
`qemu-img check`, inspected to confirm the expected backing-file policy, synced,
and only then renamed to `PATH`.

The fast cancellation artifact may be a thin qcow2 overlay referencing an
immutable managed base. Base images therefore need stable IDs/digests and a
retention rule. A request for a self-contained/portable image triggers flattening
after seal.

## Worker and recovery

Production cancellation safety needs a small node-local checkpoint service (or
equivalent root-owned service scope) outside the Slurm task cgroup. Its API is a
local Unix socket with peer-credential checks; users can enqueue only their own
checkpoint records and destinations.

Responsibilities:

- own a persistent queue and per-checkpoint lock;
- reconcile journals left by dead supervisors;
- run throttled `qemu-img convert/check` jobs without holding QEMU or VFIO;
- resume an interrupted export from the sealed source by restarting the derived
  output, never by trusting a partial file;
- publish success/failure status and retain failed source for an administrator-
  configured period;
- enforce destination ownership, no-symlink/overwrite policy, quotas, and file
  modes.

The worker is not permission to move QEMU outside Slurm accounting. QEMU always
remains in the job cgroup. Only post-QEMU image processing runs in the service,
with explicit CPU/I/O limits. If the service is unavailable, sealing still
succeeds and the checkpoint remains `SEALED`; it must not be deleted.

For a smaller first delivery, a persistent sealed bundle can be the final
artifact without a daemon. The daemon becomes mandatory before promising that a
portable flattened qcow2 will finish after arbitrary cancellation.

## SPANK changes

1. Register and propagate save/resume options through allocator and remote
   contexts, including `salloc` followed by `srun`.
2. Pass job ID, step ID, and task ID to `vmocs run`.
3. Record save intent in the runtime journal before QEMU launch.
4. Replace destructive `slurm_spank_exit -> vmocs stop` behavior for save-enabled
   jobs with `vmocs checkpoint reconcile --if-exists`.
5. Prefer `slurm_spank_task_exit` for the recovery notification because it is
   called as task status is collected; keep `slurm_spank_exit` as a final
   idempotent fallback.
6. Never perform a large synchronous conversion inside a SPANK hook. Pyxis can
   export a stopped container filesystem there, but a VM export is much larger
   and also has QEMU/device-state ordering requirements.

Cluster configuration should expose `KillWait` to vmocs (configured value or a
conservative default) so the supervisor can budget its cancellation phases.
Increasing `KillWait` is useful operational headroom, but is not the correctness
mechanism.

## Resume path

Stage 1 uses `vmocs run TEMPLATE --resume CHECKPOINT` and the Slurm
`--vm-resume=CHECKPOINT` option. Keeping the template explicit supplies the
device and sidecar configuration and makes the compatibility check obvious.

Cold resume:

1. Verify `COMPLETE`, the manifest/schema/template, and the qcow2 backing chain.
   Later stages add ownership, hashes, stable base-image identity, and external
   dependency checks.
2. Create a new working overlay on the sealed disk chain (or use the portable
   disk as the new base).
3. Boot with fresh NVRAM, TPM, credentials, and sidecars in Stage 1; Stage 2
   restores managed persistent NVRAM/TPM state.
4. Boot normally and write a new runtime journal. A later save creates a new
   checkpoint; never mutate the old one.

Suspended resume:

1. Validate QEMU/machine/CPU/device/resource compatibility before starting.
2. Recreate all sidecars and block nodes at the same stable QEMU IDs/addresses.
3. Start QEMU paused with incoming migration from `memory.state`.
4. Wait for migration completion, verify status, then continue the VM.
5. Fail closed on any mismatch. Do not boot the disk cold unless the user
   explicitly asks to recover that way.

## API decision

Stage 1 intentionally changes `--save PATH` / `--vm-save=PATH` from a flat
qcow2 file to a checkpoint directory and adds `--resume` / `--vm-resume`.
vmocs is still pre-1.0, and a single bundle contract is clearer for the demo
and gives later stages a versioned expansion point. A portable flattened qcow2
will return as an explicit export command/format in Stage 4; it will not be
confused with a resumable checkpoint.

Whichever spelling is selected, `suspend + qcow2` is invalid because one qcow2
does not carry all sidecar/external-dependency metadata safely.

Useful status commands:

```text
vmocs checkpoint status CHECKPOINT_OR_JOB
vmocs checkpoint retry CHECKPOINT_OR_JOB
vmocs checkpoint inspect CHECKPOINT
vmocs checkpoint recover CHECKPOINT --cold   # explicit downgrade only
```

## Delivery plan

### Stage 1: thin primary-disk checkpoint (implemented)

- Persist save path and checkpoint ID before QEMU launch.
- Handle `SIGTERM`/`SIGINT` in attached sessions without abandoning cleanup.
- Stop QEMU before reading the overlay and retain runtime state on failure.
- Copy and validate the thin overlay in a private partial directory, then
  atomically publish `manifest.json`, `disk.qcow2`, and `COMPLETE`.
- Resume through a fresh overlay without mutating the checkpoint.
- Add CLI and SPANK save/resume options plus a real-QEMU save/resume harness.

Success criterion: normal save/resume preserves a guest primary-disk marker,
recreates GPU-like vfio-user sidecars, and cleans both runtime directories.

### Stage 2: complete managed cold bundle

- Move save-owned writable state into persistent staging before launch.
- Add the full journal/state machine, per-checkpoint lock, ownership checks,
  stable base-image identity, hashes, and idempotent reconciliation.
- Capture managed extra-disk overlays, UEFI variables, swtpm state, and the
  first-resume SSH credential; explicitly fingerprint external dependencies.
- Require flush-honoring disk-cache modes for save-enabled jobs.
- Validate the resolved template and device topology, not only its name.

Success criterion: all vmocs-managed persistent state resumes together, and an
incompatible or incomplete bundle fails before QEMU starts.

### Stage 3: cancellation and crash recovery

- Put staging/reconciliation in a root-owned node service outside the Slurm job
  cgroup, with peer-credential authorization and per-user retention.
- Make signal handling cover boot as well as the attached session and budget
  clean-stop/seal work against `KillWait`.
- Add `slurm_spank_task_exit` recovery notification; keep `spank_exit` as an
  idempotent fallback that never destroys a pending checkpoint.
- Recover after supervisor `SIGKILL`, node-service restart, and partially
  completed publication; expose status and retry commands.

Success criterion: after normal exit, `scancel`, wall-time termination, or
supervisor death, there is either a bootable `COMPLETE` checkpoint or retained,
diagnosable source state—never a plausible truncated artifact.

### Stage 4: portable export worker

- Add the node-local queue/service and Unix-socket authorization.
- Export sealed disk chains to standalone qcow2 using partial names.
- Validate with `qemu-img check/info`, sync, and atomically publish.
- Add retry, retention, quotas, I/O throttling, and service-restart recovery.

Success criterion: cancel during any second of a multi-minute Windows image
conversion; the worker later publishes one valid standalone image and the job's
runtime source is not lost.

### Stage 5: suspended full-VM checkpoints

- Add QMP block flush and direct file migration helpers.
- Define the strict compatibility manifest/fingerprint.
- Implement capability rejection for VFIO, vhost-user, mutable external disks,
  and untested TPM combinations.
- Restore with incoming migration and exact topology.
- Add a cooperative checkpoint request and pre-time-limit signal workflow.

Success criterion: a software-only test VM resumes an in-memory counter/process
at the exact saved point; each unsupported device configuration fails before the
job starts with a precise reason.

### Stage 6: agent workflow polish

- Add an optional guest control channel/guest agent for sync, progress, and
  explicit save requests.
- Expose checkpoint status in job output and machine-readable CLI output.
- Add checkpoint generations, retention policy, and garbage collection.
- Document framework-level checkpoints as the supported path for GPU agents.

## Test and fault-injection matrix

Unit tests:

- every legal/illegal state transition and repeated recovery call;
- atomic JSON/manifest writes and refusal to consume partial destinations;
- signal during attached SSH, boot, QEMU stop, seal, export, and cleanup;
- save error preserves runtime; success removes it only after policy permits;
- plugin option propagation and task-exit reconciliation;
- manifest compatibility checks for QEMU topology, disks, NVRAM, TPM, mounts,
  VFIO, and virtio-fs;
- destination collision, symlink, ownership, quota, and ENOSPC handling.

Slurm end-to-end tests:

1. normal Linux guest exit, save, resume, and verify an on-disk marker;
2. `scancel` while the guest is writing, then boot and run filesystem checks;
3. cancel during a deliberately slow export and verify worker completion;
4. wall-time expiry with and without an advance `USR1` signal;
5. kill the supervisor at every persisted state and run reconciliation;
6. restart the checkpoint service during export;
7. save/resume UEFI NVRAM and TPM state;
8. large Windows image export and reboot validation;
9. extra persistent disk changed between save and resume;
10. suspended software-only VM exact continuation;
11. suspended save rejection for VFIO and virtio-fs;
12. destination full/unavailable and later retry.

For portable qcow2 acceptance, require all of:

- `qemu-img check` succeeds;
- `qemu-img info` reports the intended backing-file policy;
- the image boots and reaches SSH within the template timeout;
- the marker written and synced before save is present;
- the final artifact appeared only by atomic rename after verification.

Track time-to-quiesce, time-to-seal, export duration/bytes, consistency level,
recovery count, and failure reason. Set a cold-seal target comfortably below the
cluster's minimum `KillWait`; do not set the target until representative Linux
and Windows workloads are measured.

## Research basis

- QEMU distinguishes complete VM snapshots (CPU, RAM, devices, and writable
  disks) from temporary `-snapshot` mode and documents device limitations:
  <https://qemu.readthedocs.io/en/master/system/images.html#vm-snapshots>
- QEMU migration restore requires matching devices/configuration, and file
  migration does not flush cached file data/metadata automatically:
  <https://www.qemu.org/docs/master/devel/migration/main.html>
- VFIO state migration requires device/driver support and has explicit stop/copy
  state machinery; it cannot be assumed for an arbitrary passed-through GPU:
  <https://www.qemu.org/docs/master/devel/migration/vfio.html>
- QEMU guest-agent fs-freeze can provide a stronger filesystem-consistency
  point when available:
  <https://qemu.readthedocs.io/en/master/interop/qemu-ga-ref.html#command-guest-fsfreeze-freeze>
- Slurm cancellation sends `SIGTERM`, waits `KillWait`, then sends `SIGKILL`:
  <https://slurm.schedmd.com/scancel.html>
- SPANK calls `slurm_spank_task_exit` as task status is collected and
  `slurm_spank_exit` before slurmstepd exits:
  <https://slurm.schedmd.com/spank.html>
- Pyxis performs Enroot export from `slurm_spank_task_exit`; that is useful
  lifecycle precedent, but Enroot exports a stopped filesystem and does not
  solve QEMU RAM/device/disk transactionality:
  <https://github.com/NVIDIA/pyxis/blob/main/pyxis_slurmstepd.c>
