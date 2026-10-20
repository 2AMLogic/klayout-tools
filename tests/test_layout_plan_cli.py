"""Tests for the ``klt layout-plan execute`` shell verb (issue #3970).

The verb is a thin wrapper over
``layout_plan_execute.execute_layout_plan_document``; these tests pin the
CLI surface (parity, exit codes, envelope, MCP visibility), not the engine.
PDK resolution uses a fabricated install under ``tmp_path`` as in
``test_layout_plan_execute.py``.
"""

from __future__ import annotations

import json

import pytest

from klayout_tools import mcp_tools, pdk
from klayout_tools.cli import main
from klayout_tools.layout_plan_execute import execute_layout_plan_document

_GATE_RAIL_NETLIST = """
.subckt gate_rail VBIAS D1 D2 D3
M1 D1 VBIAS S1 S1 nfet L=0.28U W=0.42U
M2 D2 VBIAS D1 D1 nfet L=0.28U W=0.42U
M3 D3 VBIAS S3 S3 nfet L=0.28U W=0.42U
.ends
"""

_UNMAPPED_BULK_NETLIST = _GATE_RAIL_NETLIST.replace("S3 S3 nfet", "S3 VNW nfet")


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    monkeypatch.delenv("PDK_ROOT", raising=False)
    monkeypatch.delenv("PDK", raising=False)
    monkeypatch.setattr(pdk, "STORE_DIRS", [])
    monkeypatch.setattr(pdk, "CONVENTIONAL_PREFIXES", [])


@pytest.fixture()
def pdk_root(tmp_path):
    root = tmp_path / "pdk_install"
    (root / "sky130A" / "libs.tech").mkdir(parents=True)
    return root


def _request(tmp_path, pdk_root, netlist_text=_GATE_RAIL_NETLIST):
    # Relative netlist path: it must resolve against the request
    # file's directory, exactly as the library call does with request_dir.
    (tmp_path / "gate_rail.spice").write_text(netlist_text)
    return {
        "schema": "klt.layout_plan.request/1",
        "netlist": {"path": "gate_rail.spice", "top": "gate_rail"},
        "pdk": {"variant": "sky130A", "root": str(pdk_root)},
        "device_groups": [
            {
                "id": f"b{i}",
                "devices": [str(i)],
                "generator": "mos_array",
                "topology": "array",
                "params": {"rows": 1, "cols": 1, "dummy": 0, "gate_contact": True},
            }
            for i in (1, 2, 3)
        ],
        "rows": [{"order": ["b1", "b2", "b3"], "spacing_um": 2.0, "align": "bottom"}],
        "routing": {"layer_role": "metal", "width_um": 0.42},
        "options": {"cell_name": "gate_rail_0", "output": str(tmp_path / "out.gds")},
    }


def _write_request(tmp_path, request, name="req.json"):
    path = tmp_path / name
    path.write_text(json.dumps(request))
    return str(path)


def _run(capsys, *argv):
    code = main(["layout-plan", "execute", *argv])
    return code, capsys.readouterr()


def test_cli_json_matches_library_response(tmp_path, pdk_root, capsys):
    request = _request(tmp_path, pdk_root)
    path = _write_request(tmp_path, request)

    code, captured = _run(capsys, path, "--format", "json")
    assert code == 0
    payload = json.loads(captured.out)

    expected = execute_layout_plan_document(request, request_dir=str(tmp_path))
    assert payload == json.loads(json.dumps(expected))
    assert payload["unrouted_nets"] == []


def test_cli_output_is_deterministic(tmp_path, pdk_root, capsys):
    path = _write_request(tmp_path, _request(tmp_path, pdk_root))
    _, first = _run(capsys, path, "--format", "json")
    _, second = _run(capsys, path, "--format", "json")
    assert first.out == second.out


def test_cli_text_format_succeeds(tmp_path, pdk_root, capsys):
    path = _write_request(tmp_path, _request(tmp_path, pdk_root))
    code, captured = _run(capsys, path)
    assert code == 0
    assert "cell_name: gate_rail_0" in captured.out
    assert "gds_path:" in captured.out


def test_cli_partial_success_exits_3_with_payload(tmp_path, pdk_root, capsys):
    request = _request(tmp_path, pdk_root, _UNMAPPED_BULK_NETLIST)
    path = _write_request(tmp_path, request)

    code, captured = _run(capsys, path, "--format", "json")
    assert code == 3
    assert json.loads(captured.out)["unmapped_netlist_nets"] == ["VNW"]

    code, captured = _run(capsys, path)
    assert code == 3
    assert "unmapped_netlist_nets:" in captured.out


def test_cli_missing_request_file_exits_1(tmp_path, capsys):
    code, captured = _run(capsys, str(tmp_path / "nope.json"), "--format", "json")
    assert code == 1
    assert "could not read request" in captured.out + captured.err


def test_cli_invalid_json_exits_1(tmp_path, capsys):
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    code, captured = _run(capsys, str(bad), "--format", "json")
    assert code == 1
    assert "not valid JSON" in captured.out + captured.err


def test_cli_plan_validation_failure_exits_1(tmp_path, pdk_root, capsys):
    request = _request(tmp_path, pdk_root)
    request["device_groups"][0]["devices"] = ["no_such_device"]
    code, _ = _run(capsys, _write_request(tmp_path, request), "--format", "json")
    assert code == 1


def test_cli_missing_netlist_field_exits_1(tmp_path, pdk_root, capsys):
    request = _request(tmp_path, pdk_root)
    del request["netlist"]
    code, _ = _run(capsys, _write_request(tmp_path, request), "--format", "json")
    assert code == 1


def test_cli_unsupported_generator_exits_1(tmp_path, pdk_root, capsys):
    request = _request(tmp_path, pdk_root)
    request["device_groups"][0]["generator"] = "no_such_generator"
    code, _ = _run(capsys, _write_request(tmp_path, request), "--format", "json")
    assert code == 1


def test_cli_usage_errors_exit_2(tmp_path, pdk_root):
    with pytest.raises(SystemExit) as missing:
        main(["layout-plan", "execute"])
    assert missing.value.code == 2

    path = _write_request(tmp_path, _request(tmp_path, pdk_root))
    with pytest.raises(SystemExit) as bad_format:
        main(["layout-plan", "execute", path, "--format", "yaml"])
    assert bad_format.value.code == 2


def test_cli_no_subcommand_prints_help_exit_2(capsys):
    assert main(["layout-plan"]) == 2
    assert "execute" in capsys.readouterr().err


def test_layout_plan_execute_is_derived_as_mcp_tool():
    names = [tool.name for tool in mcp_tools.derive_tools()]
    assert any("layout" in n and "plan" in n and "execute" in n for n in names), names
