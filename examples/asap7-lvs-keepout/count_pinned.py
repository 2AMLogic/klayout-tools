import re
import sys
from collections import Counter

from klayout_tools.lvs_known_limits import KNOWN_UNSUPPORTED_ASAP7_CELLS

VERILOG_INSTANCE = re.compile(
    r"^\s*(?!module\b)(\w+)\s+(?:\\\S+|[A-Za-z_$][\w$]*)\s*\(", re.M
)
DEF_COMPONENT = re.compile(r"^\s*-\s+\S+\s+(\S+)\s+\+", re.M)

for path in sys.argv[1:]:
    pattern = DEF_COMPONENT if path.endswith(".def") else VERILOG_INSTANCE
    types = Counter(pattern.findall(open(path).read()))
    pinned = {
        cell: n
        for cell, n in sorted(types.items())
        if cell in KNOWN_UNSUPPORTED_ASAP7_CELLS
    }
    print(f"{path}: {sum(types.values())} instances, pinned {pinned or 0}")
