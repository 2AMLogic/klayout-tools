# `klt version`

Report which klayout-tools build is running — its version, the git commit it
was built from, and whether that commit is this version's release tag
(issue #1202) — plus the KLayout engine version actually resolved and the
version this build/commit was tested against (issue #1490).

```
klt version [--format text|json]
klt --version            # same one-line string as `klt version --format text`
```

## Why a version string is not an identity

The package version is a static string in `pyproject.toml`. A wheel built
from the `v0.2.0` tag and a wheel built from a commit 300 changes later both
declare `0.2.0` to `pip`, because that is the version they were built from —
so a repo that commits `klt` output as **evidence** could record `klt 0.2.0`,
believe it had pinned a release, and only discover much later that every
artefact it held came from an unreleased build shipping different rule decks.

`klt version` closes that: a build that is not a confirmed tagged release
reports a PEP 440 local-version segment, so "am I on a release?" is
answerable by string inspection alone.

| Build | Reported version |
|---|---|
| Made at this version's release tag, clean tree | `0.2.0` |
| Any other commit | `0.2.0+g<12-char sha>` |
| Uncommitted changes in the tree it was built from | `0.2.0+g<sha>.dirty` |
| No recoverable git provenance | `0.2.0+unknown` |

**A tagged release reports the bare `X.Y.Z` it always did** — that is a
deliberate regression guard, so anything already parsing `klt X.Y.Z` is
unaffected. The suffix appears only on builds that previously masqueraded as
a release.

## `klt version --format json`

```json
{
  "schema_version": 1,
  "version": "0.2.0+g4f1c8a9b2d3e",
  "package_version": "0.2.0",
  "git_commit": "4f1c8a9b2d3e5f60718293a4b5c6d7e8f9a0b1c2",
  "git_tag": null,
  "dirty": false,
  "is_release": false,
  "grading_ruleset_id": "sha256:1a2b3c4d5e6f...",
  "klayout_version": "0.30.12",
  "klayout_version_expected": "0.30.10"
}
```

- `version` — the identity-bearing version string above, also recorded as
  `provenance.klt_version` in reports.
- `package_version` — the plain package version (`klayout_tools.__version__`).
  Equals `version` exactly when `is_release` is `true`.
- `git_commit` — the commit the build was made from, or `null` when
  unrecoverable.
- `git_tag` — the tag the build sits exactly on, or `null`. A tag belonging to
  a *different* version is reported here but does not make `is_release` true.
- `dirty` — whether the tree had uncommitted changes at build time (`null`
  when unknown).
- `is_release` — **tri-state**, mirroring `provenance.deck.released`
  (see [`../json-contract.md`](../json-contract.md)): `true` for a confirmed
  release build, `false` for a confirmed non-release build, `null` when the
  question is unanswerable. A consumer gating on "this is a release" must
  require `is_release === true`; `null` is not a weaker `true`.
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

Always exits `0`. Identifying the running build cannot fail — an
unrecoverable identity is reported as `+unknown` with `is_release: null`,
never as an error.

## How the identity is determined

Two sources, in priority order:

1. **Recorded at build time.** `hatch_build.py` (this repo's hatchling build
   hook) captures the commit/tag/dirty state of the checkout a wheel or sdist
   was built from and writes it into the distribution as
   `klayout_tools/_build_info.py`. This is the source that matters for an
   installed wheel, which has no `.git` directory of its own — without a
   build-time record its origin would be unrecoverable, which is exactly why
   `pip install git+…@<sha>` used to be indistinguishable from a release.
2. **Probed live from the checkout.** For an editable/source install the hook
   deliberately records nothing (the working tree moves under the install with
   every commit), so the git state is read from the checkout the package files
   live in. A non-editable install into a `.venv/` that merely sits inside
   some unrelated checkout does *not* inherit that repo's HEAD — the probe
   requires the package directory to contain files git actually tracks.

The **policy** (what counts as a release) lives in
`src/klayout_tools/build_identity.py`, not in the build hook: the hook records
raw git facts only.

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
