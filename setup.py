from setuptools import setup, find_packages

setup(
    name='vmocs',
    version='0.0.3',
    description='Lightweight VM launcher for SLURM',
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
