# Golden-artifact determinism (hash seed + checkout path)

This repo commits generated artifacts and byte-compares them in tests —
`tests/golden_deck/<deck>/manifest.json`, `tests/corpus/golden/**.layers.json`,
`tests/golden_metrics/*.json`. Each one's generator documents itself as
deterministic, and every generator here is. That claim is the thing this page
is about: **nothing was checking it**, because every existing check
regenerates an artifact at most once, from one checkout, under whatever hash
seed the runner happens to have.

- Job: `Golden artifacts (hash seed + path varied)` in
  [`.github/workflows/ci.yml`](../../.github/workflows/ci.yml)
- Check: [`scripts/check_artifact_determinism.py`](../../scripts/check_artifact_determinism.py)
- Tests: `tests/test_artifact_determinism.py`

## The two bug classes it catches

Both shipped downstream this week and were caught only in CI, after an
environment finally differed (2AMLogic/gf180-surge PR #39; the SXT-013
slates), having passed every local run on macOS first:

1. **Set/dict-iteration-dependent ordering.** `PYTHONHASHSEED` only defaults
   to random when it is *unset*, and a CI environment that effectively fixes
   it reproduces one ordering forever. A count-tied list emitted in
   set-iteration order then byte-compares clean on every run — until someone
   regenerates on a machine that hashes differently, at which point a
   byte-compare test fails somewhere that looks unrelated to the change.
2. **Host-absolute paths baked into a committed artifact.** `str(some_path)`
   instead of a repo-relative path byte-compares clean on the machine that
   produced it and nowhere else.

Neither is visible to a suite that regenerates once and compares. Both are
visible the moment you regenerate *twice*, varying exactly those two
variables — which is all this job does.

## What the job does

1. Checks the repo out **twice**, at two different filesystem paths
   (`primary` and `second-checkout/nested/deeper-than-the-primary`).
2. Runs the same artifact generators in each checkout under **two different,
   explicitly-set `PYTHONHASHSEED` values**, drawn randomly per run and
   echoed in the report.
3. Byte-compares every regenerated artifact across the two runs, and scans
   each for a host-absolute path.
4. Fails with the differing artifact paths named.

Runtime is ~1.5 s of generators on top of one `uv sync`; the subset is
deliberately small (see [`ci-wall-clock-budget.md`](ci-wall-clock-budget.md)).

## Reading a failure

Each finding names the artifact by repo-relative path and classifies it:

| Kind | Means |
|---|---|
| `unstable_ordering` | Bytes differ across hash seeds — a `set`/`dict` iteration order reached the output. Sort it, or key it on something stable. |
| `path_dependent` | Bytes differ between checkouts, and the artifact embeds its own checkout's absolute path. Record a repo-relative path instead. |
| `host_path` | The artifact contains a host-absolute path (`/Users/…`, `/home/…`, `/tmp/…`, `C:\Users\…`). Identical in both runs, so only the scan finds it — this is the committed-from-a-laptop case. |
| `missing` | An artifact was regenerated in one checkout but not the other. |

Exit codes mirror `scripts/check_ci_wall_clock.py`: `0` clean, `1` a real
difference, `2` the check could not run (a generator failed, fewer than two
distinct checkouts, an unreadable manifest). Exit `2` is a red, never a
silent pass.

## Reproducing locally

The seeds used are printed in the report, so a CI failure replays exactly:

```bash
# A second copy at a different path (any path will do).
git clone . /tmp/klt-second-checkout

python3 scripts/check_artifact_determinism.py \
    --checkout "$PWD" \
    --checkout /tmp/klt-second-checkout \
    --seed 238184080 --seed 3770007606        # the seeds CI printed
```

Both checkouts must be at the same commit; the check regenerates in place,
so run it on a clean tree (`git status` should be clean afterwards on a
passing run).

## Adding a generator to the checked subset

Add an entry to `DEFAULT_GENERATORS` in
`scripts/check_artifact_determinism.py`: the repo-relative script path, plus
the globs it writes. `tests/test_artifact_determinism.py::
test_default_manifest_names_real_generators` asserts every entry still names
a real script that matches at least one committed artifact, so a renamed
generator fails loudly instead of quietly dropping out of coverage.

The subset is a **runtime budget**, not a list of everything that could be
checked: keep it to generators that are fast and that write committed,
byte-compared artifacts. A generator that writes somewhere its globs do not
declare is still compared — the check unions its declared globs with
whatever the run newly dirties in git — but declaring the globs is what
makes coverage legible.

## Relationship to `klt lint-envelope` (#2224)

Issue #2224 proposes a static, schema-level lint for absolute paths in `klt`
envelopes. That is a different mechanism against an overlapping failure mode:
a lint reads one artifact and reasons about its contents; this job reasons
about *reproducibility* by producing the artifact twice under different
conditions. Defence in depth — neither subsumes the other.
