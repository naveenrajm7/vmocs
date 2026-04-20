#  Copyright (C) 2014-2015 CEA/DAM/DIF
#  Copyright (C) 2026 Naveenraj Muthuraj
#
#  Based on pcocc Templates.py by CEA/DAM/DIF
#  SPDX-License-Identifier: GPL-3.0-or-later

import os
import yaml

from .error import InvalidConfigError

# (required, default, inheritable)
TEMPLATE_SETTINGS = {
    'image':         (False, None,       True),
    'image-dir':     (False, None,       True),
    'machine-type':  (False, 'q35',      True),
    'disk-model':    (False, 'virtio',   True),
    'disk-cache':    (False, 'unsafe',   True),
    'custom-args':   (False, [],         True),
    'qemu-bin':      (False, None,       True),
    'mount-points':  (False, {},         True),
    'user-data':     (False, None,       True),
    'bind-vcpus':    (False, False,      True),
    'kernel':        (False, None,       True),
    'gpu':           (False, None,       True),   # 'full', 'sriov', or None
    'ssh-user':      (False, 'root',     True),
    'ssh-timeout':   (False, 120,        True),
    'snapshot':      (False, None,       True),   # path to snapshot dir (memory + disk)
    'inherits':      (False, None,       False),
    'description':   (False, '',         False),
}


class TemplateConfig(dict):
    """Manages the VM template definitions. Adapted from pcocc TemplateConfig."""

    def load(self, path):
        try:
            with open(path) as f:
                data = yaml.safe_load(f) or {}
        except yaml.YAMLError as e:
            raise InvalidConfigError(f'YAML error in {path}: {e}')
        except IOError as e:
            raise InvalidConfigError(f'cannot read {path}: {e}')

        for name, attrs in data.items():
            if name.startswith('_'):
                raise InvalidConfigError(
                    f"template name '{name}' is reserved (starts with _)")
            if name in self:
                raise InvalidConfigError(f"duplicate template name '{name}'")
            for key in (attrs or {}):
                if key not in TEMPLATE_SETTINGS:
                    raise InvalidConfigError(
                        f"template '{name}' has unknown setting '{key}'")
            self[name] = Template(name, attrs or {}, path)

        # Validate inheritance chains now that all templates are loaded
        for tpl in self.values():
            tpl.validate(self)


class Template:
    """Single template definition with lazy inheritance. Adapted from pcocc Template."""

    def __init__(self, name, settings, source_file):
        self.name = name
        self.settings = settings
        self.source = source_file

    def __getattr__(self, attr):
        # Convert Python attr (underscores) back to YAML key (dashes)
        key = attr.replace('_', '-')
        if key not in TEMPLATE_SETTINGS:
            raise AttributeError(attr)

        required, default, heritable = TEMPLATE_SETTINGS[key]

        if key in self.settings:
            val = self.settings[key]
            return default if val is None else val

        # Walk up the inheritance chain (needs _all_templates set by validate)
        if heritable and self._parent:
            return getattr(self._parent, attr)

        if required:
            raise InvalidConfigError(
                f"template '{self.name}' has no '{key}' setting")
        return default

    def validate(self, all_templates):
        """Resolve and cache parent; check inheritance chain is acyclic."""
        self._parent = None
        parent_name = self.settings.get('inherits')
        if parent_name:
            if parent_name not in all_templates:
                raise InvalidConfigError(
                    f"template '{self.name}' inherits from unknown "
                    f"template '{parent_name}'")
            self._parent = all_templates[parent_name]

    def resolved(self):
        """Return dict of all settings with inheritance applied."""
        result = {}
        for key in TEMPLATE_SETTINGS:
            attr = key.replace('-', '_')
            try:
                result[key] = getattr(self, attr)
            except (InvalidConfigError, AttributeError):
                pass
        return result
