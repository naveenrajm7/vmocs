# vmocs TODO

## Pending

### Two-level template system (system + user) with decoupled templates path

Currently `templates.yaml` must live in the same directory as `vmocs.yaml`. This
prevents cluster admins from placing templates in a shared path, and there is no
per-user template support.

**Goal:** Mirror pcocc's pattern — system templates loaded first (required), user
templates merged on top (optional, silently skipped if absent).

**Files to change:**

- `lib/vmocs/config.py` line 39 — replace `self.templates_path` with two attrs:
  - `self.system_templates_path` — `$VMOCS_SYSTEM_CONF_DIR/templates.yaml`
    (falls back to dirname of config file if env var not set)
  - `self.user_templates_path` — `~/.vmocs/templates.yaml`

- `lib/vmocs/templates.py` `TemplateConfig.load()` — add `required=True` parameter;
  when `required=False` and file is absent (`errno.ENOENT`), silently return instead
  of raising `InvalidConfigError`. Duplicate names across loads still raise an error.

- `lib/vmocs/cli.py` `_load()` — call `tpls.load()` twice:
  ```python
  tpls.load(cfg.system_templates_path, required=True)
  tpls.load(cfg.user_templates_path, required=False)
  ```

**Behaviour:**

| Scenario | Result |
|---|---|
| `/etc/vmocs/templates.yaml` exists | Loaded as system templates (required) |
| `VMOCS_SYSTEM_CONF_DIR=/shared/cluster/vmocs` set | Loads from that path instead |
| `~/.vmocs/templates.yaml` exists | Merged on top (optional) |
| `~/.vmocs/templates.yaml` absent | Silently skipped |
| Duplicate name in system + user | `InvalidConfigError` raised |

### QMP event watcher — reboot-survives, shutdown-ends-job

Currently `-no-shutdown` is an all-or-nothing flag: either both guest reboot and guest
shutdown keep QEMU alive, or neither does. Slurm users need the split behaviour:

- Guest reboot (`shutdown /r`) → job stays, VM comes back
- Guest shutdown (`shutdown /s`) → job ends cleanly

QEMU emits different QMP events for each: `RESET` for a reboot, `SHUTDOWN` for a
shutdown. A small watcher thread in the blocking launch path can act on them:

- `RESET` event → do nothing (QEMU resets the VM internally)
- `SHUTDOWN` event → call `mon.quit()` → QEMU exits → `waitpid` returns → job ends

Implementation sketch:
1. Start a daemon thread after `mon.cont()` that opens a second QMP connection and
   reads events in a loop.
2. On `SHUTDOWN` event, call `mon.quit()` and set an event so the main thread knows
   to skip the `waitpid` cleanup (QEMU will have already exited).
3. `-no-shutdown` remains in the template so QEMU doesn't self-exit before the watcher
   sees the event.

This is only needed in blocking mode. Detach mode has no `waitpid` so job lifetime is
unaffected by guest-initiated shutdown regardless.

### Improve VM boot time

Current baseline on this machine (HEAD, `5c9dcc8`):

| Template        | Boot mode   | Time to SSH |
|-----------------|-------------|-------------|
| `base-ubuntu`   | cloud-init  | ~5m 30s     |
| `debian-vagrant`| vagrant BIOS| ~6m 14s     |
| `debian-vagrant-uefi` | vagrant UEFI | ~17m 34s |

UEFI boot is ~3x slower than BIOS — likely OVMF POST overhead, not a code issue.
BIOS/cloud-init times (~5-6m) may also be hardware-dependent; revisit on a different
machine before optimising.

**Possible directions:**
- OVMF: try `OVMF_CODE_4M.ms.fd` (pre-enrolled keys) or disable Secure Boot to
  reduce POST time.
- Snapshot restore (`vmocs snapshot`) already exists for cloud-init — measure vs
  cold boot to confirm it is worth using by default.
- Profile guest boot with `systemd-analyze` to see if time is in firmware, kernel,
  or userspace.
