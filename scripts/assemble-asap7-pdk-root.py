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
from pathlib import Path

from klayout_tools.liberty_merge import LibertyMergeError, _find_matching, merge_liberty

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


def preserve_pin_limits(text: str) -> tuple[str, int]:
    """Move the SIMPLE library's inherited 4000 ps limit onto its signal pins."""
    default = re.compile(r"(?m)^(\s*default_max_transition\s*:\s*)4000(\s*;)")
    if not default.search(text):
        return text, 0
    text = default.sub(r"\g<1>320\2", text, count=1)
    pin = re.compile(r"(?m)^([ \t]*)pin\s*\([^()\n]+\)\s*\{")
    pieces = []
    start = 0
    count = 0
    for match in pin.finditer(text):
        end = _find_matching(text, match.end() - 1, "{", "}")
        if end == len(text):
            raise ValueError("unclosed Liberty pin group")
        if not re.search(r"(?m)^\s*max_transition\s*:", text[match.end() : end]):
            pieces.extend(
                [
                    text[start : match.end()],
                    "\n" + match[1] + "  max_transition : 4000;",
                ]
            )
            start = match.end()
            count += 1
    return "".join([*pieces, text[start:]]), count


def align_corrected_ff(text: str, reference: str) -> tuple[str, dict]:
    """Match the corrected SIMPLE library's names to the other RVT/FF groups."""
    # Only the identifier changes. merge_liberty still compares process,
    # voltage, temperature and every other operating-condition attribute.
    old, new = "FF_0.77_0", "PVT_0P77V_0C"
    text = re.sub(r"(?<![\w.])FF_0\.77_0(?![\w.])", new, text)
    # The separately characterized SIMPLE file reuses template names from
    # SEQ for different tables. Keep both definitions and each cell's lookup.
    templates = ("delay_template_7x7", "power_template_7x7")
    renamed = {
        name: f"simple_ff_250407_{name}"
        for name in templates
        if re.search(rf"\(\s*{name}\s*\)", text)
    }
    for name, replacement in renamed.items():
        text = re.sub(rf"(?<=\()\s*{re.escape(name)}\s*(?=\))", replacement, text)
    additions = []
    for kind in ("input_voltage", "output_voltage"):
        # Do not supply electrical definitions to a pin which references them.
        # The corrected SIMPLE file uses neither of these named groups.
        if re.search(rf"\b{kind}\b", text):
            continue
        item = re.search(rf"(?m)^[ \t]*{kind}\s*\([^()]+\)\s*\{{", reference)
        if item:
            end = _find_matching(reference, item.end() - 1, "{", "}")
            additions.append(reference[item.start() : end + 1])
    first_cell = re.search(r"(?m)^[ \t]*cell\s*\(", text)
    if first_cell is None:
        raise ValueError("corrected RVT FF SIMPLE Liberty contains no cells")
    pos = first_cell.start()
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

        copy(source / PROCESS / LAYER_MAP, "klayout/tech/asap7.map")
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
