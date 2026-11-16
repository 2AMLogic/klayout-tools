"""`timing.regions` per-region ABC critical paths (issue #4743).

Split out of `tests/test_synthesize.py` to respect the per-file size ratchet.
"""

from __future__ import annotations

from klayout_tools import synthesize

_ABC_STIME_LINE = (
    'ABC: WireLoad = "none"  Gates =    297 (  5.7 %)   Cap =  6.2 ff ( 12.4 %)'
    "   Area =     1986.91 ( 66.3 %)   Delay =  2485.93 ps  ( 32.3 %)               "
)


def _banner(module: str) -> str:
    return (
        f"4.1. Extracting gate netlist of module `\\{module}' "
        "to `<abc-temp-dir>/input.blif'.."
    )


def test_timing_regions_attribute_each_stime_to_its_module(tmp_path):
    """Issue #4743: `timing.regions` lists every `stime` report in log order,
    named by the preceding `Extracting gate netlist of module` banner; the
    maximum equals `critical_path_ps`; a banner-less report gets `null`."""
    log = tmp_path / "abc.log"
    log.write_text(
        "\n".join(
            [
                _ABC_STIME_LINE.replace("2485.93 ps", "500.00 ps"),  # no banner
                _banner("sub"),
                _ABC_STIME_LINE.replace("2485.93 ps", "3910.55 ps"),
                _banner("top"),
                _ABC_STIME_LINE.replace("2485.93 ps", "1204.10 ps"),
            ]
        )
    )

    timing = synthesize._read_abc_timing(str(log), 1600)

    assert timing["regions"] == [
        {"module": None, "critical_path_ps": 500.0},
        {"module": "sub", "critical_path_ps": 3910.55},
        {"module": "top", "critical_path_ps": 1204.1},
    ]
    assert (
        max(r["critical_path_ps"] for r in timing["regions"])
        == (timing["critical_path_ps"])
    )
    assert timing["delay_target_ps"] == 1600


def test_timing_regions_single_region_and_no_stime(tmp_path):
    log = tmp_path / "abc.log"
    log.write_text(_banner("top") + "\n" + _ABC_STIME_LINE)
    timing = synthesize._read_abc_timing(str(log), None)
    assert timing["regions"] == [{"module": "top", "critical_path_ps": 2485.93}]

    log.write_text(_banner("top") + "\nABC: nothing here\n")
    assert synthesize._read_abc_timing(str(log), None) is None
