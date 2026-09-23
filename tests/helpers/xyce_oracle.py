"""Drive Sandia's **Xyce** as an independent cross-validation oracle for `klt
sim`'s ngspice results (issue #2016, pairing #5 of tracking issue #2007,
"independent cross-validation oracles for klt verdicts").

Every other part of `tests/test_sim.py` runs the same netlists through one
engine: ngspice. ngspice is a SPICE 3f5 derivative, and every numerical
answer `klt sim` has ever produced came from that one codebase. Xyce is
Sandia's from-scratch SPICE implementation (C++, Trilinos-based solvers, no
shared code and no shared history with Berkeley SPICE), which makes it a
*genuinely independent* oracle: agreement between the two is evidence about
`klt sim`'s numerics that no amount of ngspice self-consistency testing can
produce -- the same argument `docs/design/fastcap-oracle.md` makes for
FastCap vs `klt mom`, and `docs/design/magic-oracle.md` for magic vs
`klt drc`/`extract`/`lvs`.

`docs/design/xyce-oracle.md` is the methodology document: what is matched
(netlist, corner, analysis, check scope), what the two engines *do not*
share, the measured agreement, the tolerance derivations, and the SPICE
syntax each engine parses differently (issue #2016's acceptance criterion
5). Read that doc before adding or relaxing an assertion in the oracle test
module.

## What this module is (and is not)

A **test-only** helper: nothing in `src/klayout_tools/` imports it, and
`klt` never shells out to Xyce at runtime except through the
`engine: "xyce"` path the caller explicitly asked for -- the same "oracle,
not runtime" call `tests/helpers/fastcap_oracle.py` records for FastCap and
`tests/helpers/magic_oracle.py` records for magic. Xyce is headless by
construction here: the `Xyce` batch binary writes files and exits, no GUI,
per this repo's headless-always rule.

## Requirements, and how a run is gated

Two things must resolve, and :func:`oracle_skip_reason` reports which is
missing, so a test module can skip cleanly (the real-binary gate
`tests/test_mom_capacitance_oracle.py` established -- absence must never
fail CI):

- an `Xyce` binary on `$PATH` (`scripts/install-xyce.sh` installs the
  pinned official build into `~/.cache/xyce-7.10.0/`; add its `bin/` to
  `$PATH`);
- an `ngspice` binary on `$PATH` -- the reference side of the pairing.

## Evidence that both engines actually ran

Per #2007's acceptance criterion 2, an exit code proves nothing (ngspice
reliably exits 0 on failed `.meas` lines; Xyce can too on soft failures),
so :func:`run_sim_engine` refuses any run that did not demonstrably
simulate *this* request:

- every corner in the response carries the *probed* engine version of the
  binary that was asked to run (a stale `Xyce` earlier on `$PATH`, or a
  response produced by some other engine, cannot pass);
- every requested measurement has a parsed value (an engine dropping a
  `.measure` surfaces as `status: "error"`, which fails the assertion --
  there is no "skip the missing one" path);
- when a waveform is requested, the parsed rawfile artifact contains at
  least the number of sweep points the analysis declares (the DC fixture's
  21-point sweep really swept; a `.tran` that silently no-ops cannot
  produce them).
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

from klayout_tools._provenance import sha256_file

#: Both binaries are PATH-discovered, like `fastcap` in
#: tests/helpers/fastcap_oracle.py. `scripts/install-xyce.sh` installs Xyce
#: into a versioned prefix under ~/.cache/ and prints the PATH line to add.
XYCE_BINARY: str | None = shutil.which("Xyce")
NGSPICE_BINARY: str | None = shutil.which("ngspice")


class XyceOracleError(RuntimeError):
    """A cross-validation run did not demonstrably execute, or produced no
    usable per-corner report."""


def xyce_version() -> str | None:
    """Xyce's own reported version via its ``-v`` flag (e.g.
    ``"XyceNF Release 7.10.0"``), or ``None``. Never raises."""
    if XYCE_BINARY is None:
        return None
    try:
        completed = subprocess.run(
            [XYCE_BINARY, "-v"],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired):  # pragma: no cover - host-dependent
        return None
    match = re.search(r"Xyce(?:NF)? Release ([\w.]+)", completed.stdout or "")
    return match.group(1) if match else None


def ngspice_version() -> str | None:
    """ngspice's own reported version from its ``--version`` banner (e.g.
    ``"47"``), or ``None``. Never raises."""
    if NGSPICE_BINARY is None:
        return None
    try:
        completed = subprocess.run(
            [NGSPICE_BINARY, "--version"],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired):  # pragma: no cover - host-dependent
        return None
    match = re.search(r"ngspice-([\w.]+)", completed.stdout or "")
    return match.group(1) if match else None


def oracle_skip_reason() -> str | None:
    """A human-readable reason this oracle cannot run, or ``None`` when it
    can. Used at module level by `tests/test_sim_xyce_oracle.py`
    (``pytest.skip(..., allow_module_level=True)``). Absence must skip,
    never fail.
    """
    if XYCE_BINARY is None and NGSPICE_BINARY is None:
        return (
            "neither Xyce nor ngspice is installed on this machine -- "
            "install Xyce with scripts/install-xyce.sh (add its bin/ to "
            "$PATH) and ngspice with your package manager (see "
            "docs/design/xyce-oracle.md)"
        )
    if XYCE_BINARY is None:
        return (
            "Xyce is not installed on this machine -- run "
            "scripts/install-xyce.sh and add its bin/ to $PATH (see "
            "docs/design/xyce-oracle.md)"
        )
    if NGSPICE_BINARY is None:
        return (
            "ngspice is not installed on this machine -- install it with "
            "your package manager (the reference side of this pairing; see "
            "docs/design/xyce-oracle.md)"
        )
    return None


def write_fixture(
    work_dir: Path,
    body: str,
    request: dict[str, Any],
    *,
    body_name: str = "body.spice",
) -> Path:
    """Write one fixture's circuit body + request into ``work_dir`` and
    return the request path. The body carries no ``.control``/``.end`` cards
    -- the same "circuit body" convention `klt sim` documents (the engines
    wrap it in their own deck shapes)."""
    (work_dir / body_name).write_text(body, encoding="utf-8")
    request_path = work_dir / "request.json"
    request_path.write_text(json.dumps(request), encoding="utf-8")
    return request_path


def _assert_corner_ran(corner: dict[str, Any], engine: str) -> None:
    """One corner's "this actually ran" evidence: the engine did not report
    an error, and every requested measurement carries a parsed value (an
    engine dropping a `.measure` surfaces as ``status: "error"``, which
    fails here -- there is no "skip the missing one" path)."""
    if corner["status"] == "error":
        raise XyceOracleError(
            f"{engine}: corner {corner['corner_id']!r} errored: {corner['diagnostics']}"
        )
    for measurement in corner["measurements"]:
        if measurement["value"] is None:
            raise XyceOracleError(
                f"{engine}: measurement {measurement['name']!r} in corner "
                f"{corner['corner_id']!r} produced no value: "
                f"{corner['diagnostics']}"
            )


def _assert_waveform_swept(
    corner: dict[str, Any], min_waveform_points: int, engine: str
) -> None:
    """The corner's waveform artifact must exist and carry at least
    ``min_waveform_points`` timepoints -- the "the analysis actually
    swept" check (a `.tran` that silently no-ops cannot produce them)."""
    wave_path = corner["artifacts"].get("waveform")
    if wave_path is None:
        raise XyceOracleError(
            f"{engine}: corner {corner['corner_id']!r} produced no "
            "waveform artifact (options.waveforms off?)"
        )
    with open(wave_path, encoding="utf-8") as handle:
        wave = json.load(handle)
    if len(wave["points"]) < min_waveform_points:
        raise XyceOracleError(
            f"{engine}: corner {corner['corner_id']!r} waveform has "
            f"{len(wave['points'])} points, expected >= "
            f"{min_waveform_points} -- the analysis did not sweep"
        )


def run_sim_engine(
    request_path: Path,
    engine: str,
    *,
    expected_version: str | None = None,
    min_waveform_points: int | None = None,
) -> dict[str, Any]:
    """Run one request through ``klayout_tools.sim.run_sim`` and require
    evidence it actually executed (see this module's docstring).

    ``expected_version`` (the value :func:`xyce_version`/:func:`ngspice_version`
    probed from the binary on ``$PATH``) must appear as the response's
    ``environment.engine_version`` -- proof the report came from *this*
    binary, not some other engine or a stale install earlier on the path.

    ``min_waveform_points``, when given, requires every corner's waveform
    artifact (``options.waveforms`` must then be on in the request) to
    contain at least that many timepoints -- the "the analysis actually
    swept" check.
    """
    # Imported here so the module (and its skip gate) still loads on a
    # machine without the klayout-tools install it belongs to.
    from klayout_tools import sim as klt_sim

    report = klt_sim.run_sim(str(request_path))
    corners = report["corners"]
    if not corners:
        raise XyceOracleError(f"{engine}: no corners in the response -- nothing ran")
    for corner in corners:
        _assert_corner_ran(corner, engine)
        if min_waveform_points is not None:
            _assert_waveform_swept(corner, min_waveform_points, engine)
    resolved_version = expected_version
    if resolved_version is not None:
        reported = report["environment"]["engine_version"]
        if reported != resolved_version:
            raise XyceOracleError(
                f"response names engine version {reported!r} but the "
                f"{engine!r} binary on $PATH reports {resolved_version!r} "
                "-- the report did not come from the binary under test"
            )
    return report


def relative_difference(a: float, b: float) -> float:
    """Symmetric relative difference of two measurement values -- the same
    denominator convention `tests/test_mom_capacitance_oracle.py` uses
    (max of the magnitudes), so the comparison has no preferred side and
    stays finite at ``a == b == 0``."""
    scale = max(abs(a), abs(b))
    if scale == 0.0:
        return 0.0
    return abs(a - b) / scale


def measurement_value(report: dict[str, Any], name: str) -> float:
    """The single value ``name`` measured in ``report``'s single corner.
    Every oracle fixture runs exactly one measurement extraction per
    request-per-corner here; a differently-named value is a test bug, not a
    mismatch."""
    corners = report["corners"]
    if len(corners) != 1:
        raise XyceOracleError(
            f"expected exactly one corner, got {len(corners)} -- use "
            "measurement_values() for multi-corner fixtures"
        )
    return measurement_value_in_corner(report, 0, name)


def measurement_value_in_corner(report: dict[str, Any], index: int, name: str) -> float:
    """The value ``name`` measured in ``report``'s ``index``-th corner."""
    for measurement in report["corners"][index]["measurements"]:
        if measurement["name"] == name:
            value = measurement["value"]
            assert value is not None  # run_sim_engine already enforced this
            return float(value)
    raise XyceOracleError(
        f"no measurement named {name!r} in corner {index} -- the two engines "
        "must be asked for the same measurements"
    )


def netlist_sha256(request_path: Path) -> str:
    """The request's netlist content hash, as the response's own
    ``environment.netlist_sha256`` provenance field reports it -- the
    #2007 shared-provenance anchor (both engines hashed the same bytes)."""
    request = json.loads(request_path.read_text(encoding="utf-8"))
    body_path = request_path.parent / request["netlist"]
    return sha256_file(str(body_path))
