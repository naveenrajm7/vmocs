#  Copyright (C) 2026 Naveenraj Muthuraj
#  SPDX-License-Identifier: GPL-3.0-or-later
#
#  Paths to bundled SSH keys used by vmocs boot modes.

import atexit
import os
import shutil
import tempfile

from .error import HypervisorError

_KEYS_DIR = os.path.join(os.path.dirname(__file__), 'keys')

# Vagrant insecure key — the canonical dev keypair from hashicorp/vagrant.
# The private key is publicly known by design; images built to the Vagrant
# convention pre-install the matching public key for the 'vagrant' user.
VAGRANT_KEY = os.path.join(_KEYS_DIR, 'vagrant_insecure_key')
VAGRANT_PUBKEY = os.path.join(_KEYS_DIR, 'vagrant_insecure_key.pub')

_staged_vagrant_key = None


def vagrant_key_path():
    """Return a Vagrant insecure key path that OpenSSH will actually load.

    The bundled key's mode and owner depend on how vmocs was installed: a
    checkout or a user-owned virtualenv leaves it at 0644 owned by the caller,
    which OpenSSH refuses ('Permissions 0644 are too open'), while a
    root-installed copy is fine. Stage a private 0600 copy once per process so
    vagrant boot mode behaves the same under every install layout.
    """
    global _staged_vagrant_key
    if _staged_vagrant_key and os.path.exists(_staged_vagrant_key):
        return _staged_vagrant_key
    if not os.path.exists(VAGRANT_KEY):
        raise HypervisorError(
            f'bundled Vagrant key is missing from the installed package '
            f'({VAGRANT_KEY}); reinstall vmocs from a build that packages '
            'vmocs/keys/')
    staging_dir = tempfile.mkdtemp(prefix='vmocs-keys-')
    atexit.register(shutil.rmtree, staging_dir, True)
    staged = os.path.join(staging_dir, 'vagrant_insecure_key')
    shutil.copyfile(VAGRANT_KEY, staged)
    os.chmod(staged, 0o600)
    _staged_vagrant_key = staged
    return staged
