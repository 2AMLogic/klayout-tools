# `klt version`

Report which klayout-tools build is running — its version, the git commit it
was built from, and whether that commit is this version's release tag
(issue #1202) — plus the KLayout engine version actually resolved and the
version this build/commit was tested against (issue #1490).

```
klt version [--format text|json]
klt --version            # same one-line string as `klt version --format text`
```

## The version *is* the identity (issue #2823)

`pyproject.toml` declares only a `base-version` (under `[tool.hatch.version]`)
— the release a checkout is at or working towards. Every wheel and sdist
computes its actual distribution version at build time from that base plus
the git facts of the checkout it was built from
(`src/klayout_tools/_version_scheme.py`, shared by the build and the
runtime):

| Build | Distribution version (`klt --version`, `pip`, `importlib.metadata`) |
|---|---|
| Clean tree exactly at the base's release tag `vX.Y.Z` | `X.Y.Z` |
| Any commit after that release tag | `X.Y.(Z+1).devN+g<12-char sha>` |
| A commit before the base has been tagged (base already bumped for the next release, or the tag is not visible to the build) | `X.Y.Z.devN+g<sha>` |
| Either of the above with uncommitted changes to tracked files | `…+g<sha>.dirty` |
| No recoverable git provenance at all | `X.Y.Z.dev0+unknown` |

`N` is the number of commits since the base's release tag when that tag is
an ancestor of the build commit; otherwise since the nearest tag reachable
from it; otherwise the total number of commits reachable from it. A shallow
clone can therefore report a smaller `N`, never a release.

**Ordering.** `X.Y.Z` < `X.Y.(Z+1).dev1+…` < `X.Y.(Z+1).dev2+…` < `X.Y.(Z+1)`.
A dev build of a not-yet-tagged base, and an unknown build, sort *before*
that base — neither can prove it is at or past it.

**A tagged release reports the bare `X.Y.Z` it always did** — the regression
guard for anything already parsing `klt X.Y.Z`.

### Exact pins (`klayout-tools==X.Y.Z`)

Only a clean build at the release tag carries the bare public version, so a
PEP 440 `==X.Y.Z` specifier can be satisfied by nothing else. Every other
build differs in its *public* version (the `.devN` segment), not merely in
its `+local` segment — which matters, because PEP 440 matching ignores the
local segment: before issue #2823 the wheel metadata of a post-tag build
declared plain `X.Y.Z` and only `klt`'s *output* added `+g<sha>`, so a dev
build already installed as a `uv` tool, or sitting in a resolver cache,
silently satisfied `uvx --from "klayout-tools==X.Y.Z"`. Builds made before
#2823 still behave that way; only builds made after it are fixed.

Dev versions are PEP 440 pre-releases, so a resolver never picks one for an
unpinned or range request unless asked (`--prerelease allow` / `--pre`) or
pinned to it exactly.

### Which artifact is running, and where it came from

The identity is a function of the installed **distribution** only: the
version is in its metadata and the git facts are in the
`klayout_tools/_build_info.py` record built into it. Running one installed
artifact through `uvx`, `uv run --no-project --with …`, or its own `klt`
console script reports the same identity; resolver cache state decides
*which* artifact runs, never what that artifact reports. Two invocations that
report different identities ran two different artifacts.

What standard wheel metadata cannot tell you is the **index or origin** an
artifact was installed from (PyPI, a local `--find-links` directory, a
`git+` URL, a cache). `klt` does not record it. Where that matters, the
installer's own records are the source (`uv pip show`, the `direct_url.json`
PEP 610 file in the installed `.dist-info` directory for URL/VCS installs).

## `klt version --format json`

```json
{
  "schema_version": 1,
  "version": "0.2.1.dev7+g4f1c8a9b2d3e",
  "package_version": "0.2.1.dev7+g4f1c8a9b2d3e",
  "git_commit": "4f1c8a9b2d3e5f60718293a4b5c6d7e8f9a0b1c2",
  "git_tag": null,
  "dirty": false,
  "is_release": false,
  "reinstall_spec": "klayout-tools @ git+https://github.com/2AMLogic/klayout-tools@4f1c8a9b2d3e5f60718293a4b5c6d7e8f9a0b1c2",
  "grading_ruleset_id": "sha256:1a2b3c4d5e6f...",
  "klayout_version": "0.30.12",
  "klayout_version_expected": "0.30.10"
}
```

- `version` — the identity-bearing version string above, also recorded as
  `provenance.klt_version` in reports.
- `package_version` — the installed distribution version
  (`klayout_tools.__version__`, i.e. `importlib.metadata.version`). For every
  built distribution (wheel, sdist, `git+` install) it **equals `version`**.
  It can differ only for an editable/source install, whose `version` is
  recomputed live from the checkout while its metadata was computed when it
  was installed (`uv sync` rebuilds it when the commit or tags move, not when
  files are merely edited, so typically only `.dirty` differs).

  **Contract change (issue #2823):** `package_version` used to be the static
  base version for *every* build (`0.2.0` on a post-tag build too) and
  equalled `version` only when `is_release` was `true`. It is now whatever
  the distribution itself declares, so a non-release build's
  `package_version` carries the same `.devN+g<sha>` form as `version`. The
  field's definition (`klayout_tools.__version__`) and type are unchanged, so
  `schema_version` stays `1`; a consumer that read `package_version` to
  recover the bare release number should use `is_release`/`git_tag` instead,
  or strip the PEP 440 dev/local segments.
- `git_commit` — the commit the build was made from, or `null` when
  unrecoverable.
- `git_tag` — the tag the build sits exactly on, or `null`. A tag belonging to
  a *different* version is reported here but does not make `is_release` true.
- `dirty` — whether *tracked* files in the tree had uncommitted changes at
  build time (`null` when unknown). Untracked files are deliberately not
  considered (issue #2248): a `git+<sha>` install clones into a scratch
  checkout a package manager can itself litter with bookkeeping files (e.g.
  `uv` writes its own checkout-completion sentinel into the tree before the
  build runs), which previously made a clean, immutable, sha-pinned revision
  misreport as dirty.
- `is_release` — **tri-state**, mirroring `provenance.deck.released`
  (see [`../json-contract.md`](../json-contract.md)): `true` for a confirmed
  release build, `false` for a confirmed non-release build, `null` when the
  question is unanswerable. A consumer gating on "this is a release" must
  require `is_release === true`; `null` is not a weaker `true`.
- `reinstall_spec` (issue #3985) — a spec for `uvx --from` / `pip install` that
  re-obtains this build, or `null` when it cannot be re-obtained. `is_release:
  true` gives `klayout-tools==<version>`; a clean build with a recorded
  `git_commit` gives `klayout-tools @ git+https://github.com/2AMLogic/klayout-tools@<git_commit>`;
  a dirty tree or a build with no recorded commit (`…dev0+unknown`) gives
  `null`, and `klt version` text then prints a `not reinstallable` note to stderr (stdout stays the one-line version).
  Additive; `schema_version` stays `1`.
- `grading_ruleset_id` (issue #2216) — **a `klt` version string alone does
  not identify the grading build.** Three installs can report the exact same
  `version`/`git_commit` yet grade a `klt signoff --manifest`/`--fleet`
  tier-verdict report by different rules — a registry wheel built from a
  release tag, a `pip install git+...@<tag>` snapshot of that same tag, and a
  full-repo checkout that has moved past the tag on `main`. This field is a
  `sha256:`-prefixed content hash of the shipped `signoff.py` grading module:
  identical between two installs of byte-identical grading code regardless
  of how each was installed, and different whenever the grading logic
  changes — see [`signoff.md`'s "Identifying the grading
  build"](signoff.md#identifying-the-grading-build) for the full mechanism,
  including `klt signoff --describe-grader`. `null` only if this install's
  `signoff.py` cannot be read at all (never fabricated).
- `klayout_version` (issue #1490) — the KLayout engine actually resolved
  into this process (`klayout.__version__`, same value as
  `provenance.klayout_version`), or `null` if unresolvable.
- `klayout_version_expected` (issue #1490) — the `klayout` version this
  build/commit was tested against, or `null` when unresolvable (a build made
  before this field existed, or a checkout with no reachable `uv.lock`). See
  "Pinning the KLayout engine version" below.

**`version`/`git_commit`/`git_tag`/`dirty`/`is_release` identify the
*install*, not only the commit it came from** — two byte-legitimate installs
of the same pinned `<sha>` can report different values for all five (issue
#2249). That is why a committed report must never be byte-compared against a
fresh re-render; see "These fields describe the install, not only the commit"
under "Gating a build before doing work" below.

Always exits `0`. Identifying the running build cannot fail — an
unrecoverable identity is reported as `X.Y.Z.dev0+unknown` with
`is_release: null`, never as an error.

## Reproducing a recorded pin (issue #3985)

A `.devN+g<sha>` version is not published to any index, so
`uvx --from "klayout-tools==<version>"` cannot resolve it. Pin on
**`git_commit`**, not on the version string: record `git_commit` (and
`reinstall_spec`) next to the result, then re-run under the same build with

```
uvx --from "<reinstall_spec>" klt ...
# e.g. uvx --from "klayout-tools @ git+https://github.com/2AMLogic/klayout-tools@<git_commit>" klt ...
```

Caveat: the re-installed build is the same commit, so `git_commit` and the
`+g<sha>` segment match, but `N` is computed from the tags visible to the
clone and may differ from the original build's. Compare `git_commit`, not
`version`. A build with `reinstall_spec: null` (dirty tree or no recorded
commit) cannot be reproduced and should not be used as a pin.

Reports record the same handle: `provenance.klt_git_commit` and
`provenance.klt_reinstall_spec` (issue #4412) equal `git_commit` and
`reinstall_spec` above, so a committed report names a re-obtainable build
without re-running `klt version`. Both are `null` when unrecoverable.

## How the identity is determined

Two sources, in priority order:

1. **Recorded at build time.** `hatch_build.py` (this repo's hatchling build
   hook) captures the commit/tag/dirty state of the checkout a wheel or sdist
   was built from, plus the distribution version computed from them, and
   writes them into the distribution as `klayout_tools/_build_info.py`. A
   wheel built from an unpacked sdist (which has no `.git`) keeps the sdist's
   version and record rather than probing git again. This is the source that matters for an
   installed wheel, which has no `.git` directory of its own — without a
   build-time record its origin would be unrecoverable, which is exactly why
   `pip install git+…@<sha>` used to be indistinguishable from a release.
2. **Probed live from the checkout.** For an editable/source install the hook
   deliberately records nothing (the working tree moves under the install with
   every commit), so the git state is read from the checkout the package files
   live in and `version` is recomputed with the same scheme, using the
   checkout's own `base-version`. A non-editable install into a `.venv/` that merely sits inside
   some unrelated checkout does *not* inherit that repo's HEAD — the probe
   requires the package directory to contain files git actually tracks.

The **policy** (what counts as a release, which version a build declares)
lives in the package — `src/klayout_tools/build_identity.py` and
`src/klayout_tools/_version_scheme.py` — not in the build hook, which loads
the scheme module by path; build and runtime share one implementation.

`klayout_version_expected` is recorded the same way, from the same
build-time-record-first / live-probe-fallback split, reading the checkout's
`uv.lock` (`klayout`'s pinned entry there is the version this project's own
CI test suite actually ran against for that commit, since CI installs via
`uv sync --locked`) instead of git facts — see
[`../design/klayout-engine-version-pin.md`](../design/klayout-engine-version-pin.md)
for the full mechanism and why a hard `klayout==` pin in `pyproject.toml`
was rejected.

## Pinning the KLayout engine version

Pinning `klayout-tools` to an exact git commit SHA does not, by itself, pin
the `klayout` engine version that commit resolves —
`pyproject.toml`'s `klayout>=0.30` dependency is an unbounded floor, so the
same commit installed on different days can resolve different `klayout`
versions, and DRC/LVS report internals (deck content hash, `rules_skipped`,
`category_counts`) can shift alongside it even though the verdict itself
does not (issue #1490).

`klayout_version_expected` above is the reproduction target; the fix is
`uv`'s own dependency override, no `klt`-specific flag needed:

```bash
uv tool install "klayout-tools @ git+https://github.com/2AMLogic/klayout-tools@<sha>" \
  --with klayout==<klayout_version_expected>
```

`klt drc`/`klt lvs` reports compare the two fields automatically —
`provenance.klayout_version_mismatch` — with a stderr warning on drift; see
[`../json-contract.md`](../json-contract.md)'s "Pinning the KLayout engine
version" section.

## Gating a build before doing work

`klt version` answers "which tool build is this?"; [`klt deck hash`](deck.md)
answers "which rule-deck revision will it use?" — the two halves a consumer
committing `klt` output as evidence needs, both without running a check or
having a layout file handy:

```bash
klt version --format json     | jq -e '.is_release == true'
klt deck hash --deck sky130 --format json \
  | jq -e '.content_hash == "sha256:<the hash your evidence was produced against>"'
```

`provenance.klt_version` in a report carries the same build identity as
`version` above (issue #2090) — see
[`../json-contract.md`](../json-contract.md).

### These fields describe the install, not only the commit (issue #2249)

**`version`, `git_commit`, `git_tag`, `dirty` and `is_release` all report the
state of the tree this install was *built from*, at the time it was built.**
Pinning a commit does not pin them: two byte-legitimate installs of the same
`<sha>` can report different values, because the routes differ in what git
facts existed at build time.

| Provisioning route for the same `<sha>` | `version` | `git_commit` | `dirty` |
| --- | --- | --- | --- |
| `uv tool install "klayout-tools @ git+https://github.com/2AMLogic/klayout-tools@<sha>"` | `X.Y.(Z+1).devN+g<sha>` (or `X.Y.Z.devN+g<sha>` if the clone carries no tags) | `<sha>` | `false` — but `true` before issue #2248, from the package manager's own untracked residue in its scratch checkout |
| Clean `git clone`/`git worktree add <sha>` + local `uv build` | `X.Y.(Z+1).devN+g<sha>` | `<sha>` | `false` |
| `pip install` of a source **tarball** of `<sha>` (GitHub `/archive/<sha>.tar.gz`, a vendored copy) | `X.Y.Z.dev0+unknown` | `null` | `null` |

The last row is not a defect and cannot be repaired: the build ran in a tree
with no `.git`, so there were no facts to record (`hatch_build.py` writes
nothing rather than guessing — see "How the identity is determined" above).

Two consequences for a gate script:

- **The byte-canonical route is a `git+…@<sha>` install** (or a clean local
  build of `<sha>`) — the only route that records the commit at all. A gate
  that wants reproducible identity bytes must provision *that* route on every
  machine; a tarball/vendored install of the same commit will report
  `.dev0+unknown` forever.
- **Never byte-compare a committed `klt` report against a fresh re-render to
  detect evidence drift.** The identity fields above are embedded in reports
  (`provenance.klt_version`, and `klt signoff`'s whole `build` block), so that
  comparison fails between two correct installs of the same pinned commit.
  Use the verbs' own `--check` modes instead — they exclude tool identity and
  only tool identity: `klt drc/lvs/extract --check`, `klt
  synthesize/place-and-route REQUEST --check`, and `klt signoff
  --manifest|--fleet M --check REPORT` (see
  [`signoff.md`](signoff.md#verifying-a-committed-report---check-issue-2249)).
