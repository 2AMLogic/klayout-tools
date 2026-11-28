"""EM artifacts and OpenROAD engine-log envelopes anchor on the request's
repo, not the process cwd (#6764).

``klt em``'s ``artifacts.{mesh,spec,report,log}`` and the retained OpenROAD
log records (P&R ``engine_logs[]``, STA ``engine_log`` /
``corners[].engine_log``) resolve their ``{path, scope}`` envelopes against
the repo containing the request file, so one request over the same retained
files yields one committed report from any invoking directory -- the same
property issue #6762 gave ``klt functional-verification``. Every engine is
stubbed; no ``geode`` and no ``openroad`` binary is required.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from helpers.subprocess_fakes import patch_openroad_run
from klayout_tools import _openroad_engine as engine
from klayout_tools.em import run_em
from klayout_tools.place_and_route import run_place_and_route
from klayout_tools.post_route_sta import run_sta
from test_em import _request as _em_request
from test_em import _stub as _geode_stub

pytestmark = pytest.mark.usefixtures("real_build_identity_git")

#: Captured at import time: a test that patches the shared ``subprocess.run``
#: module attribute and then stubs again in the same monkeypatch context must
#: not re-capture its own earlier stub as "real".
_REAL_RUN = subprocess.run

#: The six retained-log fields a record's ``{path, scope}`` anchor governs.
_SIX = (
    "script_path",
    "metrics_path",
    "directory",
    "stdout_path",
    "stderr_path",
    "metadata_path",
)


class EngineError(Exception):
    pass


def _checkout(root: Path) -> Path:
    """A directory ``find_repo_root`` recognises (a ``.git`` entry is all it
    checks)."""
    root.mkdir(parents=True, exist_ok=True)
    (root / ".git").mkdir()
    return root


def _invoking_dirs(tmp_path: Path, checkout: Path) -> dict[str, Path]:
    """The directories one request is driven from: inside its own checkout,
    outside every checkout, and inside an unrelated one."""
    driver = tmp_path / "driver"
    driver.mkdir()
    return {
        "own checkout": checkout,
        "outside any checkout": driver,
        "unrelated checkout": _checkout(tmp_path / "checkout-b"),
    }


def _field(envelope: dict[str, Any]) -> tuple[Any, str]:
    return (envelope["path"], envelope["scope"])


def _shape(record: dict[str, Any]) -> str:
    """A record as comparable text: everything but its fresh invocation ID
    (which names a per-run directory) must be identical across runs."""
    return json.dumps(record, sort_keys=True).replace(record["invocation_id"], "<id>")


def _em_project(tmp_path: Path) -> tuple[Path, str]:
    """``checkout-a/proj`` holding a stubbed-geode EM request."""
    proj = _checkout(tmp_path / "checkout-a") / "proj"
    proj.mkdir()
    return proj, _em_request(proj, _geode_stub(proj))


# --------------------------------------------------------------------------- #
# `klt em`
# --------------------------------------------------------------------------- #


def test_em_artifacts_do_not_depend_on_the_invoking_directory(tmp_path, monkeypatch):
    proj, request = _em_project(tmp_path)
    runs = {}
    for label, cwd in _invoking_dirs(tmp_path, tmp_path / "checkout-a").items():
        monkeypatch.chdir(cwd)
        runs[label] = run_em(request)["artifacts"]

    reference = runs["own checkout"]
    assert set(reference) == {"mesh", "spec", "report", "log"}
    assert _field(reference["mesh"]) == ("proj/m.msh", "repo")
    for key, name in (
        ("spec", "spec.json"),
        ("report", "report.json"),
        ("log", "solve.log"),
    ):
        assert _field(reference[key]) == (f"proj/out/{name}", "repo"), key
    for envelope in reference.values():
        assert envelope["sha256"].startswith("sha256:")
    for label, got in runs.items():
        assert got == reference, label


def test_em_request_outside_any_checkout_is_external_never_absolute(
    tmp_path, monkeypatch
):
    loose = tmp_path / "loose"
    loose.mkdir()
    request = _em_request(loose, _geode_stub(loose))
    for cwd in (tmp_path, _checkout(tmp_path / "checkout-b")):
        monkeypatch.chdir(cwd)
        artifacts = run_em(request)["artifacts"]
        for key in ("mesh", "spec", "report", "log"):
            assert _field(artifacts[key]) == (None, "external"), key
        assert str(loose) not in json.dumps(artifacts)


def test_em_keep_artifacts_false_reports_only_the_mesh(tmp_path, monkeypatch):
    proj = _checkout(tmp_path / "checkout-a") / "proj"
    proj.mkdir()
    stub = _geode_stub(proj)
    request = _em_request(
        proj, None, options={"geode_binary": str(stub), "keep_artifacts": False}
    )
    monkeypatch.chdir(_checkout(tmp_path / "checkout-b"))

    artifacts = run_em(request)["artifacts"]

    assert set(artifacts) == {"mesh"}
    assert _field(artifacts["mesh"]) == ("proj/m.msh", "repo")


def test_em_mesh_outside_the_request_checkout_is_external(tmp_path, monkeypatch):
    """A supplied mesh in a second checkout is ``external`` even when the
    invoking cwd is that other checkout."""
    proj, _ = _em_project(tmp_path)
    other = _checkout(tmp_path / "checkout-b")
    (other / "foreign.msh").write_text("mesh", encoding="utf-8")
    request = _em_request(
        proj,
        _geode_stub(proj),
        mesh={"path": str(other / "foreign.msh"), "length_unit_m": 1e-6},
    )
    monkeypatch.chdir(other)

    artifacts = run_em(request)["artifacts"]

    assert _field(artifacts["mesh"]) == (None, "external")
    for key, name in (
        ("spec", "spec.json"),
        ("report", "report.json"),
        ("log", "solve.log"),
    ):
        assert _field(artifacts[key]) == (f"proj/out/{name}", "repo"), key


def test_em_output_dir_in_a_second_checkout_is_external(tmp_path, monkeypatch):
    """``options.output_dir`` in a second checkout makes every retained
    artifact ``external`` even when the invoking cwd is that checkout."""
    proj, _ = _em_project(tmp_path)
    other = _checkout(tmp_path / "checkout-b")
    stub = _geode_stub(proj)
    request = _em_request(
        proj,
        stub,
        options={"geode_binary": str(stub), "output_dir": str(other / "out")},
    )
    monkeypatch.chdir(other)

    artifacts = run_em(request)["artifacts"]

    assert _field(artifacts["mesh"]) == ("proj/m.msh", "repo")
    for key in ("spec", "report", "log"):
        assert _field(artifacts[key]) == (None, "external"), key


# --------------------------------------------------------------------------- #
# The shared OpenROAD recorder
# --------------------------------------------------------------------------- #


def _invoke_openroad(proj: Path, monkeypatch, *, repo_root: Any, exit_code: int = 0):
    script = proj / "pnr_top_route.tcl"
    script.write_text("# invoked script\n", encoding="utf-8")

    def local_process(argv, **kwargs):
        assert argv[-1] == str(script)
        return _REAL_RUN(
            [
                sys.executable,
                "-c",
                f"import sys; print('full stdout α'); "
                f"print('full stderr β', file=sys.stderr); sys.exit({exit_code})",
            ],
            **kwargs,
        )

    patch_openroad_run(monkeypatch, local_process)
    return engine.run_openroad(
        str(script),
        str(proj / "metrics.json"),
        error_cls=EngineError,
        repo_root=repo_root,
    )


def test_engine_log_anchored_on_the_request_repo_from_every_cwd(tmp_path, monkeypatch):
    checkout = _checkout(tmp_path / "checkout-a")
    proj = checkout / "proj"
    proj.mkdir()
    shapes = set()
    for cwd in _invoking_dirs(tmp_path, checkout).values():
        monkeypatch.chdir(cwd)
        record = _invoke_openroad(proj, monkeypatch, repo_root=str(checkout)).engine_log
        logs = f"proj/openroad-logs/{record['invocation_id']}"
        expected = {
            "script_path": "proj/pnr_top_route.tcl",
            "metrics_path": "proj/metrics.json",
            "directory": logs,
            "stdout_path": f"{logs}/stdout.log",
            "stderr_path": f"{logs}/stderr.log",
            "metadata_path": f"{logs}/invocation.json",
        }
        for key, path in expected.items():
            assert _field(record[key]) == (path, "repo"), key
        on_disk = json.loads(
            (
                proj / "openroad-logs" / record["invocation_id"] / "invocation.json"
            ).read_text(encoding="utf-8")
        )
        assert on_disk == record
        shapes.add(_shape(record))
    assert len(shapes) == 1


def test_engine_log_explicit_no_root_marks_every_present_path_external(
    tmp_path, monkeypatch
):
    """``repo_root=None`` is the explicit no-root case: even with the cwd
    inside a checkout, every present path is ``external``."""
    checkout = _checkout(tmp_path / "checkout-a")
    proj = checkout / "proj"
    proj.mkdir()
    monkeypatch.chdir(proj)

    record = _invoke_openroad(proj, monkeypatch, repo_root=None).engine_log

    for key in _SIX:
        assert _field(record[key]) == (None, "external"), key


def test_engine_log_retention_failure_keeps_anchor_and_absent(tmp_path, monkeypatch):
    """A stream that cannot be retained stays ``absent`` while every other
    field keeps the request anchor; the on-disk record agrees."""
    checkout = _checkout(tmp_path / "checkout-a")
    proj = checkout / "proj"
    proj.mkdir()
    real_open = Path.open

    def fail_stdout(path, *args, **kwargs):
        if path.name == "stdout.log" and args == ("xb",):
            raise PermissionError(13, "Permission denied", str(path))
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", fail_stdout)
    driver = tmp_path / "driver"
    driver.mkdir()
    monkeypatch.chdir(driver)

    record = _invoke_openroad(proj, monkeypatch, repo_root=str(checkout)).engine_log

    assert record["retention_errors"]
    assert _field(record["stdout_path"]) == (None, "absent")
    for key in _SIX:
        if key != "stdout_path":
            assert record[key]["scope"] == "repo", key
    on_disk = json.loads(
        (
            proj / "openroad-logs" / record["invocation_id"] / "invocation.json"
        ).read_text(encoding="utf-8")
    )
    assert on_disk == record


def test_launch_failure_diagnostic_embeds_the_anchored_record(tmp_path, monkeypatch):
    checkout = _checkout(tmp_path / "checkout-a")
    proj = checkout / "proj"
    proj.mkdir()

    def missing(*args, **kwargs):
        raise FileNotFoundError("engine missing")

    patch_openroad_run(monkeypatch, missing)
    monkeypatch.chdir(_checkout(tmp_path / "checkout-b"))
    with pytest.raises(EngineError, match="engine missing") as exc:
        engine.run_openroad(
            str(proj / "run.tcl"),
            "metrics",
            error_cls=EngineError,
            repo_root=str(checkout),
        )
    _, marker, raw = str(exc.value).partition(" -- openroad invocation: ")
    assert marker
    record = json.loads(raw)
    assert _field(record["script_path"]) == ("proj/run.tcl", "repo")


# --------------------------------------------------------------------------- #
# Caller wiring: P&R
# --------------------------------------------------------------------------- #


def test_pnr_engine_logs_anchor_on_the_request_repo(tmp_path, monkeypatch):
    import test_place_and_route as pnr

    checkout = _checkout(tmp_path / "checkout-a")
    proj = checkout / "proj"
    proj.mkdir()
    request = pnr._setup_success_env(proj, monkeypatch, target_stage="floorplan")
    pnr._stub_openroad_success(monkeypatch, stages=("floorplan",))

    shapes = set()
    for cwd in _invoking_dirs(tmp_path, checkout).values():
        monkeypatch.chdir(cwd)
        logs = run_place_and_route(request)["engine_logs"]
        assert len(logs) == 1
        record = logs[0]
        stage = "proj/.klt/place-and-route"
        logs_dir = f"{stage}/openroad-logs/{record['invocation_id']}"
        expected = {
            "script_path": f"{stage}/pnr_gcd_floorplan.tcl",
            "metrics_path": f"{stage}/gcd_floorplan_metrics.json",
            "directory": logs_dir,
            "stdout_path": f"{logs_dir}/stdout.log",
            "stderr_path": f"{logs_dir}/stderr.log",
            "metadata_path": f"{logs_dir}/invocation.json",
        }
        for key, path in expected.items():
            assert _field(record[key]) == (path, "repo"), key
        shapes.add(_shape(record))
    assert len(shapes) == 1


def test_pnr_outputs_dir_in_a_second_checkout_is_external(tmp_path, monkeypatch):
    import test_place_and_route as pnr

    checkout = _checkout(tmp_path / "checkout-a")
    proj = checkout / "proj"
    proj.mkdir()
    other = _checkout(tmp_path / "checkout-b")
    request = pnr._setup_success_env(
        proj,
        monkeypatch,
        target_stage="floorplan",
        outputs={"dir": str(other / "pnr-out")},
    )
    pnr._stub_openroad_success(monkeypatch, stages=("floorplan",))
    monkeypatch.chdir(other)

    record = run_place_and_route(request)["engine_logs"][0]

    for key in _SIX:
        assert _field(record[key]) == (None, "external"), key


def test_pnr_sweep_and_spef_logs_anchor_on_the_request_repo(tmp_path, monkeypatch):
    """The corner-sweep and SPEF-STA invocations are wired to the request
    root too -- driven from an unrelated checkout, every entry of the full
    flow's ``engine_logs[]`` is repo-scoped."""
    import test_place_and_route as pnr

    checkout = _checkout(tmp_path / "checkout-a")
    proj = checkout / "proj"
    proj.mkdir()
    request = pnr._setup_success_env(proj, monkeypatch, post_route_spef=True)
    pnr._make_pdk_install(proj / "install", "sky130A", corner="ss_100C_1v60")
    pnr._stub_openroad_success_with_post_route_spef(monkeypatch)
    pnr._stub_merge_def_to_gds(monkeypatch)
    pnr._stub_run_extract_for_post_route_spef(monkeypatch)
    monkeypatch.chdir(_checkout(tmp_path / "checkout-b"))

    report = run_place_and_route(request)

    names = {entry["script_name"] for entry in report["engine_logs"]}
    assert {
        "pnr_gcd_route.tcl",
        "pnr_gcd_route_corners.tcl",
        "pnr_gcd_route_spef.tcl",
    } <= names
    for entry in report["engine_logs"]:
        for key in _SIX:
            assert entry[key]["scope"] == "repo", (entry["script_name"], key)
        assert (
            entry["script_path"]["path"]
            == f"proj/.klt/place-and-route/{entry['script_name']}"
        ), entry["script_name"]


# --------------------------------------------------------------------------- #
# Caller wiring: STA
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("multi_corner", [False, True])
def test_sta_engine_log_anchors_on_the_request_repo(
    tmp_path, monkeypatch, multi_corner
):
    import test_post_route_sta as sta

    checkout = _checkout(tmp_path / "checkout-a")
    proj = checkout / "proj"
    proj.mkdir()
    if multi_corner:
        request = sta._setup_multi_corner_env(proj, monkeypatch)
        sta._stub_openroad_multi_corner(monkeypatch)
    else:
        request = sta._setup_success_env(proj, monkeypatch)
        sta._stub_openroad_success(monkeypatch)

    shapes = set()
    for cwd in _invoking_dirs(tmp_path, checkout).values():
        monkeypatch.chdir(cwd)
        report = run_sta(request)
        entries = report["corners"] if multi_corner else [report]
        assert len(entries) == (2 if multi_corner else 1)
        for entry in entries:
            record = entry["engine_log"]
            assert record["script_path"]["scope"] == "repo"
            assert record["script_path"]["path"].startswith("proj/.klt/sta/sta_top")
            for key in _SIX:
                assert record[key]["scope"] == "repo", key
            shapes.add(_shape(record))
    assert len(shapes) == (2 if multi_corner else 1)
