"""Liberty (``.lib``) **write** path: render an NLDM timing model as text
(issue #2502, the emission half of ``klt characterize``).

Nothing in this tree emitted Liberty before this module. ``klt sta``,
``klt synthesize``, and ``klt place-and-route`` all *resolve* a vendor
``.lib`` (``pdk_cells.resolve_liberty_for_cell_library``), and
``native/statime/src/liberty.rs`` *parses* one (its ``Table2D``/``Pin``/
``Cell`` model, consumed by ``nldm.rs``'s bilinear interpolation) -- both
read-only. This module is the counterpart: plain data in (the dataclasses
below), Liberty text out.

**Why the writer is Python and not Rust.** The obvious alternative was to
extend ``native/statime/src/liberty.rs`` with a serialiser beside its
parser, keeping the format's two halves colocated. Three things decided
against it: (1) the data being written originates in Python -- it is
``klt sim``'s own per-corner measurement report, reshaped -- so a Rust
writer would need a new pyo3 entry point plus a full marshalling layer for
tables that exist only on the Python side, and would reuse nothing from the
Rust parser's model (``Table2D`` there is a *parse target*, with no
serialisation code to share); (2) the round-trip acceptance bar --
"the emitted ``.lib`` parses in ``statime``" -- is satisfied identically
either way, since it exercises the *reader*, wherever the writer lives
(see :func:`klayout_tools.characterize.roundtrip_check`); (3) keeping it
here means the emission path has no Rust toolchain dependency at all, so
``klt characterize`` degrades to "emitted, round-trip not verified" rather
than "cannot emit" on a machine with no built extension.

Scope: exactly the NLDM subset ``klt characterize``'s first increment emits
-- library-level units/thresholds/operating conditions, one
``lu_table_template``, and per-cell pins carrying ``direction``,
``capacitance``, ``function``, and combinational ``timing()`` groups with
``cell_rise``/``cell_fall``/``rise_transition``/``fall_transition``. No
power tables (``rise_power``/``fall_power``/``leakage_power``), no
sequential-cell constraint arcs (``setup_*``/``hold_*``), no ``bus``/
``bundle`` pins, no ``when``-qualified arc splitting. Those are deliberate
omissions of this increment (power/multi-cell: issue #2503), not of the
format -- a consumer of this module should not read the absence of a group
as an assertion about the cell.

Every number is rendered through :func:`format_number` (``%.<n>g``, default
6 significant digits) rather than ``repr``/``str``: a fixed-precision
``%g`` conversion is deterministic across hosts and libms, which
``docs/json-contract.md``'s "Pinned derived artifacts" rule cares about for
anything a caller may commit.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

#: Significant digits every rendered number carries. Matches the precision
#: IHP's own ``sg13g2_stdcell`` ``.lib`` views use for table values, and is
#: well inside double precision for the picosecond-scale delays this writer
#: emits.
DEFAULT_PRECISION = 6

#: Recognised ``timing_sense`` values -- the three
#: ``native/statime/src/liberty.rs``'s ``parse_timing_sense`` distinguishes.
TIMING_SENSES = ("positive_unate", "negative_unate", "non_unate")

#: Recognised ``direction`` values for a :class:`Pin`. ``inout`` is accepted
#: for completeness (the reader models it) but ``klt characterize`` never
#: emits one in this increment.
DIRECTIONS = ("input", "output", "inout")


class LibertyWriteError(Exception):
    """Raised when the data handed to :func:`render_library` cannot be
    rendered as a well-formed Liberty file -- a non-finite table value, a
    table whose ``values`` shape disagrees with its indices, an unknown
    ``timing_sense``/``direction``, or an empty index axis.

    This is deliberately a hard error rather than a best-effort render: a
    ``.lib`` with a ragged ``values`` block parses structurally (the reader
    is a generic group/attribute parser) and then silently interpolates
    against the wrong grid, which is far worse than refusing to write it.
    """


def format_number(value: float, *, precision: int = DEFAULT_PRECISION) -> str:
    """Render ``value`` as a Liberty numeric literal.

    Uses ``%.<precision>g``, which is deterministic for a given double on
    every conforming host (no ``pow``/``exp`` in the conversion path), and
    normalises the two textual forms that are legal C but awkward in
    Liberty: a bare ``-0`` becomes ``0``, and an exponent keeps its ``e``
    lowercase. Raises :class:`LibertyWriteError` for a non-finite value --
    ``nan``/``inf`` would render as an identifier the reader's number parser
    silently drops, turning a failed measurement into a *missing* table
    entry rather than a visible error.
    """
    if not math.isfinite(value):
        raise LibertyWriteError(
            f"cannot render non-finite Liberty value {value!r} -- a failed or "
            "unextractable measurement must be reported as an error, never "
            "written into a lookup table"
        )
    text = f"{float(value):.{precision}g}"
    if text in ("-0", "-0.0"):
        return "0"
    return text


@dataclass(frozen=True)
class Table2D:
    """One NLDM 2D lookup table.

    ``index_1`` is the input net transition axis in the library's time unit,
    ``index_2`` the total output net capacitance axis in its capacitance
    unit, and ``values`` is row-major over ``index_1`` (one row per
    ``index_1`` entry, one column per ``index_2`` entry) -- the same
    orientation ``native/statime/src/liberty.rs``'s ``Table2D`` reads and
    ``nldm.rs`` interpolates over.
    """

    index_1: tuple[float, ...]
    index_2: tuple[float, ...]
    values: tuple[tuple[float, ...], ...]

    def validate(self, where: str) -> None:
        """Raise :class:`LibertyWriteError` unless this table's ``values``
        block is a full ``len(index_1) x len(index_2)`` rectangle."""
        if not self.index_1 or not self.index_2:
            raise LibertyWriteError(
                f"{where}: an NLDM table needs a non-empty index_1 and index_2 axis"
            )
        if len(self.values) != len(self.index_1):
            raise LibertyWriteError(
                f"{where}: table has {len(self.values)} value rows but "
                f"{len(self.index_1)} index_1 entries"
            )
        for row_index, row in enumerate(self.values):
            if len(row) != len(self.index_2):
                raise LibertyWriteError(
                    f"{where}: value row {row_index} has {len(row)} entries "
                    f"but index_2 declares {len(self.index_2)}"
                )


@dataclass(frozen=True)
class TimingArc:
    """One combinational ``timing()`` group on an output pin."""

    related_pin: str
    timing_sense: str
    cell_rise: Table2D
    cell_fall: Table2D
    rise_transition: Table2D
    fall_transition: Table2D
    timing_type: str = "combinational"


@dataclass(frozen=True)
class Pin:
    """One ``pin()`` group."""

    name: str
    direction: str
    capacitance_pf: float | None = None
    function: str | None = None
    max_capacitance_pf: float | None = None
    arcs: tuple[TimingArc, ...] = ()


@dataclass(frozen=True)
class Cell:
    """One ``cell()`` group."""

    name: str
    pins: tuple[Pin, ...]
    area: float | None = None


@dataclass(frozen=True)
class Thresholds:
    """The library-level measurement thresholds every table in the file was
    characterised against.

    Defaults are IHP ``sg13g2_stdcell``'s own (50% delay thresholds, 20/80%
    slew thresholds, no slew derate) -- which are also the most common
    open-PDK convention -- so a caller that does not care gets a file whose
    numbers mean what a reader assumes they mean.
    """

    input_threshold_pct_rise: float = 50.0
    input_threshold_pct_fall: float = 50.0
    output_threshold_pct_rise: float = 50.0
    output_threshold_pct_fall: float = 50.0
    slew_lower_threshold_pct_rise: float = 20.0
    slew_lower_threshold_pct_fall: float = 20.0
    slew_upper_threshold_pct_rise: float = 80.0
    slew_upper_threshold_pct_fall: float = 80.0
    slew_derate_from_library: float = 1.0


@dataclass(frozen=True)
class OperatingConditions:
    """The single PVT point the file's tables describe."""

    name: str
    process: float
    temperature_c: float
    voltage_v: float


@dataclass(frozen=True)
class Library:
    """A whole ``.lib`` file's worth of data.

    ``time_unit``/``capacitive_load_unit`` are rendered verbatim into the
    library header, so every :class:`Table2D` in ``cells`` must already be
    expressed in those units -- this module converts nothing.
    """

    name: str
    operating_conditions: OperatingConditions
    cells: tuple[Cell, ...]
    thresholds: Thresholds = field(default_factory=Thresholds)
    time_unit: str = "1ns"
    capacitive_load_unit: tuple[float, str] = (1.0, "pf")
    voltage_unit: str = "1V"
    current_unit: str = "1uA"
    leakage_power_unit: str = "1pW"
    pulling_resistance_unit: str = "1kohm"
    comment: str | None = None
    #: Name of the single ``lu_table_template`` every table references. The
    #: template is emitted from the first table's own axes; Liberty requires
    #: a table group to name *a* template even when it restates its own
    #: ``index_1``/``index_2`` (which this writer always does, so a reader
    #: that ignores templates -- ``statime``'s does -- is unaffected).
    template_name: str = "klt_char_template"


def render_library(library: Library, *, precision: int = DEFAULT_PRECISION) -> str:
    """Render ``library`` as Liberty text (with a trailing newline).

    Validates first and raises :class:`LibertyWriteError` on any malformed
    input -- see that class for why this is not best-effort.
    """
    if not library.cells:
        raise LibertyWriteError("a Liberty library needs at least one cell")

    lines: list[str] = []
    if library.comment is not None:
        lines.append("/*")
        for comment_line in library.comment.splitlines():
            lines.append(f" * {comment_line}" if comment_line else " *")
        lines.append(" */")
    lines.append(f"library ({library.name}) {{")
    lines.extend(_render_header(library, precision))
    for cell in library.cells:
        lines.extend(_render_cell(cell, library, precision))
    lines.append("}")
    return "\n".join(lines) + "\n"


def _render_header(library: Library, precision: int) -> list[str]:
    conditions = library.operating_conditions
    cap_scale, cap_unit = library.capacitive_load_unit
    thresholds = library.thresholds
    number = _numberer(precision)

    lines = [
        "  delay_model : table_lookup;",
        f'  time_unit : "{library.time_unit}";',
        f'  voltage_unit : "{library.voltage_unit}";',
        f'  current_unit : "{library.current_unit}";',
        f'  leakage_power_unit : "{library.leakage_power_unit}";',
        f'  pulling_resistance_unit : "{library.pulling_resistance_unit}";',
        f"  capacitive_load_unit ({number(cap_scale)}, {cap_unit});",
        f"  nom_process : {number(conditions.process)};",
        f"  nom_temperature : {number(conditions.temperature_c)};",
        f"  nom_voltage : {number(conditions.voltage_v)};",
    ]
    if library.comment is not None:
        # Liberty's own `comment` attribute, in addition to the C-style
        # banner above: a reader that keeps attributes but drops comments
        # (the common case) still carries the provenance line.
        first_line = library.comment.splitlines()[0]
        lines.append(f'  comment : "{_escape(first_line)}";')
    for name, value in (
        ("input_threshold_pct_rise", thresholds.input_threshold_pct_rise),
        ("input_threshold_pct_fall", thresholds.input_threshold_pct_fall),
        ("output_threshold_pct_rise", thresholds.output_threshold_pct_rise),
        ("output_threshold_pct_fall", thresholds.output_threshold_pct_fall),
        ("slew_lower_threshold_pct_rise", thresholds.slew_lower_threshold_pct_rise),
        ("slew_lower_threshold_pct_fall", thresholds.slew_lower_threshold_pct_fall),
        ("slew_upper_threshold_pct_rise", thresholds.slew_upper_threshold_pct_rise),
        ("slew_upper_threshold_pct_fall", thresholds.slew_upper_threshold_pct_fall),
        ("slew_derate_from_library", thresholds.slew_derate_from_library),
    ):
        lines.append(f"  {name} : {number(value)};")

    lines.append(f"  operating_conditions ({conditions.name}) {{")
    lines.append(f"    process : {number(conditions.process)};")
    lines.append(f"    temperature : {number(conditions.temperature_c)};")
    lines.append(f"    voltage : {number(conditions.voltage_v)};")
    lines.append("  }")
    lines.append(f'  default_operating_conditions : "{conditions.name}";')

    template = _first_table(library)
    # The header's `lu_table_template` and `default_max_transition` are
    # derived from this table's axes, *before* `_render_arc` gets to validate
    # anything -- so validate it here too. Without this an empty `index_1`
    # reaches `max()` as a bare `ValueError` instead of the named
    # `LibertyWriteError` every other malformed-table path raises.
    template.validate(f"library '{library.name}' lu_table_template source")
    lines.append(f"  lu_table_template ({library.template_name}) {{")
    lines.append("    variable_1 : input_net_transition;")
    lines.append("    variable_2 : total_output_net_capacitance;")
    lines.append(f'    index_1 ("{_index_text(template.index_1, number)}");')
    lines.append(f'    index_2 ("{_index_text(template.index_2, number)}");')
    lines.append("  }")
    lines.append(f"  default_max_transition : {number(max(template.index_1))};")
    return lines


def _render_cell(cell: Cell, library: Library, precision: int) -> list[str]:
    number = _numberer(precision)
    lines = [f"  cell ({cell.name}) {{"]
    if cell.area is not None:
        lines.append(f"    area : {number(cell.area)};")
    for pin in cell.pins:
        if pin.direction not in DIRECTIONS:
            raise LibertyWriteError(
                f"cell '{cell.name}' pin '{pin.name}': unknown direction "
                f"{pin.direction!r} (expected one of {', '.join(DIRECTIONS)})"
            )
        lines.append(f"    pin ({pin.name}) {{")
        lines.append(f'      direction : "{pin.direction}";')
        if pin.function is not None:
            lines.append(f'      function : "{_escape(pin.function)}";')
        if pin.capacitance_pf is not None:
            lines.append(f"      capacitance : {number(pin.capacitance_pf)};")
        if pin.max_capacitance_pf is not None:
            lines.append(f"      max_capacitance : {number(pin.max_capacitance_pf)};")
        for arc in pin.arcs:
            lines.extend(_render_arc(arc, cell, pin, library, number))
        lines.append("    }")
    lines.append("  }")
    return lines


def _render_arc(
    arc: TimingArc,
    cell: Cell,
    pin: Pin,
    library: Library,
    number,
) -> list[str]:
    if arc.timing_sense not in TIMING_SENSES:
        raise LibertyWriteError(
            f"cell '{cell.name}' pin '{pin.name}' arc from "
            f"'{arc.related_pin}': unknown timing_sense {arc.timing_sense!r} "
            f"(expected one of {', '.join(TIMING_SENSES)})"
        )
    lines = ["      timing () {"]
    lines.append(f'        related_pin : "{arc.related_pin}";')
    lines.append(f"        timing_sense : {arc.timing_sense};")
    lines.append(f"        timing_type : {arc.timing_type};")
    for group, table in (
        ("cell_rise", arc.cell_rise),
        ("rise_transition", arc.rise_transition),
        ("cell_fall", arc.cell_fall),
        ("fall_transition", arc.fall_transition),
    ):
        where = (
            f"cell '{cell.name}' pin '{pin.name}' arc from '{arc.related_pin}' {group}"
        )
        table.validate(where)
        lines.extend(_render_table(group, table, library.template_name, number))
    lines.append("      }")
    return lines


def _render_table(
    group: str,
    table: Table2D,
    template_name: str,
    number,
) -> list[str]:
    lines = [f"        {group} ({template_name}) {{"]
    lines.append(f'          index_1 ("{_index_text(table.index_1, number)}");')
    lines.append(f'          index_2 ("{_index_text(table.index_2, number)}");')
    lines.append("          values ( \\")
    rendered = [
        '            "' + ", ".join(number(value) for value in row) + '"'
        for row in table.values
    ]
    for offset, row_text in enumerate(rendered):
        suffix = ", \\" if offset + 1 < len(rendered) else " \\"
        lines.append(row_text + suffix)
    lines.append("          );")
    lines.append("        }")
    return lines


def _index_text(axis: tuple[float, ...], number) -> str:
    return ", ".join(number(value) for value in axis)


def _numberer(precision: int):
    def number(value: float) -> str:
        return format_number(value, precision=precision)

    return number


def _first_table(library: Library) -> Table2D:
    for cell in library.cells:
        for pin in cell.pins:
            for arc in pin.arcs:
                return arc.cell_rise
    raise LibertyWriteError(
        "a Liberty NLDM library needs at least one timing arc to derive its "
        "lu_table_template axes from"
    )


def _escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace('"', '\\"')
