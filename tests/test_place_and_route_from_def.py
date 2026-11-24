"""``klt place-and-route --from-def`` (issue #7721): the DEF→GDS merge as a
standalone local entry point for a *committed* routed stream.

Same tier structure as ``tests/test_place_and_route_def_drcfill.py``'s
end-to-end tier: a synthetic LEF/DEF/GDS fixture with real KLayout readers
and a real fabricated open_pdks-layout install, but driven **through the
public CLI** with an OpenROAD stub that fails the test if any engine
process is ever invoked -- the whole point of the flag is that no stage
runs. The merge result is then compared, geometry and repair reports alike,
against a direct call to the existing merger on the same DEF and views, and
the caller's input DEF plus a pre-existing comparison GDS are checked to
retain their hashes.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from klayout_tools import _openroad_engine, place_and_route, place_and_route_merge_only
from klayout_tools import pdk as pdk_module
from klayout_tools.cli import main
from test_place_and_route import _isolate_pdk
from test_place_and_route_def_drcfill import (
    _write_cell_gds,
    _write_cell_lef,
    _write_def,
    _write_layer_map,
    _write_tech_lef,
)

kdb = pytest.importorskip("klayout.db")

pytestmark = pytest.mark.usefixtures("real_build_identity_git")

_CELL = "testcell"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _build_install(tmp_path: Path, monkeypatch) -> Path:
    """A fabricated open_pdks-layout ``asap7`` install holding the same
    real-reader fixture views ``test_place_and_route_def_drcfill`` uses,
    resolvable by ``find_pdk`` through ``$PDK_ROOT`` alone."""
    _isolate_pdk(monkeypatch, tmp_path)
    root = tmp_path / "install"
    variant = root / "asap7"
    lib = variant / "libs.ref" / _CELL
    (lib / "lef").mkdir(parents=True)
    (lib / "techlef").mkdir(parents=True)
    (lib / "gds").mkdir(parents=True)
    klayout_tech = variant / "libs.tech" / "klayout" / "tech"
    klayout_tech.mkdir(parents=True)

    _write_tech_lef(lib / "techlef" / f"{_CELL}__nom.tlef")
    _write_cell_lef(lib / "lef" / f"{_CELL}.lef", _CELL)
    _write_cell_gds(lib / "gds" / f"{_CELL}.gds", _CELL)
    _write_layer_map(klayout_tech / "asap7.map")

    monkeypatch.setenv("PDK_ROOT", str(root))
    return root


def _build_env(tmp_path: Path, monkeypatch) -> tuple[str, str]:
    """The committed-stream environment: install + request + a routed DEF +
    a populated artifacts directory holding the original merged GDS.

    Returns ``(request_path, def_path)``."""
    _build_install(tmp_path, monkeypatch)
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    def_path = evidence / "top.def"
    _write_def(def_path, _CELL)

    request_path = tmp_path / "request.json"
    request_path.write_text(
        json.dumps(
            {
                "hdl_toplevel": "top",
                "pdk": {"cell_library": _CELL},
                "outputs": {"dir": "artifacts"},
            }
        ),
        encoding="utf-8",
    )
    # The populated `outputs.dir` of the original run, holding its committed
    # merged GDS -- what a re-grade on a later pin compares the regenerated
    # stream against. Arbitrary bytes: their only contract is that the
    # merge-only run leaves them (and the populated-dir refusal) alone.
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    (artifacts / "top.gds").write_bytes(b"committed merged gds\n")
    return str(request_path), str(def_path)


def _fail_if_openroad_invoked(monkeypatch) -> None:
    """Any OpenROAD launch fails the test: `--from-def` must run no stage,
    probe no engine version, and dispatch nothing."""

    def _refuse(cmd, *args, **kwargs):  # pragma: no cover - fails if reached
        raise AssertionError(f"OpenROAD was invoked: {cmd[:2]}")

    monkeypatch.setattr(place_and_route.subprocess, "run", _refuse)
    monkeypatch.setattr(_openroad_engine, "Popen", _refuse)


def _run_cli(request_path: str, def_path: str, *extra: str) -> int:
    return main(["place-and-route", request_path, "--from-def", def_path, *extra])


def _run_cli_json(capsys, request_path: str, def_path: str, *extra: str) -> dict:
    assert _run_cli(request_path, def_path, "--format", "json", *extra) == 0
    return json.loads(capsys.readouterr().out)


def test_cli_merge_only_runs_no_openroad_and_reports_repairs(
    tmp_path, monkeypatch, capsys
):
    request_path, def_path = _build_env(tmp_path, monkeypatch)
    _fail_if_openroad_invoked(monkeypatch)

    report = _run_cli_json(capsys, request_path, def_path)

    assert report["status"] == "ok"
    assert report["hdl_toplevel"] == "top"
    assert report["def_path"] == def_path

    # The GDS is a *separate* file: its own name, never the committed
    # `top.gds` the caller holds.
    artifacts = tmp_path / "artifacts"
    assert report["gds_path"] == str(artifacts / "top_from_def.gds")
    assert os.path.isfile(report["gds_path"])
    # No stage artifact of any kind: no ODB, no commit record, no Tcl, no
    # metrics -- only the merged GDS beside the committed evidence.
    assert sorted(os.listdir(artifacts)) == ["top.gds", "top_from_def.gds"]

    # Repair/scan reports, same fields a route-stage run reports.
    tip = report["via_pad_trim"]["tip_check"]
    assert tip["status"] == "violations"
    assert tip["router_fill_violations"] == 1
    assert tip["via_pad_violations"] == 0
    assert tip["router_fill_scan"]["status"] == "scanned"
    assert report["power_stack_repair"]["status"] == "skipped"
    assert (
        report["power_stack_repair"]["reason"]
        == "no M4/M5/V3/V4 layer in the KLayout layer map"
    )
    assert report["min_area_repair"] is not None
    assert report["layer_map"]["path"] == str(
        tmp_path / "install" / "asap7" / "libs.tech" / "klayout" / "tech" / "asap7.map"
    )
    # Actual merge inputs, with the supplied DEF pinned by content hash.
    inputs = report["from_def"]
    assert inputs["def_path"] == def_path
    assert inputs["def_content_hash"] == f"sha256:{_sha256(Path(def_path))}"
    assert inputs["cell_gds"] == str(
        tmp_path / "install" / "asap7" / "libs.ref" / _CELL / "gds" / f"{_CELL}.gds"
    )


def test_cli_preserves_input_def_and_existing_gds_bytes(tmp_path, monkeypatch, capsys):
    request_path, def_path = _build_env(tmp_path, monkeypatch)
    _fail_if_openroad_invoked(monkeypatch)
    committed = tmp_path / "artifacts" / "top.gds"
    def_hash, gds_hash = _sha256(Path(def_path)), _sha256(committed)

    _run_cli_json(capsys, request_path, def_path)

    assert _sha256(Path(def_path)) == def_hash
    assert _sha256(committed) == gds_hash


def test_cli_matches_a_direct_call_to_the_merger(tmp_path, monkeypatch, capsys):
    """The CLI's GDS and repair reports equal what calling the existing
    merger directly on the same DEF and views produces -- re-opened and
    compared as geometry, not just as bytes."""
    request_path, def_path = _build_env(tmp_path, monkeypatch)
    _fail_if_openroad_invoked(monkeypatch)
    report = _run_cli_json(capsys, request_path, def_path)

    pdk_info = pdk_module.find_pdk(variant="asap7", root=str(tmp_path / "install"))
    direct_out = tmp_path / "direct.gds"
    direct = place_and_route._merge_def_to_gds(
        def_path=def_path,
        tech_lef=str(
            tmp_path
            / "install"
            / "asap7"
            / "libs.ref"
            / _CELL
            / "techlef"
            / f"{_CELL}__nom.tlef"
        ),
        cell_lef=str(
            tmp_path / "install" / "asap7" / "libs.ref" / _CELL / "lef" / f"{_CELL}.lef"
        ),
        pdk_info=pdk_info,
        cell_library=_CELL,
        hdl_toplevel="top",
        macros=[],
        out_path=str(direct_out),
    )

    # Geometry: byte-identical output (the merge disables GDS timestamps),
    # and re-opened through KLayout with per-layer regions compared.
    assert Path(report["gds_path"]).read_bytes() == direct_out.read_bytes()

    def _regions(path: str) -> dict[tuple[int, int], list[str]]:
        layout = kdb.Layout()
        layout.read(path)
        top = layout.top_cell()
        regions: dict[tuple[int, int], list[str]] = {}
        for index in layout.layer_indexes():
            info = layout.get_info(index)
            merged = kdb.Region(top.begin_shapes_rec(index)).merged()
            regions[(info.layer, info.datatype)] = [str(polygon) for polygon in merged]
        return regions

    cli_regions = _regions(report["gds_path"])
    direct_regions = _regions(str(direct_out))
    assert cli_regions == direct_regions

    # Repair results: every report field the merge returns matches.
    for field in (
        "def_net_names",
        "min_area_repair",
        "power_stack_repair",
        "via_pad_trim",
        "pin_conductor",
    ):
        assert report[field] == direct[field]
    assert report["layer_map"]["path"] == direct["path"]
    assert report["layer_map"]["resolution"] == direct["resolution"]


def test_cli_accepts_the_original_runs_full_request(tmp_path, monkeypatch, capsys):
    """A consumer points `--from-def` at the request file of the original
    run unchanged: every extra key the full flow takes is accepted, and the
    netlist/floorplan/seed it names are never read (the netlist here does
    not even exist)."""
    request_path, def_path = _build_env(tmp_path, monkeypatch)
    _fail_if_openroad_invoked(monkeypatch)
    full = json.loads(Path(request_path).read_text(encoding="utf-8"))
    full.update(
        {
            "engine": "openroad",
            "netlist": "gone_synth.v",
            "floorplan": {"method": "utilization", "utilization_pct": 38},
            "seed": 1,
            "target_stage": "route",
        }
    )
    Path(request_path).write_text(json.dumps(full), encoding="utf-8")

    report = _run_cli_json(capsys, request_path, def_path)
    assert report["status"] == "ok"


def test_cli_resolves_the_first_variant_shipping_the_library(
    tmp_path, monkeypatch, capsys
):
    """Issue #4204 parity: with no explicit variant selected and a default
    install that lacks the cell library, the merge resolves from the first
    installed variant that ships it -- the same substitution the full flow's
    liberty resolution applies, so both entry points pick the same install."""
    _build_install(tmp_path, monkeypatch)
    # An alphabetically-first variant holding no cell library at all: the
    # plain `find_pdk` default would pick it and the LEF resolution fail.
    empty = tmp_path / "install" / "aaa_empty"
    (empty / "libs.tech").mkdir(parents=True)
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    def_path = evidence / "top.def"
    _write_def(def_path, _CELL)
    request_path = tmp_path / "request.json"
    request_path.write_text(
        json.dumps({"hdl_toplevel": "top", "pdk": {"cell_library": _CELL}}),
        encoding="utf-8",
    )
    _fail_if_openroad_invoked(monkeypatch)

    report = _run_cli_json(capsys, str(request_path), str(def_path))
    assert report["status"] == "ok"
    assert report["from_def"]["cell_gds"] == str(
        tmp_path / "install" / "asap7" / "libs.ref" / _CELL / "gds" / f"{_CELL}.gds"
    )


def test_cli_missing_def_is_a_named_error(tmp_path, monkeypatch, capsys):
    request_path, _ = _build_env(tmp_path, monkeypatch)
    missing = str(tmp_path / "evidence" / "nope.def")

    code = main(
        ["place-and-route", request_path, "--from-def", missing, "--format", "json"]
    )
    assert code == 1
    assert "--from-def DEF not found" in capsys.readouterr().err


def test_cli_missing_cell_gds_view_is_the_existing_error(tmp_path, monkeypatch, capsys):
    request_path, def_path = _build_env(tmp_path, monkeypatch)
    os.unlink(
        tmp_path / "install" / "asap7" / "libs.ref" / _CELL / "gds" / f"{_CELL}.gds"
    )

    code = main(
        ["place-and-route", request_path, "--from-def", def_path, "--format", "json"]
    )
    assert code == 1
    assert "standard-cell GDS view not found" in capsys.readouterr().err


def test_cli_top_cell_mismatch_is_the_existing_error(tmp_path, monkeypatch, capsys):
    request_path, def_path = _build_env(tmp_path, monkeypatch)
    request = json.loads(Path(request_path).read_text(encoding="utf-8"))
    request["hdl_toplevel"] = "wrongtop"
    Path(request_path).write_text(json.dumps(request), encoding="utf-8")

    code = main(
        ["place-and-route", request_path, "--from-def", def_path, "--format", "json"]
    )
    assert code == 1
    assert "does not define top cell 'wrongtop'" in capsys.readouterr().err


def test_cli_refuses_post_route_spef_request(tmp_path, monkeypatch, capsys):
    request_path, def_path = _build_env(tmp_path, monkeypatch)
    request = json.loads(Path(request_path).read_text(encoding="utf-8"))
    request["post_route_spef"] = True
    Path(request_path).write_text(json.dumps(request), encoding="utf-8")

    code = main(
        ["place-and-route", request_path, "--from-def", def_path, "--format", "json"]
    )
    assert code == 1
    assert "post_route_spef" in capsys.readouterr().err


def test_cli_refuses_flow_flag_combinations(tmp_path, monkeypatch, capsys):
    request_path, def_path = _build_env(tmp_path, monkeypatch)
    for extra in (
        ["--resume"],
        ["--backend", "local"],
        ["--rerun"],
        ["--check", "x.json"],
    ):
        argv = [
            "place-and-route",
            request_path,
            "--from-def",
            def_path,
            *extra,
            "--format",
            "json",
        ]
        code = main(argv)
        assert code == 1
        assert "--from-def cannot be combined with" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("outputs", "expected_message"),
    [
        ({"dire": "comparison"}, "unknown field 'outputs.dire'"),
        ("artifacts", "request.outputs must be an object"),
        (None, "request.outputs must be an object"),
        ({"dir": 42}, "request.outputs.dir must be a non-empty string"),
        ({"dir": "   "}, "request.outputs.dir must be a non-empty string"),
    ],
)
def test_cli_rejects_malformed_outputs_before_views_or_output_creation(
    tmp_path, monkeypatch, capsys, outputs, expected_message
):
    request_path, def_path = _build_env(tmp_path, monkeypatch)
    _fail_if_openroad_invoked(monkeypatch)

    def _refuse_view_lookup(*args, **kwargs):  # pragma: no cover - fails if reached
        raise AssertionError(
            "PDK/view resolution ran before request.outputs validation"
        )

    monkeypatch.setattr(place_and_route_merge_only, "find_pdk", _refuse_view_lookup)

    request = json.loads(Path(request_path).read_text(encoding="utf-8"))
    request["outputs"] = outputs
    Path(request_path).write_text(json.dumps(request), encoding="utf-8")

    code = main(
        ["place-and-route", request_path, "--from-def", def_path, "--format", "json"]
    )
    captured = capsys.readouterr()

    assert code == 1
    assert captured.out == ""
    err_payload = json.loads(captured.err)
    assert err_payload["schema_version"] == 1
    assert err_payload["error"]["command"] == "place-and-route"
    assert expected_message in err_payload["error"]["message"]

    assert not (tmp_path / ".klt").exists()
    assert not (tmp_path / "comparison").exists()
    assert sorted(os.listdir(tmp_path / "artifacts")) == ["top.gds"]


def test_cli_text_format_prints_merge_summary(tmp_path, monkeypatch, capsys):
    request_path, def_path = _build_env(tmp_path, monkeypatch)
    _fail_if_openroad_invoked(monkeypatch)

    assert _run_cli(request_path, def_path, "--format", "text") == 0
    out = capsys.readouterr().out
    assert f"def_path: {def_path}" in out
    assert "top_from_def.gds" in out
    assert "via_pad_trim.tip_check: violations" in out
    assert (
        "power_stack_repair: skipped "
        "(no M4/M5/V3/V4 layer in the KLayout layer map)" in out
    )
