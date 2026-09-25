"""Checks that every release-facing package reports one version."""

import re
import runpy
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _output(*command):
    return subprocess.run(
        command,
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def test_release_versions_are_synchronized():
    runtime = re.search(
        r"^__version__ = '([^']+)'$",
        (ROOT / 'lib/vmocs/__init__.py').read_text(),
        re.MULTILINE,
    ).group(1)

    assert _output('make', '-s', '-C', 'plugins/slurm', 'print-version') == runtime
    assert _output(
        sys.executable, 'development/check_release.py', '--tag', f'v{runtime}'
    ) == runtime

    docs = runpy.run_path(str(ROOT / 'docs/source/conf.py'))
    assert docs['version'] == runtime
    assert docs['release'] == runtime


def test_release_tag_mismatch_is_rejected():
    result = subprocess.run(
        [sys.executable, 'development/check_release.py', '--tag', 'v9.9.9'],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1
    assert 'does not match version' in result.stderr
