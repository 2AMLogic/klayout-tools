# `klt em` numeric validation: closed-form oracles, convergence order, `klt mom` cross-check

Brief 1.C of the finite-element epic ([#708](https://github.com/2AMLogic/klayout-tools/issues/708),
Phase 1), delivered for
[#5618](https://github.com/2AMLogic/klayout-tools/issues/5618) on top of the
verb ([#5616](https://github.com/2AMLogic/klayout-tools/issues/5616)) and the
GDS + stackup geometry path ([#5617](https://github.com/2AMLogic/klayout-tools/issues/5617)).

Where [`docs/design/mom-validation.md`](mom-validation.md) records what the
`klt mom` result is *worth*, this document does the same for the `klt em`
quasi-static capacitance slice — with one twist the epic's
reality-grounding discipline forces: geode-fem was originally the candidate
*external reference* and is now the *engine*, so it cannot validate itself.
The oracles here are (a) closed forms and (b) `klt mom` — a genuinely
different method class (boundary/integral, open space) rather than a second
volumetric solve. A Palace comparison is pending upstream in geode-fem and is
**not** part of this epic.

Upstream geode-fem already validates its own electrostatics against idealized
fixtures it generates in-process (coax `rel_err = 1.52e-3`, concentric
spheres `rel_err = 5.14e-3`, `convergence_order = 2.0202`;
`benchmarks/electrostatic/results.toml` at geode 0.9.0). Re-proving those
numbers on upstream's own fixtures would check nothing of ours. Every check
below therefore runs **through `klt`'s own request paths** — the GDS +
stackup layout path of #5617 or the supplied-mesh path of #5616 — on geometry
this repo's pipeline produced, which is exactly the integration surface
upstream's suite has never exercised
([`em-field-sim-spike.md`](em-field-sim-spike.md) § 5's "validation bar
before geode-fem becomes the default engine").

The executable form of everything below is
`tests/test_em_validation.py`; the `klt mom` cross-check numbers are
committed as machine-checkable evidence in
`tests/fixtures/em/cross_validation_evidence.json` (audited by a test that
runs with no engine installed). Live tests need `geode` and Gmsh
(`docs/cli/em.md` "Resolving `geode`"), report as *skipped*, never passed,
when they are absent, and reproduce bit-identically for a fixed
geode/Gmsh pair. Values below were measured on 2026-11-19 with geode 0.9.0
(`4215e958e7fb`) and Gmsh 4.15.2.

## 1. Parallel plate through the stackup path — `C = εr ε0 A / d`

The fixture is [`mom-validation.md`](mom-validation.md) § 1's — two
coincident square plates — realized as a GDS on sky130A conductors: plates on
`met1` (68/20) and `met2` (69/20), labels on their pin layers, everything
else derived from `klt pdk stackup` (never a duplicated table): the facing
gap `d = 0.27 µm` (ild3), `εr = 3.9`, the plates' real z-extents, geode's
grounded box (substrate plane, grounded top at 5.3711 µm, lateral walls one
stack height out), and the default mesh settings.

The ideal formula ignores fringing, so for finite plates it is a **strict
lower bound** whose excess shrinks as `d/L → 0` — both facts asserted. What
the boxed solve changes versus mom's open space is only *how much* of the
fringing survives: the grounded walls absorb part of the flux that open
space would wrap back onto the counterplate. At the stackup's own gaps the
10 × 10 µm of § 1 is nowhere near the closed form's regime:

| plate `L` | `L/d` | measured `−C01` | `εr ε0 A/d` | excess | mesh |
| --------- | ----- | --------------- | ----------- | ------ | ---- |
| 10 µm | 37 | 14.3827 fF | 12.7894 fF | +12.5 % | 9 077 nodes |
| 20 µm | 74 | 54.2913 fF | 51.1575 fF | +6.1 % | 29 756 nodes |
| 40 µm | 148 | 210.9232 fF | 204.6301 fF | +3.1 % | 106 401 nodes |

The excess halves with each doubling of `L` — perimeter-scaled physics
(fringing) plus a perimeter-scaled share of mesh error (the plate-edge
wedge singularity, § 2), exactly the `d/L → 0` behaviour § 1 asserts for
`klt mom`. The **5 % bar of `em-field-sim-spike.md` § 5** is therefore
asserted at `L/d ≈ 150`: `L = 40 µm`, measured +3.08 %.

**Stated tolerance** at the 40 µm operating point: `C01 ∈ [1.00, 1.05] ×
εr ε0 A / d` — one-sided-by-physics like mom-validation.md § 1's own band
(the lower edge is a real bound, not a fudge), the upper edge the spike
doc's 5 % bar with ~40 % headroom over the measurement. (For comparison,
§ 1's `klt mom` band at `L/d = 20` is `[1.00, 1.25]`: at the stackup's own
gap the literal 10 µm fixture sits at `L/d = 37`, where ~10 % of fringing
is *physics*, not error — the test suite asserts the sweep's monotone
shrink instead of pretending otherwise.)

## 2. Convergence under mesh refinement

### 2a. Observed order vs the element's theoretical order — coax

The order gate needs an oracle the discretisation can actually converge to:
on plate edges the field has a wedge singularity (charge density
`~ r^{−1/3}` at a 90 ° corner), which caps the observable order far below
the element's long before asymptotic refinement is affordable. The gate
therefore uses the **exact** closed form `C = 2π ε0 L / ln(b/a)` on the one
geometry with no edges in its field — the coaxial line, upstream's own
oracle 1, at upstream's own dimensions (`a = 1`, `b = 2.5`, `L = 1`, SI).
Meshes are gmsh tetrahedra of the annulus tagged through klt's
supplied-mesh request (end faces deliberately untagged: the natural
condition is the radial field's exact symmetry plane, so the continuum
answer is the 2-D radial field with no end effect). The element's
theoretical order is **2** — P1 tets, capacitance from electrostatic
energy, `O(h²)`; upstream's smooth-fixture study measures 2.0202 on the
same oracle family.

| `h` | `C` | rel. err | observed order |
| --- | --- | -------- | -------------- |
| 0.4 | 6.105147e-11 F | 5.543e-3 | — |
| 0.2 | 6.086924e-11 F | 2.542e-3 | 1.13 |
| 0.1 | 6.076090e-11 F | 7.576e-4 | 1.75 |
| 0.05 | 6.072745e-11 F | 2.066e-4 | 1.87 |

Converging from above toward the exact value, order rising toward 2 (the
logarithmic radial field on linear elements plus curved-boundary
discretisation keep it just under 2 at affordable `h`). **Stated
tolerance**: the gated levels are `h = 0.2 / 0.1 / 0.05` (0.4 is
pre-asymptotic at ~4 elements across the annulus); every consecutive pair's
observed order must lie in `[1.5, 2.4]` — below 1.5 the solve is not
converging at anything like the element's rate, above 2.4 it is not a P1
discretisation at all — errors must shrink strictly, and the finest must be
≤ 1e-3 (measured 2.1e-4).

### 2b. Refinement through the layout path — edge-singularity-limited

The layout path gets its own refinement study because that is the path real
requests take. The document is `prepare_layout`'s own output with only the
two mesh-size fields scaled (met1/met2, 10 µm plates, everything else
identical), solved through the supplied-mesh request:

| refinement | `−C01` | `× oracle` | mesh |
| ---------- | ------ | ---------- | ---- |
| ×1 (default) | 14.3827 fF | 1.1246 | 9 077 nodes |
| ×2 | 14.0365 fF | 1.0975 | 39 373 nodes |
| ×4 | 13.7778 fF | 1.0773 | 230 164 nodes |

Monotone, from above, toward the boxed geometry's own value — the same
direction and the same edge-singularity-limited rate upstream's layout-path
golden observes on its guard-ring fixture (+1.15 % → +0.64 % → +0.22 %,
`geode-cli/tests/mesh_static_golden.rs`, which gates monotone convergence
rather than order for exactly this reason). **Stated tolerance**: the live
gate asserts the from-above direction and the § 1 sanity band on ×1/×2
(the affordable levels); the ×4 rung is recorded here as evidence. The
order *gate* stays on the smooth oracle above, where "consistent with the
element's theoretical order" is a meaningful, falsifiable claim.

## 3. `klt mom` cross-check — the independent method

One shared GDS fixture, two request paths, two methods that share no
numerics: `klt mom` (constant-density BEM, open space, homogeneous εr) and
`klt em` (P1 FEM, geode's grounded box, layered slabs). The fixture is
§ 1's shape on the **li1/met1** pair (gap 0.34 µm, `εr = 3.9` — the
inter-plate dielectric is a single uniform slab, so mom's homogeneous-medium
model is exact here, verified programmatically from the stackup). li1/met1
rather than met1/met2 because mom's panel-width guard needs panels at or
below the gap while staying under the 8000-panel dense-solve cap: at
10 × 10 µm this pair discretises warning-free at 4 896 panels.

| solve | `−C01` | `× oracle` | discretisation |
| ----- | ------ | ---------- | -------------- |
| `klt em` | 10.9212 fF | 1.0753 | 99 048 nodes / 456 154 tets |
| `klt mom` | 11.1625 fF | 1.0991 | 4 896 panels (0.29 µm) |
| oracle `εr ε0 A/d` | 10.1563 fF | 1.0000 | — |

Difference 0.2413 fF = **2.16 % of the mom value**. **Stated tolerance**:
`|em − mom| / mom ≤ 5 %`, ~2.3× headroom. The band is set by the *model*
difference, not by either solver's residual: mom solves the open-space pair
(both plates' fringe flux wraps back onto the pair), em solves inside the
grounded box (walls absorb part of it), and that difference is bounded by
the fringing excess itself — ~10 % of the oracle at `L/d = 29` and shrinking
with `d/L` (§ 1's sweep). Each solve is separately bounded by `[1.00, 1.25] ×
oracle` — mom-validation.md § 1's own band at this aspect ratio: the closed
form is a strict lower bound, and ~10 % of fringing is the physics at
`L/d ≈ 30`.

Both values, the difference and the band are committed as evidence in
`tests/fixtures/em/cross_validation_evidence.json` and audited offline (the
oracle and difference arithmetic recompute from constants, and the fixture's
gap/εr/z must still agree with the curated sky130A stackup — if the stackup
table moves, the audit fails and the evidence must be re-measured).

## 4. Re-running

```sh
# engines: geode (0.9.0 tested) via KLT_GEODE_BINARY or PATH, Gmsh >= 4.11,
# and for the cross-check the mom extension (docs/cli/mom.md):
uv sync --extra dev --group mom
KLT_GEODE_BINARY=/path/to/geode PATH="$PWD/.venv/bin:$PATH" \
  pytest tests/test_em_validation.py -v --capture=tee-sys
```

Values reproduce bit-identically for a fixed geode + Gmsh pair; geode-fem's
own docs note other Gmsh versions mesh slightly differently, which the
stated bands absorb. Without the engines every live test reports as
skipped, and the evidence audit still runs.
