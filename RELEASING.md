# Releasing klayout-tools

This is the release process for `klayout-tools`: bump the version, tag it,
push the tag, and `.github/workflows/publish.yml` builds and publishes to
PyPI via trusted publishing.

> **TL;DR** — Bump `pyproject.toml` + `uv lock` → merge to `main` → tag
> `vX.Y.Z` on the merged commit → push the tag. Pushing the tag is what
> triggers the publish workflow.

## Release cadence

Without a stated trigger, the gap between the latest PyPI tag and `main`
grows silently until someone pins the release and hits stale behavior — this
has already happened twice (#342, #953). The rule below exists so "is a
release due right now?" has a checkable answer instead of "when it seems
warranted."

**Cut a release when either condition is true:**

1. **Event-based (primary)** — a merged fix that was requested to be
   pinned-and-installable (i.e. someone asked "is this fixed in what I can
   install?" or a downstream consumer is blocked on it) has landed on `main`
   and is not yet in a tagged release.
2. **Commit-count backstop** — `main` is more than **25 commits** ahead of
   the latest tag, so the gap never grows unbounded even if condition 1 goes
   unnoticed. Check with:

   ```bash
   git rev-list --count "$(git tag --sort=-creatordate | head -1)"..origin/main
   ```

   A result over 25 means a release is due; cut one via the sequence below
   (`/repo:release` if using the Repo Skills wrapper) even if nothing has
   explicitly been requested as installable.

   **You do not have to remember to run this.**
   [`scripts/check-release-lag.sh`](scripts/check-release-lag.sh) computes the
   same count (it *parses the 25 above out of this file*, so the two can never
   drift apart), and `.github/workflows/release-lag.yml` runs it on every push
   to `main`, weekly, and on demand — reporting an over-threshold count as a
   job-summary entry plus a warning annotation. That check is advisory only:
   it never tags, pushes, or triggers `publish.yml`. Run it locally the same
   way CI does:

   ```bash
   scripts/check-release-lag.sh            # exit 1 ⇒ a release is due
   scripts/check-release-lag.sh --format json
   ```

25 is deliberately closer to the low end of the 18–200+ commit range that
#342 and #953 both surfaced friction within — cutting a release is cheap
(one version bump PR + one tag push, see the sequence below), so the backstop
favors staying ahead of drift over minimizing release count.

This policy does **not** mean cutting a release on every merge to
`pyproject.toml`/`VERSION`, and does not require anything beyond the
one-time trusted-publisher registration below — it only states *when* to run
the mechanical sequence that section and the rest of this doc already
describe.

**Interim answer, between releases**: `CHANGELOG.md`'s `## Unreleased` →
`### Fixed since release` section is the source of truth for "is fix X
installable yet" — every dated, issue-numbered entry there has landed on
`main` but not yet shipped to PyPI. Check it before assuming a closed issue
is installable.

## One-time prerequisite (human-only — completed before v0.1.0; kept for reference)

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
   - **Deployment branch/tag policy**: the environment's protection rules
     default to "Protected branches only", which will **reject** every
     publish. `publish.yml` triggers on `push: tags: ["v*"]`, and the
     `publish` job runs under `environment: pypi` — GitHub evaluates the
     *triggering ref* (a tag) against the environment's policy, and a tag
     is not a protected branch. Under the default policy the job fails
     with "not allowed to deploy to pypi due to environment protection
     rules", even though trusted-publisher registration and the
     environment are otherwise correctly configured. Set a **custom**
     deployment branch/tag policy instead: repo Settings → Environments →
     `pypi` → "Deployment branches and tags" → "Selected branches and
     tags" → add a tag rule `v*`.

Until this is done, pushing a `v*` tag will trigger the workflow but the
`uv publish` step will fail authentication — that is expected, not a bug in
the workflow. This registration is out of scope for automated Builder work;
track it separately if it hasn't happened yet.

## Version source of truth

`pyproject.toml`'s `version = "…"` is authoritative. Regenerate `uv.lock`
(`uv lock`) whenever you bump it so the lockfile stays in sync. The runtime
`klayout_tools.__version__` is derived from installed package metadata
(`importlib.metadata.version("klayout-tools")`) — there is no second bump
site to keep in sync.

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

## Quick checklist

- [x] One-time PyPI trusted-publisher registration (see above) — completed
      before v0.1.0.
- [ ] Version bumped in `pyproject.toml`; `uv.lock` regenerated to match.
- [ ] Bump commit merged to `main` via a PR.
- [ ] Annotated tag `vX.Y.Z` created on the merged commit on `main`.
- [ ] `git push origin vX.Y.Z` — confirm `publish.yml` runs and succeeds.
