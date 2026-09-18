"""Drive [FastCap 2.0](https://github.com/ediloren/FastCap2) as an
independent cross-validation oracle for `klt mom`'s capacitance matrix
(issue #2015, pairing #4 of tracking issue #2007).

`klt mom`'s Maxwell capacitance matrix comes from **one** implementation:
this repo's own Rust core (`native/mom/`), whose `solver.rs` is a
point-collocation constant-panel Method of Moments. `tests/test_mom.py`
pins its contract and `tests/test_mom_validation.py` checks it against
analytic closed forms, but a closed form only exists for a handful of
idealised shapes -- for anything else there is nothing to ask. FastCap is
a different implementation of exactly that job (1992 M.I.T. C, no shared
code, no shared language, and a materially *better* discretisation: an
analytic panel-to-panel double integral plus a multipole-accelerated
solve, where `solver.rs` deliberately uses the bare point-charge kernel
between panel centroids -- its own docstring says so). `solver.rs` cites
FastCap's own paper (Nabors & White, IEEE TCAD 1991) as the method it
implements, which is what makes agreement here informative: two
independent implementations of the same physics, one of them the reference
the other was written from.

`docs/design/fastcap-oracle.md` is the methodology document: what is
matched (geometry, units, model, check scope), what the two stacks *do*
share, the measured agreement, and the cases neither covers. Read it
before changing anything here.

## What this module is (and is not)

A **test-only** helper: nothing in `src/klayout_tools/` imports it, and
`klt` never shells out to FastCap at runtime -- the same "oracle, not
runtime" call `tests/helpers/magic_oracle.py` records for magic and
`docs/design/mom-cross-validation.md` records for NEC2++. FastCap is
headless by construction here: this module only ever runs it in its
shell-only capacitance mode (no `-m`/`-q` PostScript dumps), per this
repo's headless-always rule.

## Requirements, and how a run is gated

One thing must resolve, and :func:`oracle_skip_reason` reports when it does
not, so a test module can skip cleanly (the real-binary gate
`tests/test_lvs.py`'s netgen tier established -- absence must never fail
CI): a `fastcap` binary on `$PATH`, which `scripts/install-fastcap.sh`
builds from a pinned, checksum-verified source archive.

## Evidence that FastCap actually ran

Per #2007's acceptance criterion 2, an exit code proves nothing here, and
that is not hypothetical: FastCap's own input reader calls `exit(0)` --
success -- on a malformed input line, a bad conductor-surface list entry,
and an unreadable file. :func:`run_fastcap` therefore refuses any run that
did not demonstrably solve *this* problem:

- a non-zero exit status, or any of FastCap's own failure phrases in its
  output (:data:`_FAILURE_MARKERS`);
- no `CAPACITANCE MATRIX` block, or a matrix that is not
  ``n_conductors`` square;
- a `Total number of panels` line disagreeing with the number of panels
  this module wrote to the `.qui` file (the matched-geometry check --
  FastCap read every panel, and no others);
- a `Number of conductors` line disagreeing with the conductor count
  asked for (two conductors silently merged into one is a *plausible*
  failure: FastCap keys conductors off the name string in each panel
  line);
- no per-column `ITERATION DATA` (the solve ran for every conductor, not
  just the parse).
"""

from __future__ import annotations

import math
import re
import shutil
import subprocess
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from klayout_tools._provenance import sha256_file

#: `fastcap` binary, or ``None`` when it is not installed (the real-binary
#: gate, mirroring `tests/helpers/magic_oracle.py`'s `MAGIC_BINARY`).
FASTCAP_BINARY = shutil.which("fastcap")

#: Extent (um) below which a box dimension is treated as exactly zero -- a
#: flat plate, or a degenerate wall to skip. Mirrors `native/mom/src/
#: geometry.rs`'s own `EPS_UM`, so this exporter's face set is the same
#: face set the solver under test discretises.
EPS_UM = 1e-9

#: Default relative-residual tolerance passed to FastCap (`-t`). Tighter
#: than FastCap's own 0.01 default for two reasons: the iterative solve
#: should not be a term in the comparison's error budget (`solver.rs`
#: converges its CG to 1e-12), and FastCap prints the matrix to
#: `2 + log10(1/tol)` significant figures -- at its default that is *four*,
#: which would quantise a sub-percent disagreement into the noise.
DEFAULT_ITER_TOL = 1e-6

#: Default multipole expansion order (`-o`). FastCap's own default.
DEFAULT_EXPANSION_ORDER = 2

_TIMEOUT_S = 900

#: Phrases FastCap prints when it did *not* solve the problem it was
#: handed. It reaches most of these through `exit(0)` -- a success status --
#: which is exactly the "an exit code is not evidence" failure #2007's
#: criterion 2 is about.
_FAILURE_MARKERS = (
    "bad quad format",
    "bad tri format",
    "bad rename format",
    "bad line format",
    "can't open",
    "Can't open",
    "no conductor names specified",
    "zero element request",
    "out of memory",
    "bad conductor surface format",
)

#: SI prefixes FastCap may choose for the capacitance-matrix header
#: ("CAPACITANCE MATRIX, femtofarads"). It picks whichever prefix puts its
#: smallest off-diagonal entry between 0.1 and 10, so the unit is *not*
#: fixed across runs and must be read from the header rather than assumed.
_UNIT_SCALE_F = {
    "": 1.0,
    "milli": 1e-3,
    "micro": 1e-6,
    "nano": 1e-9,
    "pico": 1e-12,
    "femto": 1e-15,
    "atto": 1e-18,
}

_VERSION_RE = re.compile(r"^Running \S+ (.+)$", re.MULTILINE)
_MATRIX_HEADER_RE = re.compile(r"^CAPACITANCE MATRIX, (\w*)farads$", re.MULTILINE)
_PANEL_COUNT_RE = re.compile(r"^\s*Total number of panels:\s*(\d+)\s*$", re.MULTILINE)
_CONDUCTOR_COUNT_RE = re.compile(r"^\s*Number of conductors:\s*(\d+)\s*$", re.MULTILINE)
_ITERATION_RE = re.compile(r"^Starting on column (\d+) \((.*)\)$", re.MULTILINE)
#: A matrix row: the conductor's (padded) name, its 1-based index, then one
#: numeric column per conductor.
_MATRIX_ROW_RE = re.compile(r"^(.*?)\s+(\d+)\s+((?:[-+0-9.eE]+\s*)+)$")

#: Conductor names safe to hand FastCap verbatim. Its `.qui` reader splits
#: on whitespace and appends a `%GROUP<n>` suffix of its own, so a name
#: containing whitespace or `%` would be silently misparsed (or two
#: conductors silently merged). Anything else is exported under a generated
#: `cond<i>` name and mapped back by position -- never guessed at.
_SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9_.\-+]+$")


class FastCapOracleError(RuntimeError):
    """FastCap could not be driven to completion, or produced no usable
    capacitance matrix."""


#: A two-panel problem used only to make FastCap print its banner, for
#: :func:`fastcap_version`. FastCap has **no** `--version` flag and blocks
#: reading stdin when given no input file, so the version has to come out of
#: a real (if trivial) run.
_VERSION_PROBE_QUI = """0 klt fastcap version probe
Q probe_a 0 0 0 1e-6 0 0 1e-6 1e-6 0 0 1e-6 0
Q probe_b 0 0 1e-6 1e-6 0 1e-6 1e-6 1e-6 1e-6 0 1e-6 1e-6
"""


def fastcap_version() -> str | None:
    """FastCap's own reported version (e.g. ``"2.0 (18Sep92)"``), or
    ``None``.

    Read from the banner it prints on a run (`Running <argv0> 2.0
    (18Sep92)`) -- FastCap has no `--version` flag, and deriving a version
    string from the install path would record the *pin* rather than the
    binary that actually produced the numbers. Never raises, mirroring
    :func:`tests.helpers.magic_oracle.magic_version`.
    """
    if FASTCAP_BINARY is None:
        return None
    try:
        with tempfile.TemporaryDirectory() as work:
            probe = Path(work) / "probe.qui"
            probe.write_text(_VERSION_PROBE_QUI, encoding="utf-8")
            completed = subprocess.run(
                [FASTCAP_BINARY, probe.name],
                cwd=work,
                capture_output=True,
                text=True,
                stdin=subprocess.DEVNULL,
                timeout=60,
            )
    except (OSError, subprocess.TimeoutExpired):  # pragma: no cover - host-dependent
        return None
    return _version_from_log(f"{completed.stdout or ''}\n{completed.stderr or ''}")


def _version_from_log(log: str) -> str | None:
    match = _VERSION_RE.search(log)
    return match.group(1).strip() if match else None


def oracle_skip_reason() -> str | None:
    """A human-readable reason this oracle cannot run, or ``None`` when it
    can.

    Used at module level by `tests/test_mom_capacitance_oracle.py`
    (``pytest.skip(..., allow_module_level=True)``). Absence must skip,
    never fail.
    """
    if FASTCAP_BINARY is None:
        return (
            "fastcap is not installed on this machine -- build the pinned "
            "version with scripts/install-fastcap.sh and add its bin/ to "
            "$PATH (see docs/design/fastcap-oracle.md)"
        )
    return None


# --------------------------------------------------------------------------- #
# Geometry export: klt mom's own box/panel model -> FastCap's `.qui` format
# --------------------------------------------------------------------------- #


def _minmax(a: float, b: float) -> tuple[float, float]:
    return (a, b) if a <= b else (b, a)


def segment_count(extent_um: float, panel_size_um: float) -> int:
    """Number of panels along an edge of length ``extent_um``.

    A verbatim port of `native/mom/src/geometry.rs`'s `face_segment_count`
    (``round(extent / panel_size)``, floored at 1, and 0 for a degenerate
    extent). Matching it exactly is the point: it makes the *mesh* -- not
    only the geometry -- identical on both sides, so what the comparison
    measures is the two kernels and nothing else. See
    docs/design/fastcap-oracle.md's "Matched inputs".
    """
    if abs(extent_um) < EPS_UM:
        return 0
    n = round(abs(extent_um) / panel_size_um)
    if not math.isfinite(n) or n < 1:
        return 1
    return int(n)


def _face_quads(
    u_range: tuple[float, float],
    v_range: tuple[float, float],
    panel_size_um: float,
    place,
) -> list[tuple[tuple[float, float, float], ...]]:
    """Tile one rectangular face into ``nu x nv`` quadrilaterals, mirroring
    `geometry.rs`'s `emit_face`. ``place(u, v)`` maps the face's own 2-D
    parameters onto 3-D, exactly as the Rust closures do."""
    u0, u1 = u_range
    v0, v1 = v_range
    nu = segment_count(u1 - u0, panel_size_um)
    nv = segment_count(v1 - v0, panel_size_um)
    if nu == 0 or nv == 0:
        return []
    du = (u1 - u0) / nu
    dv = (v1 - v0) / nv
    quads = []
    for i in range(nu):
        for j in range(nv):
            ua, ub = u0 + i * du, u0 + (i + 1) * du
            va, vb = v0 + j * dv, v0 + (j + 1) * dv
            quads.append(
                (place(ua, va), place(ub, va), place(ub, vb), place(ua, vb)),
            )
    return quads


def box_quads(
    box: dict[str, float], panel_size_um: float
) -> list[tuple[tuple[float, float, float], ...]]:
    """Every panel of one axis-aligned box, as 4-point quadrilaterals in um.

    Mirrors `geometry.rs`'s `discretize_box`: a box with ``z0_um == z1_um``
    is a flat plate and contributes a *single* XY face (not six coincident
    ones); a box with thickness contributes all six faces of the prism, with
    any degenerate face skipped.
    """
    x0, x1 = _minmax(float(box["x0_um"]), float(box["x1_um"]))
    y0, y1 = _minmax(float(box["y0_um"]), float(box["y1_um"]))
    z0, z1 = _minmax(float(box["z0_um"]), float(box["z1_um"]))
    flat = abs(z1 - z0) < EPS_UM

    quads = _face_quads((x0, x1), (y0, y1), panel_size_um, lambda u, v: (u, v, z1))
    if not flat:
        quads += _face_quads((x0, x1), (y0, y1), panel_size_um, lambda u, v: (u, v, z0))
        quads += _face_quads((y0, y1), (z0, z1), panel_size_um, lambda u, v: (x0, u, v))
        quads += _face_quads((y0, y1), (z0, z1), panel_size_um, lambda u, v: (x1, u, v))
        quads += _face_quads((x0, x1), (z0, z1), panel_size_um, lambda u, v: (u, y0, v))
        quads += _face_quads((x0, x1), (z0, z1), panel_size_um, lambda u, v: (u, y1, v))
    return quads


def _boxes_touch(a: dict[str, float], b: dict[str, float]) -> bool:
    """True when two boxes overlap or share any part of a face.

    Both solvers handle a multi-box conductor, but they handle it
    *differently*: `geometry.rs` de-duplicates coincident panels within a
    conductor and otherwise leaves the enclosed faces in place, while a
    `.qui` file has no notion of an interior face at all. Rather than guess
    which convention would be "the same question", :func:`write_qui`
    rejects touching boxes outright -- see docs/design/fastcap-oracle.md's
    "Unsupported".
    """
    for axis in ("x", "y", "z"):
        a0, a1 = _minmax(float(a[f"{axis}0_um"]), float(a[f"{axis}1_um"]))
        b0, b1 = _minmax(float(b[f"{axis}0_um"]), float(b[f"{axis}1_um"]))
        if a1 < b0 - EPS_UM or b1 < a0 - EPS_UM:
            return False
    return True


def _assert_disjoint(conductors: Sequence[dict[str, Any]]) -> None:
    """Refuse any pair of boxes -- within a conductor or across two -- that
    overlaps or touches. See :func:`_boxes_touch` for why."""
    boxes = [box for conductor in conductors for box in conductor["boxes"]]
    for index, first in enumerate(boxes):
        for second in boxes[index + 1 :]:
            if _boxes_touch(first, second):
                raise FastCapOracleError(
                    f"boxes {first!r} and {second!r} overlap or touch -- this "
                    "oracle only exports conductors built from disjoint boxes "
                    "(see docs/design/fastcap-oracle.md's 'Unsupported')"
                )


def _export_names(conductors: Sequence[dict[str, Any]]) -> list[str]:
    """The name each conductor is written under in the `.qui` file: its own
    name when FastCap can carry it losslessly, else a generated
    ``cond<i>``. Raises on a collision, which would silently merge two
    conductors into one."""
    names = []
    for index, conductor in enumerate(conductors, start=1):
        name = str(conductor["name"])
        names.append(name if _SAFE_NAME_RE.match(name) else f"cond{index}")
    if len(set(names)) != len(names):
        raise FastCapOracleError(
            f"conductor names collide once exported to FastCap: {names!r} -- "
            "FastCap keys conductors off this string, so a collision would "
            "silently merge two conductors into one"
        )
    return names


def write_qui(
    path: str | Path,
    conductors: Sequence[dict[str, Any]],
    panel_size_um: float,
    *,
    title: str = "klt mom capacitance cross-validation (issue #2015)",
) -> tuple[int, list[str]]:
    """Write ``conductors`` as a FastCap "quickif" (`.qui`) surface file.

    ``conductors`` is exactly the request shape
    :func:`klayout_tools.mom.solve_capacitance_matrix` takes --
    ``[{"name": str, "boxes": [{"x0_um", "y0_um", "x1_um", "y1_um",
    "z0_um", "z1_um"}, ...]}, ...]`` -- so both solvers are handed the same
    Python objects, not two transcriptions of one design.

    Coordinates are written in **metres**: FastCap is an MKS code (its
    `4 pi eps_0` constant is the SI value), while `klt mom`'s request is in
    micrometres. This 1e-6 factor is the *only* unit conversion in the
    comparison.

    Returns ``(panel_count, export_names)``.
    """
    if panel_size_um <= 0:
        raise FastCapOracleError(f"panel_size_um must be positive, got {panel_size_um}")
    if not conductors:
        raise FastCapOracleError("at least one conductor is required")

    _assert_disjoint(conductors)
    names = _export_names(conductors)
    lines = [f"0 {title}"]
    panel_count = 0
    for name, conductor in zip(names, conductors, strict=True):
        if not conductor["boxes"]:
            raise FastCapOracleError(f"conductor {name!r} has no boxes")
        emitted = 0
        for box in conductor["boxes"]:
            for quad in box_quads(box, panel_size_um):
                coords = " ".join(
                    f"{value * 1e-6:.12e}" for point in quad for value in point
                )
                lines.append(f"Q {name} {coords}")
                emitted += 1
        if emitted == 0:
            # Per conductor, not cumulative: `geometry.rs` raises for exactly
            # this case too, and a conductor FastCap never sees would silently
            # drop a column from the matrix.
            raise FastCapOracleError(
                f"conductor {name!r} produced zero panels -- every box is a "
                "degenerate point or line"
            )
        panel_count += emitted
    path = Path(path)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return panel_count, names


# --------------------------------------------------------------------------- #
# Running FastCap and parsing its capacitance matrix
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class FastCapResult:
    """One FastCap solve: the Maxwell capacitance matrix it printed,
    converted to femtofarads, plus everything needed to prove the run was
    the run that was asked for."""

    conductors: tuple[str, ...]
    capacitance_matrix_ff: tuple[tuple[float, ...], ...]
    panel_count: int
    reported_panel_count: int
    reported_conductor_count: int
    solved_columns: tuple[int, ...]
    unit_prefix: str
    version: str | None
    qui_path: str
    list_path: str
    qui_sha256: str | None
    background_permittivity: float
    panel_size_um: float
    expansion_order: int
    iter_tol: float
    command: tuple[str, ...]
    log: str

    def entry_ff(self, row: str, column: str) -> float:
        """``C[row][column]`` in femtofarads, addressed by conductor name
        rather than by index -- so a caller can never silently compare two
        different conductors' entries."""
        i = self.conductors.index(row)
        j = self.conductors.index(column)
        return self.capacitance_matrix_ff[i][j]


def _check_ran(log: str, returncode: int, command: Sequence[str]) -> None:
    if returncode != 0:
        raise FastCapOracleError(
            f"fastcap exited {returncode} for {' '.join(command)}\n{log}"
        )
    for marker in _FAILURE_MARKERS:
        if marker in log:
            raise FastCapOracleError(
                f"fastcap exited 0 but reported {marker!r} -- it did not solve "
                f"the problem it was given\n{log}"
            )


def _parse_matrix(log: str, names: Sequence[str]) -> tuple[list[list[float]], str]:
    header = _MATRIX_HEADER_RE.search(log)
    if header is None:
        raise FastCapOracleError(f"fastcap printed no capacitance matrix\n{log}")
    prefix = header.group(1)
    if prefix not in _UNIT_SCALE_F:
        raise FastCapOracleError(
            f"fastcap reported an unrecognised capacitance unit {prefix!r}farads\n{log}"
        )
    scale_to_ff = _UNIT_SCALE_F[prefix] * 1e15

    # Everything after the header line: a blank remainder-of-line, then
    # FastCap's column-number heading, then one row per conductor. Skipping
    # by *content* (drop lines until the first one that starts with a
    # non-space) rather than by a fixed offset, because the heading's
    # indentation depends on the longest conductor name.
    body = log[header.end() :].splitlines()
    while body and not body[0][:1].strip():
        body.pop(0)
    rows: list[list[float]] = []
    for line in body:
        if not line.strip():
            break
        match = _MATRIX_ROW_RE.match(line)
        if match is None:
            raise FastCapOracleError(
                f"unparsable capacitance-matrix row {line!r}\n{log}"
            )
        # FastCap appends its own `%GROUP<n>` suffix to every conductor
        # name; strip it and check the row is the conductor this module
        # exported in that position, so a reordered or renamed matrix can
        # never be read as if it were in the requested order.
        if len(rows) >= len(names):
            raise FastCapOracleError(
                f"fastcap printed more matrix rows than the {len(names)} "
                f"conductors exported\n{log}"
            )
        reported = match.group(1).strip().rsplit("%", 1)[0]
        index = int(match.group(2))
        expected = names[len(rows)]
        if reported != expected or index != len(rows) + 1:
            raise FastCapOracleError(
                f"fastcap's matrix row {len(rows) + 1} is {reported!r} (index "
                f"{index}), expected {expected!r} -- the matrix is not in the "
                f"order this module exported\n{log}"
            )
        rows.append([float(v) * scale_to_ff for v in match.group(3).split()])

    if len(rows) != len(names) or any(len(row) != len(names) for row in rows):
        raise FastCapOracleError(
            f"fastcap printed a {len(rows)}-row matrix for {len(names)} "
            f"conductors\n{log}"
        )
    return rows, prefix


def run_fastcap(
    conductors: Sequence[dict[str, Any]],
    *,
    background_permittivity: float,
    panel_size_um: float,
    work_dir: str | Path,
    expansion_order: int = DEFAULT_EXPANSION_ORDER,
    iter_tol: float = DEFAULT_ITER_TOL,
) -> FastCapResult:
    """Solve ``conductors`` with FastCap and return its Maxwell capacitance
    matrix in femtofarads.

    The dielectric is passed through FastCap's **conductor-surface list
    file** (`-l`, a single ``C <file> <outer permittivity> 0 0 0`` entry),
    not its `-p` command-line permittivity factor. That is deliberate and
    load-bearing: `-p` is applied *twice* in FastCap 2.0 (once as the
    surface's `outer_perm` during the solve, once again when the matrix is
    printed), so `-p 3.9` reports 3.9^2 times the free-space answer.
    Measured directly against this repo's geometry while writing this
    module -- see docs/design/fastcap-oracle.md's "The `-p` trap". The list
    file applies it exactly once, matching `klt mom`'s
    ``background_permittivity``, which the Laplace problem is exactly
    linear in on both sides.

    Raises :class:`FastCapOracleError` for any run that did not demonstrably
    solve this problem -- see the module docstring.
    """
    if FASTCAP_BINARY is None:  # pragma: no cover - callers gate on this
        raise FastCapOracleError("fastcap is not installed")
    if background_permittivity <= 0:
        raise FastCapOracleError(
            f"background_permittivity must be positive, got {background_permittivity}"
        )

    work = Path(work_dir)
    work.mkdir(parents=True, exist_ok=True)
    qui_path = work / "klt_mom_oracle.qui"
    list_path = work / "klt_mom_oracle.lst"

    panel_count, names = write_qui(qui_path, conductors, panel_size_um)
    # FastCap resolves the surface file relative to its own cwd, which is
    # `work` below -- so the list file names it by basename.
    list_path.write_text(
        f"* klt mom capacitance cross-validation (issue #2015)\n"
        f"C {qui_path.name} {background_permittivity!r} 0.0 0.0 0.0\n",
        encoding="utf-8",
    )

    command = [
        FASTCAP_BINARY,
        f"-l{list_path.name}",
        f"-o{expansion_order}",
        f"-t{iter_tol!r}",
    ]
    try:
        completed = subprocess.run(
            command,
            cwd=str(work),
            capture_output=True,
            text=True,
            timeout=_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired as exc:  # pragma: no cover - host-dependent
        raise FastCapOracleError(
            f"fastcap did not finish within {_TIMEOUT_S}s: {' '.join(command)}"
        ) from exc

    log = f"{completed.stdout or ''}\n{completed.stderr or ''}"
    _check_ran(log, completed.returncode, command)

    reported_panels = _PANEL_COUNT_RE.search(log)
    if reported_panels is None:
        raise FastCapOracleError(f"fastcap reported no panel count\n{log}")
    if int(reported_panels.group(1)) != panel_count:
        raise FastCapOracleError(
            f"fastcap read {reported_panels.group(1)} panels, but this module "
            f"wrote {panel_count} -- it did not solve the geometry it was "
            f"given\n{log}"
        )

    reported_conductors = _CONDUCTOR_COUNT_RE.search(log)
    if reported_conductors is None:
        raise FastCapOracleError(f"fastcap reported no conductor count\n{log}")
    if int(reported_conductors.group(1)) != len(conductors):
        raise FastCapOracleError(
            f"fastcap found {reported_conductors.group(1)} conductors, expected "
            f"{len(conductors)} -- conductors were merged or split\n{log}"
        )

    solved_columns = tuple(int(m.group(1)) for m in _ITERATION_RE.finditer(log))
    if solved_columns != tuple(range(1, len(conductors) + 1)):
        raise FastCapOracleError(
            f"fastcap solved columns {solved_columns}, expected one per "
            f"conductor\n{log}"
        )

    version = _version_from_log(log)
    if version is None:
        raise FastCapOracleError(f"fastcap printed no version banner\n{log}")

    matrix, prefix = _parse_matrix(log, names)

    return FastCapResult(
        conductors=tuple(str(c["name"]) for c in conductors),
        capacitance_matrix_ff=tuple(tuple(row) for row in matrix),
        panel_count=panel_count,
        reported_panel_count=int(reported_panels.group(1)),
        reported_conductor_count=int(reported_conductors.group(1)),
        solved_columns=solved_columns,
        unit_prefix=prefix,
        # From this run's own banner -- the binary that produced these
        # numbers, not a second invocation that might resolve differently.
        version=version,
        qui_path=str(qui_path),
        list_path=str(list_path),
        qui_sha256=sha256_file(str(qui_path)),
        background_permittivity=float(background_permittivity),
        panel_size_um=float(panel_size_um),
        expansion_order=expansion_order,
        iter_tol=iter_tol,
        command=tuple(command),
        log=log,
    )


# --------------------------------------------------------------------------- #
# Provenance (#2007 criterion 4)
# --------------------------------------------------------------------------- #


def oracle_provenance(
    *,
    oracle: FastCapResult,
    klt_report: dict[str, Any],
    layout_path: str | None = None,
    spec_path: str | None = None,
) -> dict[str, Any]:
    """The shared provenance block for one capacitance comparison, per
    #2007's criterion 4 ("recorded tool/deck versions and input hashes").

    Mirrors `tests/helpers/magic_oracle.py`'s
    :func:`~tests.helpers.magic_oracle.oracle_provenance` shape so the two
    pairings' records are directly diffable::

        {
          "oracle": {"tool": "fastcap", "version": "2.0 (18Sep92)",
                     "settings": {"expansion_order": 2, "iter_tol": 1e-06,
                                  "panel_size_um": 0.5,
                                  "background_permittivity": 3.9},
                     "input": {"path": ".../klt_mom_oracle.qui",
                               "content_hash": "sha256:...",
                               "panel_count": 816}},
          "input": {"layout": {...}, "spec": {...}},
          "klt": {"schema_version": 2, "klayout_version": "...",
                  "klt_mom_native_source_fingerprint": "533a05c9b421a763",
                  "panel_size_um": 0.5, "panel_count": 816,
                  "background_permittivity": 3.9}
        }

    There is deliberately no shared *input file* hash the way the magic
    pairing has one: FastCap cannot read GDSII, so the geometry it is given
    is exported from `klt mom`'s own box request rather than read
    independently (the shared surface this pairing declares -- see
    docs/design/fastcap-oracle.md). What is recorded instead is the hash of
    the exported `.qui` and the panel counts both solvers report, which is
    what makes a future disagreement attributable to a specific mesh.
    """
    from klayout_tools import _provenance

    # The Rust solver under test identifies itself by the content
    # fingerprint `native/mom/build.rs` embeds (an FNV-1a-64 hash of the
    # crate's own sources) -- the same identity ci.yml's freshness gate
    # checks. A crate version would not distinguish two builds of an
    # unreleased `0.1.0`; this does.
    native_fingerprint: str | None
    try:  # pragma: no cover - trivially environment-dependent
        import klt_mom_native

        native_fingerprint = getattr(klt_mom_native, "__source_fingerprint__", None)
    except ImportError:  # pragma: no cover - callers gate on the extension
        native_fingerprint = None

    block: dict[str, Any] = {
        "oracle": {
            "tool": "fastcap",
            "version": oracle.version,
            "settings": {
                "expansion_order": oracle.expansion_order,
                "iter_tol": oracle.iter_tol,
                "panel_size_um": oracle.panel_size_um,
                "background_permittivity": oracle.background_permittivity,
            },
            "input": {
                "path": oracle.qui_path,
                "content_hash": (
                    f"sha256:{oracle.qui_sha256}" if oracle.qui_sha256 else None
                ),
                "panel_count": oracle.panel_count,
            },
        },
        "input": {
            "layout": _input_record(layout_path),
            "spec": _input_record(spec_path),
        },
        "klt": {
            "schema_version": klt_report.get("schema_version"),
            "klayout_version": _provenance._klayout_version(),
            "klt_version": _provenance._klt_version(),
            "klt_mom_native_source_fingerprint": native_fingerprint,
            "panel_size_um": klt_report.get("panel_size_um"),
            "panel_count": klt_report.get("panel_count"),
            "background_permittivity": klt_report.get("background_permittivity"),
        },
    }
    return block


def _input_record(path: str | None) -> dict[str, Any] | None:
    if path is None:
        return None
    digest = sha256_file(path)
    return {
        "path": path,
        "content_hash": f"sha256:{digest}" if digest else None,
    }
