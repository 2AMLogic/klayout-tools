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


def _print_population_limits(mc: dict) -> None:
    """The dispersion-bound verdict, one line next to the sigma-window line
    (issue #6967) -- a courtesy; the JSON contract is the API. A no-op when
    the measurement declared no ``population_limits``."""
    population = mc.get("population_limits")
    if population is None:
        return
    print(
        f"    mc: population {population['k']:g}*sigma <= "
        f"{population['max']!r} [{population['status']}] "
        f"(margin={population['margin']!r}, "
        f"worst_corner={population['worst_corner']!r})"
    )


def print_monte_carlo(mc: dict | None) -> None:
    if mc is None:
        return
    stats = f"n={mc['n']} mean={mc['mean']!r} sigma={mc['stddev']!r}"
    quantiles = "  ".join(f"{key}={value!r}" for key, value in mc["quantiles"].items())
    print(f"    mc: {stats}  {quantiles}".rstrip())
    window = mc["sigma_window"]
    if window is not None:
        print(
            f"    mc: mean+/-{window['k']:g}sigma = "
            f"[{window['low']!r}, {window['high']!r}] "
            f"[{window['status']}] (margin={window['margin']!r})"
        )
    _print_population_limits(mc)
