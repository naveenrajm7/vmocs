#  Copyright (C) 2026 Naveenraj Muthuraj
#  SPDX-License-Identifier: GPL-3.0-or-later
#
#  Paths to bundled SSH keys used by vmocs boot modes.

import os

_KEYS_DIR = os.path.join(os.path.dirname(__file__), 'keys')

# Vagrant insecure key — the canonical dev keypair from hashicorp/vagrant.
# The private key is publicly known by design; images built to the Vagrant
# convention pre-install the matching public key for the 'vagrant' user.
VAGRANT_KEY = os.path.join(_KEYS_DIR, 'vagrant_insecure_key')
VAGRANT_PUBKEY = os.path.join(_KEYS_DIR, 'vagrant_insecure_key.pub')
