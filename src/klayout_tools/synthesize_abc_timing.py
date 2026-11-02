"""ABC ``stime`` log parsing for ``klt synthesize`` (issues #807, #4743).

Split out of ``synthesize.py`` so the per-region attribution
(``timing.regions``) does not grow that file past its size ceiling.
"""

from __future__ import annotations

import re
from typing import NamedTuple

#: ABC's own ``stime -p`` summary line, as it reaches the Yosys log (and,
#: via ``tee -q -o``, the captured ABC log this module parses):
#: ``ABC: WireLoad = "none"  Gates = 297 ( 5.7 %)   Cap = 6.2 ff ( 12.4 %)
#: Area = 1986.91 ( 66.3 %)   Delay = 2485.93 ps  ( 32.3 %)``. Verified live
#: (Yosys 0.68+48, sky130_fd_sc_hd, issue #807).
_ABC_STIME_RE = re.compile(
    r'WireLoad\s*=\s*"(?P<wire_load>[^"]*)".*?Delay\s*=\s*(?P<delay_ps>[\d.]+)\s*ps'
)

#: Yosys's per-region banner in the captured ABC log (verified live against
#: Yosys 0.68+; issue #4743): ``4.1. Extracting gate netlist of module
#: `\top' to `<abc-temp-dir>/input.blif'..``. The most recent one before an
#: ``stime`` line names the module that ABC run mapped. Yosys escapes public
#: names with a leading backslash, which is stripped.
_ABC_MODULE_RE = re.compile(r"Extracting gate netlist of module `\\?([^']+)'")


class AbcRegion(NamedTuple):
    """One ``stime`` report: owning module (or ``None``), delay, wire load."""

    module: str | None
    delay_ps: float
    wire_load: str


def parse_abc_regions(log_text: str) -> list[AbcRegion]:
    """Every ``stime`` report in ``log_text``, in log order."""
    modules = [(m.start(), m.group(1)) for m in _ABC_MODULE_RE.finditer(log_text)]
    regions: list[AbcRegion] = []
    for match in _ABC_STIME_RE.finditer(log_text):
        preceding = [name for pos, name in modules if pos < match.start()]
        regions.append(
            AbcRegion(
                preceding[-1] if preceding else None,
                float(match.group("delay_ps")),
                match.group("wire_load"),
            )
        )
    return regions
