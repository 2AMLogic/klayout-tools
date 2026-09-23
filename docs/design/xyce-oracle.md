# `klt sim` cross-validation against Xyce

Methodology for the Xyce-backed cross-validation oracle for `klt sim` --
issue #2016, pairing #5 of tracking issue #2007 ("independent
cross-validation oracles for klt verdicts"). The oracle itself lives in
`tests/test_sim_xyce_oracle.py`, driven by `tests/helpers/xyce_oracle.py`;
the engine execution path is `src/klayout_tools/sim.py`'s
`engine: "xyce"` branch (`_write_xyce_deck`, `_parse_xyce_measurements`,
`_classify_xyce_diagnostics`).

The house pattern this follows is `docs/design/fastcap-oracle.md`
(FastCap vs `klt mom`): a second, independently implemented solver for the
same job, handed the same inputs, with agreement stated inside a derived
tolerance band and a seeded defect proving the comparison can fail.

## The gap this closes

Every other `klt sim` test -- contract, rollup, corner expansion, waveform
parsing, the whole of `tests/test_sim.py` -- runs its netlists through one
codebase: ngspice, a SPICE 3f5 derivative. If ngspice's device evaluation,
time-step control, or output formatting disagreed with the underlying
circuit theory, nothing in the repo could see it. Xyce is Sandia's
from-scratch SPICE implementation (C++, Trilinos solver stack): no shared
code, no shared history, and a materially different netlist frontend. Two
independently written SPICE implementations agreeing on the same fixtures
is evidence about `klt sim`'s numerics that no amount of ngspice
self-consistency testing can produce -- the same argument the FastCap
pairing makes for `klt mom`.

## Why the official installer and not a source build

#2007 ranked this pairing last because of setup cost, and the spike
confirmed it: an open-source Xyce build requires building **Trilinos**
first (the pinned-package cmake build is a multi-hour job even on the
fleet box class this repo targets, and it is the documented reason the
`eda-sim` docker image cannot simply bake Xyce on its Debian base).
Sandia publishes official, versioned installers per platform instead
(xyce.sandia.gov/downloads/executables/).

The path pinned here is the **macOS arm64 serial installer, XyceNF
7.10.0**, installed by `scripts/install-xyce.sh` (checksum-pinned URL,
fail-closed verification, user-owned prefix, smoke-tested before
declaring success -- the same conventions as `scripts/install-fastcap.sh`).

Honest disclosure about that artifact: it is the **XyceNF** ("NORAD")
distribution, which adds proprietary device models Sandia may distribute
as binaries but not as source. The SPICE frontend, netlist parser, and the
R/C/D/V primitives the cross-validation fixtures use are the same
open-source Xyce 7.10 code either way, so the oracle property -- an
independent implementation -- is unaffected; a caller with strict
open-source-only requirements should build from source (recipe below) and
point `$PATH` at that binary instead.

## Matched inputs (#2007 criterion 1)

Both sides run **the same request document** through the one engine
dispatch seam `klt sim` already gates on (`request.engine`) -- never two
hand-built decks. Matched by construction:

- **Netlist**: one circuit-body file per fixture, `.include`d (Xyce) /
  `.include`d (ngspice) from the engine-generated decks;
  `environment.netlist_sha256` is asserted equal to the fixture hash for
  both engines.
- **Corner / analysis definition**: same `analysis.kind` and arguments,
  same `corners.temperature_c` list, same `.measure` cards verbatim.
- **Check scope**: same `measurements[]` names, and (negative control)
  the same `limits`.

## Evidence the work ran (#2007 criterion 2)

`tests/helpers/xyce_oracle.py::run_sim_engine` refuses any run that did
not demonstrably simulate the fixture: per-corner `status: "error"` is
raised as a test failure, every requested measurement must carry a parsed
value, every corner's response must name the **probed** engine version of
the binary on `$PATH` (a stale install earlier on the path cannot pass),
and the DC fixture additionally requires each engine's waveform artifact
to contain at least the 21 sweep points its analysis card declares. The
CI leg (`.github/workflows/xyce-oracle.yml`) re-checks the skip half:
a dispatch run in which the oracle skipped fails the job.

## Fixtures and measured results (#2007 criterion 3)

Measured 2026-09-22: XyceNF **7.10.0** (macOS arm64 serial installer,
sha256 `06cd438e…` as pinned in `scripts/install-xyce.sh`) vs ngspice
**47** (Homebrew), `klt` at the feature/issue-2016 tree. Relative
difference is `|a-b| / max(|a|, |b|)`, the same symmetric convention as
the FastCap pairing.

| Fixture | Measurement | ngspice 47 | Xyce 7.10.0 | rel. diff |
|---|---|---:|---:|---:|
| DC divider sweep (0–5 V, 21 pts) | `vout_mid` (AT=2.5 V) | 0.5 | 0.5 | 0 |
| | `vout_upper` (AT=4.0 V) | 0.8 | 0.8 | 0 |
| RC transient (pulse, 5 ps step) | `tphl` (TRIG/TARG) | 1.391500e-09 | 1.391548e-09 | 3.4e-05 |
| | `vmax` (MAX) | 8.820120e-01 | 8.820727e-01 | 6.9e-05 |
| | `vout_at_5n` (FIND AT=5 ns) | 7.465010e-01 | 7.462823e-01 | 2.9e-04 |
| Diode, temperature corners 27 °C / 85 °C | `vdiode` @ 27 °C | 6.551200e-01 | 6.550998e-01 | 3.1e-05 |
| | `vdiode` @ 85 °C | 5.508630e-01 | 5.508392e-01 | 4.3e-05 |

Tolerance derivation: the band is **3e-3** relative. The worst measured
difference (2.9e-4, the transient FIND interpolating a single timepoint
on a fast exponential edge -- where independent time-step placement drifts
most) is ~10x inside it; a different host's arithmetic and small version
drift fit comfortably. The seeded defect moves ~88% -- ~290x the band --
so the band cannot hide a real device-parameter regression.

Negative control (`test_the_comparison_can_actually_fail`, the house
pattern): R1 4k→40k in the divider. Both engines must move `vout_mid`
outside 100x the band **and** flip the same `limits` verdict from `pass`
to `fail` -- measured: clean `pass` at 0.5 V, defective `fail` at
6.098e-02 V on both engines.

## Provenance (#2007 criterion 4)

The response's `environment` block already carries everything the shared
provenance bar asks for, per engine: `engine`, `engine_version` (asserted
equal to the probed binary's own `-v`/`--version` report),
`netlist_sha256`, and `models_lib_sha256` when a process corner ran. The
oracle test pins all of these. The install recipe itself is the other
half of provenance: `scripts/install-xyce.sh` pins URL + sha256, and
records the version in its marker file.

## Where the two engines disagree by design (criterion 5)

SPICE syntax each engine parses differently -- the reason `_write_xyce_deck`
is not a transcription of `_write_corner_deck` (all verified against
XyceNF 7.10.0):

- **No `.control` block, no `alter`**: Xyce is command-line driven; the
  analysis card is a plain top-level dot card. Consequence: per-corner
  supply patching (`corners.supply_v`) is impossible without rewriting the
  netlist body, so `run_sim` refuses that axis for this engine with a
  clear error. Fixed-supply netlists (the fixtures) are unaffected.
- **`.temp` is silently ignored** -- the nastiest divergence, because it
  would silently produce 27 °C results for a hot corner. Xyce's equivalent
  is `.options device temp=<T>`, which `_write_xyce_deck` emits instead;
  verified to move device physics (diode drop −104 mV from 27 °C to 85 °C)
  where `.temp` provably did not.
- **Library sections must close with a named `.endl <section>`** when the
  file is included via `.lib <file> <section>`; a bare `.endl` is a parse
  error for Xyce where ngspice accepts it. Real PDK decks that close
  sections bare will need that one-word edit before an `engine: "xyce"`
  process-corner run.
- **`.meas` output shape**: Xyce prints `NAME = value` with
  analysis-specific trailers (`at time = ...`, `for AT = ...`,
  `with targ = ... and trig = ...`) and upper-cases the measurement name;
  results land in a `***** Measure Functions *****` log section plus
  per-analysis `<deck>.mt0`/`.ms0` files. `_parse_xyce_measurements` parses
  the log section, lower-cases names, and treats `NAME = FAILED ...` the
  way the ngspice path treats `... failed!`.
- **Rawfiles**: Xyce's `-r <file> -a` produces an ASCII rawfile that is
  ngspice-*like* (same `Title:/Plotname:/Flags:/No. Variables:` header
  grammar) but not identical; `parse_ascii_rawfile` reads it unmodified
  for the waveform artifact.
- **Divergences accepted as out of scope for `engine: "xyce"` v1**: Monte
  Carlo sampling (no `.options seed=` wiring), the fail-fast calibration
  probe (reads ngspice's rawfile stream), and the `remote`/`batch`
  backends. Each is refused up front with a `SimError` naming what *is*
  supported.

## Shared dependencies -- what this pairing does *not* prove

Both engines read the same netlist text and the same `.measure` requests
that `klt sim` parses, so a bug in `klt sim`'s own request parsing
(measurement card mangling, corner expansion) would feed both engines the
same wrong input and pass here. That layer is covered from the other side:
`tests/test_sim.py` pins the request/deck contract, and the analytic
anchor on the divider fixture (the divider law) catches a shared
systematic error on the fixture itself. What this pairing proves is the
numerics *behind* the frontend: device evaluation, matrix solve, analysis
sweep, and measurement extraction, as independently implemented as they
come in the SPICE world.

## From-source build recipe (Linux / non-arm64-macOS)

For the fleet box or any host the installer does not cover. Recorded from
the Xyce 7.10 building guide; **not yet machine-verified by this repo**
(the fleet box was unreachable while this pairing was built -- the
installer path above is what is verified). Xyce 7.10 requires Trilinos
with at least Teuchos, Epetra, Tpetra, Kokkos, Sacado, Triutils, Amesos,
AztecOO/NOX, Ifpack and Isorropia enabled (serial build: no MPI, no
PyTrilinos):

1. Trilinos 16.0.0 (release tag `trilinos-release-16-0-0`):
   `cmake -DCMAKE_BUILD_TYPE=Release -DTrilinos_ENABLE_Teuchos=ON …`
   with the package list above, then `make -j$(nproc) install` into a
   dedicated prefix (expect 30-90 min).
2. Xyce `Release-7.10.0`:
   `cmake -DCMAKE_BUILD_TYPE=Release
   -DTrilinos_DIR=<trilinos-prefix>/lib/cmake/Trilinos
   -DCMAKE_INSTALL_PREFIX=<xyce-prefix> <src>/Xyce` then
   `make -j$(nproc) install` (expect 10-30 min).
3. `export PATH=<xyce-prefix>/bin:$PATH` -- the oracle's gate is exactly
   `shutil.which("Xyce")`.

If this pairing is ever promoted from dispatch-only CI to per-PR
gating, the box that runs it should be pinned and recorded here the way
the magic-oracle's `MAGIC_VERSION` is.

## See also

- Issue #2016 (this pairing), #2007 (oracle tracking, validity bar)
- `docs/design/fastcap-oracle.md` -- the same methodology for `klt mom`
- `docs/design/magic-oracle.md` -- the same methodology for
  `klt drc`/`extract`/`lvs`
- `docs/cli/sim.md` -- the `engine` field contract and the `xyce`
  support boundary
