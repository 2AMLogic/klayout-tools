"""``klt em`` tests (issue #5616).

Unit tests run with no ``geode`` binary: a stub script replays a report
fixture whose engine-side keys (``kind``, ``status``, ``terminals``,
``c_farad``, ...) are the keys of geode-fem's released ``CapacitanceReport``
(``crates/geode-cli/src/report.rs``, read over ``gh api``). The fixture is
hand-written but not invented: only its *numbers* are synthetic, and its
shape is pinned to geode's published report schema, vendored verbatim
under ``fixtures/em/`` (see
:func:`test_fixture_is_valid_against_geode_report_schema`). Only
:func:`test_live_geode` exercises the real binary, and it is *skipped*
(never passed) when ``geode`` is absent.
"""

import hashlib
import json
import os
import re
import shutil
import stat
from pathlib import Path

import jsonschema
import pytest

from klayout_tools.cli.parser import create_parser
from klayout_tools.em import EmError, parse_capacitance_report, run_em

FIXTURE = Path(__file__).parent / "fixtures" / "em" / "geode_capacitance_report.json"
GEODE_REPORT_SCHEMA = (
    Path(__file__).parent / "fixtures" / "em" / "geode_report.schema.json"
)
#: geode-fem commit the vendored report schema was copied verbatim from
#: (``crates/geode-cli/schemas/report.schema.json``). Do not read the
#: geode-fem checkout at test time: the path is not portable to CI.
GEODE_REPORT_SCHEMA_COMMIT = "592e8898"
#: sha256 of the vendored schema's raw bytes (issue #6667): the
#: load-bearing pin that keeps the copy upstream's verbatim bytes.
GEODE_REPORT_SCHEMA_SHA256 = (
    "d18a8aa542dc7c66a521a4c550e05d05edf9fc250259b9d75de9cc99b6ff06a1"
)


@pytest.fixture(autouse=True)
def _no_ambient_geode(monkeypatch):
    monkeypatch.delenv("KLT_GEODE_BINARY", raising=False)


def _stub(tmp_path: Path, *, exit_code: int = 0, report: Path = FIXTURE) -> Path:
    """A fake ``geode``: ``--version`` banner, then replay ``report`` into
    the ``--output`` path the real CLI is invoked with
    (``capacitance <spec> --output <report>`` -> ``$4``)."""
    path = tmp_path / "geode-stub"
    path.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "--version" ]; then echo "geode 0.4.0 (1a2b3c4)"; exit 0; fi\n'
        f"if [ {exit_code} -ne 0 ]; then echo boom >&2; exit {exit_code}; fi\n"
        f'cat "{report}" > "$4"\n'
    )
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return path


def _error_stub(tmp_path: Path, message: str, *, exit_code: int = 2) -> Path:
    """A fake ``geode`` that fails the way the real one does: nonzero exit
    plus a ``kind: "error"`` report written to the ``--output`` path
    (``main.rs`` ``finish``)."""
    path = tmp_path / "geode-error-stub"
    body = json.dumps(
        {
            "kind": "error",
            "status": "error",
            "error": {"code": "invalid_spec", "message": message},
        }
    )
    path.write_text(f"#!/bin/sh\nprintf '%s' '{body}' > \"$4\"\nexit {exit_code}\n")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return path


def _request(tmp_path: Path, stub: Path | None, **extra) -> Path:
    (tmp_path / "m.msh").write_text("mesh")
    req = {
        "engine": "geode_fem",
        "solve_mode": "quasi_static",
        "outputs": ["c"],
        "mesh": {"path": "m.msh", "length_unit_m": 1e-6},
        "terminals": [
            {"name": "sig", "surface": "S1"},
            {"name": "gnd", "surface": "S2"},
        ],
        "ground": [{"name": "sub", "surface": "S3"}],
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


def test_fixture_is_valid_against_geode_report_schema():
    """Issue #6514: the engine-side fixture is pinned to geode's published
    report schema (vendored verbatim from geode-fem ``GEODE_REPORT_SCHEMA_COMMIT``),
    so the suite asserts the envelope ``geode`` emits, not the adapter's
    guess at it. ``backend``'s documented value set and ``mesh.sha256``'s
    documented shape (hex SHA-256 of the mesh bytes) are asserted separately
    because the schema types both as bare strings. ``git_sha: "fixture"`` is
    deliberately *not* a documented value (``<sha>[-dirty]`` / ``unknown``):
    it is the one marker that this report is hand-authored, not a real run."""
    schema = json.loads(GEODE_REPORT_SCHEMA.read_text(encoding="utf-8"))
    report = json.loads(FIXTURE.read_text(encoding="utf-8"))
    jsonschema.validate(report, schema)
    assert report["backend"] in ("ndarray", "wgpu", "cuda", "metal")
    assert re.fullmatch(r"[0-9a-f]{64}", report["mesh"]["sha256"])
    # Negative control: the validation bites on this fixture, so a future
    # edit that drops a required field fails here rather than silently
    # re-diverging from the engine's contract.
    report.pop("c_flux_diag_farad")
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(report, schema)


def test_vendored_geode_report_schema_is_upstream_bytes():
    """Issue #6667: the vendored schema is an independent oracle only while
    it is upstream's bytes; ``GEODE_REPORT_SCHEMA_COMMIT`` alone documents
    the pin without checking it. Raw bytes, not text, so a line-ending
    normalizing checkout cannot change the digest."""
    digest = hashlib.sha256(GEODE_REPORT_SCHEMA.read_bytes()).hexdigest()
    assert digest == GEODE_REPORT_SCHEMA_SHA256, (
        "tests/fixtures/em/geode_report.schema.json no longer matches the "
        "pinned geode-fem bytes. To refresh it, copy "
        "crates/geode-cli/schemas/report.schema.json from the new geode-fem "
        "commit and update GEODE_REPORT_SCHEMA_COMMIT and "
        "GEODE_REPORT_SCHEMA_SHA256 together."
    )


def test_success_payload(tmp_path):
    report = run_em(str(_request(tmp_path, _stub(tmp_path))))
    assert report["schema_version"] == 1
    assert report["status"] == "pass"
    assert report["environment"]["engine"] == "geode_fem"
    assert report["environment"]["engine_version"] == "0.4.0"
    assert report["results"]["c"]["matrix"][0][1] == pytest.approx(-1.5e-14)
    for key in ("mesh", "spec", "report", "log"):
        assert "path" in report["artifacts"][key]
    assert "field" not in json.dumps(report["results"])


def test_geode_spec_targets_the_real_contract(tmp_path):
    """The written ProblemSpec carries the engine's keys (``schema_version``,
    ``mesh.length_unit_m``, ``capacitance.terminals``/``ground`` as
    physical-group names), not the caller's names."""
    run_em(str(_request(tmp_path, _stub(tmp_path))))
    spec = json.loads((tmp_path / "out" / "spec.json").read_text(encoding="utf-8"))
    assert spec["schema_version"] == 1
    assert spec["mesh"]["length_unit_m"] == 1e-6
    assert Path(spec["mesh"]["path"]).is_absolute()
    assert spec["capacitance"] == {"terminals": ["S1", "S2"], "ground": ["S3"]}
    assert "sig" not in spec and "sub" not in spec


def test_matrix_reordered_to_requested_terminals(tmp_path):
    swapped = tmp_path / "swapped.json"
    swapped.write_text(
        json.dumps(
            {
                "kind": "capacitance",
                "status": "ok",
                "terminals": ["S2", "S1"],
                "c_farad": [[3e-14, -1.5e-14], [-1.5e-14, 2e-14]],
            }
        )
    )
    report = run_em(str(_request(tmp_path, _stub(tmp_path, report=swapped))))
    assert report["results"]["c"]["matrix"][0][0] == pytest.approx(2e-14)


def test_mismatched_report_terminals_is_error(tmp_path):
    wrong = tmp_path / "wrong.json"
    wrong.write_text(
        json.dumps(
            {
                "kind": "capacitance",
                "status": "ok",
                "terminals": ["S9", "S2"],
                "c_farad": [[1e-14, 0.0], [0.0, 1e-14]],
            }
        )
    )
    with pytest.raises(EmError, match="do not match"):
        run_em(str(_request(tmp_path, _stub(tmp_path, report=wrong))))


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


def test_engine_error_report_message_is_surfaced(tmp_path):
    """A failed run's ``kind: "error"`` report is the useful diagnostic."""
    stub = _error_stub(tmp_path, "capacitance.ground must name at least one")
    with pytest.raises(EmError, match="capacitance.ground must name at least one"):
        run_em(str(_request(tmp_path, stub)))


def test_unsupported_values_rejected(tmp_path):
    stub = _stub(tmp_path)
    for extra in ({"engine": "openems"}, {"solve_mode": "driven"}, {"outputs": ["l"]}):
        with pytest.raises(EmError, match="unsupported"):
            run_em(str(_request(tmp_path, stub, **extra)))


@pytest.mark.parametrize(
    "extra",
    [
        {"mesh": "m.msh"},  # legacy string form: no length_unit_m
        {"mesh": {"path": "m.msh"}},
        {"mesh": {"path": "m.msh", "length_unit_m": 0}},
        {"mesh": {"path": "m.msh", "length_unit_m": -1e-6}},
        {"ground": []},
        {"ground": [{"name": "sig", "surface": "S9"}]},
        {"ground": [{"name": "sub", "surface": "S1"}]},
        {"terminals": []},
        {"materials": [{"physical_group": "ox", "eps_r": [3.9, 0.1]}]},
        {"materials": [{"physical_group": "ox", "eps_r": [0.0, 0.0]}]},
        {"materials": [{"physical_group": "ox", "eps_r": [3.9]}]},
        {"materials": "oxide"},
        {"options": {"output_dir": 17}},
        {"options": {"timeout_s": 0}},
    ],
)
def test_invalid_requests_rejected_with_envelope_error(tmp_path, extra):
    stub = _stub(tmp_path)
    request = _request(tmp_path, stub, **extra)
    with pytest.raises(EmError):
        run_em(str(request))


def test_materials_default_to_vacuum_but_are_carried_into_the_spec(tmp_path):
    ox = {"physical_group": "ox", "eps_r": [3.9, 0.0]}
    run_em(str(_request(tmp_path, _stub(tmp_path), materials=[ox])))
    spec = json.loads((tmp_path / "out" / "spec.json").read_text(encoding="utf-8"))
    assert spec["materials"] == [{"physical_group": "ox", "eps_r": [3.9, 0.0]}]


@pytest.mark.parametrize(
    "text",
    [
        "not json",
        "[]",
        "{}",
        '{"kind":"error","error":{"code":"invalid_spec","message":"x"}}',
        '{"terminals":["S1","S2"],"c_farad":[[1,2],[2,"x"]]}',
        '{"terminals":["S1","S2"],"c_farad":[[1,2]]}',
        '{"terminals":["S1","S2"],"c_farad":[[1,2],[2,null]]}',
    ],
)
def test_malformed_report_is_error(text):
    with pytest.raises(EmError):
        parse_capacitance_report(text)


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
        pytest.skip("set KLT_EM_LIVE_MESH (mesh with surfaces S1,S2,S3) to run live")
    tmp = Path(os.environ.get("TMPDIR", "/tmp"))
    req = {
        "mesh": {"path": mesh, "length_unit_m": 1e-6},
        "terminals": [
            {"name": "sig", "surface": "S1"},
            {"name": "gnd", "surface": "S2"},
        ],
        "ground": [{"name": "sub", "surface": "S3"}],
        "options": {"output_dir": str(tmp / "klt-em-live")},
    }
    path = tmp / "klt-em-live-req.json"
    path.write_text(json.dumps(req))
    run_em(str(path))
