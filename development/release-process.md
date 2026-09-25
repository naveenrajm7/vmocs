# vmocs release process

vmocs uses one semantic version for the Python package, CLI, documentation,
and Slurm plugin packages. The canonical value is `__version__` in
`lib/vmocs/__init__.py`. `setup.py`, the Sphinx configuration, and the Slurm
Makefile read it directly. The top entry in `plugins/slurm/debian/changelog`
must carry the same version.

Publishing a GitHub release triggers `.github/workflows/publish.yml`. The
workflow builds every package first and attaches assets only after all builds
succeed, preventing a release from receiving a partial set of packages.

## Prepare a release

1. Choose the next semantic version. Use a minor bump for compatible features
   and a patch bump for fixes. Release tags always use `vX.Y.Z`.
2. Update `lib/vmocs/__init__.py`.
3. Add matching top entries to:
   - `plugins/slurm/debian/changelog`
   - the `%changelog` section of
     `plugins/slurm/vmocs-slurm-plugin.spec`
4. Run the release and project checks:

   ```bash
   python3 development/check_release.py --tag vX.Y.Z
   PYTHONPATH=lib pytest -q
   python3 -m compileall -q lib tests
   make -C plugins/slurm clean all
   make -C plugins/slurm deb
   git diff --check
   ```

   The Debian build needs `build-essential`, `debhelper`, `fakeroot`, and
   `libslurm-dev` (or SchedMD's `slurm-smd-dev`). The RPM build is covered in
   CI using Fedora and does not need to be reproduced on an Ubuntu workstation.
5. Merge the release-preparation PR into `main` and wait for every required CI
   check to pass.

## Publish from GitHub

1. Open **Releases → Draft a new release**.
2. Choose **Create new tag**, enter `vX.Y.Z`, and target the current `main`.
3. Use **Generate release notes**, edit them if needed, and publish the release.

That is the only publishing action. The `release: published` workflow checks
that the tag exactly matches the version in the tagged source, then produces:

- `vmocs-X.Y.Z-py3-none-any.whl`
- `vmocs-X.Y.Z.tar.gz`
- a Debian Slurm-plugin package whose filename includes the build-time Slurm
  series, Ubuntu release, and architecture
- an RPM Slurm-plugin package whose release suffix includes the build-time
  Slurm series and Fedora release
- `SHA256SUMS` covering all four packages

The final upload job runs only when the Python, Debian, and RPM builds all
succeed. A package-build failure therefore leaves the release without a
misleading partial asset set.

## Verify and consume the release

Check the workflow and asset list:

```bash
gh run list --workflow publish.yml --limit 1
gh release view vX.Y.Z
gh release download vX.Y.Z --dir /tmp/vmocs-release-X.Y.Z
cd /tmp/vmocs-release-X.Y.Z
sha256sum -c SHA256SUMS
```

Install the Python wheel with pip:

```bash
python3 -m pip install ./vmocs-X.Y.Z-py3-none-any.whl
vmocs --version
```

Install the Slurm package matching the cluster's Slurm `X.YY` series and host
distribution:

```bash
srun --version
sudo apt install ./vmocs-slurm-plugin_*_slurm-X.YY_ubuntu-*_amd64.deb
# or
sudo dnf install ./vmocs-slurm-plugin-*.slXYY*.rpm
```

SPANK plugins embed the Slurm version from the build headers. A binary package
built for a different Slurm `X.YY` series is not portable to that cluster;
build the plugin from the release source archive against the cluster's own
Slurm development headers instead.

After installation, include the packaged `/usr/share/vmocs/vmocs.conf` from
the cluster's `plugstack.conf`, deploy to both submission and compute nodes,
restart `slurmd`, and validate:

```bash
srun --help | grep -- --vm-image
srun -N1 -n1 -c2 --mem=4G --vm-image base-ubuntu hostname
```

## Recover from a failed release workflow

For a transient runner or network failure, rerun the failed jobs in GitHub
Actions. For a packaging or version error, delete the unpublished/broken
release and its tag, fix and merge the release metadata on `main`, and publish
the same tag again. Never move a published release tag after users may have
downloaded its assets.
