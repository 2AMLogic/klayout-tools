# `klt mom` capacitance cross-validation against FastCap

Methodology for the FastCap-backed cross-validation oracle — issue
[#2015](https://github.com/2AMLogic/klayout-tools/issues/2015), pairing #4 of
tracking issue [#2007](https://github.com/2AMLogic/klayout-tools/issues/2007)
("independent cross-validation oracles for `klt` verdicts"). The
implementation is
[`tests/helpers/fastcap_oracle.py`](../../tests/helpers/fastcap_oracle.py) plus
[`tests/test_mom_capacitance_oracle.py`](../../tests/test_mom_capacitance_oracle.py).

Run it with:

```bash
scripts/install-fastcap.sh          # pinned FastCap 2.0 -> ~/.cache/fastcap-2.0-ec3479e
export PATH="$HOME/.cache/fastcap-2.0-ec3479e/bin:$PATH"
uv sync --extra dev --group mom     # builds klt_mom_native
uv run pytest tests/test_mom_capacitance_oracle.py -v --capture=tee-sys
```

In CI it runs on every PR, as two steps of `ci.yml`'s `Native engines (Rust)`
(`mom`) leg — the same leg that already builds `klt_mom_native` and runs the
NEC2++ full-wave cross-check, with the same "assert it was not skipped" gate.

## The gap this closes

`klt mom`'s capacitance matrix comes from **one** implementation:
`native/mom/src/solver.rs`, a constant-panel, point-collocation Method of
Moments written for this repo. Two test tiers already cover it:

| Tier | What it can catch | What it cannot |
| --- | --- | --- |
| `tests/test_mom.py` | contract breaks — shape, signs, warnings, errors | any numerically wrong-but-well-formed matrix |
| `tests/test_mom_validation.py` | disagreement with analytic closed forms on the ~4 shapes that *have* one | anything on a geometry with no closed form, i.e. every real layout |
| `tests/test_mom_cross_validation.py` | full-wave `S`-parameter disagreement with NEC2++ | nothing about the **quasi-static capacitance matrix**, a separate solver path (`solver.rs`, not `fullwave.rs`) |

[FastCap](https://github.com/ediloren/FastCap2) closes exactly that gap. It is
a different implementation of the same job — 1992 M.I.T. C, no shared code,
no shared language. Both codes are constant-panel collocation: `solver.rs`
(since #2061) integrates the source panel's potential at the target centroid
for near-field pairs and keeps the cheap centroid point-charge kernel for
well-separated ones, while FastCap evaluates the same collocation integral
analytically everywhere and accelerates the far field with multipoles.
Agreement between them is therefore evidence about `klt mom`'s numerics
that no amount of self-consistency testing can produce.

It is also, unusually for an oracle, the *source*: `solver.rs` cites Nabors &
White, "FastCap: A Multipole Accelerated 3-D Capacitance Extraction Program",
IEEE TCAD 1991 as the method it implements. So this pairing answers a sharp
question — **does this repo's simplified re-implementation of FastCap's method
agree with FastCap?** — rather than a vague "do two tools agree".

**Oracle, not runtime.** Nothing in `src/klayout_tools/` imports
`tests/helpers/fastcap_oracle.py` or shells out to `fastcap`; it exists only
under `tests/`, the same call
[`magic-oracle.md`](magic-oracle.md) records for magic and
[`mom-cross-validation.md`](mom-cross-validation.md) for NEC2++.

## Why FastCap and not Palace

#2007 named two candidates and asked for the choice to be justified at
adoption time.

| | FastCap 2.0 | [Palace](https://github.com/awslabs/palace) |
| --- | --- | --- |
| Method | Boundary-element MoM — **the same method class** `klt mom` implements | Finite-element (FEM) |
| Input | A flat list of quadrilateral panels (`.qui`), which is exactly what `klt mom` already discretises its boxes into | A volume mesh (Gmsh) of the conductors **and the dielectric**, plus a JSON solver config |
| Build | 23 C files, `make`, no dependencies beyond libm — **7 seconds** from a cold fetch on a CI runner | CMake superbuild over MPI, MFEM, libCEED, PETSc/SLEPc, and (in practice) Gmsh; tens of minutes, GB-scale |
| Reported accuracy | FastFieldSolvers' own "de-facto golden reference standard" framing — **their** description, not an independent finding | sub-0.3% against commercial solvers (arXiv:2511.01220) — also the authors' own report |
| Shared surface with `klt mom` | none (different language, codebase, discretisation, era) | none |

Both claims in that last-but-one row were taken at face value by neither this
document nor the tests: what is recorded below is agreement **measured against
this repo's own geometry**, which is the only claim these tests make.

FastCap wins on every practical axis for a check that must run on every PR:
the mesh `klt mom` already builds maps onto FastCap's input format
one-for-one (so the comparison isolates the *solver*, with meshing held
fixed), and the whole install fits in the existing `mom` CI leg's wall-clock
budget. Palace's FEM formulation would require meshing the dielectric volume
and would make the comparison "two different meshes of two different models",
which is weaker evidence, not stronger — and it cannot be provisioned inside
this repo's CI budget (`.github/ci-wall-clock-budget.json`).

**Palace is not ruled out forever.** The case for it gets stronger exactly
when `klt mom` grows the thing it cannot do today: a genuinely
inhomogeneous, multi-slab dielectric stack (`mom.py`'s MVP solves a single
homogeneous medium, and `_resolve_stackup_from_pdk` raises rather than
average two permittivities). At that point a volume-meshing FEM oracle is
checking something a BEM oracle in a uniform medium cannot. Until then it
would be a much larger CI bill for weaker evidence.

## Which FastCap source, and licensing

`ediloren/FastCap2` has three branches. This repo pins **`master`**, the
original M.I.T. distribution, at commit `ec3479e` (the repository's only
commit on that branch — it is a 2015 snapshot, not an active fork), fetched
and checksum-verified at install time by
[`scripts/install-fastcap.sh`](../../scripts/install-fastcap.sh). Nothing is
vendored.

The branch choice is a **licensing** decision. `master`'s sources carry
M.I.T.'s 2003 relicensing — "License to use, copy, modify, sell and/or
distribute this software and its documentation for any purpose is hereby
granted without royalty" — a permissive license compatible with pointing a
public MIT repo's contributors and CI at it. The `WRCad` branch (Whiteley
Research) already carries modern-toolchain fixes, but its sources still carry
M.I.T.'s **original 1990** license: "Permission to use, copy and modify for
internal, noncommercial purposes is hereby granted. Any distribution of this
program or any part thereof is strictly prohibited without prior written
consent of M.I.T." That is the wrong license to build a public project's CI
on, so this repo takes the permissive branch and carries the build fixes
itself.

Those fixes are
[`scripts/patches/fastcap-2.0-modern-toolchain.patch`](../../scripts/patches/fastcap-2.0-modern-toolchain.patch),
two hunks, both build-only — nothing in them touches the discretisation, the
expansion, the kernel or the solve, so the numbers are FastCap's own:

1. **`src/mulGlobal.h`** — allocate through `calloc`/`malloc` instead of
   FastCap's own `sbrk()`-based `ualloc`. `ualloc` returns `char *` but is
   never prototyped, so on LP64 every allocation is truncated to the
   implicitly declared `int` return type and the **first dereference
   segfaults**. (Confirmed by building the unpatched source: it dies in
   `main` at its very first `CALLOC`.) This is the same switch Whiteley
   Research made in their branch, for the same reason.
2. **`src/mulSetup.c`** — forward-declare that file's seven `static`
   helpers, each of which is called above its K&R definition. The implicit
   declaration that creates is `extern`, and the later `static` definition
   is then a hard error on every C compiler since GCC 9.

The build also pins `-std=gnu89 -fcommon -fno-strict-aliasing`: this is K&R
C from 1992, and each of those flags restores a language/codegen default that
has since changed underneath it.

`patch` is invoked without `--forward`/`--fuzz`, so a drifted upstream fails
the install loudly rather than silently building something else.

## Matched inputs (#2007 criterion 1)

A disagreement is only informative if both solvers were asked the same
question.

- **Geometry.** Both are handed the *same in-memory conductor request* — the
  `[{"name", "boxes"}]` structure
  [`mom.solve_capacitance_matrix`](../../src/klayout_tools/mom.py) itself
  takes — not two transcriptions of one design.
- **Mesh.** `fastcap_oracle.box_quads` reproduces
  `native/mom/src/geometry.rs`'s face set and subdivision **exactly**: one XY
  face for a zero-thickness lamina (`z0_um == z1_um`), all six prism faces
  otherwise, degenerate faces skipped, and `round(extent / panel_size)`
  segments per edge floored at 1. `test_exported_mesh_matches_the_rust_discretisation`
  asserts the resulting panel count equals the Rust discretiser's, for three
  fixtures at three panel sizes, and every comparison asserts
  `klt_report["panel_count"] == oracle.panel_count`. Holding the mesh fixed
  is deliberate: it makes the measured difference attributable to the
  **kernel and the solve**, which is the part of `klt mom` with no other
  independent check.
- **Units.** `klt mom` works in micrometres; FastCap is MKS (its constant is
  `4 pi eps_0` in SI). The exporter's `1e-6` is the only unit conversion in
  the comparison. FastCap chooses its output SI prefix per run (whichever
  puts its smallest off-diagonal between 0.1 and 10), so the parser reads the
  prefix out of the `CAPACITANCE MATRIX, <prefix>farads` header rather than
  assuming femtofarads.
- **Model.** Both report the **Maxwell (short-circuit) capacitance matrix**:
  positive diagonal, negative off-diagonals. `solver.rs` grounds every other
  conductor while holding one at 1 V; FastCap's `mksCapDump` does the same and
  warns on its own stdout if its result is not diagonally dominant. Sign
  convention is asserted on both sides, so a future "SPICE-style"
  (all-positive coupling) convention change on either side fails here.
- **Dielectric.** A single homogeneous medium of relative permittivity
  `background_permittivity`, which is all `klt mom`'s MVP solves. Passed to
  FastCap through its conductor-surface **list file** — see the trap below.

### The `-p` trap

FastCap's `-p<factor>` command-line "permittivity factor" is applied
**twice**: once as the surface's `outer_perm` during the solve, and again
when `mksCapDump` prints the matrix (`scale*FPIEPS*relperm*sym_mat[j][i]`).
Measured directly against this repo's geometry while writing this oracle:
`-p3.9` reports `3.9^2 = 15.21x` the free-space answer, a 74% error against
`klt mom` that looks exactly like a solver disagreement.

`run_fastcap` therefore never passes `-p`. It writes a one-line
conductor-surface list file (`C <file> <eps_r> 0 0 0`) and passes `-l`, which
applies the permittivity exactly once. Cross-checked both ways: through the
list file, `eps_r = 3.9` reproduces `3.9x` the `eps_r = 1.0` matrix on both
sides, which is the exact linearity in `eps` that
`tests/test_mom_validation.py` already asserts for `klt mom` alone.

## Evidence the work ran (#2007 criterion 2)

An exit code proves nothing here, and that is not hypothetical: FastCap's
input reader calls **`exit(0)`** — success — on a malformed input line, a bad
list-file entry, and an unreadable file. `run_fastcap` therefore fails closed
on all of:

- a non-zero exit status, or any of FastCap's own failure phrases in its
  output (`bad quad format`, `can't open`, `no conductor names specified`,
  `zero element request`, `out of memory`, …);
- no `CAPACITANCE MATRIX` block, an unrecognised unit prefix, or a matrix
  that is not `n_conductors` square;
- a `Total number of panels` line disagreeing with the number of panels the
  exporter wrote (FastCap read every panel, and no others);
- a `Number of conductors` line disagreeing with the request (FastCap keys
  conductors off the name string in each panel line, so a silent merge is a
  plausible failure — and the exporter refuses name collisions up front for
  the same reason);
- a missing per-column `ITERATION DATA` block (it solved one right-hand side
  per conductor, not just parsed the input);
- no version banner (the field recorded in provenance comes from *this*
  run's own output, never from a second invocation or the install path).

The export side fails closed too: boxes that overlap or touch are rejected
(see "Unsupported"), as is a conductor with no panels.

## Fixtures and measured results (#2007 criterion 3)

Recorded 2026-09-22 on `feature/issue-2061` (near-field quadrature kernel,
#2061), FastCap **2.0 (18Sep92)** (`ec3479e` + the patch above), `klt`
**0.5.0**, KLayout **0.30.10**, `klt_mom_native` fingerprint
`189c88096c8c97df`, panel size 0.5 µm unless stated. All 26 tests pass in
~12 s. The pre-#2061 measurements this table supersedes are in git history
(0.17% / 1.29% / 3.29% worst-case) and the story of the one big mover — the
parallel-plate fixture — is told in "Where the two solvers disagree most"
below.

| Fixture | Panels | Worst-case `klt mom` vs FastCap | Notes |
| --- | --- | --- | --- |
| Coupled lines (2 × 20 × 2 × 0.6 µm, 1.0 µm apart, ε_r 3.9) | 816 | **0.033%** (coupling), 0.006% (self) | the primary known-good case |
| Same, through `klt mom`'s GDS + stackup-spec file path | 816 | **0.033%** | `run_mom`'s matrix is bit-identical to the in-memory solve's |
| Parallel plates (10 µm square laminae, 1.0 µm apart, vacuum) | 800 | **0.32%** | flat laminae — see below |
| Coupled lines over a grounded plane (3 conductors) | 3068 | **0.46%** | full 3×3 matrix |
| Seeded spacing defect (1.5 µm instead of 1.0 µm) | 816 | **0.065%** | both see the defect |
| Seeded missing conductor (ground plane deleted) | 816 | **0.033%** | both see the defect |

Analytic anchor, on the parallel-plate fixture: the textbook `ε₀A/d =
0.885419 fF` is a strict lower bound for finite plates (fringing is
additive). `klt mom` reports 1.145× it, FastCap 1.148× — both above it, by
the same magnitude, which is the three-way agreement #2007's criterion 3 asks
for on a "historically known" geometry.

Seeded-defect signatures, and why they matter:

| Seeded defect | `klt mom` sees | FastCap sees | vs. the 3% band |
| --- | --- | --- | --- |
| 0.5 µm spacing error | coupling −19.04% | coupling −18.97% | **6×** the band |
| ground plane deleted | `C[a][a]` −38.62% | `C[a][a]` −38.67% | **13×** the band |

That ratio is the point. An oracle whose noise floor is the size of the error
it is meant to catch proves nothing; here the defect signature is an order of
magnitude above the disagreement, and the two solvers size the defect to
within 0.4% of each other (0.37% on the spacing error, 0.13% on the deleted
ground plane).

`test_the_comparison_can_actually_fail` is the negative control on the
comparison itself: `klt mom`'s clean answer against FastCap's *defective*
one differs by **23.45%**, far outside the band. Without it, every agreement
test above would also pass if the exporter silently ignored its input.

### Where the two solvers disagree most

Pre-#2061 this was the flat parallel-plate fixture, disagreeing by **3.29%**
against 0.17% for the coupled lines — two zero-thickness laminae a couple of
panel widths apart being exactly where a point charge is a poor stand-in for
the source-panel integral, and exactly where FastCap's analytic integral is
not. The near-field/far-field split (#2061) removed that gap: the same
fixture now agrees to **0.32%**, and the flat-plate band collapsed from 6%
into the common 3% envelope.

What the fix exposed is worth stating plainly: the old 3.29% was the
*visible* part of a two-error coincidence. The centroid kernel also
overstated near-field coupling by a few percent, which — on top of the
kernel's error — happened to offset the constant-density basis's own
under-resolution of the charge piling up on gap-facing surfaces. Correcting
the kernel therefore made some closed-form comparisons (Kirchhoff's fringing
law, the square-coax line) read a couple of percent *further* from the
textbook even as this oracle's agreement tightened tenfold — see
[`mom-validation.md`](mom-validation.md), where those tolerances are
recalibrated with FastCap co-witness runs on the same fixtures.

Where the two solvers still disagree most is the shielded triple (0.46%,
against 0.03% for the bare coupled lines): the grounded plane 1 µm below the
lines is the largest near-field region in any fixture, and `solver.rs`'s
4-point-per-axis quadrature plus its 3-circumradius near-field threshold
leave it a shade less exact than FastCap's analytic integral. It is a
discretisation-level residual, an order of magnitude inside the band.

This is also the most useful thing this pairing tells `klt mom`'s users:
where the in-repo solver's simplification still costs accuracy, and how much.

### Behaviour under refinement

Agreement at one mesh could be a coincidence, so the same fixture is refined
over four meshes:

| Panel size | Panels | `klt mom` C[a][b] | FastCap C[a][b] | rel.diff |
| --- | --- | --- | --- | --- |
| 2.00 µm | 84 | −1.321503 fF | −1.377758 fF | 4.08% |
| 1.00 µm | 248 | −1.411542 fF | −1.419067 fF | 0.53% |
| 0.50 µm | 816 | −1.448400 fF | −1.447923 fF | 0.033% |
| 0.25 µm | 3264 | −1.479796 fF | −1.478646 fF | 0.078% |

Measured 2026-09-22 with the near-field kernel (#2061). The disagreement is
still **not monotone** — both solvers approach the answer from below at
different rates and cross over between 0.5 µm and 0.25 µm (the thin bars'
sharp-edge charge singularity makes both converge slowly, the same effect
`mom-validation.md` documents against the Richardson oracle). The test
therefore asserts what is actually true and meaningful — both move in the
same direction at every refinement step, the finest mesh agrees far better
than the coarsest, and every mesh at 1.0 µm or finer is inside the band —
rather than a monotonicity that would have required picking the mesh
sequence that produced it.

## Provenance (#2007 criterion 4)

`fastcap_oracle.oracle_provenance` returns one block mirroring
`tests/helpers/magic_oracle.py`'s shape, so the two pairings' records are
directly diffable:

```json
{
  "oracle": {
    "tool": "fastcap",
    "version": "2.0 (18Sep92)",
    "settings": {"expansion_order": 2, "iter_tol": 1e-06,
                 "panel_size_um": 0.5, "background_permittivity": 3.9},
    "input": {"path": ".../klt_mom_oracle.qui",
              "content_hash": "sha256:...", "panel_count": 816}
  },
  "input": {"layout": {"path": "...", "content_hash": "sha256:..."},
            "spec":   {"path": "...", "content_hash": "sha256:..."}},
  "klt": {"schema_version": 2, "klayout_version": "0.30.10",
          "klt_version": "0.5.0",
          "klt_mom_native_source_fingerprint": "533a05c9b421a763",
          "panel_size_um": 0.5, "panel_count": 816,
          "background_permittivity": 3.9}
}
```

Two deliberate differences from the magic pairing's block:

- The Rust solver identifies itself by the **content fingerprint**
  `native/mom/build.rs` embeds (an FNV-1a-64 hash of the crate's own
  sources) — the same identity `ci.yml`'s freshness gate checks. A crate
  version would not distinguish two builds of an unreleased `0.1.0`.
- There is no *shared input-file* hash, because there cannot be one: FastCap
  cannot read GDSII. What is hashed instead is the exported `.qui` plus the
  layout and spec `klt mom` read, and the two panel counts are asserted
  equal — see the shared surface below.

## Shared dependencies — what this pairing does *not* prove

Per #2007's criterion 5, the point is to name the shared surface, not pretend
it is absent.

- **Geometry extraction is not cross-validated.** FastCap cannot read GDSII,
  so its geometry is *exported from* `klt mom`'s own conductor request. A bug
  in `mom.py`'s GDS-layer→box extraction (`_stackup_boxes`) or in the stackup
  spec's interpretation would be handed to both solvers identically and this
  oracle would report perfect agreement. That surface belongs to the
  magic pairing ([`magic-oracle.md`](magic-oracle.md), `klt extract`) and to
  `tests/test_mom.py`/`tests/test_mom_stackup_from_pdk.py`, not here. The
  `run_mom` file-path test bounds it a little — it proves the GDS + spec
  round trip reproduces the in-memory boxes *bit-for-bit* — but it cannot
  validate the mapping itself.
- **The mesh is shared on purpose.** Both solvers panel the same faces at the
  same subdivision, so a discretisation that is systematically wrong *for
  both* (say, a face set that omits a wall) would not show up. This is the
  price of isolating the kernel; the refinement table above is the partial
  answer, since a wrong face set would not converge with panel size.
- **One host, one arithmetic.** Both solvers run on the same machine in IEEE
  double precision. Recorded on both sides as hashes and versions, which is
  what makes a future disagreement attributable rather than ambiguous.
- **The band is empirical.** 3% across every fixture is set from the
  agreement measured here, with headroom — it is not derived from an error
  analysis of either solver. Post-#2061 the flat-plate fixture sits at
  0.32%, so its old dedicated 6% band collapsed into the common envelope.
  It is tight enough that every seeded defect
  above is an order of magnitude outside it, which is the property that
  matters.

## Unsupported / deliberately unmatched

- **Touching or overlapping boxes.** `geometry.rs` de-duplicates coincident
  panels within a conductor and otherwise leaves interior faces in place; a
  `.qui` file has no notion of an interior face. Rather than guess which
  convention would make the two "the same question", `write_qui` rejects
  touching boxes outright. This excludes `docs/cli/mom.md`'s multi-wall
  coax worked example from this oracle (it is covered by the analytic
  square-coax closed form in `tests/test_mom_validation.py`).
- **Multi-dielectric stacks.** Neither side supports them here: `klt mom`'s
  MVP is a single homogeneous medium, and this oracle's list file declares a
  single `C` surface. FastCap itself *can* do piecewise-constant dielectrics
  (`D`/`B` surface types) — that capability becomes relevant when `klt mom`
  grows the same, and is the trigger to revisit Palace as well.
- **Inductance, resistance, full-wave.** `klt mom`'s PEEC
  (`compute_inductance`) and retarded-kernel full-wave paths are different
  solvers in the same crate. FastCap is electrostatic only; the full-wave
  path already has its own oracle (NEC2++,
  [`mom-cross-validation.md`](mom-cross-validation.md)), and PEEC's is
  FastHenry2 (#1886).
- **Non-box geometry.** `klt mom` discretises axis-aligned boxes only, so
  that is all the exporter handles. `.qui`'s triangle (`T`) records are
  unused.
- **Panel counts above `MAX_PANELS`.** `geometry.rs` caps a solve at 8000
  panels; FastCap has no such cap, but a fixture over it cannot be compared
  because one side refuses to run.

## See also

- Tracking issue #2007 — the oracle-validity bar and the rest of the pairings.
- [`mom-validation.md`](mom-validation.md) — the analytic closed-form tier
  this complements, and the source of the parallel-plate anchor.
- [`mom-cross-validation.md`](mom-cross-validation.md) — the same pattern for
  `klt mom`'s full-wave S-parameters against NEC2++.
- [`magic-oracle.md`](magic-oracle.md) — pairing #1, and the provenance-block
  shape this one mirrors.
- [`docs/cli/mom.md`](../cli/mom.md) — the verb under test, its spec-file
  schema, and its stated scope and limitations.
