---
name: vmocs-release
description: Prepare, validate, and publish a vmocs GitHub release containing the Python wheel and source distribution plus Debian and RPM Slurm-plugin packages. Use when the user asks to cut, tag, publish, verify, recover, or document a vmocs release, requests a version bump, or invokes /vmocs-release.
---

# vmocs-release

Treat this file as the canonical vmocs release procedure. `$SCRATCH` means the
agent's scratch directory, or a directory created with `mktemp -d`.

Accept one optional argument: an exact stable semantic version `x.y.z`. If it
is omitted, propose the next patch version. If the user requests a major or
minor bump, calculate that version instead. Release tags are always `vX.Y.Z`.

## Ground rules

- Require explicit user approval of the exact tag, target commit, and release
  notes before publishing the GitHub release. Publishing triggers public
  package builds and cannot be cleanly undone after users download assets.
- Release only a commit merged into `main`, with `main` equal to
  `origin/main`. Never release from a feature branch.
- Never move or replace a published tag that has assets or may have been
  consumed. Use a new patch version instead.
- Keep unrelated working-tree changes untouched. Commit only release metadata.
- Do not inspect or change GitHub Actions secrets or variables.

## Step 1: Preflight

Run:

```sh
git fetch origin --tags
git status --short
git rev-parse --abbrev-ref HEAD
git rev-list --left-right --count HEAD...origin/main
python3 development/check_release.py
gh auth status
gh release list --limit 10
```

Stop and ask before continuing if:

- the working tree is not clean;
- `main` and `origin/main` differ;
- GitHub authentication is unavailable; or
- the requested tag already exists locally, remotely, or as a GitHub release.

Read the current version from `python3 development/check_release.py`. Treat the
newest published GitHub release as the previous release. The requested version
must be greater than that release. It must also be greater than the source
version when a bump is needed. If the source version already equals the
requested version and its tag does not exist, skip the metadata bump and use
current `main` as the release candidate.

## Step 2: Review changes and choose the version

Inspect everything merged since the previous release:

```sh
git log --first-parent --reverse --format='%H %an <%ae>%n  %s' <previous-tag>..origin/main
```

For each merge or squash commit, inspect the corresponding pull request when
needed:

```sh
gh pr view <number> --json title,body,author,commits,files,url
```

Summarize user-visible features, fixes, compatibility changes, and packaging
changes. Omit internal refactors and tests unless they materially change what
ships. Use semantic versioning and explain the proposed bump to the user if
they did not supply an exact version.

## Step 3: Prepare release metadata

If a bump is required, create a focused branch following the active agent's
required branch-prefix convention. Update only:

1. `lib/vmocs/__init__.py`: set `__version__ = 'x.y.z'`.
2. `plugins/slurm/debian/changelog`: prepend an `x.y.z` entry with the release
   changes, maintainer, and RFC 2822 date.
3. `plugins/slurm/vmocs-slurm-plugin.spec`: prepend a matching `%changelog`
   entry using RPM's date format.

Do not edit `setup.py`, the Sphinx configuration, or the Slurm Makefile merely
to bump the version; they derive it from `lib/vmocs/__init__.py`.

Validate the candidate:

```sh
python3 development/check_release.py --tag vx.y.z
PYTHONPATH=lib pytest -q
python3 -m compileall -q lib tests
python3 -m build
python3 -m twine check dist/*
make -C plugins/slurm clean all
git diff --check
```

Use an isolated virtual environment for missing Python build/test tools. If
local Slurm headers are unavailable, record that the SPANK build was skipped;
the pull-request CI jobs remain mandatory.

Commit the three metadata files, push the branch, and open a pull request. Its
description must cover the version, changes, validation, expected release
assets, and Slurm ABI limitation. Wait for all seven CI checks: Python package,
Python 3.8/3.10/3.12 tests, SPANK smoke test, Debian package, and RPM package.
Do not merge unless the user authorized it. Otherwise, stop with the PR URL and
ask them to merge it.

After the PR merges, return to `main`, pull with `--ff-only`, and repeat the
preflight. Record the exact target SHA:

```sh
git switch main
git pull --ff-only
python3 development/check_release.py --tag vx.y.z
TARGET_SHA=$(git rev-parse HEAD)
```

## Step 4: Draft and approve release notes

Draft notes from the merged history. GitHub's generated notes are a useful
starting point:

```sh
REPOSITORY=$(gh repo view --json nameWithOwner --jq .nameWithOwner)
gh api --method POST "repos/$REPOSITORY/releases/generate-notes" \
  -f tag_name=vx.y.z \
  -f target_commitish="$TARGET_SHA" \
  -f previous_tag_name=<previous-tag> \
  --jq .body > "$SCRATCH/release-notes-vx.y.z.md"
```

Edit the draft for clarity and completeness. Show the user:

- the full release notes;
- title `vx.y.z`, tag `vx.y.z`, and exact target SHA;
- included changes and intentionally omitted internal changes; and
- the expected wheel, source archive, Debian package, RPM package, and checksum
  file.

Stop and wait until the user approves this exact release. Apply requested edits
and show the result again. If the version changes, return to Step 1.

## Step 5: Publish

After approval, create the release and let GitHub create the lightweight tag at
the verified commit:

```sh
gh release create vx.y.z \
  --target "$TARGET_SHA" \
  --title vx.y.z \
  --notes-file "$SCRATCH/release-notes-vx.y.z.md"
```

`.github/workflows/publish.yml` runs on `release: published`. It validates the
tag against the source version, independently builds the Python, Debian, and
RPM packages, then attaches all of them only after every build succeeds. The
final job also creates `SHA256SUMS`.

Find the release-event run whose `headSha` equals `$TARGET_SHA`, watch it to
completion, and inspect failed logs rather than blindly rerunning a packaging
failure:

```sh
gh run list --workflow publish.yml --event release --limit 5 \
  --json databaseId,status,conclusion,headSha,url
gh run watch <run-id> --exit-status
```

## Step 6: Verify published artifacts

Confirm that the tag resolves to `$TARGET_SHA` and that the release contains
exactly these five asset classes:

- `vmocs-x.y.z-py3-none-any.whl`
- `vmocs-x.y.z.tar.gz`
- one `vmocs-slurm-plugin_x.y.z_slurm-*_ubuntu-*_*.deb`
- one `vmocs-slurm-plugin-x.y.z-*.sl*.fc*.rpm`
- `SHA256SUMS`

Download the actual release assets and verify their bytes:

```sh
ASSET_DIR=$(mktemp -d)
gh release view vx.y.z --json tagName,targetCommitish,assets,url
gh release download vx.y.z --dir "$ASSET_DIR"
cd "$ASSET_DIR"
sha256sum -c SHA256SUMS
```

When Docker is available, perform clean consumer installs of both Python
distributions and both Slurm packages:

```sh
docker run --rm -v "$ASSET_DIR:/assets:ro" python:3.12-slim sh -ec '
  python -m venv /tmp/wheel
  /tmp/wheel/bin/pip install /assets/*.whl
  /tmp/wheel/bin/vmocs --version
'
docker run --rm -v "$ASSET_DIR:/assets:ro" python:3.12-slim sh -ec '
  python -m venv /tmp/sdist
  /tmp/sdist/bin/pip install /assets/*.tar.gz
  /tmp/sdist/bin/vmocs --version
'
docker run --rm -v "$ASSET_DIR:/assets:ro" ubuntu:24.04 sh -ec '
  apt-get install -y /assets/*.deb
  dpkg-query -W vmocs-slurm-plugin
  test -f /usr/lib/x86_64-linux-gnu/slurm/spank_vmocs.so
  test -f /usr/share/vmocs/vmocs.conf
'
docker run --rm -v "$ASSET_DIR:/assets:ro" fedora:43 sh -ec '
  dnf install -y /assets/*.rpm
  rpm -q vmocs-slurm-plugin
  test -f /usr/lib64/slurm/spank_vmocs.so
  test -f /usr/share/vmocs/vmocs.conf
'
```

Verify both `vmocs --version` results report `x.y.z`, the Debian query reports
`x.y.z`, and the RPM query reports the expected build. If Docker is unavailable,
report these consumer tests as skipped instead of silently omitting them.

The `.deb` and `.rpm` names record the distribution and build-time Slurm
`X.YY` series because SPANK binaries are not portable across Slurm release
series. Users whose cluster does not match must build the plugin from the
release source archive against their cluster's Slurm development headers.

Report the release URL, workflow URL, target SHA, exact asset names, checksum
result, and every consumer test run or skipped with its reason.

## Recover from a failed publication

- For a transient runner or network error, rerun only the failed jobs.
- For a source, packaging, or workflow bug, fix it in a pull request and wait
  for CI. The gated uploader should leave the release with no generated assets.
- Recreate the same release/tag only after verifying the exact release has zero
  assets, confirming no package was consumed, and receiving explicit user
  approval to delete it. Delete only that named release and tag, then recreate
  it at corrected `main`.
- If any asset was attached or the release may have been consumed, preserve the
  tag and publish a new patch release.
