# `klt erc` runtime profile on a routed dense sky130 layout

Issue [#2229](https://github.com/2AMLogic/klayout-tools/issues/2229),
follow-up to [#2219](https://github.com/2AMLogic/klayout-tools/issues/2219)
/ PR #2228 (`--findings-only`). This note records where `run_erc`'s time
goes on real routed sky130 layouts. The aim is that the next `klt erc`
performance issue targets the part that actually costs time.

## Verdict

**Neither hypothesis from #2229 holds as posed. The answer depends on
whether the spec declares `stackup[0].active_layer` (#1979).**

- **With `active_layer` (the regime that matches #2219's ~94 ms/gate
  net):** the per-gate walk is 98.7% of the runtime, but the per-role
  accumulation (`_accumulated_levels`) is only 0.2%. Primary extraction is
  0.7% and tie extraction is 0.5%. Almost the entire walk is one statement
  per candidate net, `(gate_poly_region & active_region).merged()`, which
  intersects each net with the flat whole-layout active region. Its cost
  scales with *candidate nets x active polygons*, so it grows
  quadratically with design size. `--findings-only` does not touch it: the
  measured end-to-end time was 3,934 s against 3,940 s for the full run.
  Filed as [#2751](https://github.com/2AMLogic/klayout-tools/issues/2751).
- **Without `active_layer`:** the run is **extraction-dominated**. The
  primary `_extract_connectivity` is 72-75% of CPU time on its own. With
  `ties[]` declared, primary plus tie-graph extraction is 84-89%. The
  complete walk is 6-20%, and `_accumulated_levels` inside it is 8-16%.
  Here #2219's reasoning holds (accumulation is most of the walk), but the
  walk is not most of the run.

The "extraction vs. accumulation" question in #2229 therefore has a third
answer: on a layout of #2219's scale, the dominant cost is the
`active_layer` gate-area intersection in the walk prologue, which is
neither of the two named costs.

## Fixtures

All fixtures are real routed layouts produced by this repo's own
`klt synthesize` -> `klt place-and-route` pipeline. They are not
replicated synthetic geometry. The RTL is the vendored
`2AMLogic/sky130-modexp` source
(`tests/corpus/sky130_modexp_canary/sources/modexp.v`, Apache-2.0,
upstream commit `1b38ab4f`). The only change is its `WIDTH` parameter
default. Floorplan, I/O, clock, and seed are the modexp canary's own
request shape: 35% utilization target, aspect 1, 4 µm core margin,
`unithd`, I/O on met3/met2, `clk` at 10 ns, seed 42, `target_stage:
"route"`. All but `WIDTH=255` also apply the shipped `sky130hd` PDN
preset (tapcells, met1 rails, met4/met5 straps, fillers).

| fixture | `WIDTH` | synth instances | PDN | die area | util. | route DRC | top-cell insts | GDS sha256 (prefix) | GDS size |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `w16` | 16 | 717 | `sky130hd` | 22,781 µm² | 38.2% | 0 | 9,130 | `98e63a58` | 1.4 MB |
| `w128` | 128 | 5,622 | `sky130hd` | 159,996 µm² | 41.7% | 0 | 80,359 | `4998dd1a` | 9.3 MB |
| `w255np` | 255 | 11,134 | none | 311,074 µm² | 41.2% | 0 | 135,826 | `cb325748` | 15.3 MB |

`WIDTH=255` with the PDN preset failed in detailed routing
(`[ERROR DRT-0085] Valid access pattern combination not found`), so the
largest fixture was routed without a PDN. 255 is the cap because modexp's
counters are 8 bits wide.

**Net and gate counts (re-counted here; do not use the historical
1,221-net figure).** These are the counts of `klt erc`'s own primary
extraction (`circuit.each_net()` with `cluster_id != 0`, i.e. the
candidates the walk visits) and of `gates[]`:

| fixture | extracted nets (candidates) | gate nets, no `active_layer` | gate nets, `active_layer` |
| --- | --- | --- | --- |
| `w16` | 2,470 | 1,858 | 1,856 |
| `w128` | **20,053** | **15,200** | **15,198** |
| `w255np` | 40,443 | 30,627 | (not run end to end; see below) |

`w128` is the closest match to #2219's "16,640 gate nets". The
`tests/corpus/sky130_modexp_canary/results.json` figure of 1,221 nets is
a `klt extract` net count of the `WIDTH=16` canary. That is a different
extractor and a different counting rule, and the canary vendors no GDS,
so the figure could not be re-counted directly. The regenerated
`WIDTH=16` layout above counts 2,470 `klt erc` nets.

**Not vendored.** The routed GDS files (1.4-15 MB) are kept out of git.
They are reproducible from the committed generator, which was checked by
regenerating `w16` from scratch with
`scripts/research/regenerate_erc_profile_fixture.py`. The output GDS was
byte-identical (sha256 `98e63a58...`) to the first generation, so the flow
is deterministic at that scale. `w128` and `w255np` were produced by an
out-of-tree precursor of that script that issues identical
`synth_request.json` / `par_request.json` payloads and the same Docker
invocation. They were not regenerated a second time.

### Fixture-generation commands

From the repo root, with `uv sync --extra dev`, host `yosys`, Docker with
`openroad/orfs:latest`, and volare `sky130A` under `~/.volare`:

```bash
python scripts/research/regenerate_erc_profile_fixture.py 16  /tmp/erc2229/w16
python scripts/research/regenerate_erc_profile_fixture.py 128 /tmp/erc2229/w128
python scripts/research/regenerate_erc_profile_fixture.py 255 /tmp/erc2229/w255np --no-power
# routed GDS: <OUTDIR>/.klt/place-and-route/modexp.gds
```

Measured generation times: `w16` synth 33 s + P&R 94 s; `w128` synth
73 s + P&R 310 s; `w255np` synth 29 s + P&R 1,022 s.

## Specs (PDK / stackup)

The four specs are committed under
[`scripts/research/erc_profile_specs/`](../../scripts/research/erc_profile_specs/).
All use the real sky130 GDS stackup: `poly` 66/20 (`role: gate`), `li1`
67/20, `met1`-`met5` 68-72/20 with `*/5` label layers, and vias `licon1`
66/44, `mcon` 67/44, `via1`-`via4` 68-71/44. That is **7 stackup roles**,
against #2219's 4.

| spec | `active_layer` | `nets[]` | `ties[]` |
| --- | --- | --- | --- |
| `sky130_stack_noactive.json` | — | — | — |
| `sky130_stack.json` | 65/20 (`diff`) | — | — |
| `sky130_stack_nets_ties_noactive.json` | — | `VDD`, `VSS` (supply) | `nwell_tap` (well 64/20, tap 65/44, requires nsdm 93/44, `connect_to: li1`, net `VDD`) |
| `sky130_stack_nets_ties.json` | 65/20 | same | same |

Full-mode runs pass `--pdk sky130`. `--findings-only` runs omit it,
because the two flags are mutually exclusive.

## Method

`scripts/research/profile_erc.py` calls `klayout_tools.erc.run_erc`
in-process. For the duration of one call it swaps pass-through timing
wrappers onto module attributes of `klayout_tools.erc`, then restores
them. **`src/` is unchanged**, and on every run marked "identical" below,
`--check-identical` re-ran `run_erc` without wrappers and confirmed
`gates`, `erc_findings`, `status`, and `erc_status` were equal.

Top-level totals are **non-overlapping** and partition the end-to-end
`run_erc` wall time:

- **primary extraction**: the first `_extract_connectivity` call in
  `run_erc`, before `for net in candidates`;
- **candidate walk**: from the return of that call until
  `_floating_gate_findings` is entered. This covers the `active_layer`
  region build, the candidate sort, and the whole `for net in candidates`
  loop;
- **tie extraction**: the second `_extract_connectivity` call, made only
  when `ties[]` is non-empty;
- **remainder**: end to end minus the three above (spec validation,
  `load_layout`, the finding rules, coverage, and provenance).

`_accumulated_levels` (full mode) and `_gate_is_floating`
(`--findings-only`) are **nested** inside the candidate walk. They are
listed as sub-costs and are never added to the top-level totals.

Each figure is shown as wall clock and process CPU time. The host (Apple
M3 Ultra, 28 cores, 96 GiB, macOS 27.0) was shared with other agent
workloads during the runs, so wall time is inflated by contention. Where
the two disagree, **the CPU shares are the more trustworthy split**. Each
run was a single repetition (`--repeat 1`).

`cProfile` was not used for the walk's internals. It cannot see the
walk's `Region` boolean operator, because `&` is a C-level number slot
and raises no profiler event (verified: a loop of `(a & b).merged()`
reports only `merged` and `area`, with the `&` time charged to the
caller's `tottime`). Instead, `--attribute-walk` re-runs the primary
extraction through `erc`'s own helpers and times each statement of the
walk's per-net prologue separately. This replica is used for measurement
only and is never compared with or fed back into a report.

Tool versions: `klt 0.6.0+g7e047a5df68f` (worktree at `7e047a5d`;
`src/klayout_tools/erc.py` is unchanged from `origin/main`
`b22ae156`; the `.dirty` suffix comes only from the untracked
research scripts), KLayout Python 0.30.12, CPython 3.14.5 (arm64), Yosys
0.69+post (`143eb14f`), OpenROAD from `openroad/orfs:latest`
(`sha256:b9c6f350...`, created 2026-10-03), and volare sky130A
`c6d73a35`. Measured 2026-10-04.

## Results

### 1. `w128` with `active_layer` (`sky130_stack_nets_ties.json`, `ties[]` declared)

Commands:

```bash
python scripts/research/profile_erc.py /tmp/erc2229/w128/.klt/place-and-route/modexp.gds \
    scripts/research/erc_profile_specs/sky130_stack_nets_ties.json --pdk sky130
python scripts/research/profile_erc.py /tmp/erc2229/w128/.klt/place-and-route/modexp.gds \
    scripts/research/erc_profile_specs/sky130_stack_nets_ties.json --findings-only
```

The two runs ran concurrently, which is why wall time is about 1.8x CPU
time.

| phase | full: wall | full: CPU | `--findings-only`: wall | `--findings-only`: CPU |
| --- | --- | --- | --- | --- |
| primary extraction | 27.1 s (0.7%) | 17.4 s (0.8%) | 27.3 s (0.7%) | 17.4 s (0.8%) |
| **candidate walk** | **3,890.0 s (98.7%)** | **2,156.4 s (98.3%)** | **3,882.9 s (98.7%)** | **2,149.9 s (98.3%)** |
| ↳ nested `_accumulated_levels` (15,198 calls) | 7.7 s (0.2%) | 4.1 s (0.2%) | — | — |
| ↳ nested `_gate_is_floating` (15,198 calls) | — | — | 3.2 s (0.1%) | 1.7 s (0.1%) |
| tie extraction | 20.8 s (0.5%) | 18.6 s (0.8%) | 21.1 s (0.5%) | 18.8 s (0.9%) |
| remainder | 2.5 s (0.1%) | 1.8 s (0.1%) | 2.5 s (0.1%) | 2.0 s (0.1%) |
| **end to end** | **3,940.4 s** | **2,194.2 s** | **3,933.7 s** | **2,188.1 s** |
| per gate net (15,198) | 259 ms | 144 ms | 259 ms | 144 ms |

After the nested accumulation, about 3,882 s of the walk remains. It is
spent in the per-net prologue: `polygons_of_net(net, gate).merged()`,
then `& active_region` with `.merged()`, then `.area()`. The statement
attribution (`--attribute-walk`, wall clock)
assigns it to the intersection:

| fixture | candidates timed | active polygons | `polygons_of_net(gate).merged()` | `(gate & active).merged()` | `.area()` |
| --- | --- | --- | --- | --- | --- |
| `w16` | 2,470 (all) | 2,519 | 0.062 ms | **15.4 ms** | 0.011 ms |
| `w128` | 500 (first by `cluster_id`) | 20,239 | 0.097 ms | **167.9 ms** | 0.020 ms |
| `w255np` | 300 (first by `cluster_id`) | 41,235 | 0.140 ms | **418.7 ms** | 0.028 ms |

The per-net intersection cost grows roughly linearly with the active
polygon count (about 6-10 µs per active polygon per net). The total walk
therefore grows as *nets x active polygons*. At 167.9 ms x 20,053
candidates, the `w128` sample extrapolates to about 3,370 s, the same
order as the measured walk of 2,156 s CPU / 3,890 s wall. `w255np` was
not run end to end with `active_layer`; by the same arithmetic it would
take about 40,443 x 0.42 s, roughly 4.7 h.

Attribution commands (`--attribute-walk-limit` bounds the replica;
`w16` timed all candidates):

```bash
python scripts/research/profile_erc.py /tmp/erc2229/w16/.klt/place-and-route/modexp.gds \
    scripts/research/erc_profile_specs/sky130_stack_nets_ties.json --attribute-walk
python scripts/research/profile_erc.py /tmp/erc2229/w128/.klt/place-and-route/modexp.gds \
    scripts/research/erc_profile_specs/sky130_stack_nets_ties.json \
    --attribute-walk --attribute-walk-limit 500
python scripts/research/profile_erc.py /tmp/erc2229/w255np/.klt/place-and-route/modexp.gds \
    scripts/research/erc_profile_specs/sky130_stack_nets_ties.json \
    --attribute-walk --attribute-walk-limit 300
```

### 2. Without `active_layer`

Commands (`SPEC` is `sky130_stack_noactive.json` or
`sky130_stack_nets_ties_noactive.json` under
`scripts/research/erc_profile_specs/`; `GDS` is the fixture's routed
`modexp.gds`):

```bash
python scripts/research/profile_erc.py GDS SPEC --pdk sky130 --check-identical [--cprofile OUT.pstats]
python scripts/research/profile_erc.py GDS SPEC --findings-only --check-identical
```

CPU seconds, with percent of end-to-end CPU in parentheses. Wall clock
follows on the end-to-end row.

| fixture / spec / mode | primary extraction | candidate walk | ↳ nested accumulation | tie extraction | remainder | end to end (CPU / wall) | identical |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `w128` / no ties / full | 20.2 s (74.2%) | 5.3 s (19.6%) | 4.2 s (15.5%) | — | 1.7 s (6.2%) | 27.2 s / 43.4 s | yes |
| `w128` / ties / full | 21.1 s (42.8%) | 5.5 s (11.1%) | 4.3 s (8.8%) | 21.0 s (42.6%) | 1.7 s (3.5%) | 49.3 s / 111.5 s | yes |
| `w128` / ties / `--findings-only` | 18.7 s (43.1%) | 2.7 s (6.1%) | 1.6 s (3.6%)¹ | 20.0 s (46.0%) | 2.1 s (4.8%) | 43.5 s / 60.4 s | yes |
| `w255np` / no ties / full | 27.2 s (72.1%) | 7.7 s (20.4%) | 5.7 s (15.2%) | — | 2.8 s (7.5%) | 37.7 s / 45.1 s | not run |
| `w255np` / ties / full | 31.5 s (40.6%) | 8.5 s (11.0%) | 6.3 s (8.1%) | 33.9 s (43.7%) | 3.7 s (4.7%) | 77.7 s / 94.3 s | not run |

¹ `_gate_is_floating`, the `--findings-only` replacement for the
accumulation.

At `w128` without `active_layer`, a full run is about 1.8 ms CPU per gate
net (27.2 s / 15,200). That is roughly 50x cheaper than #2219's measured
~94 ms. The `w255np` no-ties end-to-end time is close to `w128`'s even
though `w255np` has twice the nets. `w255np` has no PDN, and the PDN's
large supply nets are part of what extraction pays for on `w128`.

`cProfile` of the `w128` / no ties / full run (`tottime`, profiler
overhead included; 31.6 s total) agrees with that split. The top two
functions are both extraction: `LayoutToNetlist.register` (19 calls,
12.6 s) and `LayoutToNetlist.extract_netlist` (1 call, 11.3 s). Next come
`Layout.read` (2.8 s) and `Region.merged` (111,253 calls, 1.5 s, mostly
the accumulation).

An unprofiled CLI run of the same configuration took 54.7 s wall / 26.6 s
user (`/usr/bin/time -p klt erc GDS sky130_stack_noactive.json --pdk
sky130 --format json`). Its user time matches the profiled run's 27.2 s
CPU, so the wrappers add no material overhead.

## Comparison with #2219 and with #2229's synthetic table

- #2219 measured ~26 min for 16,640 gate nets, about 94 ms per gate net.
  The only regime measured here that comes within an order of magnitude
  of that is `active_layer` declared: 144 ms CPU per gate net at `w128`
  (15,198 gate nets, 20,239 active polygons, 7 roles). The no-`active_layer`
  regime is about 2-3 ms per gate net, roughly 30-50x too cheap to explain
  #2219. This is an **inference**: #2219's layout and spec are not
  available here. However, `active_layer` is the documented fix for the
  #1979 tie-cell false positives, so a real signoff spec at that scale is
  likely to declare it. If it did, #2219's runtime was this intersection
  and not the accumulation `--findings-only` removes.
- #2229's synthetic 5,000-gate fixtures ran at about 0.1 ms per gate net,
  with extraction at about 75%. That matches the no-`active_layer` rows
  here (primary extraction 72-75% without ties), so those fixtures were
  representative of that regime. They could not reproduce #2219 because
  they had no `active_layer` and no whole-chip active region.

## Acceptance-criteria status

- **Profile end to end on a real dense layout (tens of thousands of gate
  nets, real PDK stackup):** met. `w128` is a routed sky130 layout with
  20,053 nets and 15,198 gate nets, profiled end to end in both modes with
  and without `active_layer`. `w255np` adds 40,443 nets / 30,627 gate nets
  for the no-`active_layer` regime. Partial: `w255np` has no PDN, and its
  `active_layer` cost is a 300-candidate sample, not an end-to-end run.
- **Record the split as absolute time and percent of end to end:** met
  (tables above). Wall and CPU are both given, and CPU is the reliable
  column on this shared host.
- **New `docs/design/` note with fixture, counts, version/commit, and
  profiler output:** met (this file). Raw profiler JSON was not vendored;
  the tables carry its numbers, and the commands above regenerate it.
- **State which hypothesis the profile supports:** see Verdict. Without
  `active_layer` the run is extraction-dominated; with it, neither named
  cost dominates.
- **No source behavior change; file any low-risk win separately:** met.
  `src/` is untouched, and the intersection cost is filed as #2751.
