#  Copyright (C) 2026 Naveenraj Muthuraj
#  SPDX-License-Identifier: GPL-3.0-or-later

import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time

import click

from . import __version__, VmocsError
from .config import Config
from .templates import TemplateConfig
from .launch import launch_vm, teardown_vm, _graceful_shutdown
from .monitor import QemuMonitor
from .snapshot import create_snapshot


def _load(config_path=None):
    cfg = Config(config_path)
    tpls = TemplateConfig()
    tpls.load(cfg.templates_path)
    return cfg, tpls


@click.group()
@click.version_option(version=__version__)
@click.option('--config', default=None, metavar='PATH',
              help='Path to vmocs.yaml (default: confs/vmocs.yaml)')
@click.pass_context
def cli(ctx, config):
    """vmocs - Lightweight VM launcher for SLURM."""
    ctx.ensure_object(dict)
    ctx.obj['config_path'] = config


# ---------------------------------------------------------------------------
# template subgroup
# ---------------------------------------------------------------------------

@cli.group('template')
def template_group():
    """List and inspect VM templates."""
    pass


@template_group.command('list')
@click.pass_context
def template_list(ctx):
    """List available templates."""
    _, tpls = _load(ctx.obj['config_path'])
    for name in sorted(tpls):
        desc = tpls[name].settings.get('description', '')
        click.echo(f'{name:<24} {desc}' if desc else name)


@template_group.command('show')
@click.argument('name')
@click.pass_context
def template_show(ctx, name):
    """Show all settings for a template (inheritance resolved)."""
    _, tpls = _load(ctx.obj['config_path'])
    if name not in tpls:
        raise VmocsError(f"template '{name}' not found")
    for key, val in sorted(tpls[name].resolved().items()):
        click.echo(f'{key:<20} {val}')


# ---------------------------------------------------------------------------
# snapshot subgroup
# ---------------------------------------------------------------------------

@cli.group('snapshot')
def snapshot_group():
    """Create and manage VM snapshots for fast boot."""
    pass


@snapshot_group.command('create')
@click.argument('template_name')
@click.argument('snap_dir')
@click.option('--cores', default=2, show_default=True, help='vCPUs for snapshot VM')
@click.option('--memory', default=2048, show_default=True,
              metavar='MB', help='RAM in MB for snapshot VM')
@click.pass_context
def snapshot_create(ctx, template_name, snap_dir, cores, memory):
    """Boot TEMPLATE_NAME, wait for SSH, save memory + disk to SNAP_DIR.

    The resulting snapshot directory can be referenced in templates.yaml
    via the 'snapshot:' field to enable fast (~3-5s) VM restores.
    """
    cfg, tpls = _load(ctx.obj['config_path'])
    if template_name not in tpls:
        raise VmocsError(f"template '{template_name}' not found")
    tpl = tpls[template_name]
    click.echo(f'Booting {template_name!r} to create snapshot at {snap_dir!r} ...')
    create_snapshot(cfg, tpl, snap_dir, cores=cores, memory_mb=memory)
    click.echo(f'Snapshot ready at {snap_dir}')
    click.echo(f'Add to templates.yaml:  snapshot: {snap_dir}')


# ---------------------------------------------------------------------------
# launch
# ---------------------------------------------------------------------------

@cli.command()
@click.argument('template_name')
@click.option('--cores', default=2, show_default=True, help='Number of vCPUs')
@click.option('--memory', default=2048, show_default=True,
              metavar='MB', help='RAM in MB')
@click.option('--job-id', default=None, type=int,
              help='Job ID (defaults to PID)')
@click.option('--pci', 'pci_devices', multiple=True, metavar='BDF',
              help='PCI device to pass through (e.g. 0000:03:00.0). Repeatable.')
@click.option('--detach', is_flag=True,
              help='Return immediately after VM is ready (QEMU runs in background). '
                   'Default blocks — required for Slurm job containment.')
@click.option('--ssh', 'open_ssh', is_flag=True,
              help='Open an interactive SSH session after boot')
@click.pass_context
def launch(ctx, template_name, cores, memory, job_id, pci_devices, detach, open_ssh):
    """Launch a VM from TEMPLATE_NAME."""
    cfg, tpls = _load(ctx.obj['config_path'])
    if template_name not in tpls:
        raise VmocsError(f"template '{template_name}' not found")

    tpl = tpls[template_name]
    click.echo(f'Launching VM from template {template_name!r} '
               f'({cores} cores, {memory} MB)...')

    meta = launch_vm(cfg, tpl, cores, memory, job_id, pci_devices=pci_devices)

    click.echo(f'VM ready  job_id={meta["job_id"]}  '
               f'ssh -i {meta["key_path"]} '
               f'-p {meta["ssh_port"]} '
               f'{meta["ssh_user"]}@127.0.0.1')

    qemu_pid    = meta['pid']
    qmp_socket  = meta['qmp_socket']
    runtime_dir = meta['runtime_dir']

    if detach:
        return

    # pcocc-style SIGTERM handling (mirrors Hypervisor.py:1840-1864):
    #   Attempt 1 & 2: send ACPI powerdown, reschedule SIGTERM in 10s if VM
    #                  does not shut down on its own.
    #   Attempt 3:     send QMP quit to force QEMU exit immediately.
    # waitpid() is restarted automatically after each signal (PEP 475), so
    # the main thread keeps waiting while the timer thread reschedules signals.
    _attempts = [0]
    _timer    = [None]

    def _cancel_timer():
        if _timer[0]:
            _timer[0].cancel()
            _timer[0] = None

    def _send_qmp(fn):
        """Run a QMP command in a daemon thread so the signal handler returns fast."""
        def _run():
            try:
                mon = QemuMonitor(qmp_socket)
                fn(mon)
                mon.close()
            except Exception:
                pass
        threading.Thread(target=_run, daemon=True).start()

    def _on_signal(signum, frame):
        _cancel_timer()
        _attempts[0] += 1
        if _attempts[0] < 3:
            _send_qmp(lambda m: m._validate('{"execute": "system_powerdown"}\n\n'))
            _timer[0] = threading.Timer(10, os.kill, [os.getpid(), signal.SIGTERM])
            _timer[0].daemon = True
            _timer[0].start()
        else:
            _send_qmp(lambda m: m.quit())
            # Belt-and-suspenders: if QMP fails, force kill after 5s
            threading.Timer(5, os.kill, [qemu_pid, 9]).start()

    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)

    if open_ssh:
        # subprocess (not execvp) so we return here when the session ends
        subprocess.call([
            'ssh',
            '-i', meta['key_path'],
            '-o', 'StrictHostKeyChecking=no',
            '-o', 'UserKnownHostsFile=/dev/null',
            '-p', str(meta['ssh_port']),
            f'{meta["ssh_user"]}@127.0.0.1',
        ])
        # User exited the SSH session — shut the VM down cleanly
        _graceful_shutdown(qmp_socket, qemu_pid)
        sys.exit(0)

    # Block until QEMU exits: Slurm SIGTERM, vmocs stop, or guest poweroff.
    try:
        os.waitpid(qemu_pid, 0)
    except ChildProcessError:
        pass
    finally:
        _cancel_timer()
        shutil.rmtree(runtime_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# list / stop
# ---------------------------------------------------------------------------

@cli.command('list')
@click.pass_context
def vm_list(ctx):
    """List running VMs."""
    cfg, _ = _load(ctx.obj['config_path'])
    runtime_base = cfg.runtime_dir

    if not os.path.isdir(runtime_base):
        return

    found = False
    for entry in sorted(os.listdir(runtime_base)):
        vm_json = os.path.join(runtime_base, entry, 'vm.json')
        if not os.path.exists(vm_json):
            continue
        with open(vm_json) as f:
            meta = json.load(f)
        # Check if QEMU is still alive
        try:
            os.kill(meta['pid'], 0)
            alive = 'running'
        except ProcessLookupError:
            alive = 'dead'
        click.echo(f"job_id={meta['job_id']}  pid={meta['pid']}  "
                   f"template={meta['template']}  "
                   f"ssh_port={meta['ssh_port']}  status={alive}")
        found = True

    if not found:
        click.echo('No VMs found.')


@cli.command()
@click.argument('job_id', type=int)
@click.option('--save', 'save_path', default=None, metavar='PATH',
              help='Flatten VM disk (with all changes) into a new qcow2 image.')
@click.pass_context
def stop(ctx, job_id, save_path):
    """Stop a running VM by JOB_ID.

    Use --save PATH to capture any changes made inside the VM into a new
    standalone qcow2 image. That image can then be used directly as a
    template image for future jobs (--vm-save equivalent).
    """
    cfg, _ = _load(ctx.obj['config_path'])
    if save_path:
        click.echo(f'Saving VM disk to {save_path} ...')
    meta = teardown_vm(job_id, runtime_base=cfg.runtime_dir, save_path=save_path)
    click.echo(f'Stopped VM job_id={meta["job_id"]}')
    if save_path:
        click.echo(f'Saved  → {save_path}')


def main():
    try:
        cli()
    except VmocsError as e:
        click.echo(f'Error: {e}', err=True)
        sys.exit(1)
