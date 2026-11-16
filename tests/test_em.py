"""``klt em`` tests (issue #5616).

Unit tests run with no ``geode`` binary: a stub script replays the recorded
report in ``tests/fixtures/em/``. NOTE: that fixture is hand-authored to the
adapter's documented report shape, not captured from a real ``geode`` run;
only :func:`test_live_geode` exercises the real binary, and it is *skipped*
(never passed) when ``geode`` is absent.
"""

import json
import os
import shutil
import stat
from pathlib import Path

import pytest

from klayout_tools.cli.parser import create_parser
from klayout_tools.em import EmError, parse_capacitance_report, run_em

FIXTURE = Path(__file__).parent / "fixtures" / "em" / "geode_capacitance_report.json"


@pytest.fixture(autouse=True)
def _no_ambient_geode(monkeypatch):
    monkeypatch.delenv("KLT_GEODE_BINARY", raising=False)


def _stub(tmp_path: Path, *, exit_code: int = 0, report: Path = FIXTURE) -> Path:
    path = tmp_path / "geode-stub"
    path.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "--version" ]; then echo "geode 0.4.0"; exit 0; fi\n'
        f"if [ {exit_code} -ne 0 ]; then echo boom >&2; exit {exit_code}; fi\n"
        f'cat "{report}"\n'
    )
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return path


def _request(tmp_path: Path, stub: Path | None, **extra) -> Path:
    (tmp_path / "m.msh").write_text("mesh")
    req = {
        "engine": "geode_fem",
        "solve_mode": "quasi_static",
        "outputs": ["c"],
        "mesh": "m.msh",
        "terminals": [
            {"name": "sig", "surface": "S1"},
            {"name": "gnd", "surface": "S2"},
        ],
        "options": {"output_dir": "out"},
    }
    if stub is not None:
        req["options"]["geode_binary"] = str(stub)
    req.update(extra)
    path = tmp_path / "req.json"
    path.write_text(json.dumps(req))
    return path


def _main(*argv: str):
    args = create_parser().parse_args(argv)
    return args.func(args)


def test_success_payload(tmp_path):
    report = run_em(str(_request(tmp_path, _stub(tmp_path))))
    assert report["schema_version"] == 1
    assert report["status"] == "pass"
    assert report["environment"]["engine"] == "geode_fem"
    assert report["environment"]["engine_version"] == "0.4.0"
    assert report["results"]["c"]["matrix"][0][1] == pytest.approx(-1.5e-14)
    for key in ("mesh", "report", "log"):
        assert "path" in report["artifacts"][key]
    assert "field" not in json.dumps(report["results"])


def test_matrix_reordered_to_requested_terminals(tmp_path):
    swapped = tmp_path / "swapped.json"
    swapped.write_text(
        json.dumps(
            {
                "terminals": ["gnd", "sig"],
                "capacitance_matrix_f": [[3e-14, -1.5e-14], [-1.5e-14, 2e-14]],
            }
        )
    )
    report = run_em(str(_request(tmp_path, _stub(tmp_path, report=swapped))))
    assert report["results"]["c"]["matrix"][0][0] == pytest.approx(2e-14)


def test_missing_binary_is_documented_error(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("PATH", str(tmp_path))
    req = _request(tmp_path, None)
    rc = _main("em", str(req), "--format", "json")
    assert rc == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    msg = json.loads(captured.err)["error"]["message"]
    assert "geode" in msg and "KLT_GEODE_BINARY" in msg and "PATH" in msg
    assert "Traceback" not in captured.err


def test_bad_explicit_binary_does_not_fall_back(tmp_path):
    req = _request(tmp_path, tmp_path / "nope")
    with pytest.raises(EmError, match="options.geode_binary"):
        run_em(str(req))


def test_env_var_binary(tmp_path, monkeypatch):
    monkeypatch.setenv("KLT_GEODE_BINARY", str(_stub(tmp_path)))
    assert run_em(str(_request(tmp_path, None)))["status"] == "pass"


def test_checks_pass_fail_and_exit_codes(tmp_path, capsys):
    stub = _stub(tmp_path)
    ok = {
        "name": "c_self",
        "output": "c",
        "terminal_pair": ["sig", "sig"],
        "unit": "fF",
        "limits": {"min": 15, "max": 25},
    }
    rc = _main("em", str(_request(tmp_path, stub, checks=[ok])), "--format", "json")
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["checks"][0]["value"] == pytest.approx(20.0)
    assert out["checks"][0]["margin"] == pytest.approx(5.0)

    bad = dict(ok, limits={"max": 10})
    rc = _main("em", str(_request(tmp_path, stub, checks=[bad])), "--format", "json")
    assert rc == 3
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "fail"
    assert out["checks"][0]["margin"] == pytest.approx(-10.0)


def test_check_without_limits_never_fails(tmp_path):
    chk = {"name": "c", "output": "c", "terminal_pair": ["sig", "gnd"]}
    report = run_em(str(_request(tmp_path, _stub(tmp_path), checks=[chk])))
    assert report["status"] == "pass"
    assert report["checks"][0]["margin"] is None


def test_engine_failure_is_error_with_retained_log(tmp_path):
    with pytest.raises(EmError, match="exited 7"):
        run_em(str(_request(tmp_path, _stub(tmp_path, exit_code=7))))
    assert "boom" in (tmp_path / "out" / "solve.log").read_text()


def test_unsupported_values_rejected(tmp_path):
    stub = _stub(tmp_path)
    for extra in ({"engine": "openems"}, {"solve_mode": "driven"}, {"outputs": ["l"]}):
        with pytest.raises(EmError, match="unsupported"):
            run_em(str(_request(tmp_path, stub, **extra)))


@pytest.mark.parametrize(
    "text",
    [
        "not json",
        "[]",
        "{}",
        '{"terminals":["sig"],"capacitance_matrix_f":[[1]]}',
        '{"terminals":["sig","gnd"],"capacitance_matrix_f":[[1,2]]}',
        '{"terminals":["sig","gnd"],"capacitance_matrix_f":[[1,2],[2,null]]}',
    ],
)
def test_malformed_report_is_error(text):
    with pytest.raises(EmError):
        parse_capacitance_report(text, ["sig", "gnd"])


def test_text_format(tmp_path, capsys):
    rc = _main("em", str(_request(tmp_path, _stub(tmp_path))))
    assert rc == 0
    assert "status: pass" in capsys.readouterr().out


@pytest.mark.skipif(
    shutil.which("geode") is None and not os.environ.get("KLT_GEODE_BINARY"),
    reason="geode binary not available",
)
def test_live_geode():  # pragma: no cover - needs geode + a real mesh
    mesh = os.environ.get("KLT_EM_LIVE_MESH")
    if not mesh:
        pytest.skip("set KLT_EM_LIVE_MESH (mesh with surfaces S1,S2) to run live")
    tmp = Path(os.environ.get("TMPDIR", "/tmp"))
    req = {
        "mesh": mesh,
        "terminals": [
            {"name": "sig", "surface": "S1"},
            {"name": "gnd", "surface": "S2"},
        ],
        "options": {"output_dir": str(tmp / "klt-em-live")},
    }
    path = tmp / "klt-em-live-req.json"
    path.write_text(json.dumps(req))
    report = run_em(str(path))
    assert report["environment"]["engine_version"]
