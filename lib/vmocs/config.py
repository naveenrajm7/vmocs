#  Copyright (C) 2026 Naveenraj Muthuraj
#  SPDX-License-Identifier: GPL-3.0-or-later

import os
import yaml

from .error import InvalidConfigError

def _default_config_path():
    # 1. Env override
    if 'VMOCS_CONF' in os.environ:
        return os.environ['VMOCS_CONF']
    # 2. confs/vmocs.yaml relative to cwd (development / in-tree)
    cwd_path = os.path.join(os.getcwd(), 'confs', 'vmocs.yaml')
    if os.path.exists(cwd_path):
        return cwd_path
    # 3. System-wide default
    return '/etc/vmocs/vmocs.yaml'


class Config:
    """Loads and holds vmocs.yaml settings."""

    def __init__(self, path=None):
        if path is None:
            path = _default_config_path()
        path = os.path.abspath(path)

        try:
            with open(path) as f:
                data = yaml.safe_load(f) or {}
        except (IOError, yaml.YAMLError) as e:
            raise InvalidConfigError(f'cannot load {path}: {e}')

        self.qemu_bin = data.get('qemu-bin', 'qemu-system-x86_64')
        self.runtime_dir = data.get('runtime-dir', '/var/run/vmocs')
        self.gpu_devices = data.get('gpu-devices', [])
        self.network = data.get('network', {})
        _default_tpl = os.path.join(os.path.dirname(path), 'templates.yaml')
        self.system_templates_path = (
            os.environ.get('VMOCS_TEMPLATES')
            or data.get('templates')
            or _default_tpl
        )
        self.user_templates_path = os.path.join(
            os.path.expanduser('~'), '.vmocs', 'templates.yaml'
        )
