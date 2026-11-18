# Decision: pinning (and detecting drift in) the KLayout engine version

**Status:** decision record, implemented. Issue
[#1490](https://github.com/2AMLogic/klayout-tools/issues/1490).

## The problem

`klayout-tools`' own `pyproject.toml` declares its KLayout dependency as an
unbounded floor:

```
dependencies = [
    "klayout>=0.30",
    ...
]
```

Pinning `klayout-tools` to an exact git commit SHA — the documented,
recommended way to get a reproducible install (`uv tool install
"klayout-tools @ git+https://github.com/2AMLogic/klayout-tools@<sha>"`) —
therefore does **not** pin the KLayout engine version that commit resolves.
`pip`/`uv` picks up whatever the latest `klayout` PyPI release is on the day
of install, independent of `klayout-tools`' own commit history. Two installs
of the *identical* commit SHA, made on different days, can resolve two
different `klayout` versions.

`klayout` is not merely a version string here — it is the engine that
performs DRC extraction and LVS geometry comparison. A KLayout point release
can shift extraction edge cases (an added checked-layer entry, an extra
`rules_skipped` entry, a `category_counts` count moving by one) even when the
verdict itself (`clean` / `match`) is unaffected. A project that commits
DRC/LVS reports as regression baselines and periodically re-verifies them
against a fresh install — the exact pattern this project's own
`--check`/`--rerun` guidance recommends (`docs/json-contract.md`, "Verifying
a committed report still reproduces") — sees this as false drift, with
nothing in `klt`'s own output distinguishing "the engine changed under me"
from "the design/deck actually changed."

## Options considered

**(a) Hard-pin `klayout==X.Y.Z` in `pyproject.toml`.** Rejected. This
repo's own dependency floor comment
(`pyproject.toml`, `klayout>=0.30`) documents a real reason for staying on a
floor rather than an exact pin: a hard pin would break installs on any
KLayout point release this project's own deck test suite has never
exercised, turning every routine KLayout bugfix release into a forced
`klayout-tools` release just to bump the pin — CI itself installs via a
floor (`uv sync --extra dev`, no lockfile pin on the *dependency
declaration*, only on the *resolved* lockfile — see "Mechanism" below). A
downstream project that legitimately wants the newest KLayout bugfixes
(most callers, most of the time) would be blocked from getting them without
an unrelated `klayout-tools` release. Pinning trades away the common case
to fix an edge case that has a cheaper, opt-in fix (see (c)).

**(b) An explicit `--klayout-version` / environment-variable override at
install time.** Rejected as unnecessary additional surface, because it
already exists without any new code: `uv tool install "klayout-tools @
git+https://github.com/2AMLogic/klayout-tools@<sha>" --with klayout==0.30.10`
installs the pinned `klayout-tools` commit with a caller-chosen `klayout`
version override — `uv`'s own `--with` flag, layered on top of the git
dependency, already does exactly this. Building a second, `klt`-specific
mechanism for the same thing would duplicate `uv`'s own feature for no
additional expressiveness. What was actually missing was not the
*mechanism* (it works today) but the *documentation* pointing a caller at it,
and a way to *know* the reproduction target version in the first place — see
below.

**(c) Surface the version this build/commit was tested against, detect
drift, warn loudly. Chosen.** `klt version --format json` gains
`klayout_version` (the engine actually resolved into this process) and
`klayout_version_expected` (the version this build/commit was tested
against); every `klt drc`/`klt lvs` report's `provenance` block gains
`klayout_version_mismatch: true|false`, plus a one-line stderr warning when
it is `true`. This does not prevent a mismatched install (the way (a)
would) — it makes the mismatch **detectable before a caller trusts a
"reproduced" report**, and gives them the exact command ((b), now
documented) to fix it. This is strictly additive to the existing
`provenance.klayout_version` field (`docs/json-contract.md`), needs no
`schema_version` bump on either command, and costs nothing for a caller who
never hits the drift case (`klayout_version_mismatch: false`, no output on
stderr).

## Mechanism: where "the version this build/commit was tested against" comes from

The chosen mechanism reuses an already-established, load-bearing pattern in
this codebase rather than inventing a new one: `hatch_build.py` (the
hatchling build hook backing `klt version`'s existing `git_commit`/`git_tag`/
`is_release` fields, issue #1202) already records facts about the checkout a
distribution was built from into a generated `klayout_tools/_build_info.py`
module, because an installed wheel has no `.git` directory of its own to
probe later.

The candidate sources considered for "the tested version":

- **A hand-maintained pin file** (e.g. `KLAYOUT_VERSION_EXPECTED.txt`) — a
  human has to remember to update it every time the resolved `klayout`
  version actually changes. Rejected: it can silently go stale, exactly the
  failure mode `provenance.deck.released` (issue #1193) was designed to
  avoid for deck content.
- **Probing `klayout.__version__` inside the build environment at build
  time.** Rejected: `uv build` (this project's own `publish.yml` build step)
  runs in an isolated build environment that installs only the
  `[build-system]` backend (`hatchling`), not the package's own runtime
  dependencies — `klayout` is not importable there. Making it importable
  would mean disabling build isolation, a much larger and riskier change to
  the release pipeline for a single derived field.
- **`uv.lock`'s own pinned `klayout` entry. Chosen.** This repo already
  commits a `uv.lock` to the repository, and CI already installs from it
  exactly (`uv sync --locked ...` throughout `.github/workflows/ci.yml`) —
  so the `klayout` version pinned in `uv.lock` at a given commit *is*, by
  construction, the version CI actually ran the test suite against for that
  commit. No new bookkeeping is required: `uv lock`/`uv add`/`uv sync
  --upgrade` already keep it current as a routine side effect of ordinary
  dependency maintenance, the same discipline that already keeps every other
  pinned dependency in `uv.lock` current.

`hatch_build.py` now also reads `uv.lock` at build time (a plain regex
match against the `klayout` `[[package]]` entry — `uv.lock`'s format is
stable and machine-generated, and adding a TOML-parsing dependency for one
field was not justified; see that module's own docstring) and writes the
result into `_build_info.py` as `KLAYOUT_VERSION_EXPECTED`, alongside the
existing `GIT_COMMIT`/`GIT_TAG`/`GIT_DIRTY` fields. This is populated for
exactly the same set of installs the existing git-identity fields are: any
non-editable build made from a real git checkout — which includes `uv tool
install "klayout-tools @ git+...@<sha>"`, the reproducibility scenario this
issue is about. `src/klayout_tools/build_identity.py` reads it back with the
same two-source, build-time-record-first, live-probe-fallback priority
`identity()` already uses for the git facts (the live probe reads the
checkout's own `uv.lock` directly, for an editable/source install).

**Why the DRC/LVS hot path (`provenance.klayout_version_mismatch`) uses only
the build-time record, never the live-probe fallback.** An early
implementation had `klt drc`/`klt lvs` call the same two-source lookup `klt
version` uses. That path shells out to `git` when the build-time record is
absent, which is exactly the case for this project's own editable/dev
checkout — and it does so **on every single `klt drc`/`klt lvs` report**,
not once per process. Beyond the wasted subprocess calls, this leaked into
existing tests that (correctly, for their own purpose) globally monkeypatch
`subprocess.run` — the netgen LVS engine tests
(`tests/test_lvs.py::test_netgen_engine_*`) intercept `subprocess.run` to
stub the `netgen` binary, and the live git probe's own `subprocess.run`
calls were caught by the same stub, corrupting its `captured_cmds` list.
Since no `uv tool install ...@<sha>` install is ever editable — the exact
install path this field exists for always gets a build-time record —
`provenance.klayout_version_mismatch` was scoped down to consult only
`_build_info.KLAYOUT_VERSION_EXPECTED`. An editable/dev checkout with no
build-time record reports `klayout_version_mismatch: false`, matching
`_klayout_version_mismatch`'s documented "unresolvable renders as `False`"
rule (never fabricate a mismatch from missing data) — the same discipline
`provenance.deck.released` already applies for a missing/unreadable deck
history table. `klt version --format json`'s `klayout_version_expected`
keeps the live-probe fallback, since it is a single explicit command, not a
per-report hot path.

## Why only `drc`/`lvs`, not every `build_provenance` caller

`klayout_version_mismatch` is opt-in
(`build_provenance(include_klayout_version_mismatch=True)`, default `False`)
and is passed only by `klt drc` and `klt lvs`. Those are the two verbs whose
output is most commonly committed as a byte-comparable regression baseline
and re-verified later (`docs/json-contract.md`'s `--check`/`--rerun`
section) — extending the same field to every provenance-carrying verb
(`extract`, `sim`, `size`, `precheck`, …) is a reasonable future extension
but is out of this issue's scope; this decision does not preclude it.

## Interaction with `--check`/`--rerun` (`_report_verify.py`)

`_report_verify.VOLATILE_PROVENANCE_PATHS` already excludes
`provenance.klayout_version` (and `klt_version`, `pdk.version`) from `klt drc
--check --rerun`/`klt lvs --check --rerun`'s drift diff — those fields
legitimately vary between two runs of identical inputs and must not report
`status: "drifted"` on their own. `provenance.klayout_version_mismatch` is
**deliberately not added to that exclusion list**. Unlike the raw version
strings, whether the *mismatch itself* changed between the committed run and
a fresh rerun is itself informative — a `false` → `true` transition means a
freshly reproduced report used a different engine than the one that
produced the committed baseline, exactly the condition a caller re-running
`--check --rerun` wants surfaced, not silently swallowed. This file's own
comparison logic is unmodified by this issue.

## Reproducing the exact engine

Once a caller has `klt version --format json`'s `klayout_version_expected`
(or a mismatch warning naming it), the fix is `uv`'s own dependency
override, not a new `klt`-specific flag (option (b) above):

```bash
# released install:
uv tool install "klayout-tools==<klt version>" \
  --with klayout==<klayout_version_expected>

# dev/git checkout:
uv tool install "klayout-tools @ git+https://github.com/2AMLogic/klayout-tools@<sha>" \
  --with klayout==<klayout_version_expected>
```

Since issue #5601, the mismatch warning itself prints whichever of the two
forms matches the running build, derived from `reinstall_spec()` — the
version-pin form on a released install, a real git-commit form on a clean
dev checkout, and no fabricated command when the build is not reinstallable
(dirty tree / no recorded commit). Options (a) and (b) above stand
unchanged.

See `docs/json-contract.md`'s "Shared `provenance` block" section for the
field reference and this same recipe alongside the existing
`provenance.klayout_version` guidance.

## GDS byte-reproducibility depends on the klayout module version

The `klayout>=0.30` floor guarantees API compatibility, not byte-identical
GDS output. Different klayout module versions can serialise the same layout
differently (a consumer observed 0.30.x re-encoding some TEXT MAG records
relative to 0.28.x; not verified here). Consumers who commit GDS artifacts
and need them byte-stable should pin the module exactly (see "Reproducing
the exact engine" above). `klt place-and-route`'s GDS merge uses only
`Layout.read`/`Layout.write`, so it does not itself require the 0.30-only
`read_bytes`/`write_bytes` API (#5768).

## Engine version compatibility

*(Added 2026-11-17, against commit `59096013d`. Line numbers cited below
drift — re-run the grep before relying on them.)*

`klayout-tools` declares its KLayout dependency as the floor
`klayout>=0.30` (`pyproject.toml`, issue
[#217](https://github.com/2AMLogic/klayout-tools/issues/217): the
`LayoutToNetlist.polygons_of_net(net, layer_index)` layer-index form that
klt's parasitic-extraction and ERC paths are built on was introduced in
KLayout 0.30). This section states what that floor means in practice —
including for a downstream that wants to co-install an *older* engine
(e.g. a byte-reproducibility consumer whose committed layout artifacts were
written by the 0.28.x serializer, issue
[#5761](https://github.com/2AMLogic/klayout-tools/issues/5761)).

- **`klayout>=0.30` is supported.** The engine version CI actually runs the
  test suite against is `uv.lock`'s pinned `klayout` entry — `0.30.12` at
  the commit this section was written — surfaced as
  `klt version --format json`'s `klayout_version_expected`. Any other
  `>=0.30` version resolves normally and is expected to work, but is **not
  tested** by this repo; `klt drc`/`klt lvs` reports flag a
  resolved-but-untested engine via `provenance.klayout_version_mismatch`
  (see "Why only `drc`/`lvs`" above), and "Reproducing the exact engine"
  below pins it.

- **Below 0.30 is unsupported.** The verbs that read per-net/per-layer
  geometry through `polygons_of_net(net, layer_index)` are expected to
  fail against a 0.28.x module: `klt extract` (including
  `--parasitics`/substrate paths), `klt pex`, `klt erc` (components, ties,
  substrate), `klt components`, `klt power` and `klt tap-distance`. Call
  sites as of `59096013d`: `src/klayout_tools/components.py:445,461,490`;
  `src/klayout_tools/erc.py:1331,1367,1601,1605,1875,2988`;
  `src/klayout_tools/erc_substrate.py:217`; `src/klayout_tools/erc_ties.py:134`;
  `src/klayout_tools/extract.py:877,1094`;
  `src/klayout_tools/extract_parasitics.py:622,624,991,993,1603,2058`;
  `src/klayout_tools/power.py:1287,1327,2138`;
  `src/klayout_tools/tap_distance.py:266` — re-derive with
  `git grep -nE 'polygons_of_net\([a-z_]+,' -- src`, dropping the two
  docstring/comment mentions it also matches.

  Installing an older engine anyway means bypassing the floor
  (`pip install --no-deps klayout-tools==<ver>` next to the older
  `klayout` module, with the remaining runtime dependencies —
  `jsonschema` at minimum — installed explicitly). That is a **best-effort,
  unverified configuration, not a supported contract**: a future klt
  release may add runtime dependencies that `--no-deps` silently skips,
  and the floor deliberately does not promise anything about API surface
  below 0.30. As a downstream observation (issue #5761, not a tested
  claim — no CI lane covers 0.28.x): `klt gen` and `klt signoff` ran
  against `klayout==0.28.17.post1`, and `klt signoff` envelopes were
  byte-identical between module versions 0.28.17 and 0.30.13.

- **Layout stream bytes are an engine-version concern on top of all this.**
  The serializer that writes a layout artifact is the `klayout` module
  itself (`ly.write()`), and its byte output can change between engine
  versions — downstream observation: a TEXT `MAG` real8 re-encoding
  between 0.28.x and 0.30.x, 2 differing records out of ~177k on a
  stock-PDK cell. Independently of engine version, `klt` does not
  guarantee that generated stream bytes are stable across *klt* versions —
  see `docs/json-contract.md`'s "Stream-stability contract" section (issue
  [#5682](https://github.com/2AMLogic/klayout-tools/issues/5682)). A
  byte-level reproducibility gate on a layout artifact therefore pins the
  **engine** exactly ("Reproducing the exact engine" above) as well as the
  `klayout-tools` version; neither pin alone suffices.

Widening the floor to `klayout>=0.28` was considered and rejected when this
section was added: it would make the unsupported `polygons_of_net` call
paths installable without any signal, and a 0.28 compatibility claim would
need a 0.28 CI lane to be more than hope. A `klt doctor`-style
dependency check was also considered and rejected as already covered:
`klt version --format json` reports the pair (`klayout_version`,
`klayout_version_expected`), and `klt drc`/`klt lvs` provenance flags the
mismatch (`docs/cli/version.md`).
