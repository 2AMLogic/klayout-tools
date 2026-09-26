"""Arc-by-arc comparison of two Liberty (``.lib``) files -- the accuracy
harness behind ``klt characterize --compare-to`` (issue #2503).

``klt characterize`` emits NLDM tables it measured itself; without an answer
key those numbers are trusted blind. IHP's ``sg13g2_stdcell`` ships a vendor
``.lib`` per corner, characterized by IHP from the same SPICE netlists and
model cards this repo already fetches (``scripts/fetch-ihp-sg13g2.sh``), so
characterizing a handful of those cells and diffing the result against the
vendor file is a self-checking accuracy test with no new library required
(#2498's stated first milestone).

This module is that diff. It is **pure** -- two paths in, a
JSON-serialisable ``dict`` out -- and works from the two ``.lib`` files
alone, so it can compare any characterizer's output against any reference,
not only ``klt characterize``'s.

What is compared, per cell present in *ours*:

- every ``timing()`` group, matched by ``(output pin, related_pin)``:
  ``cell_rise``, ``cell_fall``, ``rise_transition``, ``fall_transition``;
- every ``internal_power()`` group, matched the same way: ``rise_power``,
  ``fall_power``;
- every ``leakage_power()`` group, matched by *input state* -- each of our
  ``when`` conditions is expanded to the input assignments it covers and the
  reference group whose own ``when`` holds under that assignment is found
  (a vendor ``when`` may also name output pins, e.g. IHP's ``"A&!Y"``, so
  output values are computed from the reference cell's own ``function``);
- ``cell_leakage_power``.

Table values are compared point by point on **our** grid. A point that lies
on a reference grid node (our axes equal to, or a subset of, the vendor's --
the intended use) reads the reference value directly; any other point is
bilinearly interpolated from the reference table, and the table's
``interpolated: true`` says so.

**Nothing is silently omitted.** A group or table present on one side and
not the other lands in that cell's ``missing`` list and in
``summary.missing``, and ``summary.fields.<field>.compared`` counts the
points actually compared -- so "``leakage_power`` within tolerance" can be
told apart from "``leakage_power`` never compared".

Tolerances are ``|ours - reference| <= max(rel * |reference|, abs)`` per
field -- a relative bound with an absolute floor, because NLDM entries near
zero (a 50%-threshold delay at a slow input edge into a light load, an
internal energy at the grid's noise floor) make a purely relative bound
meaningless. :data:`DEFAULT_TOLERANCES` holds the bounds this repo enforces
against ``sg13g2_stdcell``; see ``docs/cli/characterize.md`` for how each was
chosen.
"""

from __future__ import annotations

import itertools
import os
import re
from dataclasses import dataclass, field
from typing import Any

from .characterize_arcs import FunctionError, parse_function

#: Version of the ``comparison`` block's shape.
SCHEMA_VERSION = 1

#: Every table field compared per arc, in report order.
TIMING_FIELDS = ("cell_rise", "cell_fall", "rise_transition", "fall_transition")
POWER_FIELDS = ("rise_power", "fall_power")
#: Every field the summary reports, in report order.
FIELDS = TIMING_FIELDS + POWER_FIELDS + ("leakage_power", "cell_leakage_power")

#: Per-field ``{"rel", "abs"}`` tolerance: a point passes when
#: ``|delta| <= max(rel * |reference|, abs)``. Units of ``abs`` are the
#: field's own (ns, pJ, pW). Not picked from thin air: each bound is the
#: worst delta observed characterizing ``sg13g2_inv_1``/``buf_1``/
#: ``nand2_1``/``nor2_1`` against IHP's own ``sg13g2_stdcell``
#: typ_1p20V_25C on the vendor's full 7x7 grid, rounded up with headroom
#: (observed worst in the trailing comment). ``docs/cli/characterize.md``'s
#: "Accuracy against a vendor library" gives the full spread and the cause
#: of each residual.
DEFAULT_TOLERANCES: dict[str, dict[str, float]] = {
    "cell_rise": {"rel": 0.15, "abs": 0.005},  # 9.9%
    "cell_fall": {"rel": 0.15, "abs": 0.005},  # 12.3%
    "rise_transition": {"rel": 0.25, "abs": 0.005},  # 13.5%
    "fall_transition": {"rel": 0.25, "abs": 0.005},  # 20.7%
    "rise_power": {"rel": 0.50, "abs": 0.003},  # 2.37 fJ
    "fall_power": {"rel": 0.50, "abs": 0.003},  # 2.44 fJ
    "leakage_power": {"rel": 0.15, "abs": 5.0},  # 11.6%
    "cell_leakage_power": {"rel": 0.10, "abs": 5.0},  # 5.2%
}


class LibertyCompareError(Exception):
    """Raised when a file cannot be read or parsed as Liberty, or when a
    requested cell is absent from *our* library (a cell absent from the
    *reference* is reported in ``missing``, not raised)."""


# --------------------------------------------------------------------------- #
# A minimal Liberty reader
# --------------------------------------------------------------------------- #


@dataclass
class Group:
    """One parsed Liberty group: ``kind (args) { ... }``."""

    kind: str
    args: list[str]
    attributes: dict[str, str] = field(default_factory=dict)
    complex_attributes: dict[str, list[str]] = field(default_factory=dict)
    groups: list[Group] = field(default_factory=list)

    def children(self, kind: str) -> list[Group]:
        return [group for group in self.groups if group.kind == kind]


_TOKEN_RE = re.compile(r'"(?:[^"\\]|\\.)*"|[(){}:;,]|[^\s(){}:;,"]+')
_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)


def parse_liberty(path: str) -> Group:
    """Parse ``path`` into its top-level ``library`` :class:`Group`.

    Deliberately minimal -- groups, simple ``name : value ;`` attributes, and
    complex ``name (a, b, ...) ;`` attributes are all this comparison needs,
    and all both IHP's vendor files and ``liberty_writer``'s output use. The
    Rust reader in ``native/statime`` is not reachable from Python on its own
    (its only pyo3 entry point runs a whole timing analysis), so this is not
    a duplicate of something callable.
    """
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            text = handle.read()
    except OSError as exc:
        raise LibertyCompareError(f"could not read '{path}': {exc}") from exc
    text = _COMMENT_RE.sub(" ", text).replace("\\\r\n", " ").replace("\\\n", " ")
    tokens = _TOKEN_RE.findall(text)
    parser = _Parser(tokens, path)
    return parser.library()


class _Parser:
    def __init__(self, tokens: list[str], path: str) -> None:
        self.tokens = tokens
        self.pos = 0
        self.path = path

    def _peek(self) -> str | None:
        return self.tokens[self.pos] if self.pos < len(self.tokens) else None

    def _next(self) -> str:
        if self.pos >= len(self.tokens):
            raise LibertyCompareError(f"'{self.path}': unexpected end of file")
        token = self.tokens[self.pos]
        self.pos += 1
        return token

    def library(self) -> Group:
        """Find the first top-level ``library (name) { ... }`` group."""
        while self._peek() is not None:
            if self._next() == "library" and self._peek() == "(":
                self.pos += 1
                args = self._args()
                if self._peek() == "{":
                    self.pos += 1
                    group = Group(kind="library", args=args)
                    self.group_body(group)
                    return group
        raise LibertyCompareError(f"'{self.path}' contains no library() group")

    def group_body(self, group: Group) -> None:
        while True:
            token = self._peek()
            if token is None:
                raise LibertyCompareError(
                    f"'{self.path}': group {group.kind}({', '.join(group.args)}) "
                    "is never closed"
                )
            if token == "}":
                self.pos += 1
                return
            if token == ";":
                self.pos += 1
                continue
            name = self._next()
            follower = self._next()
            if follower == ":":
                value_tokens: list[str] = []
                while self._peek() not in (";", "}", None):
                    value_tokens.append(self._next())
                if self._peek() == ";":
                    self.pos += 1
                group.attributes[name] = _unquote(" ".join(value_tokens))
            elif follower == "(":
                args = self._args()
                if self._peek() == "{":
                    self.pos += 1
                    child = Group(kind=name, args=args)
                    self.group_body(child)
                    group.groups.append(child)
                else:
                    if self._peek() == ";":
                        self.pos += 1
                    group.complex_attributes[name] = args
            else:
                raise LibertyCompareError(
                    f"'{self.path}': expected ':' or '(' after '{name}', got "
                    f"'{follower}'"
                )

    def _args(self) -> list[str]:
        args: list[str] = []
        current: list[str] = []
        depth = 0
        while True:
            token = self._next()
            if token == "(":
                depth += 1
                current.append(token)
            elif token == ")":
                if depth == 0:
                    if current:
                        args.append(_unquote(" ".join(current)))
                    return args
                depth -= 1
                current.append(token)
            elif token == "," and depth == 0:
                args.append(_unquote(" ".join(current)))
                current = []
            else:
                current.append(token)


def _unquote(text: str) -> str:
    text = text.strip()
    if len(text) >= 2 and text[0] == '"' and text[-1] == '"':
        return text[1:-1]
    return text


# --------------------------------------------------------------------------- #
# Cell model
# --------------------------------------------------------------------------- #


@dataclass
class Table:
    index_1: tuple[float, ...]
    index_2: tuple[float, ...]
    values: tuple[tuple[float, ...], ...]


@dataclass
class CellData:
    name: str
    inputs: tuple[str, ...]
    outputs: tuple[str, ...]
    functions: dict[str, str]
    timing: dict[tuple[str, str], dict[str, Table]]
    power: dict[tuple[str, str], dict[str, Table]]
    leakage: list[tuple[str, float]]
    cell_leakage_power: float | None
    notes: list[str]


def load_cells(
    path: str, names: list[str] | tuple[str, ...] | None = None
) -> dict[str, CellData]:
    """Parse ``path`` and return comparable data for each cell in ``names``
    (every cell when ``None``) that the file defines, by name. Cells not
    asked for are never interpreted, so a vendor file's sequential cells
    (scalar/1D power tables, ``ff`` groups) cannot fail a combinational
    comparison."""
    library = parse_liberty(path)
    wanted = set(names) if names is not None else None
    return {
        cell.args[0]: _cell_data(cell, path)
        for cell in library.children("cell")
        if cell.args and (wanted is None or cell.args[0] in wanted)
    }


def _cell_data(cell: Group, path: str) -> CellData:
    name = cell.args[0]
    inputs: list[str] = []
    outputs: list[str] = []
    functions: dict[str, str] = {}
    timing: dict[tuple[str, str], dict[str, Table]] = {}
    power: dict[tuple[str, str], dict[str, Table]] = {}
    notes: list[str] = []
    for pin in cell.children("pin"):
        if not pin.args:
            continue
        pin_name = pin.args[0]
        direction = pin.attributes.get("direction", "")
        if direction == "input":
            inputs.append(pin_name)
        elif direction == "output":
            outputs.append(pin_name)
        if "function" in pin.attributes:
            functions[pin_name] = pin.attributes["function"]
        for kind, target, fields in (
            ("timing", timing, TIMING_FIELDS),
            ("internal_power", power, POWER_FIELDS),
        ):
            for group in pin.children(kind):
                related = group.attributes.get("related_pin")
                if related is None:
                    continue
                key = (pin_name, related)
                if key in target:
                    # A second (typically `when`-qualified) group for the
                    # same pin pair: keep the first, and say so rather than
                    # silently choosing.
                    notes.append(
                        f"{kind} {related}->{pin_name}: more than one group; "
                        "compared the first"
                    )
                    continue
                tables: dict[str, Table] = {}
                for table_group in group.groups:
                    if table_group.kind in fields:
                        tables[table_group.kind] = _table(table_group, path, name)
                target[key] = tables
    leakage = []
    for group in cell.children("leakage_power"):
        value = group.attributes.get("value")
        if value is None:
            continue
        leakage.append((group.attributes.get("when", ""), float(value)))
    cell_leakage = cell.attributes.get("cell_leakage_power")
    return CellData(
        name=name,
        inputs=tuple(inputs),
        outputs=tuple(outputs),
        functions=functions,
        timing=timing,
        power=power,
        leakage=leakage,
        cell_leakage_power=float(cell_leakage) if cell_leakage is not None else None,
        notes=notes,
    )


def _numbers(text: str) -> tuple[float, ...]:
    return tuple(float(item) for item in text.replace(",", " ").split())


def _table(group: Group, path: str, cell: str) -> Table:
    try:
        index_1 = _numbers(" ".join(group.complex_attributes.get("index_1", [])))
        index_2 = _numbers(" ".join(group.complex_attributes.get("index_2", [])))
        rows = tuple(
            _numbers(row) for row in group.complex_attributes.get("values", [])
        )
    except ValueError as exc:
        raise LibertyCompareError(
            f"'{path}' cell '{cell}' {group.kind}: non-numeric table entry ({exc})"
        ) from exc
    if not index_1 or not index_2 or len(rows) != len(index_1):
        raise LibertyCompareError(
            f"'{path}' cell '{cell}' {group.kind}: expected a 2D table with "
            f"index_1/index_2 and one values row per index_1 entry (got "
            f"{len(index_1)} x {len(index_2)} axes, {len(rows)} rows) -- "
            "scalar and 1D tables are not compared"
        )
    for row in rows:
        if len(row) != len(index_2):
            raise LibertyCompareError(
                f"'{path}' cell '{cell}' {group.kind}: ragged values row"
            )
    return Table(index_1=index_1, index_2=index_2, values=rows)


# --------------------------------------------------------------------------- #
# Comparison
# --------------------------------------------------------------------------- #


def compare_libraries(
    ours_path: str,
    reference_path: str,
    *,
    cells: list[str] | tuple[str, ...] | None = None,
    tolerances: dict[str, dict[str, float]] | None = None,
) -> dict[str, Any]:
    """Compare ``ours_path`` against ``reference_path`` for ``cells`` (every
    cell in ours when ``None``). See the module docstring for what is
    compared and how; the returned ``dict`` is the documented
    ``comparison`` block."""
    tolerances = {**DEFAULT_TOLERANCES, **(tolerances or {})}
    ours = load_cells(ours_path, cells)
    names = list(cells) if cells is not None else sorted(ours)
    reference = load_cells(reference_path, names)
    absent = [name for name in names if name not in ours]
    if absent:
        raise LibertyCompareError(
            f"'{ours_path}' does not define cell(s) {', '.join(absent)}"
        )

    accumulators = {name: _FieldAccumulator() for name in FIELDS}
    cell_reports = []
    summary_missing: list[str] = []
    for name in names:
        if name not in reference:
            summary_missing.append(f"{name}: not in the reference library")
            cell_reports.append(
                {
                    "name": name,
                    "arcs": [],
                    "leakage": None,
                    "missing": ["cell not in the reference library"],
                    "notes": [],
                }
            )
            continue
        report = _compare_cell(ours[name], reference[name], tolerances, accumulators)
        summary_missing.extend(f"{name}: {entry}" for entry in report["missing"])
        cell_reports.append(report)

    fields = {name: accumulators[name].summary(tolerances[name]) for name in FIELDS}
    compared_fields = [name for name, entry in fields.items() if entry["compared"]]
    return {
        "schema_version": SCHEMA_VERSION,
        "ours": os.path.abspath(ours_path),
        "reference": os.path.abspath(reference_path),
        "tolerances": {name: dict(tolerances[name]) for name in FIELDS},
        "cells": cell_reports,
        "summary": {
            "cell_count": len(names),
            "fields": fields,
            "missing": summary_missing,
            # True only when every field was compared at least once, every
            # compared point is within its tolerance, and nothing was missing.
            "within_tolerance": (
                not summary_missing
                and len(compared_fields) == len(FIELDS)
                and all(entry["within_tolerance"] for entry in fields.values())
            ),
        },
    }


class _FieldAccumulator:
    def __init__(self) -> None:
        self.compared = 0
        self.violations = 0
        self.max_abs: float | None = None
        self.max_rel: float | None = None
        self.worst: dict[str, Any] | None = None

    def add(self, entry: dict[str, Any], where: str) -> None:
        self.compared += 1
        if not entry["within_tolerance"]:
            self.violations += 1
        magnitude = abs(entry["delta"])
        if self.max_abs is None or magnitude > self.max_abs:
            self.max_abs = magnitude
        rel = entry["rel_delta"]
        if rel is not None and (self.max_rel is None or abs(rel) > self.max_rel):
            self.max_rel = abs(rel)
        # "Worst" = the point closest to (or furthest past) its own bound.
        score = entry["tolerance_used"]
        if self.worst is None or score > self.worst["tolerance_used"]:
            self.worst = {"where": where, **entry}

    def summary(self, tolerance: dict[str, float]) -> dict[str, Any]:
        return {
            "compared": self.compared,
            "violations": self.violations,
            "max_abs_delta": self.max_abs,
            "max_rel_delta": self.max_rel,
            "worst": self.worst,
            "tolerance": dict(tolerance),
            "within_tolerance": self.compared > 0 and self.violations == 0,
        }


def _point(ours: float, reference: float, tolerance: dict[str, float]) -> dict:
    delta = ours - reference
    bound = max(tolerance["rel"] * abs(reference), tolerance["abs"])
    return {
        "ours": ours,
        "reference": reference,
        "delta": delta,
        "rel_delta": delta / abs(reference) if reference != 0 else None,
        "bound": bound,
        # |delta| as a fraction of its bound: <= 1 passes.
        "tolerance_used": abs(delta) / bound if bound > 0 else float("inf"),
        "within_tolerance": abs(delta) <= bound,
    }


def _compare_cell(
    ours: CellData,
    reference: CellData,
    tolerances: dict[str, dict[str, float]],
    accumulators: dict[str, _FieldAccumulator],
) -> dict[str, Any]:
    missing: list[str] = []
    arcs = []
    keys = sorted(set(ours.timing) | set(ours.power))
    for key in keys:
        output_pin, related_pin = key
        tables_report: dict[str, Any] = {}
        for kind, ours_groups, ref_groups, fields in (
            ("timing", ours.timing, reference.timing, TIMING_FIELDS),
            ("internal_power", ours.power, reference.power, POWER_FIELDS),
        ):
            if key not in ours_groups:
                continue
            if key not in ref_groups:
                missing.append(
                    f"{kind} {related_pin}->{output_pin}: not in the reference"
                )
                continue
            for field_name in fields:
                ours_table = ours_groups[key].get(field_name)
                ref_table = ref_groups[key].get(field_name)
                if ours_table is None:
                    if ref_table is not None:
                        missing.append(
                            f"{field_name} {related_pin}->{output_pin}: in the "
                            "reference but not in ours"
                        )
                    continue
                if ref_table is None:
                    missing.append(
                        f"{field_name} {related_pin}->{output_pin}: not in the "
                        "reference"
                    )
                    continue
                tables_report[field_name] = _compare_table(
                    ours_table,
                    ref_table,
                    tolerances[field_name],
                    accumulators[field_name],
                    where=f"{ours.name} {related_pin}->{output_pin} {field_name}",
                )
        arcs.append(
            {
                "output_pin": output_pin,
                "related_pin": related_pin,
                "tables": tables_report,
            }
        )
    for key in sorted(set(reference.timing) - set(ours.timing)):
        missing.append(
            f"timing {key[1]}->{key[0]}: in the reference but not characterized"
        )

    leakage = _compare_leakage(ours, reference, tolerances, accumulators, missing)
    return {
        "name": ours.name,
        "arcs": arcs,
        "leakage": leakage,
        "missing": missing,
        "notes": ours.notes + reference.notes,
    }


def _compare_table(
    ours: Table,
    reference: Table,
    tolerance: dict[str, float],
    accumulator: _FieldAccumulator,
    *,
    where: str,
) -> dict[str, Any]:
    points = []
    interpolated = False
    for row_index, slew in enumerate(ours.index_1):
        ref_row = _axis_position(reference.index_1, slew)
        for col_index, load in enumerate(ours.index_2):
            ref_col = _axis_position(reference.index_2, load)
            if ref_row is not None and ref_col is not None:
                # On a reference grid node (our axis equal to, or a subset
                # of, the reference's): read the vendor value directly.
                ref_value = reference.values[ref_row][ref_col]
            else:
                ref_value = _bilinear(reference, slew, load)
                interpolated = True
            entry = _point(ours.values[row_index][col_index], ref_value, tolerance)
            accumulator.add(
                {"index_1": slew, "index_2": load, **entry},
                where,
            )
            points.append({"index_1": slew, "index_2": load, **entry})
    worst = max(points, key=lambda point: point["tolerance_used"])
    rels = [
        abs(point["rel_delta"]) for point in points if point["rel_delta"] is not None
    ]
    return {
        "interpolated": interpolated,
        "points": len(points),
        "violations": sum(1 for point in points if not point["within_tolerance"]),
        "max_abs_delta": max(abs(point["delta"]) for point in points),
        "max_rel_delta": max(rels) if rels else None,
        "mean_rel_delta": sum(rels) / len(rels) if rels else None,
        "worst": worst,
        "within_tolerance": all(point["within_tolerance"] for point in points),
    }


def _axis_position(axis: tuple[float, ...], value: float) -> int | None:
    """Index of ``value`` on ``axis`` (to a relative 1e-6, well inside the
    6 significant digits a Liberty writer renders), else ``None``."""
    for index, entry in enumerate(axis):
        if abs(entry - value) <= 1e-6 * max(abs(entry), abs(value), 1e-12):
            return index
    return None


def _bilinear(table: Table, x: float, y: float) -> float:
    """Bilinear inter-/extrapolation over ``table`` -- the same lookup
    ``native/statime``'s ``nldm.rs`` performs."""

    def bracket(axis: tuple[float, ...], value: float) -> tuple[int, int, float]:
        if len(axis) == 1:
            return 0, 0, 0.0
        upper = 1
        while upper < len(axis) - 1 and axis[upper] < value:
            upper += 1
        lower = upper - 1
        span = axis[upper] - axis[lower]
        return lower, upper, (value - axis[lower]) / span if span else 0.0

    i0, i1, tx = bracket(table.index_1, x)
    j0, j1, ty = bracket(table.index_2, y)
    v = table.values
    low = v[i0][j0] + (v[i0][j1] - v[i0][j0]) * ty
    high = v[i1][j0] + (v[i1][j1] - v[i1][j0]) * ty
    return low + (high - low) * tx


def _compare_leakage(
    ours: CellData,
    reference: CellData,
    tolerances: dict[str, dict[str, float]],
    accumulators: dict[str, _FieldAccumulator],
    missing: list[str],
) -> dict[str, Any]:
    states = []
    ref_conditions = []
    names = frozenset(reference.inputs) | frozenset(reference.outputs)
    for when, value in reference.leakage:
        try:
            expr = parse_function(when, allowed_pins=names) if when else None
        except FunctionError:
            expr = None
            missing.append(
                f'leakage_power when "{when}": reference condition unparseable'
            )
        ref_conditions.append((when, value, expr))
    output_exprs = {}
    for pin in reference.outputs:
        function = reference.functions.get(pin)
        if function is None:
            continue
        try:
            output_exprs[pin] = parse_function(
                function, allowed_pins=frozenset(reference.inputs)
            )
        except FunctionError:
            continue

    for when, value in ours.leakage:
        try:
            ours_expr = parse_function(when, allowed_pins=frozenset(ours.inputs))
        except FunctionError:
            missing.append(f'leakage_power when "{when}": our condition unparseable')
            continue
        matches: set[int] = set()
        covered = 0
        for bits in itertools.product((False, True), repeat=len(ours.inputs)):
            assignment = dict(zip(ours.inputs, bits, strict=True))
            if not ours_expr.evaluate(assignment):
                continue
            covered += 1
            full = dict(assignment)
            for pin, expr in output_exprs.items():
                full[pin] = expr.evaluate(assignment)
            for index, (_, _, expr) in enumerate(ref_conditions):
                if expr is not None and _safe_eval(expr, full):
                    matches.add(index)
        if covered == 0 or len(matches) != 1:
            missing.append(
                f'leakage_power when "{when}": '
                + (
                    "no reference leakage_power group covers this state"
                    if not matches
                    else "matches more than one reference leakage_power group"
                )
            )
            continue
        ref_when, ref_value, _ = ref_conditions[matches.pop()]
        entry = _point(value, ref_value, tolerances["leakage_power"])
        accumulators["leakage_power"].add(
            {"when": when, **entry}, f"{ours.name} leakage_power {when}"
        )
        states.append({"when": when, "reference_when": ref_when, **entry})

    total = None
    if ours.cell_leakage_power is not None:
        if reference.cell_leakage_power is None:
            missing.append("cell_leakage_power: not in the reference")
        else:
            total = _point(
                ours.cell_leakage_power,
                reference.cell_leakage_power,
                tolerances["cell_leakage_power"],
            )
            accumulators["cell_leakage_power"].add(
                dict(total), f"{ours.name} cell_leakage_power"
            )
    elif reference.cell_leakage_power is not None:
        missing.append("cell_leakage_power: in the reference but not in ours")
    if not ours.leakage and reference.leakage:
        missing.append("leakage_power: in the reference but not in ours")
    return {"states": states, "cell_leakage_power": total}


def _safe_eval(expr: Any, state: dict[str, bool]) -> bool:
    try:
        return bool(expr.evaluate(state))
    except KeyError:
        return False
