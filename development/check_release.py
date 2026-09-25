#!/usr/bin/env python3
"""Validate release metadata and an optional Git tag."""

import argparse
import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SEMVER = re.compile(r'^[0-9]+\.[0-9]+\.[0-9]+$')


def _match(path, pattern, label):
    text = path.read_text()
    match = re.search(pattern, text, re.MULTILINE)
    if not match:
        raise ValueError(f'cannot find {label} in {path.relative_to(ROOT)}')
    return match.group(1)


def release_version():
    runtime = _match(
        ROOT / 'lib/vmocs/__init__.py',
        r"^__version__ = '([^']+)'$",
        'runtime version',
    )
    debian = _match(
        ROOT / 'plugins/slurm/debian/changelog',
        r'^vmocs-slurm-plugin \(([^)]+)\)',
        'Debian package version',
    )
    if runtime != debian:
        raise ValueError(
            f'version mismatch: runtime={runtime!r}, Debian={debian!r}')
    if not SEMVER.fullmatch(runtime):
        raise ValueError(
            f'release version must be stable X.Y.Z semantic version: {runtime!r}')
    return runtime


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument(
        '--tag', help='release tag to validate (must be v<version>)')
    args = parser.parse_args(argv)

    try:
        version = release_version()
        if args.tag and args.tag != f'v{version}':
            raise ValueError(
                f'release tag {args.tag!r} does not match version v{version}')
    except ValueError as error:
        print(f'release metadata error: {error}', file=sys.stderr)
        return 1

    print(version)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
