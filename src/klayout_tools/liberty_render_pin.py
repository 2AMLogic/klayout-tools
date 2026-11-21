"""Pin-level Liberty group rendering split out of :mod:`liberty_writer`
(size ratchet, issue #4756): the pin-capacitance attributes and the
setup/hold ``timing()`` constraint groups.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .liberty_writer import Table2D


def _error() -> type[Exception]:
    from .liberty_writer import LibertyWriteError  # local: circular import

    return LibertyWriteError


@dataclass(frozen=True)
class ConstraintArc:
    """One setup/hold ``timing()`` group on a data pin (issue #4756).

    ``timing_type`` is ``setup_rising``/``hold_rising`` (or ``*_falling`` for a
    falling-edge clock). Tables are over ``index_1`` = the constrained (data)
    pin transition and ``index_2`` = the related (clock) pin transition, in
    the library's time unit; either table may be ``None`` when that data edge
    was not characterized.
    """

    related_pin: str
    timing_type: str
    rise_constraint: Table2D | None = None
    fall_constraint: Table2D | None = None


def render_pin_capacitance(pin: Any, cell: Any, number: Any) -> list[str]:
    """The pin's ``capacitance`` attributes, in the order a vendor library
    writes them (bare, then the rise/fall split, then ``max_capacitance``).

    The split must be whole or absent: ``native/statime``'s reader resolves
    ``(Some(rise), Some(fall), _)`` to their mean *before* it looks at the
    bare attribute, so emitting a lone half would have it read as the whole
    pin capacitance.
    """
    if (pin.rise_capacitance_pf is None) != (pin.fall_capacitance_pf is None):
        raise _error()(
            f"cell '{cell.name}' pin '{pin.name}': rise_capacitance and "
            "fall_capacitance must be emitted as a pair -- a reader that "
            "prefers the split (native/statime's does) averages the two, "
            "so a lone half would be read as the whole"
        )
    lines = []
    for attribute, value in (
        ("capacitance", pin.capacitance_pf),
        ("rise_capacitance", pin.rise_capacitance_pf),
        ("fall_capacitance", pin.fall_capacitance_pf),
        ("max_capacitance", pin.max_capacitance_pf),
    ):
        if value is not None:
            lines.append(f"      {attribute} : {number(value)};")
    return lines


def constraint_template_lines(
    library: Any, number: Callable[[float], str], index_text: Callable[..., str]
) -> list[str]:
    """The ``lu_table_template`` for setup/hold tables, derived from the first
    constraint table; empty when no pin carries a constraint."""
    template = None
    for cell in library.cells:
        for pin in cell.pins:
            for arc in pin.constraints:
                template = template or arc.rise_constraint or arc.fall_constraint
    if template is None:
        return []
    template.validate(f"library '{library.name}' constraint template source")
    return [
        f"  lu_table_template ({library.constraint_template_name}) {{",
        "    variable_1 : constrained_pin_transition;",
        "    variable_2 : related_pin_transition;",
        f'    index_1 ("{index_text(template.index_1, number)}");',
        f'    index_2 ("{index_text(template.index_2, number)}");',
        "  }",
    ]


def render_constraint(
    arc: ConstraintArc,
    cell: Any,
    pin: Any,
    template_name: str,
    number: Callable[[float], str],
    render_table: Callable[..., list[str]],
) -> list[str]:
    lines = ["      timing () {"]
    lines.append(f'        related_pin : "{arc.related_pin}";')
    lines.append(f"        timing_type : {arc.timing_type};")
    emitted = 0
    for group, table in (
        ("rise_constraint", arc.rise_constraint),
        ("fall_constraint", arc.fall_constraint),
    ):
        if table is None:
            continue
        table.validate(
            f"cell '{cell.name}' pin '{pin.name}' {arc.timing_type} "
            f"from '{arc.related_pin}' {group}"
        )
        lines.extend(render_table(group, table, template_name, number))
        emitted += 1
    if not emitted:
        raise _error()(
            f"cell '{cell.name}' pin '{pin.name}' {arc.timing_type} from "
            f"'{arc.related_pin}': a constraint arc needs at least one table"
        )
    lines.append("      }")
    return lines
