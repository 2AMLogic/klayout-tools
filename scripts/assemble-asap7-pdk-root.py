#!/usr/bin/env python3
"""Assemble a klt descriptor root from the public ASAP7 7.5-track checkouts."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import NamedTuple

from klayout_tools.liberty_merge import (
    LibertyMergeError,
    _find_matching,
    _next_significant,
    _tokens,
    merge_liberty,
)
from klayout_tools.place_and_route_gds_merge import _lef_net_layer_gds_map

CELLS = "asap7sc7p5t_28"
PROCESS = "asap7_pdk_r1p7"
LAYER_MAP = "cdslib/asap7_TechLib_10/asap7_fromAPR.layermap"
TECH_LEF = "techlef_misc/asap7_tech_1x_201209.lef"
FLAVOURS = {"RVT": "R", "LVT": "L", "SLVT": "SL", "SRAM": "SRAM"}
GROUPS = {
    "AO": "211120",
    "INVBUF": "220122",
    "OA": "211120",
    "SEQ": "220123",
    "SIMPLE": "211120",
}


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def commits(source: Path) -> dict[str, str]:
    result = {}
    for name in (".", CELLS, PROCESS):
        checkout = source / name
        cmd = ["git", "-C", str(checkout), "rev-parse", "--show-toplevel", "HEAD"]
        status = subprocess.run(cmd, capture_output=True, text=True)
        lines = status.stdout.splitlines()
        if (
            status.returncode
            or len(lines) != 2
            or Path(lines[0]).resolve() != checkout.resolve()
        ):
            raise ValueError(f"expected an initialized Git checkout at {checkout}")
        result[name] = lines[1]
    return result


def write_json(path: Path, value: dict) -> None:
    path.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def members(
    text: str, names: set[str] | None, depth: int | None = None
) -> Iterator[tuple[int, int, str]]:
    """Locate named members and their delimiters outside strings and comments."""
    nesting = 0
    for kind, start, end in _tokens(text):
        if kind == "{":
            nesting += 1
        elif kind == "}":
            nesting -= 1
        elif (depth is None or nesting == depth) and (
            names is None or text[start:end] in names
        ):
            pos, delimiter = _next_significant(text, end)
            yield start, pos, delimiter


class GroupHeader(NamedTuple):
    start: int
    args: slice
    brace: int


def group_headers(
    text: str, names: set[str] | None, depth: int | None = None
) -> Iterator[GroupHeader]:
    for start, opening, delimiter in members(text, names, depth):
        if delimiter == "(":
            close = _find_matching(text, opening, "(", ")")
            brace, delimiter = _next_significant(text, close + 1)
            if delimiter == "{":
                yield GroupHeader(start, slice(opening + 1, close), brace)


def attribute_start(text: str, name: str, depth: int) -> int | None:
    for _, colon, delimiter in members(text, {name}, depth):
        if delimiter == ":":
            return _next_significant(text, colon + 1)[0]
    return None


def preserve_pin_limits(text: str) -> tuple[str, int]:
    """Move the SIMPLE library's inherited 4000 ps limit onto its signal pins."""
    default = attribute_start(text, "default_max_transition", depth=1)
    if default is None or not text.startswith("4000", default):
        return text, 0
    end = default + len("4000")
    if _next_significant(text, end)[1] != ";":
        return text, 0
    text = text[:default] + "320" + text[end:]
    pieces = []
    start = 0
    count = 0
    for pin in group_headers(text, {"pin"}):
        end = _find_matching(text, pin.brace, "{", "}")
        if end == len(text):
            raise ValueError("unclosed Liberty pin group")
        body_start = pin.brace + 1
        if attribute_start(text[body_start:end], "max_transition", depth=0) is None:
            pieces.extend([text[start:body_start], " max_transition : 4000;"])
            start = body_start
            count += 1
    return "".join([*pieces, text[start:]]), count


def template_args(text: str) -> Iterator[slice]:
    """The definitions in the header and lookup tables inside cell arcs, in file order."""
    definitions = ("lu_table_template", "power_lut_template")
    for group in group_headers(text, {*definitions, "timing", "internal_power"}):
        if text.startswith(definitions, group.start):
            yield group.args
            continue
        start = group.brace + 1
        end = _find_matching(text, group.brace, "{", "}")
        for table in group_headers(text[start:end], names=None, depth=0):
            yield slice(start + table.args.start, start + table.args.stop)


def rename_ff_templates(text: str) -> tuple[str, dict[str, str]]:
    """Keep each corrected SIMPLE timing or power table bound to its own axes."""
    identifier = re.compile(r'("?)(?P<name>(?:delay|power)_template_7x7)\1')
    pieces = []
    start = 0
    renamed = {}
    for args in template_args(text):
        arg, _ = _next_significant(text, args.start)
        match = identifier.match(text, arg)
        if match and _next_significant(text, match.end())[0] == args.stop:
            name = match["name"]
            renamed[name] = f"simple_ff_250407_{name}"
            pieces.extend([text[start : match.start("name")], renamed[name]])
            start = match.end("name")
    return "".join([*pieces, text[start:]]), renamed


def align_corrected_ff(text: str, reference: str) -> tuple[str, dict]:
    """Match the corrected SIMPLE library's names to the other RVT/FF groups."""
    # Only the identifier changes. merge_liberty still compares process,
    # voltage, temperature and every other operating-condition attribute.
    old, new = "FF_0.77_0", "PVT_0P77V_0C"
    text = re.sub(r"(?<![\w.])FF_0\.77_0(?![\w.])", new, text)
    text, renamed = rename_ff_templates(text)
    additions = []
    for kind in ("input_voltage", "output_voltage"):
        # Do not supply electrical definitions to a pin which references them.
        # The corrected SIMPLE file uses neither of these named groups.
        if next(members(text, {kind}), None) is not None:
            continue
        item = next(group_headers(reference, {kind}, depth=1), None)
        if item:
            end = _find_matching(reference, item.brace, "{", "}")
            additions.append(reference[item.start : end + 1])
    first_cell = next(group_headers(text, {"cell"}, depth=1), None)
    if first_cell is None:
        raise ValueError("corrected RVT FF SIMPLE Liberty contains no cells")
    pos = first_cell.start
    text = text[:pos] + "\n".join(additions) + "\n" + text[pos:]
    return text, {
        "operating_condition_alias": {old: new},
        "unused_voltage_groups_added": len(additions),
        "template_aliases": renamed,
    }


def liberty_source(source: Path, group: str, vt: str, corner: str) -> Path:
    folder = source / CELLS / "LIB" / "NLDM"
    prefix = f"asap7sc7p5t_{group}_{vt}_{corner}_nldm_"
    revisions = [GROUPS[group]]
    if (group, vt, corner) == ("SIMPLE", "RVT", "FF"):
        # The 250407 revision corrects the 211120 FF file's TT header.
        revisions.insert(0, "250407")
    for revision in revisions:
        plain = folder / f"{prefix}{revision}.lib"
        archived = plain.with_suffix(".lib.7z")
        if archived.is_file():
            return archived
        if plain.is_file():
            return plain
    raise ValueError(f"missing Liberty source {plain} or {archived}")


def unpack(source: Path, data: bytes, scratch: Path) -> bytes:
    if source.suffix != ".7z":
        return data
    binary = shutil.which("7zz") or shutil.which("7z")
    if binary is None:
        raise ValueError(f"install 7-Zip (7zz or 7z) to unpack {source}")
    # Read only the expected member, to stdout, from the exact hashed bytes.
    archive = scratch / "source.7z"
    archive.write_bytes(data)
    proc = subprocess.run(
        [binary, "x", "-so", str(archive), source.stem], capture_output=True
    )
    if proc.returncode or not proc.stdout:
        error = proc.stderr.decode(errors="replace").strip()
        raise ValueError(f"cannot unpack member {source.stem} from {source}: {error}")
    return proc.stdout


def stage_layer_map(source: Path, dest: Path, read: Callable[[Path], bytes]) -> None:
    data = read(source)
    # Git with core.symlinks=false checks out the upstream link as its basename.
    if re.fullmatch(rb"[\w.-]+\.layermap", data.strip()):
        data = read(source.with_name(data.strip().decode("ascii")))
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)
    routing = _lef_net_layer_gds_map(str(dest))
    for layer, gds in (("M2", 20), ("M3", 30)):
        if routing.get(layer) != (gds, 0):
            raise ValueError(f"{source}: expected {layer} NET mapping to GDS {gds}/0")


def assemble(
    source: Path, output: Path, flavours: list[str], corners: list[str]
) -> None:
    source = source.resolve()
    if output.exists() or output.is_symlink():
        raise ValueError(f"output already exists: {output}")
    revisions = commits(source)
    inputs: dict[str, str] = {}
    normalizations: dict[str, dict] = {}
    liberty: dict[str, dict] = {}
    views: dict[str, dict] = {}
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=output.parent, prefix=".asap7-") as tmp:
        scratch = Path(tmp)
        root = scratch / "root"
        root.mkdir()

        def read(path: Path) -> bytes:
            data = path.read_bytes()
            inputs[path.relative_to(source).as_posix()] = digest(data)
            return data

        def copy(origin: Path, dest: str) -> str:
            path = root / dest
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(read(origin))
            return dest

        stage_layer_map(
            source / PROCESS / LAYER_MAP, root / "klayout/tech/asap7.map", read
        )
        for vt in flavours:
            lib = f"asap7sc7p5t_{vt.lower()}"
            prefix = f"{CELLS}_{FLAVOURS[vt]}"
            physical = source / CELLS
            base = f"libs/{lib}"
            views[lib] = {
                "cell_lef": copy(
                    physical / "LEF" / f"{prefix}_1x_220121a.lef",
                    f"{base}/lef/{lib}.lef",
                ),
                "gds": copy(
                    physical / "GDS" / f"{prefix}_220121a.gds", f"{base}/gds/{lib}.gds"
                ),
                "tech_lef": {
                    "nom": copy(physical / TECH_LEF, f"{base}/techlef/{lib}__nom.tlef")
                },
            }
            liberty[lib] = {}
            for corner in corners:
                groups = []
                for group in GROUPS:
                    path = liberty_source(source, group, vt, corner)
                    raw = unpack(path, read(path), scratch)
                    text = raw.decode("utf-8")
                    if group == "SIMPLE":
                        text, count = preserve_pin_limits(text)
                        if text.encode() != raw:
                            normalizations[path.relative_to(source).as_posix()] = {
                                "default_max_transition": {"from": 4000, "to": 320},
                                "pins_with_inherited_limit_made_explicit": count,
                            }
                    if path.name.startswith("asap7sc7p5t_SIMPLE_RVT_FF_nldm_250407."):
                        text, changes = align_corrected_ff(
                            text, Path(groups[0]).read_text(encoding="utf-8")
                        )
                        normalizations.setdefault(
                            path.relative_to(source).as_posix(), {}
                        ).update(changes)
                    normalized = scratch / path.name.removesuffix(".7z")
                    normalized.write_text(text, encoding="utf-8")
                    groups.append(str(normalized))
                merged = merge_liberty(groups)
                rel = f"{base}/lib/{lib}__{corner}.lib"
                (root / rel).parent.mkdir(exist_ok=True)
                shutil.copyfile(merged, root / rel)
                liberty[lib][corner] = rel
        write_json(
            root / "klt-pdk.json",
            {
                "schema_version": 1,
                "variant": "asap7",
                "assets": {"libs_ref": "libs", "klayout": "klayout"},
                "liberty": liberty,
                "views": views,
            },
        )
        outputs = {
            p.relative_to(root).as_posix(): digest(p.read_bytes())
            for p in root.rglob("*")
            if p.is_file()
        }
        write_json(
            root / "klt-pdk-sources.json",
            {
                "schema_version": 1,
                "commits": revisions,
                "inputs": inputs,
                "outputs": outputs,
                "normalizations": normalizations,
            },
        )
        root.rename(output)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--asap7",
        required=True,
        type=Path,
        help="ASAP7 checkout with both 7.5-track and process submodules initialized",
    )
    parser.add_argument(
        "-o", "--output", required=True, type=Path, help="new descriptor-root directory"
    )
    parser.add_argument(
        "--vt",
        nargs="+",
        choices=FLAVOURS,
        default=["RVT"],
        help="threshold-voltage flavours (default RVT)",
    )
    parser.add_argument(
        "--corner",
        nargs="+",
        choices=("TT", "SS", "FF"),
        default=["TT"],
        help="process corners (default TT)",
    )
    args = parser.parse_args()
    try:
        assemble(
            args.asap7,
            args.output,
            list(dict.fromkeys(args.vt)),
            list(dict.fromkeys(args.corner)),
        )
    except (OSError, ValueError, LibertyMergeError) as exc:
        print(f"ASAP7 assembly failed: {exc}", file=sys.stderr)
        return 1
    print(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
