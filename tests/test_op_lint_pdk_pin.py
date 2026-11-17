"""`klt sim --op-lint` PDK pinning and reporting (issue #5751).

A sibling of ``test_sim_pdk_root.py`` / ``test_op_sanity.py`` (both under the
file-size ratchet); reuses their fake-ngspice and two-install PDK helpers.
"""

from __future__ import annotations

import json

import pytest

from klayout_tools.cli import main
from klayout_tools.cli.sim_op_lint_text import _print_op_lint_text
from klayout_tools.op_sanity import OpSanityError, run_op_sanity
from test_op_sanity import (
    _GOOD_NETLIST,
    _SATURATED_NMOS,
    _SATURATED_PMOS,
    _install_fake_ngspice,
    _write_request,
)
from test_sim import _isolate_pdk_search, _make_pdk_store

pytestmark = pytest.mark.usefixtures("real_build_identity_git")

_VALUES = {"XM1": _SATURATED_NMOS, "XM2": _SATURATED_PMOS}


def _two_store_lint_request(tmp_path, monkeypatch, **models):
    ciel = _make_pdk_store(tmp_path, "ciel", stamp="open_pdks aaaa111")
    volare = _make_pdk_store(tmp_path, "volare", stamp="open_pdks bbbb222")
    _isolate_pdk_search(monkeypatch, [ciel, volare])
    _install_fake_ngspice(monkeypatch, _VALUES)
    request = _write_request(
        tmp_path,
        _GOOD_NETLIST,
        models={
            "pdk": "sky130A",
            "lib": "libs.tech/ngspice/sky130.lib.spice",
            **models,
        },
    )
    return request, ciel, volare


def _lint_json(request, *extra, capsys):
    code = main(["sim", str(request), "--op-lint", "--format", "json", *extra])
    captured = capsys.readouterr()
    return code, json.loads(captured.out), captured.err


def test_pdk_root_flag_pins_the_op_lint_install(tmp_path, monkeypatch, capsys):
    request, _ciel, volare = _two_store_lint_request(tmp_path, monkeypatch)

    code, report, err = _lint_json(request, "--pdk-root", str(volare), capsys=capsys)

    assert code == 0
    block = report["provenance"]["pdk"]
    assert block["source"] == "--pdk-root flag"
    assert block["version"] == "open_pdks bbbb222"
    assert "ambiguous_sources" not in block
    assert "installs provide PDK variant" not in err


def test_pdk_root_flag_wins_over_request_models_pdk_root(tmp_path, monkeypatch, capsys):
    request, ciel, volare = _two_store_lint_request(
        tmp_path, monkeypatch, pdk_root=None
    )
    data = json.loads(request.read_text())
    data["models"]["pdk_root"] = str(ciel)
    request.write_text(json.dumps(data))

    _code, report, _err = _lint_json(request, "--pdk-root", str(volare), capsys=capsys)

    assert report["provenance"]["pdk"]["version"] == "open_pdks bbbb222"


def test_pdk_root_flag_relative_path_resolves_against_cwd(
    tmp_path, monkeypatch, capsys
):
    request, _ciel, volare = _two_store_lint_request(tmp_path, monkeypatch)
    monkeypatch.chdir(volare.parent)

    _code, report, _err = _lint_json(request, "--pdk-root", volare.name, capsys=capsys)

    assert report["provenance"]["pdk"]["version"] == "open_pdks bbbb222"


def test_pdk_root_flag_without_models_block_is_a_request_error(
    tmp_path, monkeypatch, capsys
):
    _install_fake_ngspice(monkeypatch, _VALUES)
    request = _write_request(tmp_path, _GOOD_NETLIST)

    code = main(
        [
            "sim",
            str(request),
            "--op-lint",
            "--pdk-root",
            str(tmp_path),
            "--format",
            "json",
        ]
    )

    assert code == 1  # the same envelope/exit an `OpSanityError` always uses
    message = json.loads(capsys.readouterr().err)["error"]["message"]
    assert "--pdk-root needs a request with a `models` block" in message
    with pytest.raises(OpSanityError):
        run_op_sanity(str(request), pdk_root=str(tmp_path))


def test_pdk_root_flag_still_refused_with_job_status(tmp_path, monkeypatch, capsys):
    request, _ciel, volare = _two_store_lint_request(tmp_path, monkeypatch)

    code = main(["sim", str(request), "--pdk-root", str(volare), "--job-status", "j-1"])

    assert code == 2
    assert "--pdk-root has no effect with --job-status" in capsys.readouterr().err


def test_unpinned_two_install_lint_warns_on_stderr_and_in_json(
    tmp_path, monkeypatch, capsys
):
    request, _ciel, _volare = _two_store_lint_request(tmp_path, monkeypatch)

    _code, report, err = _lint_json(request, capsys=capsys)

    assert report["provenance"]["pdk"]["ambiguous_sources"]
    assert "installs provide PDK variant" in err


def test_unpinned_two_install_lint_text_prints_pdk_and_warning(
    tmp_path, monkeypatch, capsys
):
    request, _ciel, _volare = _two_store_lint_request(tmp_path, monkeypatch)

    main(["sim", str(request), "--op-lint", "--format", "text"])

    captured = capsys.readouterr()
    assert "pdk: sky130A open_pdks aaaa111 (via " in captured.out
    assert "pdk: WARNING: variant also installed under" in captured.out
    assert "installs provide PDK variant" in captured.err


def test_unambiguous_lint_prints_pdk_line_without_warning(
    tmp_path, monkeypatch, capsys
):
    request, ciel, _volare = _two_store_lint_request(tmp_path, monkeypatch)
    _isolate_pdk_search(monkeypatch, [ciel])

    code, report, err = _lint_json(request, capsys=capsys)
    assert code == 0
    assert "ambiguous_sources" not in report["provenance"]["pdk"]
    assert err == ""

    main(["sim", str(request), "--op-lint", "--format", "text"])
    captured = capsys.readouterr()
    assert "pdk: sky130A open_pdks aaaa111 (via " in captured.out
    assert "WARNING" not in captured.out
    assert captured.err == ""


def test_print_op_lint_text_without_pdk_block_prints_no_pdk_line(capsys):
    request_report = {
        "netlist": {"path": "n.spice", "scope": "repo"},
        "environment": {"models_lib": {"path": None, "scope": "absent"}},
        "provenance": {},
        "status": "clean",
        "corner": {"corner_id": "c"},
        "device_count": 0,
        "finding_count": 0,
        "error_count": 0,
        "warning_count": 0,
        "devices": [],
        "findings": [],
        "diagnostics": [],
    }

    _print_op_lint_text(request_report)

    out = capsys.readouterr().out
    assert "status: clean" in out
    assert "pdk:" not in out
