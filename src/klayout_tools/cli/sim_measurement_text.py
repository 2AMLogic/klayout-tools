"""Text-mode measurement header for ``klt sim`` (issue #5385)."""

from __future__ import annotations

from ..sim_process_legs import print_value_table


def print_measurement_head(m: dict, corners: list[dict]) -> None:
    """Name/status/worst-case line, then the per-corner value rows."""
    worst = m["worst_case"]
    if worst is not None:
        worst_desc = (
            f"worst={worst['value']!r} @ {worst['corner_id']} "
            f"(margin={worst['margin']!r})"
        )
    else:
        worst_desc = "worst=-"
    print(f"  {m['name']} [{m['status']}]  {worst_desc}")
    print_value_table(m["name"], corners)
