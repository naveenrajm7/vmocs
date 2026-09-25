import re
from pathlib import Path

from setuptools import setup, find_packages


version_text = (Path(__file__).parent / 'lib/vmocs/__init__.py').read_text()
version_match = re.search(r"^__version__ = '([^']+)'$", version_text, re.MULTILINE)
if not version_match:
    raise RuntimeError('cannot find vmocs __version__')
long_description = (Path(__file__).parent / 'README.md').read_text()

setup(
    name='vmocs',
    version=version_match.group(1),
    description='Lightweight VM launcher for SLURM',
    long_description=long_description,
    long_description_content_type='text/markdown',
    url='https://github.com/naveenrajm7/vmocs',
    license='GPL-3.0-or-later',
    package_dir={'': 'lib'},
    packages=find_packages(where='lib'),
    python_requires='>=3.8',
    install_requires=[
        'click',
        'PyYAML',
    ],
    entry_points={
        'console_scripts': [
            'vmocs=vmocs.cli:main',
        ],
    },
)
