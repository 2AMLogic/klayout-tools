# Releasing klayout-tools

This is the release process for `klayout-tools`: bump the version, tag it,
push the tag, and `.github/workflows/publish.yml` builds and publishes to
PyPI via trusted publishing.

> **TL;DR** — Releases are automated (see "Automated release" below): a weekly
> workflow opens a release PR when `main` is over the backstop; merging it tags
> and publishes. The manual sequence further down is the documented fallback:
> run `release_prep.py prepare` + `uv lock` → merge to `main` → tag
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
   drift apart), and `.github/workflows/release.yml` runs it first on every
   scheduled or dispatched run (see "Automated release"). The script itself is
   advisory and never tags, pushes, or triggers `publish.yml`. When it can read
   Actions history (a `gh` on PATH plus `GH_TOKEN`/`GITHUB_TOKEN`), the report
   links to the most recent failed `release.yml` run, including after a later
   successful run. The lookup is best-effort and preserves the exit codes
   (issue #6911). Run it
   locally the same way CI does:

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

**Versioning rule for the batch runner:** a change that breaks the batch
runner contract (a request key, report field or job-command shape the fleet
runner must understand) ships in a minor release (`vX.Y.0`), never a patch,
and every minor release is published to PyPI and gets a rebaked runner
image -- the runner version gate compares major.minor only (#3184, see
`docs/cli/sim.md` "Runner/client `klt` version skew").

For changes on `main` awaiting release, see the pending entries in
[`changelog.d/`](changelog.d/) and any remaining entries under
[`CHANGELOG.md`](CHANGELOG.md)'s `## Unreleased` heading.

## Changelog fragments

For each new changelog entry, add one Markdown bullet in
[`changelog.d/<issue>-<slug>.md`](changelog.d/README.md). At release time,
`scripts/release_prep.py prepare` inserts the new version heading under
`## Unreleased`, appends the fragments in filename order after any existing
entries, and removes the consumed files. The release commit includes the
updated `CHANGELOG.md` and those deletions.

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

`pyproject.toml`'s `[tool.hatch.version]` `base-version = "…"` is
authoritative: the release the checkout is at or working towards. `[project]`
declares `dynamic = ["version"]`, and each build computes the version it
actually declares from that base plus the checkout's git facts (issue #2823,
`src/klayout_tools/_version_scheme.py`). Run `uv lock` after bumping it as
before (`uv.lock` records no version for this dynamic-version project, so the
bump usually leaves it unchanged). The runtime `klayout_tools.__version__` is
derived from installed package metadata
(`importlib.metadata.version("klayout-tools")`) — there is no second bump
site to keep in sync.

### Distribution version and build identity (issues #1202, #2823)

- A distribution built from a **clean checkout sitting exactly on `vX.Y.Z`**
  (where `X.Y.Z` matches `base-version`) declares bare `X.Y.Z` — in its wheel
  and sdist metadata and on `klt --version`. That is what a PyPI release must
  look like, and what `publish.yml` produces because it is triggered by the
  tag push.
- Anything else declares a distinct PEP 440 dev version — `X.Y.(Z+1).devN+g<sha>`
  after the release tag, `X.Y.Z.devN+g<sha>` between the bump merge and the
  tag, `.dirty` for uncommitted changes, `X.Y.Z.dev0+unknown` with no git
  facts. So a `pip install git+…@<sha>` build can never satisfy
  `klayout-tools==X.Y.Z`, nor be mistaken for the release.
- Every built distribution also carries `klayout_tools/_build_info.py`
  (commit, exact-match tag, dirty flag, and the chosen version), so a wheel
  rebuilt from the sdist keeps the originating version and `klt version` can
  show the git facts behind it.
- `publish.yml` checks out with `fetch-depth: 0` so the tag is visible to
  `git describe` at build time. If a release ever ships declaring a `.dev`
  version, that checkout/tag visibility is the first thing to look at — and
  `uv publish` of such a build would upload a dev pre-release, not `X.Y.Z`.

Nothing here needs a manual step during a release; it is computed and
recorded automatically at build time.

## Automated release

`.github/workflows/release.yml` (issue #4920) cuts releases so they stop
depending on someone remembering. Design decisions:

- **Trigger: cadence, not every merge.** Weekly cron (Mondays 15:17 UTC) plus
  `workflow_dispatch`. It acts only when `scripts/check-release-lag.sh` reports
  lag over the backstop above (`force` skips that gate). A per-merge release
  was rejected: too much PyPI noise.
- **Version: always the next minor** (`vX.(Y+1).0`), which satisfies the
  batch-runner rule above by construction. A patch is a manual override via the
  dispatch `version` input.
- **A release PR, never a push to `main`** (branch protection rejects direct
  pushes). `scripts/release_prep.py` bumps `base-version`, assembles the
  versioned changelog section, and resolves the
  "first release after `vPREV`" notes in `docs/cli/signoff.md`; the workflow
  then runs `uv lock`, `scripts/gen_site_cli_data.py` and
  `scripts/generate_deck_history.py --as-release vX.Y.Z`. The prep refuses
  (exit 2) when both `## Unreleased` and the fragment directory have no
  entries. This stops an empty release before tagging (issue #6086). Only one
  release PR (`release/v*` branch) is open at a time. It is never auto-merged.
- **Tag on merge.** A `push` to `main` that touches `pyproject.toml` runs the
  tag job. It tags the commit only if its subject is
  `chore(release): bump version to X.Y.Z`, `base-version` equals `X.Y.Z` and
  the tag does not exist yet. The tag is on that exact commit, and
  `publish.yml` builds from the tag with `fetch-depth: 0`, so a `.dev` version
  cannot be published; `publish.yml` also refuses any ref that is not a
  `v*` tag.
- **Token caveat, resolved.** Tags pushed with the default `GITHUB_TOKEN` do not
  fire `publish.yml`'s `push: tags` trigger. The tag job therefore runs
  `gh workflow run publish.yml --ref vX.Y.Z` (a `workflow_dispatch` made with
  `GITHUB_TOKEN` is allowed). It runs as `publish.yml` at top level, so the
  PyPI trusted publisher and the `pypi` environment's `v*` tag policy apply
  unchanged. For the same reason a PR opened with `GITHUB_TOKEN` does not start
  required checks: set the optional `RELEASE_BOT_TOKEN` secret (a token that may
  open PRs) or push an empty commit to the release branch.
- **Deck history (old step 5)** is regenerated *in the release PR* with
  `--as-release`, so no push to `main` is needed;
  `tests/test_deck_history.py` accepts exactly the pending `base-version` tag
  ahead of `git tag`. The pending entry's `git_commit` is `null` (the sha is unknowable before
  merge). After tagging the job regenerates against the real tag and files an
  issue if the table differs in anything other than `git_commit`.
- **Gates fail loudly, once.** `scripts/check_native_extras.py` runs before the
  PR is opened and again before tagging; if a `klt-*-native` pin is unpublished
  the job fails at that step and opens no PR or tag. `publish.yml` keeps its
  own copy of the gate.
- **A failed job is reported, recoverably (issue #6911).** A `report-failure`
  job runs after either the prepare or the tag job fails and files — or
  refreshes in place — the one open "Automated release blocked" issue with the
  failed step, its diagnostic, and the run URL, using the workflow's own token
  (never the release-PR credential, which may be exactly what failed; this
  covers native-extras failures too). The prepare job also checks the
  PR-creation setup *before* pushing anything, and a PR-creation failure after
  a successful push is reconciled against the exact branch that run pushed —
  see "When the automated release fails" below.
- **The GitHub Release is part of the pipeline.** After the PyPI upload
  succeeds, `publish.yml`'s `github-release` job creates the GitHub Release
  for the tag from the matching `## X.Y.Z (date)` section of `CHANGELOG.md`
  (`scripts/release_notes.py`) and marks it latest. A section over GitHub's
  125,000-character release-body cap is cut at a line boundary under 120k
  characters and ends with links to the CHANGELOG at the tag and the PyPI
  version. The job is idempotent: a re-run of `publish.yml` on an
  already-released tag edits the existing release instead of failing.
- **Re-run after a failed publish:** run `publish.yml` via "Run workflow" on the
  existing `vX.Y.Z` tag; do not re-tag.
- `release-lag.yml` was retired; its check is the first step of `release.yml`.

### Administrator setup (human-only)

These settings are owned by a repository administrator (or, when an
organization policy enforces a value, by the organization owner — automation
cannot read or change either):

1. **PR creation.** Enable **Settings → Actions → General → Workflow
   permissions → "Allow GitHub Actions to create and approve pull requests"**
   for this repository. Without it, the default `GITHUB_TOKEN` cannot open the
   release PR: this was observed live on 2026-11-16
   ([run 43981896573](https://github.com/2AMLogic/klayout-tools/actions/runs/43981896573)),
   where GitHub accepted the push of `release/v0.10.0` and one second later
   denied `createPullRequest` with "GitHub Actions is not permitted to create
   or approve pull requests", leaving the branch orphaned on the remote (the
   workflow now probes this setting before pushing and reconciles the branch
   on a PR failure; #6897 tracks the one-time disposition of the two orphaned
   branches).
   The prepare job's setup check reads the setting when it can; a readable
   `can_approve_pull_request_reviews: false` stops the run *before* anything is
   pushed (a 403 — the value is admin-only — leaves it unknown and the run
   proceeds under the push-time recovery above).
2. **`RELEASE_BOT_TOKEN` (alternative to 1).** An optional repository secret
   holding a token that may open pull requests (e.g. a GitHub App token). When
   set, the checkout, the push, and the PR all use its own permissions instead
   and the setting in 1 does not apply.
3. **Required checks on the release PR.** A PR opened with the default
   `GITHUB_TOKEN` does not start other workflows' `pull_request` events, so
   required checks never run on it. With `RELEASE_BOT_TOKEN` (2) they run on
   the PR as usual. Without it, start CI by pushing an empty commit to the
   release branch (`git commit --allow-empty -m ci --no-verify && git push`).

Until the applicable setting in 1 or the token in 2 is in place, the automated
release reports a blocked issue instead of opening a PR; use a `dry_run`
dispatch to exercise the pipeline safely.

### When the automated release fails

- Any failed `prepare` or `tag` job produces **one** open
  "Automated release blocked" issue, refreshed in place by the next failure
  (not re-filed): the failed step, the diagnostic tail, and links to the run
  and job. Start there. The release-lag report also names the newest failed
  `release.yml` run when Actions history is available.
- **The release branch was kept** (the log says so): the open-PR lookup or the
  remote-ref lookup was inconclusive, or the remote branch had moved off the
  SHA this run pushed. Check for an open PR on that head first — the PR may
  exist even though the creating call reported an error — and delete the
  branch by hand only when there is none and it still points at the run's
  recorded SHA. A kept branch makes the next attempt's push fail fast rather
  than overwrite it; delete or merge it before re-running.
- **A PR failure after a successful push is recoverable on retry**: the run
  deletes the branch it pushed (expected-SHA lease) when no open PR exists for
  it, so a re-run prepares a fresh commit and pushes cleanly. Fix the reported
  cause first — usually the administrator setup above.
- **The tag was pushed but `publish.yml` was not dispatched**: run
  `publish.yml` via "Run workflow" on the existing `vX.Y.Z` tag; do not re-tag.

## The release sequence (manual fallback)

Let `X.Y.Z` be the new version.

1. **Prepare the release** on a branch and merge it to `main` through a normal
   PR. The order mirrors the prepare job in
   `.github/workflows/release.yml` — in particular the version bump must be
   *committed* before the generators run (the site data reads the new
   CHANGELOG heading, the deck history reads HEAD's `pyproject.toml` as the
   release), and the generated files are folded in by amending that commit:

   ```bash
   # The native gate both automated jobs run first (docs/releasing-native.md).
   uv run --python 3.12 --no-project python scripts/check_native_extras.py

   git checkout -b release/vX.Y.Z
   python3 scripts/release_prep.py prepare --version X.Y.Z
   uv lock
   git add -A -- pyproject.toml uv.lock CHANGELOG.md changelog.d/ \
     docs/cli/signoff.md
   git commit -m "chore(release): bump version to X.Y.Z"
   uv run python scripts/gen_site_cli_data.py
   python3 scripts/generate_deck_history.py --as-release vX.Y.Z
   git add -A
   git commit --amend --no-edit
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

4. **Verify availability notes.** Release preparation resolves the
   `--attest` / `--inventory` availability notes in `docs/cli/signoff.md`
   anchored on the version being superseded. Review other notes about
   availability in the release PR. `scripts/release_prep.py prepare` refuses
   (exit 2) if a "first/next release after" note still references an older
   version.

5. **Regenerate the deck history table** now that the new tag exists (the
   generator can only see tags that have already been pushed — see
   `docs/cli/deck.md`'s "Regenerating" section). Step 1 already added the
   pending entry via `--as-release` (its `git_commit` is `null` until the tag
   exists); this replaces that placeholder with the tag's sha:

   ```bash
   python scripts/generate_deck_history.py
   git add src/klayout_tools/decks/_history.json
   git commit -m "chore(decks): regenerate deck history table for vX.Y.Z"
   git push origin main
   ```

   Skipping this step leaves `src/klayout_tools/decks/_history.json` stale
   relative to `git tag`, which fails
   `tests/test_deck_history.py::test_checked_in_history_table_covers_every_release`
   on the next `main` checkout.

6. **Refresh the site's release badge.** The klayout-tools.org landing page
   reads the release, verb list and MCP tool count from
   `site/src/data/cli.generated.json`, which is generated from the newest
   `## X.Y.Z (YYYY-MM-DD)` heading in `CHANGELOG.md` and the CLI registry.
   Step 1 regenerates this file in the PR that adds the heading.
   `tests/test_site_cli_data.py` fails until you do, and the commit
   (under `site/`) is what triggers the site redeploy.

**Changelog convention for deck changes.** Any changelog entry that
alters a built-in deck's content hash (rules added, removed or adjusted in
`sky130`, `gf180mcu`, `sg13g2`, ...) must include the literal phrase
`deck hash changes`, so consumers pinning `provenance.deck.content_hash` can
search for it. Hashes are stable only per package version; see
`docs/cli/deck.md` "Reproducing a run under a pinned deck".

## Verifying a release

- Watch the run under the repo's **Actions** tab (`Publish to PyPI` workflow).
- Once green, confirm the new version is live:
  `pip index versions klayout-tools` or check
  `https://pypi.org/project/klayout-tools/`.

## Quick checklist

- [x] One-time PyPI trusted-publisher registration (see above) — completed
      before v0.1.0.
- [ ] Every `klt-*-native` pin in `pyproject.toml`'s extras is on PyPI
      (`docs/releasing-native.md`); `publish.yml` enforces this with
      `scripts/check_native_extras.py`, which you can run first.
- Automated path: review and merge the `chore(release)` PR; the tag job does
  the tag and dispatches `publish.yml`. The items below are the manual fallback.
- [ ] Release prepared with `scripts/release_prep.py prepare` and `uv lock`.
- [ ] Bump commit merged to `main` via a PR.
- [ ] Annotated tag `vX.Y.Z` created on the merged commit on `main`.
- [ ] `git push origin vX.Y.Z` — confirm `publish.yml` runs and succeeds.
- [ ] `src/klayout_tools/decks/_history.json` regenerated
      (`python scripts/generate_deck_history.py`) and committed to `main`.
- [ ] `site/src/data/cli.generated.json` regenerated
      (`uv run python scripts/gen_site_cli_data.py`) in the PR that adds the
      `## X.Y.Z` CHANGELOG heading.
