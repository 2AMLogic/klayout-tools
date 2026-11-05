"""Virtual-clock support for place-and-route (issue #4913).

``request.constraints.clock_period_ns`` without ``clock_port`` is a *virtual
clock*: a purely combinational block has no clock port, but still needs a
period so ``input_delay_ns``/``output_delay_ns`` constrain its paths and
``route`` reports a real worst slack instead of the unconstrained sentinel.
"""

from __future__ import annotations

from typing import Any

VIRTUAL_CLOCK_NAME = "klt_virtual_clk"


def validate_clock_constraints(
    constraints: dict[str, Any], error_cls: type[Exception]
) -> tuple[str | None, float | None]:
    """The ``clock_port`` / ``clock_period_ns`` pair of ``request.constraints``.

    A period alone is a virtual clock; a port alone is an error (a port needs
    a period). Both omitted -> ``(None, None)``.
    """
    clock_port = constraints.get("clock_port")
    clock_period_ns = constraints.get("clock_period_ns")
    if clock_port is None and clock_period_ns is None:
        return None, None
    if clock_port is not None and not (isinstance(clock_port, str) and clock_port):
        raise error_cls("request.constraints.clock_port must be a non-empty string")
    if not (
        isinstance(clock_period_ns, (int, float))
        and not isinstance(clock_period_ns, bool)
        and clock_period_ns > 0
    ):
        raise error_cls("request.constraints.clock_period_ns must be a positive number")
    return clock_port, float(clock_period_ns)


def virtual_clock_lines(clock_period_ns: float | None, unit_line: str) -> list[str]:
    """``create_clock`` for a port-less clock (``[]`` without a period)."""
    if clock_period_ns is None:
        return []
    return [
        unit_line,
        f"create_clock -name {VIRTUAL_CLOCK_NAME} -period {clock_period_ns}",
    ]


def virtual_io_delay_lines(
    input_delay_ns: float | None,
    output_delay_ns: float | None,
) -> list[str]:
    """``set_input_delay``/``set_output_delay`` against the virtual clock.

    No port to exclude, so the input delay applies to every input. Request
    validation guarantees a period whenever a delay is set.
    """
    lines = []
    if input_delay_ns is not None:
        lines.append(
            f"set_input_delay {input_delay_ns} -clock {VIRTUAL_CLOCK_NAME} [all_inputs]"
        )
    if output_delay_ns is not None:
        lines.append(
            f"set_output_delay {output_delay_ns} "
            f"-clock {VIRTUAL_CLOCK_NAME} [all_outputs]"
        )
    return lines
