# Releasing klayout-tools

This is the release process for `klayout-tools`: bump the version, tag it,
push the tag, and `.github/workflows/publish.yml` builds and publishes to
PyPI via trusted publishing.

> **TL;DR** — Bump `pyproject.toml` + `uv lock` → merge to `main` → tag
> `vX.Y.Z` on the merged commit → push the tag. Pushing the tag is what
> triggers the publish workflow.

## One-time prerequisite (human-only, do this before the first release)

`publish.yml` publishes using [PyPI trusted publishing](https://docs.pypi.org/trusted-publishers/)
(OIDC) — there is no `PYPI_TOKEN` secret stored in this repo. Before the
**first** tag is ever pushed, a human with access to the `2AM Logic` PyPI
account must register a trusted publisher for this project:

1. On [pypi.org](https://pypi.org), either claim the existing `klayout-tools`
   project or use ["Add a pending publisher"](https://pypi.org/manage/account/publishing/)
   if the project does not exist yet (PyPI supports pre-registering a trusted
   publisher for a project that hasn't published its first release).
2. Configure the trusted publisher with:
   - **Repository**: `2AMLogic/klayout-tools`
   - **Workflow name**: `publish.yml`
   - **Environment name**: `pypi`
3. Create a `pypi` [GitHub Environment](https://github.com/2AMLogic/klayout-tools/settings/environments)
   for this repository (matches the `environment: pypi` job in
   `publish.yml`). Optionally add required reviewers/protections on that
   environment for extra safety before a publish runs.

Until this is done, pushing a `v*` tag will trigger the workflow but the
`uv publish` step will fail authentication — that is expected, not a bug in
the workflow. This registration is out of scope for automated Builder work;
track it separately if it hasn't happened yet.

## Version source of truth

`pyproject.toml`'s `version = "…"` is authoritative. Regenerate `uv.lock`
(`uv lock`) whenever you bump it so the lockfile stays in sync.

## The release sequence

Let `X.Y.Z` be the new version.

1. **Bump the version** on a branch, and merge it to `main` through a normal
   PR:

   ```bash
   git checkout -b release/vX.Y.Z
   # edit pyproject.toml: version = "X.Y.Z"
   uv lock            # regenerate uv.lock to match
   git add pyproject.toml uv.lock
   git commit -m "chore(release): bump version to X.Y.Z"
   git push -u origin release/vX.Y.Z
   gh pr create --title "chore(release): bump version to X.Y.Z" --body "Release X.Y.Z"
   ```

   Merge the PR once it's reviewed (`./.loom/scripts/merge-pr.sh <PR-NUMBER>`
   is the repo's preferred merge path — see `.loom/CLAUDE.md`).

2. **Tag the merged commit on `main`**:

   ```bash
   git checkout main
   git pull origin main
   git tag -a vX.Y.Z -m "Release X.Y.Z"
   ```

3. **Push the tag** — this is what triggers `publish.yml`:

   ```bash
   git push origin vX.Y.Z
   ```

   The workflow builds the tagged commit with `uv build` and publishes the
   resulting sdist/wheel with `uv publish` under PyPI trusted publishing.

## Verifying a release

- Watch the run under the repo's **Actions** tab (`Publish to PyPI` workflow).
- Once green, confirm the new version is live:
  `pip index versions klayout-tools` or check
  `https://pypi.org/project/klayout-tools/`.
- `pip install klayout-tools` (and the README's "not yet published" caveat)
  can be updated to drop the caveat once the first version has actually
  published.

## Quick checklist

- [ ] One-time PyPI trusted-publisher registration done (see above) —
      only needed before the very first release.
- [ ] Version bumped in `pyproject.toml`; `uv.lock` regenerated to match.
- [ ] Bump commit merged to `main` via a PR.
- [ ] Annotated tag `vX.Y.Z` created on the merged commit on `main`.
- [ ] `git push origin vX.Y.Z` — confirm `publish.yml` runs and succeeds.
