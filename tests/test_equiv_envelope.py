"""Characterization tests for the ``klt equiv`` report envelope.

Both engines (``"yosys"`` and ``"yosys-sequential"``) emit one envelope
shape. The contracts pinned here -- key insertion order, the raw Yosys log
file layout, what a process timeout and an unlaunchable binary leave on
disk, and the saved input digest -- were captured from the two engines
before their report assembly was deduplicated (issue #7766). Each
expectation is written out rather than derived from the builder under test,
so a drift in either engine fails on its own. ``subprocess.run`` is stubbed;
no Yosys install is needed.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from helpers.subprocess_fakes import fake_completed
from klayout_tools import _paths as paths_module
from klayout_tools import equiv
from klayout_tools._digest import content_hash
from klayout_tools.equiv import EquivError, run_equiv

pytestmark = pytest.mark.usefixtures("real_build_identity_git")

_EQUIV = "equiv.ys"
_STAGE1 = "equiv_seq_stage1.ys"
_STAGE2 = "equiv_seq_stage2.ys"

_STAGES = [
    ("yosys", _EQUIV, "equiv.log"),
    ("yosys-sequential", _STAGE1, "equiv_seq_stage1.log"),
    ("yosys-sequential", _STAGE2, "equiv_seq_stage2.log"),
]

_SCRIPTS = [row[:2] for row in _STAGES]

_NETLIST_BY_SCRIPT = {_EQUIV: "equiv_netlist.v", _STAGE1: "equiv_seq_netlist.v"}

_COMBINATIONAL_KEYS = [
    "schema_version",
    "engine",
    "engine_version",
    "yosys_binary",
    "sim_backend",
    "status",
    "gold",
    "gate",
    "port_map",
    "timeout_s",
    "elapsed_s",
    "counterexample",
    "diagnostics",
    "artifacts",
    "provenance",
]

_SEQUENTIAL_KEYS = [
    "schema_version",
    "engine",
    "engine_version",
    "yosys_binary",
    "sim_backend",
    "status",
    "gold",
    "gate",
    "port_map",
    "timeout_s",
    "elapsed_s",
    "induction_depth",
    "counterexample",
    "diagnostics",
    "artifacts",
    "provenance",
]

_PROVENANCE_KEYS = [
    "klt_version",
    "klt_git_commit",
    "klt_reinstall_spec",
    "klayout_version",
    "pdk",
    "deck",
    "input",
]

_RTL = "module top(input a, input b, output y);\n  assign y = a & b;\nendmodule\n"

_NETLIST = "module gold(a, b, y);\n  input a;\n  input b;\n  output y;\nendmodule\n"

_SAT_SUCCESS_TEXT = "SAT proof finished - no model found: SUCCESS!\n"

_STAGE1_ALL_PROVEN_TEXT = (
    "4. Executing EQUIV_STATUS pass.\nEquivalence successfully proven!\n"
)

_STAGE1_UNPROVEN_TEXT = (
    "Found 3 unproven $equiv cells (3 groups) in equiv:\n"
    "  Of those cells 1 are proven and 2 are unproven.\n"
)

_SUCCESS_BY_ENGINE = {
    "yosys": (_EQUIV, _SAT_SUCCESS_TEXT),
    "yosys-sequential": (_STAGE1, _STAGE1_ALL_PROVEN_TEXT),
}

_TIMEOUT_MESSAGE = {
    _EQUIV: "yosys did not complete within 60.0s (process killed)",
    _STAGE1: "yosys (stage 1: equiv_make/equiv_induct) did not complete within "
    "60.0s (process killed)",
    _STAGE2: "yosys (stage 2: bounded counterexample search) did not complete "
    "within 60.0s (process killed)",
}


@pytest.fixture(autouse=True)
def _bare_name_yosys_resolution(monkeypatch):
    real_which = shutil.which

    def fake_which(cmd, *args, **kwargs):
        if cmd == "yosys":
            return cmd
        return real_which(cmd, *args, **kwargs)

    monkeypatch.setattr(paths_module.shutil, "which", fake_which)


class _FakeYosys:
    """``subprocess.run`` stand-in keyed by generated script file name. Every
    script in ``stdout_by_script`` completes cleanly with its canned stdout
    and leaves the stage's netlist behind, except ``failing``: that one
    answers with ``stderr``/``returncode``, or raises when ``timeout`` or
    ``missing`` says the process was killed or could not be launched."""

    def __init__(
        self,
        stdout_by_script: dict[str, str],
        *,
        failing: str | None = None,
        stderr: str = "",
        returncode: int = 0,
        timeout: bool = False,
        missing: bool = False,
        netlists: bool = True,
    ) -> None:
        self._stdout_by_script = stdout_by_script
        self._failing = failing
        self._stderr = stderr
        self._returncode = returncode
        self._timeout = timeout
        self._missing = missing
        self._netlists = netlists
        self.scripts: list[str] = []

    def __call__(self, cmd, **kwargs):
        if cmd[1:] == ["-V"]:
            return fake_completed(stdout="Yosys 0.67 (git sha1 deadbeef)\n")
        assert cmd[1] == "-s", cmd
        script = Path(cmd[2]).name
        assert script in self._stdout_by_script, script
        self.scripts.append(script)
        if script == self._failing:
            if self._timeout:
                raise subprocess.TimeoutExpired(cmd=cmd, timeout=kwargs.get("timeout"))
            if self._missing:
                raise FileNotFoundError(2, "No such file or directory", cmd[0])
        written = _NETLIST_BY_SCRIPT.get(script) if self._netlists else None
        if written is not None:
            Path(cmd[2]).with_name(written).write_text(_NETLIST, encoding="utf-8")
        return fake_completed(
            stdout=self._stdout_by_script[script],
            stderr=self._stderr if script == self._failing else "",
            returncode=self._returncode if script == self._failing else 0,
        )


def _request(tmp_path: Path, engine: str | None = None, **extra) -> str:
    for name in ("gold.v", "gate.v"):
        (tmp_path / name).write_text(_RTL, encoding="utf-8")
    request: dict = {
        "gold": {"sources": ["gold.v"], "top": "top"},
        "gate": {"sources": ["gate.v"], "top": "top"},
    }
    if engine is not None:
        request["engine"] = engine
    request.update(extra)
    path = tmp_path / "r.json"
    path.write_text(json.dumps(request), encoding="utf-8")
    return str(path)


def _reaches(script: str, stdout: str) -> dict[str, str]:
    """Script answers making a run reach ``script``, see ``stdout`` there and
    finish every later stage."""
    if script == _EQUIV:
        return {_EQUIV: stdout}
    return {_STAGE1: _STAGE1_UNPROVEN_TEXT, _STAGE2: _SAT_SUCCESS_TEXT, script: stdout}


def _artifacts(tmp_path: Path) -> dict[str, object]:
    equiv_dir = tmp_path / ".klt" / "equiv"
    return {
        "script_path": str(equiv_dir / _EQUIV),
        "netlist_path": str(equiv_dir / "equiv_netlist.v"),
        "log_path": str(equiv_dir / "equiv.log"),
    }


@pytest.mark.parametrize("resume", [False, True])
def test_combinational_envelope_key_order(tmp_path, monkeypatch, resume):
    fake = _FakeYosys({_EQUIV: _SAT_SUCCESS_TEXT})
    monkeypatch.setattr(equiv.subprocess, "run", fake)

    report = run_equiv(_request(tmp_path), resume=resume)

    assert list(report) == [*_COMBINATIONAL_KEYS, *(["resume"] if resume else [])]
    assert report["artifacts"] == _artifacts(tmp_path)
    assert list(report["artifacts"]) == ["script_path", "netlist_path", "log_path"]
    assert report["status"] == "equivalent"
    assert fake.scripts == [_EQUIV]
    assert report.get("resume") == (
        {"resumed_stage": 0, "record_path": None} if resume else None
    )


def test_absent_netlist_artifact_is_null(tmp_path, monkeypatch):
    fake = _FakeYosys({_EQUIV: _SAT_SUCCESS_TEXT}, netlists=False)
    monkeypatch.setattr(equiv.subprocess, "run", fake)

    report = run_equiv(_request(tmp_path))

    assert report["artifacts"]["netlist_path"] is None


@pytest.mark.parametrize(
    ("stage2", "status", "resume"),
    [
        (False, "equivalent", False),
        (False, "equivalent", True),
        (True, "inconclusive", False),
        (True, "inconclusive", True),
    ],
)
def test_sequential_envelope_key_order(tmp_path, monkeypatch, stage2, status, resume):
    stage1 = _STAGE1_UNPROVEN_TEXT if stage2 else _STAGE1_ALL_PROVEN_TEXT
    stdouts = {_STAGE1: stage1, **({_STAGE2: _SAT_SUCCESS_TEXT} if stage2 else {})}
    fake = _FakeYosys(stdouts)
    monkeypatch.setattr(equiv.subprocess, "run", fake)

    report = run_equiv(
        _request(tmp_path, "yosys-sequential", induction_depth=6), resume=resume
    )

    assert list(report) == [*_SEQUENTIAL_KEYS, *(["resume"] if resume else [])]
    assert list(report["artifacts"]) == [
        "script_path",
        "netlist_path",
        "log_path",
        "stage2_script_path",
        "stage2_log_path",
        "stage1_blacklist_path",
    ]
    equiv_dir = tmp_path / ".klt" / "equiv"
    assert report["artifacts"] == {
        "script_path": str(equiv_dir / _STAGE1),
        "netlist_path": str(equiv_dir / "equiv_seq_netlist.v"),
        "log_path": str(equiv_dir / "equiv_seq_stage1.log"),
        "stage2_script_path": str(equiv_dir / _STAGE2) if stage2 else None,
        "stage2_log_path": str(equiv_dir / "equiv_seq_stage2.log") if stage2 else None,
        "stage1_blacklist_path": None,
    }
    assert report["status"] == status
    assert report["induction_depth"] == 6
    assert fake.scripts == [_STAGE1, *([_STAGE2] if stage2 else [])]
    assert report.get("resume") == (
        {"resumed_stage": 0, "record_path": str(equiv_dir / "stage1.commit.json")}
        if resume
        else None
    )


@pytest.mark.parametrize(("engine", "script", "log"), _STAGES)
@pytest.mark.parametrize("stderr", ["", "Warning: a yosys warning\n"])
def test_log_file_holds_stdout_then_stderr(
    tmp_path, monkeypatch, engine, script, log, stderr
):
    stdout = f"stdout of {script}\n"
    fake = _FakeYosys(_reaches(script, stdout), failing=script, stderr=stderr)
    monkeypatch.setattr(equiv.subprocess, "run", fake)

    run_equiv(_request(tmp_path, engine))

    expected = stdout if not stderr else stdout + "\n--- stderr ---\n" + stderr
    assert (tmp_path / ".klt" / "equiv" / log).read_text(encoding="utf-8") == expected


@pytest.mark.parametrize(("engine", "script", "log"), _STAGES)
def test_process_timeout_is_inconclusive_with_empty_logs(
    tmp_path, monkeypatch, engine, script, log
):
    fake = _FakeYosys(_reaches(script, ""), failing=script, timeout=True)
    monkeypatch.setattr(equiv.subprocess, "run", fake)

    report = run_equiv(_request(tmp_path, engine))

    assert report["status"] == "inconclusive"
    assert report["counterexample"] is None
    assert report["diagnostics"] == [
        {
            "severity": "error",
            "code": "process_timeout",
            "message": _TIMEOUT_MESSAGE[script]
            + " -- proof is inconclusive, not 'equivalent'",
        }
    ]
    equiv_dir = tmp_path / ".klt" / "equiv"
    assert sorted(path.name for path in equiv_dir.glob("*.log")) == sorted(
        {Path(name).with_suffix(".log").name for name in fake.scripts}
    )
    assert (equiv_dir / log).read_text(encoding="utf-8") == ""
    if script == _STAGE1:
        record = json.loads(
            (equiv_dir / "stage1.commit.json").read_text(encoding="utf-8")
        )
        assert record["partial"] is True


@pytest.mark.parametrize(("engine", "script"), _SCRIPTS)
def test_unlaunchable_yosys_is_a_clean_error(tmp_path, monkeypatch, engine, script):
    fake = _FakeYosys(_reaches(script, ""), failing=script, missing=True)
    monkeypatch.setattr(equiv.subprocess, "run", fake)

    with pytest.raises(EquivError, match=r"^could not launch yosys: "):
        run_equiv(_request(tmp_path, engine))

    equiv_dir = tmp_path / ".klt" / "equiv"
    assert [path.name for path in equiv_dir.glob("*.log")] == (
        ["equiv_seq_stage1.log"] if script == _STAGE2 else []
    )


@pytest.mark.parametrize(("engine", "script", "log"), _STAGES)
def test_failing_yosys_exit_is_reported_after_its_log_is_written(
    tmp_path, monkeypatch, engine, script, log
):
    stdout = f"stdout of {script}\n"
    fake = _FakeYosys(
        _reaches(script, stdout), failing=script, stderr="ERROR: boom\n", returncode=1
    )
    monkeypatch.setattr(equiv.subprocess, "run", fake)

    with pytest.raises(EquivError):
        run_equiv(_request(tmp_path, engine))

    written = tmp_path / ".klt" / "equiv" / log
    assert (
        written.read_text(encoding="utf-8")
        == stdout + "\n--- stderr ---\nERROR: boom\n"
    )


@pytest.mark.parametrize("engine", ["yosys", "yosys-sequential"])
def test_saved_input_digest_is_the_check_recipe(tmp_path, monkeypatch, engine):
    script, success = _SUCCESS_BY_ENGINE[engine]
    monkeypatch.setattr(equiv.subprocess, "run", _FakeYosys({script: success}))
    request = _request(tmp_path, engine)

    report = run_equiv(request)

    assert list(report["provenance"]) == _PROVENANCE_KEYS
    assert list(report["provenance"]["input"]) == ["content_hash", "role"]
    assert report["provenance"]["input"]["role"] == "source"
    assert report["provenance"]["input"]["content_hash"] == equiv._equiv_input_hash(
        report["gold"], report["gate"]
    )
    committed = tmp_path / "report.json"
    committed.write_text(json.dumps(report), encoding="utf-8")
    assert equiv.check_equiv_report(str(committed), request)["status"] == "match"


def test_single_source_digest_is_the_per_file_content_hash(tmp_path):
    source = tmp_path / "only.v"
    source.write_text(_RTL, encoding="utf-8")
    gold = {"sources": [str(source)]}
    gate = {"sources": []}

    report = equiv._build_report(
        engine="yosys",
        engine_version=None,
        yosys_binary="yosys",
        sim_backend="iverilog",
        status="equivalent",
        gold=gold,
        gate=gate,
        port_map=None,
        timeout_s=60.0,
        elapsed_s=0.0,
        counterexample=None,
        diagnostics=[],
        script_path="equiv.ys",
        netlist_path="equiv_netlist.v",
        log_path="equiv.log",
    )

    assert report["provenance"]["input"] == {
        "content_hash": content_hash(str(source)),
        "role": "source",
    }
    assert report["provenance"]["input"]["content_hash"] == equiv._equiv_input_hash(
        gold, gate
    )
