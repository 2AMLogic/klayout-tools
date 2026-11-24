"""Text-mode measurement header for ``klt sim`` (issue #5385)."""

from __future__ import annotations

from ..sim_process_legs import print_value_table


def print_measurement_head(m: dict, corners: list[dict]) -> None:
    """Name/status/worst-case line, then the per-corner value rows."""
    if m.get("statistic") == "probit_sigma":
        print(
            f"  {m['name']} [{m['status']}]  {m['grid']}={m['value']!r} "
            f"{m['unit']} (max={m['limits']['max']!r}, source={m['source']})"
        )
        for fit in m["by_corner"]:
            print(f"    {fit['corner_id']}: {fit['reason'] or fit['sigma']}")
        return
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
