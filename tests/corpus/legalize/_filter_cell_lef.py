#!/usr/bin/env python3
"""Trim a full standard-cell LEF down to only the MACRO blocks referenced by
one or more DEF files' COMPONENTS sections -- used by regenerate.sh to keep
the checked-in fixture small (the full `sky130_fd_sc_hd.lef` is ~1.4 MB;
`gcd`+`modexp` together reference under 70 of its ~440 MACROs). Every
retained MACRO block's own text is copied verbatim (byte-identical to the
real SkyWater LEF, Apache-2.0) -- this only removes unreferenced blocks, it
never edits a kept one.

Usage: _filter_cell_lef.py <full_cell.lef> <out.lef> <def1> [<def2> ...]
"""

from __future__ import annotations

import re
import sys

_COMPONENT_RE = re.compile(r"^\s*-\s+\S+\s+(\S+)\s")


def referenced_macros(def_path: str) -> set[str]:
    macros: set[str] = set()
    in_components = False
    with open(def_path, encoding="utf-8", errors="replace") as handle:
        for line in handle:
            stripped = line.strip()
            if stripped.startswith("COMPONENTS "):
                in_components = True
                continue
            if stripped == "END COMPONENTS":
                in_components = False
                continue
            if in_components:
                match = _COMPONENT_RE.match(line)
                if match:
                    macros.add(match.group(1))
    return macros


def filter_lef(full_lef_path: str, wanted: set[str]) -> str:
    with open(full_lef_path, encoding="utf-8", errors="replace") as handle:
        text = handle.read()

    header_end = text.index("\nMACRO ") + 1
    header = text[:header_end]

    out = [header.rstrip("\n")]
    for block_match in re.finditer(
        r"^MACRO (\S+)\n(.*?)\nEND \1\n", text, re.DOTALL | re.MULTILINE
    ):
        name = block_match.group(1)
        if name in wanted:
            out.append(block_match.group(0).rstrip("\n"))
    out.append("END LIBRARY\n")
    return "\n\n".join(out[:-1]) + "\n\n" + out[-1]


def main() -> int:
    full_lef_path, out_path, *def_paths = sys.argv[1:]
    wanted: set[str] = set()
    for def_path in def_paths:
        wanted |= referenced_macros(def_path)

    filtered = filter_lef(full_lef_path, wanted)
    with open(out_path, "w", encoding="utf-8") as handle:
        handle.write(filtered)

    found = len(re.findall(r"^MACRO ", filtered, re.MULTILINE))
    print(f"wrote {out_path}: {found}/{len(wanted)} referenced macros found")
    if found != len(wanted):
        missing = wanted - {
            m.group(1) for m in re.finditer(r"^MACRO (\S+)", filtered, re.MULTILINE)
        }
        print(
            f"warning: missing macros in source LEF: {sorted(missing)}", file=sys.stderr
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
