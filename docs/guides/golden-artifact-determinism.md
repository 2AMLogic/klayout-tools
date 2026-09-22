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

## The three bug classes it catches

All three shipped downstream this week and were caught only in CI, after an
environment finally differed (2AMLogic/gf180-surge PRs #39/#45/#49; the
SXT-013 slates), having passed every local run on macOS first:

1. **Set/dict-iteration-dependent ordering.** `PYTHONHASHSEED` only defaults
   to random when it is *unset*, and a CI environment that effectively fixes
   it reproduces one ordering forever. A count-tied list emitted in
   set-iteration order then byte-compares clean on every run — until someone
   regenerates on a machine that hashes differently, at which point a
   byte-compare test fails somewhere that looks unrelated to the change.
2. **Host-absolute paths baked into a committed artifact.** `str(some_path)`
   instead of a repo-relative path byte-compares clean on the machine that
   produced it and nowhere else.
3. **Host-libm float provenance** (issue #2279). A generator that computes a
   committed value through a transcendental (`math.pow`, `math.exp`, `**` on
   a float, numpy `power`/`float_power`) emits bytes whose last ulp is
   *allowed* to differ between conforming libms — gf180-surge's RTL
   coefficients diverged exactly this way across macOS py3.14 and ubuntu
   py3.12 (PR #49), and `np.float32` power diverged again from
   double-pow-then-round under NEP-50's float32 contagion (PR #45). Both of
   this job's runs sit on one host, so the divergence is invisible to the
   byte-compare **by construction**; classes 1–2 are caught by making the
   environment differ, class 3 by a static scan of each declared generator
   script instead.

Neither of the first two is visible to a suite that regenerates once and
compares. Both are visible the moment you regenerate *twice*, varying exactly
those two variables — which is all this job does.

## What the job does

1. Checks the repo out **twice**, at two different filesystem paths
   (`primary` and `second-checkout/nested/deeper-than-the-primary`).
2. Runs the same artifact generators in each checkout under **two different,
   explicitly-set `PYTHONHASHSEED` values**, drawn randomly per run and
   echoed in the report. `pinned-artifact` generators (see
   [below](#float-provenance-rules-for-committed-evidence-2279)) are never
   regenerated — their pin is verified against the commit instead.
3. Byte-compares every regenerated artifact across the two runs, and scans
   each for a host-absolute path.
4. Statically scans every non-pinned generator script for host-libm float
   computation, and verifies every pinned generator's recorded hashes.
5. Fails with the differing artifact paths named.

Runtime is ~1.5 s of generators on top of one `uv sync`; the subset is
deliberately small (see [`ci-wall-clock-budget.md`](ci-wall-clock-budget.md)).

## Reading a failure

Each finding names the artifact by repo-relative path and classifies it:

| Kind | Means |
|---|---|
| `unstable_ordering` | Bytes differ across hash seeds — a `set`/`dict` iteration order reached the output. Sort it, or key it on something stable. |
| `path_dependent` | Bytes differ between checkouts, and the artifact embeds its own checkout's absolute path. Record a repo-relative path instead. |
| `host_path` | The artifact contains a host-absolute path (`/Users/…`, `/home/…`, `/tmp/…`, `C:\Users\…`). Identical in both runs, so only the scan finds it — this is the committed-from-a-laptop case. |
| `host_float_op` | The generator script computes committed bytes through host libm (`math.pow(…)`, `** 0.5`, …) without a `pinned-artifact` declaration. The last ulp is host's; see the float-provenance section below. |
| `stale_pin` | A pinned generator's source no longer hashes to its recorded `generator_sha256` — the committed artifacts can no longer be attributed to the committed generator. |
| `pin_drift` | A pinned artifact's committed bytes no longer hash to the pin's `artifact_sha256` (or are gone). The bytes moved without a re-pin. |
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

## Float provenance rules for committed evidence (#2279)

Path hygiene and stable ordering cover the first two classes. The third —
bytes that depend on the *generating host's floating-point environment* —
has its own rule:

> An evidence-generating code path must be **integer-exact**, read its
> floats from a **committed pinned table**, or commit a **pinned artifact**
> with the generator's identity + inputs recorded so regeneration is
> verifiable rather than silently trusted.

Every generator in the determinism manifest declares `float_discipline`:

| Discipline | Promise | What the check does |
|---|---|---|
| `integer-exact` (default) | Only integer/exact arithmetic reaches the committed bytes. | Statically scans the generator script for host-libm float computation; any hit is a `host_float_op` finding (exit 1). |
| `pinned-table` | Float constants come from a committed literal table in the source. | The same scan: a table of literals is safe (parsing `0.1` to the nearest double is correctly rounded, so it is byte-stable on every host), but *computing* over the table is the same hazard. The discipline's value is the promise it records and the remedy it suggests. |
| `pinned-artifact` | The bytes are committed once and pinned; regeneration is not trusted across hosts. | The scan is skipped; the pin is verified instead (below). |

**What the scan flags:** `math.pow`/`exp`/`log`/`sin`/… (and the `cmath`
equivalents, plus `hypot`/`dist`/`cbrt`), numpy's elementwise
transcendentals (`exp`, `log`, `power`, `float_power`, … — `np.sqrt` excepted:
it is the correctly-rounded hardware square root), `from math import pow`
followed by bare `pow(...)`, the builtin two-argument `pow` with a float
literal, and `**`/`**=` with a float literal on either side — on CPython,
float `**` *is* libm `pow`.

**What is deliberately not flagged:** `+ - * / %` and comparisons
(IEEE-exact or correctly rounded by the standard), `math.sqrt`/`fabs`/
`fmod`/`fsum`/`fma`/`ldexp` (same), float *literals* (safe — see above), and
float *formatting* (shortest-repr and fixed-precision rendering are correctly
rounded, so the same double prints the same everywhere). The scan is a
source-level screen, not a proof: it cannot see through a name to a value's
dtype, so `arr ** 2` on a `float32` array goes unflagged — that residual risk
is exactly what `pinned-artifact` is for.

**The scan's boundary is the generator script itself**, not the library
modules it imports. Floats in analysis code are legitimate (issue #2279's
stated non-goal); only the committed-byte path is held to the rule.

### The pinned-artifact escape hatch

When float generation is unavoidable, commit the *derived artifact* (the
gf180-surge `cfg.hex` shape) and pin it — in the manifest:

```json
{
  "script": "rtl/gen_coeffs.py",
  "artifacts": ["rtl/cfg.hex"],
  "float_discipline": "pinned-artifact",
  "generator_sha256": "<64 hex of gen_coeffs.py at pin time>",
  "regenerate": "python3 rtl/gen_coeffs.py > rtl/cfg.hex",
  "artifact_sha256": { "rtl/cfg.hex": "<64 hex of the committed bytes>" }
}
```

For a JSON artifact, mirror the same facts in the artifact's envelope:
`"pinned": true`, `"generator"`, `"generator_sha256"`, `"regenerate"` —
documented in
[`docs/json-contract.md`](../json-contract.md) → "Pinned derived artifacts".

A pinned generator is **never regenerated by the check** — drift detection
compares against the pin, not silent regeneration. The check verifies,
statically, on every run:

- the generator source still hashes to `generator_sha256`, else **`stale_pin`**
  (exit 1): the committed artifacts can no longer be attributed to the
  committed generator;
- every pinned file still hashes to its `artifact_sha256`, else
  **`pin_drift`** (exit 1): the committed bytes moved without a re-pin.

Re-pinning is a reviewed act: regenerate on a trusted host, review the diff,
and update the hashes **in the same commit as the regenerated bytes**, so the
pin and the bytes move together.

## Adding a generator to the checked subset

Add an entry to `DEFAULT_GENERATORS` in
`scripts/check_artifact_determinism.py`: the repo-relative script path, plus
the globs it writes, plus its `float_discipline` (`integer-exact` is the
default and is what a generator whose committed bytes involve no computed
floats should declare — the scan verifies the claim on every run; use
`pinned-artifact` only with the pin fields, per the section above).
`tests/test_artifact_determinism.py::
test_default_manifest_names_real_generators` asserts every entry still names
a real script that matches at least one committed artifact, and
`test_default_manifest_generators_declare_integer_exact_and_scan_clean`
re-scans every entry's source, so a renamed generator — or one that starts
computing bytes through host libm — fails loudly instead of quietly dropping
out of coverage.

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
conditions. Defence in depth — neither subsumes the other. The float
scan (#2279) is the same relationship one class over: `lint-envelope` reads
the committed artifact; the scan reads the *generator* that produces it.
