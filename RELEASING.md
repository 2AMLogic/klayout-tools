# Releasing klayout-tools

This is the release process for `klayout-tools`: a cadence-gated patch bump
committed by the fleet publisher, a tag only on a green `main` CI run, and
`.github/workflows/publish.yml` building and publishing to PyPI via trusted
publishing — with no person involved (issue #5005, operator ruling r6-q22).

> **TL;DR** — Releases are automatic. Merges to `main` accumulate; at most
> one patch bump per `RELEASE_MIN_INTERVAL` (default 24 h) is committed to
> `main` by the fleet publisher, and the first green `CI` run of the version
> it carries is tagged and published. To cut a release by hand, use the
> workflow dispatch (below); there is no release PR anymore.

## Release cadence

Without a stated trigger, the gap between the latest PyPI tag and `main`
grows silently until someone pins the release and hits stale behavior — this
has already happened twice (#342, #953). The rule since issue #5005:

**At most one patch bump per `RELEASE_MIN_INTERVAL` seconds** (a GitHub
repository *variable*, not a secret; default 86400 = 24 h when unset, which
the gate treats as the value). Merges inside the window accumulate,
unreleased; the next eligible run ships them all in one `X.Y.Z -> X.Y.(Z+1)`
bump. A non-integer value fails the workflow red rather than being silently
ignored.

- `release.yml` runs on every push to `main` touching `src/**`, hourly as a
  catch-up (which also picks up `pyproject.toml`-only dependency changes),
  and on demand (`workflow_dispatch`). Each run consults
  [`scripts/version-bump-gate.sh`](scripts/version-bump-gate.sh) — a pure,
  git-history-only decision — before doing anything.
- The bump is one **patch** step. This also satisfies the batch-runner rule
  below by construction: a patch never changes `major.minor`.

**Versioning rule for the batch runner:** a change that breaks the batch
runner contract (a request key, report field or job-command shape the fleet
runner must understand) ships in a minor release (`vX.Y.0`), never a patch,
and every minor release is published to PyPI and gets a rebaked runner
image -- the runner version gate compares major.minor only (#3184, see
`docs/cli/sim.md` "Runner/client `klt` version skew"). Each tag also costs a
runner rebake; the 24 h default interval reflects that the rebake is not
automatic yet (2AMLogic/2am#6568) — tighten the interval once it is.

For changes on `main` awaiting release, see the pending entries in
[`changelog.d/`](changelog.d/) and any remaining entries under
[`CHANGELOG.md`](CHANGELOG.md)'s `## Unreleased` heading.

## The automated flow (`release.yml`)

Three jobs (Loom's model, `rjwalters/loom`
`.github/workflows/version-bump-on-merge.yml`):

1. **`bump`** — when the cadence gate says *bump*, computes the only edit a
   release is: `pyproject.toml`'s `base-version` (one patch step) plus the
   `## X.Y.Z (date)` heading directly under `CHANGELOG.md`'s single
   `## Unreleased` line ([`scripts/release_prep.py`](scripts/release_prep.py)).
   Nothing is committed or pushed here.
2. **`publish-bump`** — the only job holding `id-token: write` (and running
   no third-party action) POSTs those two files to the fleet publisher at
   `https://fleet-publish.2amlogic.com/v1/publish_version_bump`, authenticated
   by the job's GitHub Actions OIDC token. The publisher — not this repo —
   holds the writing key and commits exactly this shape to `main` as
   `chore: bump version to X.Y.Z`: only `pyproject.toml` (its `base-version`
   line) and `CHANGELOG.md` (the heading), one patch step,
   compare-and-swap on the parent we name. **There is no direct push and no
   fallback** — a refusal fails the run red.
3. **`tag`** — on a completed `CI` run (`workflow_run`) for a *push* to this
   repo's `main` that concluded *success*: reads the `base-version` the
   CI-tested commit carries, and when that version has no tag yet (and no
   newer tag exists) runs the native-extras gate
   ([`scripts/check_native_extras.py`](scripts/check_native_extras.py), which
   files/refreshes a single tracking issue instead of tagging on failure),
   tags `vX.Y.Z` on that commit and dispatches `publish.yml` on the tag ref.
   A red bump is never tagged; its version ships later, from the first later
   commit with the same `base-version` that goes green. If a run pushed the
   tag but failed to dispatch `publish.yml`, the next run sees a tag that is
   the newest and that `publish.yml` has never run on, and **dispatches only**
   (no re-tag, no native-extras gate, never an older tag a newer release has
   passed); a run list it cannot read fails red instead of reading as
   "released".

`tests/test_release_workflow_identity.py` pins the security-relevant shape of
this workflow (only the publish job mints the OIDC token, no fallback push,
no secret read, the `src/**` push filter that keeps the publisher's own
commit from re-triggering the workflow), with mutation controls; mirror of
Loom's `test-version-bump-workflow-identity.sh`.
`tests/test_release_workflow_tag_job.py` guards the tag job and runs its own
steps against a stub `gh` and a local origin, including the stranded-tag
recovery above.

### Generated files are not in the bump commit

The publisher accepts exactly `pyproject.toml` and `CHANGELOG.md`, so the two
files that used to ride along in the release commit moved into the build:

- **`src/klayout_tools/decks/_history.json`** (packaged data):
  `publish.yml` checks out the tag, runs
  `scripts/generate_deck_history.py --output "$RUNNER_TEMP/_history.json"`
  (the real tag exists by then, so every row carries the tag's commit — no
  pseudo-tag), and `uv build` runs with `KLT_DECK_HISTORY_PATH` pointing at
  it: `hatch_build.py` force-includes that file as the packaged
  `decks/_history.json` in wheel and sdist. No tracked file changes before
  the build, which is what keeps the distribution version exactly `X.Y.Z`
  (any tracked edit dirties the checkout and renders
  `X.Y.(Z+1).dev0+g<sha>.dirty`; `tests/test_release_build_identity.py`
  pins both the good path and that failure mode). The committed copy on
  `main` lags until a normal PR runs the generator;
  `tests/test_deck_history.py` accepts a table whose tags are a prefix of
  `git tag`.
- **`site/src/data/cli.generated.json`** (site data): generated at site-build
  time by [`scripts/deploy-site.sh`](scripts/deploy-site.sh) and no longer
  committed; a `CHANGELOG.md`/`pyproject.toml` change redeploys the site, so
  the release badge tracks the newest heading. CI's `site/` job generates it
  before `tsc -b`; the Python drift tests call the generator in-process.
- **`README.md`'s release claim** ("PyPI X.Y.Z"): the bump commit cannot carry
  it, so in that commit the new `CHANGELOG.md` heading is one patch ahead of
  the README. `tests/test_readme_drift.py` therefore lets the claim lag by
  patch releases inside the newest release's `X.Y` line (the bump commit must
  stay green or nothing is tagged); a minor or major release updates the
  README with it, and a claim from an older `X.Y` line, or ahead of the
  changelog, fails.

### Changelog fragments and release notes

`changelog.d/*.md` fragments are **not folded into `CHANGELOG.md` at release
time** anymore (the publisher's commit is heading-only). They are rendered
into the GitHub Release notes at publish time:
[`scripts/release_notes.py`](scripts/release_notes.py) builds the notes from
the tag's `CHANGELOG.md` section **plus** the fragments *added* under
`changelog.d/` since the previous tag
(`git diff --diff-filter=A --name-only <prev-tag> HEAD -- changelog.d/`), so
no release repeats an earlier one's fragments. "First/next release after
`vPREV`" availability placeholders anchored on the superseded release are
resolved in the rendered notes only; the persistent text in
`docs/cli/signoff.md` stays as written. An entirely empty release (no
section body, no new fragments) fails the `github-release` job red rather
than publishing empty notes. Folding fragments into `CHANGELOG.md` as
housekeeping is a separate concern, not part of a release.

### Minor (and major) releases

A minor is deliberate: dispatch `release.yml` with `version=X.Y.0`. The
workflow forwards the version; the **publisher refuses it**
(`version_jump_too_large`) until a reviewed 2am change lists that release for
klayout-tools (2AMLogic/2am `infra/cloudflare/github-write/src/publish/`
`minor_releases` — Loom's `LOOM_MINOR_RELEASES` model). The listing is not
made in this repo. A forced patch (operator hotfix) is
`force=true` + `reason=…`, which skips the interval, never the
"anything unreleased?" check, and records actor and reason in the bump
commit.

### Operator prerequisite for the first live run (not for building)

The publisher moves `main` through the Git Data API, which branch protection
sees as a push. As of 2026-11-24, ruleset 24397320 (required lint + Python
checks on `main`) lists no bypass for the publisher App (`Integration
5821196`) — it is recorded as a bypass only on loom (2am#7698, which left
klayout-tools unread). **Before the first live bump, an admin must add the
App (`bypass_mode: always`) to that ruleset and record it in 2am's
klayout-tools ruleset snapshot.** Until then the publisher's commit is
rejected; report that as the prerequisite, not as a workflow defect. The
live proof — a tag publishing to PyPI with no person involved — is tracked
on issue #5005.

## One-time prerequisite (human-only — completed before v0.1.0; kept for reference)

`publish.yml` publishes using [PyPI trusted publishing](https://docs.pypi.org/trusted-publishers/)
(OIDC) — there is no `PYPI_TOKEN` secret stored in this repo. The trusted
publisher is registered with repository `2AMLogic/klayout-tools`, workflow
name `publish.yml`, environment `pypi`, and the `pypi` GitHub Environment is
configured with a custom deployment branch/tag policy allowing tag rule `v*`
(see the details in git history if they ever need re-checking). Until this is
done, pushing a `v*` tag will trigger the workflow but the `uv publish` step
will fail authentication — that is expected, not a bug in the workflow.

## Version source of truth

`pyproject.toml`'s `[tool.hatch.version]` `base-version = "…"` is
authoritative: the release the checkout is at or towards. `[project]`
declares `dynamic = ["version"]`, and each build computes the version it
actually declares from that base plus the checkout's git facts (issue #2823,
`src/klayout_tools/_version_scheme.py`). `uv.lock` records no version for
this dynamic-version project (`source = { editable = "." }`), so a bump does
not touch it. The runtime `klayout_tools.__version__` is derived from
installed package metadata (`importlib.metadata.version("klayout-tools")`) —
there is no second bump site to keep in sync.

### Distribution version and build identity (issues #1202, #2823, #5005)

- A distribution built from a **clean checkout sitting exactly on `vX.Y.Z`**
  (where `X.Y.Z` matches `base-version`) declares bare `X.Y.Z` — in its wheel
  and sdist metadata and on `klt --version`. That is what a PyPI release must
  look like, and what `publish.yml` produces: it checks out the tag with
  `fetch-depth: 0` and generates the deck history *outside* the tree, so the
  build cannot dirty itself (see above).
- Anything else declares a distinct PEP 440 dev version — `X.Y.(Z+1).devN+g<sha>`
  after the release tag, `X.Y.Z.devN+g<sha>` between the bump merge and the
  tag, `.dirty` for uncommitted changes, `X.Y.Z.dev0+unknown` with no git
  facts. So a `pip install git+…@<sha>` build can never satisfy
  `klayout-tools==X.Y.Z`, nor be mistaken for the release.
- Every built distribution also carries `klayout_tools/_build_info.py`
  (commit, exact-match tag, dirty flag, and the chosen version), so a wheel
  rebuilt from the sdist keeps the originating version and `klt version` can
  show the git facts behind it.
- If a release ever ships declaring a `.dev` version, the first thing to look
  at is whether anything edited a tracked file before `uv build` (the
  deck-history path above is the one that used to).

Nothing here needs a manual step during a release; it is computed and
recorded automatically at build time.

## Re-running after a failure

- **The publisher refused the bump** (red `publish-bump`): read the refusal
  reason in the job log. A `head_moved` is retried automatically; anything
  else is a real policy refusal (e.g. an unlisted minor) or the missing
  ruleset bypass above.
- **A red `CI` on the bump commit:** nothing is tagged; fix forward, and the
  next green commit carrying the same `base-version` is tagged instead.
- **A tag with no publish run** (the `tag` job's dispatch step failed): re-run
  the failed `Release` run. The tag job dispatches `publish.yml` on the
  existing tag without re-tagging (see the `tag` job above).
- **A failed publish after the tag:** run `publish.yml` via "Run workflow" on
  the existing `vX.Y.Z` tag; do not re-tag.

## The manual fallback (a hand-cut release PR)

For a release the automation cannot cut (e.g. the first minor after this
lands, pending its 2am listing), the old hand path still works — it is what
the v0.10.0 bridge (#6897) used:

1. Prepare a release branch and PR:
   `python3 -I scripts/release_prep.py prepare --version X.Y.Z` (bumps
   `base-version`, inserts the heading), commit as
   `chore(release): bump version to X.Y.Z`, open a PR, merge it.
2. Tag the merged commit on `main` (`git tag -a vX.Y.Z -m "Release X.Y.Z"`)
   and push the tag — or let the `tag` job do it: the merged commit's green
   CI run tags it automatically (its `base-version` is unreleased).
3. Regenerate the deck history for the new tag and commit it:
   `python scripts/generate_deck_history.py` (see
   `docs/cli/deck.md` "Regenerating").

**Changelog convention for deck changes.** Any changelog entry that alters a
built-in deck's content hash (rules added, removed or adjusted in `sky130`,
`gf180mcu`, `sg13g2`, ...) must include the literal phrase `deck hash
changes`, so consumers pinning `provenance.deck.content_hash` can search for
it. Hashes are stable only per package version; see `docs/cli/deck.md`
"Reproducing a run under a pinned deck".

## Verifying a release

- Watch the run under the repo's **Actions** tab (`Release`, then
  `Publish to PyPI` workflows).
- Once green, confirm the new version is live:
  `pip index versions klayout-tools` or check
  `https://pypi.org/project/klayout-tools/`.

## Quick checklist

- [x] One-time PyPI trusted-publisher registration (see above) — completed
      before v0.1.0.
- [x] Fleet publisher target pinned to this repo's `release.yml` (2am#6876).
- [ ] **Before the first live bump:** ruleset 24397320 bypasses the publisher
      App (see "Operator prerequisite"; an admin step, recorded in 2am's
      snapshot).
- [ ] Every `klt-*-native` pin in `pyproject.toml`'s extras is on PyPI
      (`docs/releasing-native.md`); the tag job enforces this with
      `scripts/check_native_extras.py`.
- Automated path: nothing — merges accumulate, the gate bumps, green CI tags,
      `publish.yml` publishes and writes the GitHub Release notes from the
      CHANGELOG section plus the fragments added since the previous tag.
