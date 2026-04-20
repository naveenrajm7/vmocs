#  Copyright (C) 2026 Naveenraj Muthuraj
#  SPDX-License-Identifier: GPL-3.0-or-later

import json
import os
import sys

import click

from . import __version__, VmocsError
from .config import Config
from .templates import TemplateConfig
from .launch import launch_vm, teardown_vm


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
# launch
# ---------------------------------------------------------------------------

@cli.command()
@click.argument('template_name')
@click.option('--cores', default=2, show_default=True, help='Number of vCPUs')
@click.option('--memory', default=2048, show_default=True,
              metavar='MB', help='RAM in MB')
@click.option('--job-id', default=None, type=int,
              help='Job ID (defaults to PID)')
@click.option('--ssh', 'open_ssh', is_flag=True,
              help='Open an interactive SSH session after boot')
@click.pass_context
def launch(ctx, template_name, cores, memory, job_id, open_ssh):
    """Launch a VM from TEMPLATE_NAME."""
    cfg, tpls = _load(ctx.obj['config_path'])
    if template_name not in tpls:
        raise VmocsError(f"template '{template_name}' not found")

    tpl = tpls[template_name]
    click.echo(f'Launching VM from template {template_name!r} '
               f'({cores} cores, {memory} MB)...')

    meta = launch_vm(cfg, tpl, cores, memory, job_id)

    click.echo(f'VM ready  job_id={meta["job_id"]}  '
               f'ssh -i {meta["key_path"]} '
               f'-p {meta["ssh_port"]} '
               f'{meta["ssh_user"]}@127.0.0.1')

    if open_ssh:
        os.execvp('ssh', [
            'ssh',
            '-i', meta['key_path'],
            '-o', 'StrictHostKeyChecking=no',
            '-o', 'UserKnownHostsFile=/dev/null',
            '-p', str(meta['ssh_port']),
            f'{meta["ssh_user"]}@127.0.0.1',
        ])


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
