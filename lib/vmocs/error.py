#  Copyright (C) 2014-2015 CEA/DAM/DIF
#  Copyright (C) 2026 Naveenraj Muthuraj
#
#  Based on pcocc Error.py by CEA/DAM/DIF
#  SPDX-License-Identifier: GPL-3.0-or-later

class VmocsError(Exception):
    """Base class for exceptions in vmocs."""
    def __init__(self, error):
        self.error = error

    def __str__(self):
        return self.error


class InvalidConfigError(VmocsError):
    """Syntax or semantic errors in configuration files."""
    def __init__(self, error):
        super().__init__('configuration error: ' + error)


class HypervisorError(VmocsError):
    """Errors during QEMU/hypervisor operations."""
    pass


class ImageError(VmocsError):
    """Errors during image operations."""
    pass
