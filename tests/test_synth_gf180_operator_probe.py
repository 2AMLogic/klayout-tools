"""Pinned golden regression for `klt synthesize` on the gf180-dx7 operator
probe (issue #3069, part of #2281).

The fixture under `tests/corpus/synth_gf180_operator_probe/` vendors three
files from `2AMLogic/gf180-dx7` @ `eb74e94f` (Apache-2.0) so the offline
checks below need no network or separate checkout. The synthesis test is
optional: it skips unless `yosys` and the gf180mcu 7t5v0 `tt_025C_5v00`
liberty are installed. See that directory's `README.md` for provenance, the
upstream-flow comparison and why the golden is a klt-today measurement and
not the historical 46,939.96 um2 reference.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from klayout_tools import pdk as pdk_module
from klayout_tools import pdk_cells
from klayout_tools.synthesize import run_synthesize

_FIXTURE = Path(__file__).parent / "corpus" / "synth_gf180_operator_probe"
_TOP = "operator_probe"
_CELL_LIBRARY = "gf180mcu_fd_sc_mcu7t5v0"
_CORNER = "tt_025C_5v00"

# git blob SHA-1 (as listed by the upstream repo) and SHA-256 of each vendored file.
_PINNED = {
    "operator_probe.v": (
        "2aa79052d36e1a47f4e5d1876e200336be02b2e2",
        "c20277c0c39258250a4acaa259847f2395e0e31f894d34ea92d9a4e710f10f04",
    ),
    "sinlog_table.hex": (
        "5c8953f5e2a209649f3b5370a103a28ffedd188f",
        "91182672af773c5df0ba41b76bc89b74b6268cc233f5b2659eb3e3c6ac836ea3",
    ),
    "sinexp_table.hex": (
        "eddb0066b6ab751c8faf9a7e776698b96937b337",
        "dc3b3a93db54df5c7d41ca1ad38e6c67e6ba7b8a891c5c2e0be4cba7856794f7",
    ),
}

# `klt synthesize` measured on the pinned inputs (Yosys 0.67+post b8e7da6f,
# gf180mcuD liberty sha256 330aa943...). NOT the historical reference.
_GOLDEN = {"area_um2": 50968.1536, "instance_count": 2686, "dffq_1": 99}
# Area/cell tolerance absorbs Yosys/ABC drift across builds; the flop count is
# fixed by the RTL's declared state, so it is exact.
_TOLERANCE = 0.03


def _check_against_golden(report: dict) -> None:
    dffq = report["instance_counts_by_type"].get(f"{_CELL_LIBRARY}__dffq_1", 0)
    assert dffq == _GOLDEN["dffq_1"], f"dffq_1 count {dffq} != {_GOLDEN['dffq_1']}"
    for key in ("area_um2", "instance_count"):
        got, want = report[key], _GOLDEN[key]
        assert abs(got - want) <= _TOLERANCE * want, (
            f"{key} {got} outside {_TOLERANCE:.0%} of golden {want}"
        )


def _git_blob_sha1(data: bytes) -> str:
    # git object id, not a security use of SHA-1
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()  # noqa: S324


def _find_gf180_library() -> tuple[str, str] | None:
    """(pdk root, variant) of an install shipping the 7t5v0 liberty, preferring
    gf180mcuD (the variant upstream's flow reads)."""
    found = []
    for install in pdk_module.list_pdks()["installs"]:
        for variant in install["variants"]:
            try:
                libraries = pdk_cells.list_cell_libraries(
                    variant=variant["name"], root=install["root"]
                )
            except pdk_module.PdkNotFoundError:
                continue
            if any(lib["name"] == _CELL_LIBRARY for lib in libraries["libraries"]):
                found.append((install["root"], variant["name"]))
    found.sort(key=lambda rv: rv[1] != "gf180mcuD")
    return found[0] if found else None


def test_fixture_hashes_and_license():
    for name, (blob_sha1, sha256) in _PINNED.items():
        data = (_FIXTURE / name).read_bytes()
        assert _git_blob_sha1(data) == blob_sha1, name
        assert hashlib.sha256(data).hexdigest() == sha256, name
    assert "Apache License" in (_FIXTURE / "LICENSE").read_text(encoding="utf-8")
    readme = (_FIXTURE / "README.md").read_text(encoding="utf-8")
    assert "eb74e94f0f2bbc0a54e15ce08e1d9c975912f841" in readme
    for _, sha256 in _PINNED.values():
        assert sha256 in readme


def test_golden_check_fails_outside_tolerance():
    ok = {
        "area_um2": _GOLDEN["area_um2"],
        "instance_count": _GOLDEN["instance_count"],
        "instance_counts_by_type": {f"{_CELL_LIBRARY}__dffq_1": 99},
    }
    _check_against_golden(ok)
    for mutation in (
        {"area_um2": _GOLDEN["area_um2"] * 1.05},
        {"instance_count": int(_GOLDEN["instance_count"] * 0.9)},
        {"instance_counts_by_type": {f"{_CELL_LIBRARY}__dffq_1": 98}},
    ):
        with pytest.raises(AssertionError):
            _check_against_golden({**ok, **mutation})


@pytest.mark.skipif(shutil.which("yosys") is None, reason="yosys is not installed")
def test_operator_probe_golden_real_yosys(tmp_path, monkeypatch):
    located = _find_gf180_library()
    if located is None:
        pytest.skip(
            f"no PDK install ships {_CELL_LIBRARY} ({_CORNER}); install gf180mcu "
            "(e.g. `volare enable --pdk gf180mcu`) and set PDK_ROOT/PDK"
        )
    root, variant = located
    monkeypatch.setenv("PDK_ROOT", root)
    monkeypatch.setenv("PDK", variant)

    # Mirror the upstream repo layout: the RTL `$readmemh`s
    # `reference/tables/*.hex` relative to the synthesis cwd, which `klt
    # synthesize` sets to the request's repo root (nearest `.git` ancestor).
    (tmp_path / ".git").mkdir()
    (tmp_path / "rtl").mkdir()
    (tmp_path / "reference" / "tables").mkdir(parents=True)
    shutil.copy(_FIXTURE / "operator_probe.v", tmp_path / "rtl")
    for table in ("sinlog_table.hex", "sinexp_table.hex"):
        shutil.copy(_FIXTURE / table, tmp_path / "reference" / "tables")
    request = tmp_path / "synth.json"
    request.write_text(
        json.dumps(
            {
                "sources": ["rtl/operator_probe.v"],
                "hdl_toplevel": _TOP,
                "pdk": {"cell_library": _CELL_LIBRARY, "corner": _CORNER},
            }
        ),
        encoding="utf-8",
    )

    report = run_synthesize(str(request))

    assert report["status"] == "ok"
    # The main Yosys run reads both tables via cwd-relative `$readmemh` and
    # aborts if either is missing, so reaching the golden check below proves
    # they resolved from the temp repo root (not from the pytest cwd).
    _check_against_golden(report)


@pytest.mark.skipif(shutil.which("yosys") is None, reason="yosys is not installed")
def test_rom_tables_unresolved_outside_repo_layout(tmp_path):
    """The tables are cwd-relative: yosys run from a directory without
    `reference/tables/` fails, so the layout above is load-bearing."""
    proc = subprocess.run(
        ["yosys", "-q", "-p", f"read_verilog {_FIXTURE / 'operator_probe.v'}"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    assert proc.returncode != 0
    assert "readmemh" in proc.stdout + proc.stderr
