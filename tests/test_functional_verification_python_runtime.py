"""The Icarus Python runtime pre-flight (issue #8015), stubbed tier.

``klt functional-verification`` measures, before building the user's design,
the CPython the ``vvp`` process actually loads for cocotb, and refuses the run
only on a confirmed release difference. These tests drive the real
``run_functional_verification`` entry point with cocotb's runner faked and
the probe's forked child run inline; the real-Icarus tier lives in
``test_functional_verification_python_runtime_live.py``.
"""

from __future__ import annotations

import collections
import errno
import importlib.metadata
import json
import os
import shutil
import sys
import tempfile
import types
from pathlib import Path
from unittest.mock import patch

import pytest

from helpers.cocotb_fakes import FakeCocotbRunner
from klayout_tools import functional_verification as fv
from klayout_tools import functional_verification_python_runtime as rt
from klayout_tools.functional_verification import (
    FunctionalVerificationError,
    run_functional_verification,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
OTHER_LIBRARY = "/usr/lib/x86_64-linux-gnu/libpython3.13.so.1.0"
RC1 = "3.13.0rc1 (main, Oct  8 2024, 12:20:24) [GCC 13.2.0]"
#: What cocotb's embed logs at startup: the wheel's compile-time version.
BANNER = (
    "     -.--ns INFO     pygpi   embed.cpp:114  in initialize   "
    "Using Python 3.12.12 interpreter at <venv>/bin/python\n"
)
PASSING = (
    '<testsuites name="results"><testsuite name="all">'
    '<testcase name="test_gcd_known_pairs" classname="test_gcd" time="0.001" />'
    "</testsuite></testsuites>"
)
_VersionInfo = collections.namedtuple(
    "version_info", "major minor micro releaselevel serial"
)


def _own_runtime_text() -> str:
    return f"{'.'.join(map(str, sys.version_info[:3]))} (main, built) [GCC]"


class ProbingRunner(FakeCocotbRunner):
    """``FakeCocotbRunner`` whose ``test()`` also stands in for the probe's
    cocotb test: it writes the measurement the real one would and logs a
    banner, and records every call so a test can tell whose build ran."""

    def __init__(self, measurement, results_xml_text=PASSING, banner=BANNER):
        super().__init__(results_xml_text)
        self.measurement = measurement
        self.banner = banner
        self.calls: list[str] = []
        self.probe_build: dict | None = None
        self.probe_test: dict | None = None
        self.probe_sys_path: list[str] = []
        self.probe_verilog = ""
        self.probe_module_bytes = b""

    def build(self, **kwargs):
        self.calls.append("build")
        if self.probe_build is None and kwargs["hdl_toplevel"] == rt.PROBE_MODULE:
            self.probe_build = kwargs
            self.probe_verilog = Path(kwargs["sources"][0]).read_text()
        super().build(**kwargs)

    def test(self, **kwargs):
        self.calls.append("test")
        if kwargs["test_module"] != rt.PROBE_MODULE:
            return super().test(**kwargs)
        self.probe_test = kwargs
        self.probe_sys_path = list(sys.path)  # what cocotb propagates as PYTHONPATH
        # The simulator imports the module through PYTHONPATH, not its cwd.
        self.probe_module_bytes = Path(
            self.probe_sys_path[0], f"{rt.PROBE_MODULE}.py"
        ).read_bytes()
        Path(kwargs["log_file"]).write_text(self.banner, encoding="utf-8")
        Path(self.probe_sys_path[0], "result.json").write_text(
            json.dumps(self.measurement), encoding="utf-8"
        )


def _inline_bounded(target, **kwargs):
    """Stand-in for the forked, bounded child: run the probe in-process."""
    try:
        with patch.dict(os.environ):
            return {"status": "measured", **target(**kwargs)}
    except Exception as exc:
        return rt._inconclusive(f"{type(exc).__name__}: {exc}")


def _stage(tmp_path: Path, **overrides) -> str:
    (tmp_path / "gcd.v").write_text("module gcd(input wire clk); endmodule\n")
    (tmp_path / "test_gcd.py").write_text("# stub testbench -- never imported\n")
    request = {
        "engine": "icarus",
        "sources": ["gcd.v"],
        "hdl_toplevel": "gcd",
        "testbench": {"module": "test_gcd", "testcase": None},
        "options": {"coverage": False, "timescale": ["1ns", "1ps"]},
    }
    request.update(overrides)
    (tmp_path / "request.json").write_text(json.dumps(request))
    return str(tmp_path / "request.json")


def _stub_runner(monkeypatch, runner: ProbingRunner):
    monkeypatch.setattr(
        fv, "_import_runner", lambda: types.SimpleNamespace(get_runner=lambda n: runner)
    )
    monkeypatch.setattr(fv, "_engine_version", lambda engine: "13.0")
    monkeypatch.setattr(fv, "_cocotb_version", lambda: "2.1.0")


def _stub_wheel(monkeypatch, tag: str) -> None:
    text = f"Wheel-Version: 1.0\nTag: {tag}\n"
    wheel = types.SimpleNamespace(read_text=lambda filename: text)
    monkeypatch.setattr(importlib.metadata, "distribution", lambda name: wheel)
    monkeypatch.setattr(importlib.metadata, "version", lambda name: "2.1.0")


def _klt_on(monkeypatch, major: int, minor: int, micro: int, level="final", serial=0):
    monkeypatch.setattr(
        sys, "version_info", _VersionInfo(major, minor, micro, level, serial)
    )


# -- release parsing ---------------------------------------------------------


@pytest.mark.parametrize(
    "text,expected",
    [
        (
            "3.14.9 (main, Oct 21 2026, 17:48:02) [Clang 23.1.3 ]",
            (3, 14, 9, "final", 0),
        ),
        (RC1, (3, 13, 0, "candidate", 1)),
        ("3.13.0b2 (main, ...)", (3, 13, 0, "beta", 2)),
        ("3.13.0alpha1", (3, 13, 0, "alpha", 1)),
        ("3.12", (3, 12, 0, "final", 0)),
    ],
)
def test_release_reads_every_component_but_the_build_text(text, expected):
    assert rt._release(text) == expected


@pytest.mark.parametrize("text", ["", "Python 3.12.3", "3.13.x", "v3.12"])
def test_release_is_none_for_non_release_text(text):
    assert rt._release(text) is None


def test_format_release_matches_platform_python_version_spelling():
    fmt = rt._format_release
    assert fmt((3, 13, 18, "final", 0)) == "3.13.18"
    assert fmt((3, 13, 0, "candidate", 1)) == "3.13.0rc1"
    assert fmt((3, 13, 0, "beta", 2)) == "3.13.0b2"


def test_release_of_this_interpreter_is_sys_version_info():
    assert rt._klt_release() == tuple(sys.version_info)


# -- the entry point ---------------------------------------------------------


def test_confirmed_mismatch_refuses_before_the_user_build(tmp_path, monkeypatch):
    """A matching ``cp313`` wheel tag, klt on 3.13.18, and a probe that
    measures 3.13.0rc1 from a distinct library: both versions and that path
    are named, and the user's build and test never start."""
    request = _stage(tmp_path)
    _klt_on(monkeypatch, 3, 13, 18)
    _stub_wheel(monkeypatch, "cp313-cp313-manylinux_2_17_x86_64")
    runner = ProbingRunner({"py_get_version": RC1, "library": OTHER_LIBRARY})
    _stub_runner(monkeypatch, runner)
    monkeypatch.setattr(rt, "_run_bounded", _inline_bounded)

    with pytest.raises(FunctionalVerificationError) as excinfo:
        run_functional_verification(request)

    message = str(excinfo.value)
    assert "klt is running on CPython 3.13.18" in message
    assert "loads CPython 3.13.0rc1 for cocotb" in message
    assert f"supplied by {OTHER_LIBRARY}" in message
    assert RC1 in message
    assert runner.calls == ["build", "test"]  # the probe's, nothing of the user's
    assert runner.probe_build["hdl_toplevel"] == rt.PROBE_MODULE


def test_build_text_and_a_configured_libpython_never_decide(monkeypatch):
    """Same release, other compiler and build date, and a ``LIBPYTHON_LOC``
    naming another library: neither is evidence, the measurement is."""
    _klt_on(monkeypatch, 3, 13, 18)
    monkeypatch.setenv("LIBPYTHON_LOC", "/opt/elsewhere/libpython3.13.so.1.0")
    outcome = {
        "status": "measured",
        "py_get_version": "3.13.18 (main, another date) [another compiler]",
        "library": OTHER_LIBRARY,
    }
    monkeypatch.setattr(rt, "_measure_vvp_python", lambda **kwargs: outcome)

    rt._check_icarus_python_runtime(object(), "icarus", [], {}, ("1ns", "1ps"), ".")


def test_prerelease_serial_decides(monkeypatch):
    """3.13.0rc1 is not 3.13.0: the level and the serial are compared."""
    _klt_on(monkeypatch, 3, 13, 0)
    outcome = {"status": "measured", "py_get_version": RC1, "library": OTHER_LIBRARY}
    monkeypatch.setattr(rt, "_measure_vvp_python", lambda **kwargs: outcome)

    with pytest.raises(FunctionalVerificationError, match="loads CPython 3.13.0rc1"):
        rt._check_icarus_python_runtime(object(), "icarus", [], {}, ("1ns", "1ps"), ".")


def test_matching_runtime_proceeds_despite_a_different_banner(tmp_path, monkeypatch):
    """The banner names another build version; ``Py_GetVersion()`` inside
    ``vvp`` names klt's own. The run proceeds through the user's build."""
    other_build = ".".join(map(str, sys.version_info[:2])) + ".99"
    banner = BANNER.replace("3.12.12", other_build)
    request = _stage(tmp_path)
    runner = ProbingRunner(
        {"py_get_version": _own_runtime_text(), "library": OTHER_LIBRARY}, banner=banner
    )
    _stub_runner(monkeypatch, runner)
    monkeypatch.setattr(rt, "_run_bounded", _inline_bounded)

    report = run_functional_verification(request)

    assert rt._release(banner.partition("Using Python ")[2]) != rt._klt_release()
    assert report["status"] == "pass"
    assert runner.calls == ["build", "test", "build", "test"]


@pytest.mark.parametrize(
    "outcome",
    [
        rt._inconclusive("RuntimeError: the probe test left no readable result"),
        {"status": "measured", "py_get_version": "not a release", "library": None},
    ],
)
def test_inconclusive_probe_lets_the_ordinary_run_continue(
    tmp_path, monkeypatch, outcome
):
    """No measurement, or one that parses to nothing: the user's run happens
    exactly as before and keeps its own diagnostics."""
    request = _stage(tmp_path)
    runner = ProbingRunner(None)
    _stub_runner(monkeypatch, runner)
    monkeypatch.setattr(rt, "_measure_vvp_python", lambda **kwargs: outcome)

    report = run_functional_verification(request)

    assert report["status"] == "pass"
    assert runner.calls == ["build", "test"]


def test_unresolved_library_path_is_still_a_confirmed_mismatch(monkeypatch):
    """``dladdr`` failing costs the path, not the finding."""
    outcome = {"status": "measured", "py_get_version": RC1, "library": None}
    monkeypatch.setattr(rt, "_measure_vvp_python", lambda **kwargs: outcome)
    _klt_on(monkeypatch, 3, 13, 18)

    with pytest.raises(FunctionalVerificationError) as excinfo:
        rt._check_icarus_python_runtime(object(), "icarus", [], {}, ("1ns", "1ps"), ".")

    assert "CPython 3.13.0rc1" in str(excinfo.value)
    assert "path could not be resolved" in str(excinfo.value)


def test_wheel_tag_mismatch_still_refuses_first(tmp_path, monkeypatch):
    """The existing check runs before the probe and needs nothing from it."""
    request = _stage(tmp_path)
    runner = ProbingRunner({"py_get_version": RC1, "library": OTHER_LIBRARY})
    _stub_runner(monkeypatch, runner)
    _stub_wheel(monkeypatch, "cp27-cp27mu-manylinux_2_17_x86_64")
    monkeypatch.setattr(
        rt,
        "_measure_vvp_python",
        lambda **kwargs: pytest.fail(
            "the probe must not run after a refused wheel tag"
        ),
    )

    with pytest.raises(FunctionalVerificationError, match="cp27-cp27mu"):
        run_functional_verification(request)

    assert runner.calls == []


def test_verilator_engine_skips_the_probe(tmp_path, monkeypatch):
    request = _stage(tmp_path, engine="verilator")
    runner = ProbingRunner(None)
    _stub_runner(monkeypatch, runner)
    monkeypatch.setattr(
        rt,
        "_measure_vvp_python",
        lambda **kwargs: pytest.fail("Verilator is out of scope"),
    )

    report = run_functional_verification(request)

    assert report["engine"] == "verilator"
    assert runner.calls == ["build", "test"]


def test_verilog_bench_never_reaches_the_probe(tmp_path, monkeypatch):
    """The pure-Verilog path returns before cocotb is even imported."""
    example = REPO_ROOT / "examples" / "functional-verification" / "verilog-tb"
    for entry in example.iterdir():
        (tmp_path / entry.name).write_bytes(entry.read_bytes())
    monkeypatch.setattr(
        rt,
        "_measure_vvp_python",
        lambda **kwargs: pytest.fail("a Verilog bench loads no Python"),
    )

    class _ReachedVerilogTop(Exception):
        pass

    def _sentinel(*args, **kwargs):
        raise _ReachedVerilogTop

    monkeypatch.setattr(fv, "_run_verilog_top", _sentinel)
    with pytest.raises(_ReachedVerilogTop):
        run_functional_verification(str(tmp_path / "request-rtl.json"))


def test_probe_sees_the_request_env_timescale_and_import_path(tmp_path, monkeypatch):
    """The probe goes through the request's own ``_run_build``/``_run_test``
    with what the real run will get -- ``request.env``, the timescale, the
    testbench's search dirs -- and with nothing that could keep its single
    test from running (testcase, plusargs, build args)."""
    (tmp_path / "benches").mkdir()
    (tmp_path / "benches" / "helper.py").write_text("")
    request = _stage(
        tmp_path,
        env={"MY_VAR": "1"},
        options={
            "coverage": False,
            "timescale": ["10ns", "1ps"],
            "build_args": ["-Wall"],
        },
        testbench={
            "module": "test_gcd",
            "search_path": [str(tmp_path), str(tmp_path / "benches")],
            "plusargs": ["+ignore=1"],
        },
    )
    runner = ProbingRunner({"py_get_version": _own_runtime_text(), "library": None})
    _stub_runner(monkeypatch, runner)
    monkeypatch.setattr(rt, "_run_bounded", _inline_bounded)
    run_functional_verification(request)

    build, test = runner.probe_build, runner.probe_test
    workdir = Path(build["sources"][0]).parent
    assert build["sources"] == [str(workdir / f"{rt.PROBE_MODULE}.v")]
    assert build["build_args"] == [] and build["hdl_toplevel"] == rt.PROBE_MODULE
    assert (build["timescale"], test["timescale"]) == (("10ns", "1ps"),) * 2
    assert test["test_module"] == test["hdl_toplevel"] == rt.PROBE_MODULE
    assert test["testcase"] is None and test["plusargs"] == []
    assert test["extra_env"] == {"MY_VAR": "1"}
    # ``vvp`` starts where the user's run starts (a relative PATH entry resolves
    # from there); the probe's own artifacts stay in the scratch directory.
    assert test["test_dir"] == runner.test_kwargs["test_dir"] != str(workdir)
    assert Path(test["log_file"]).parent == Path(test["results_xml"]).parent == workdir
    assert runner.probe_sys_path[:3] == [
        str(workdir),
        str(tmp_path),
        str(tmp_path / "benches"),
    ]
    assert (
        runner.probe_module_bytes
        == (REPO_ROOT / "src" / "klayout_tools" / rt._PROBE_TEST_SOURCE).read_bytes()
    )
    assert runner.probe_verilog == f"module {rt.PROBE_MODULE};\nendmodule\n"
    # The user's own build still gets its build args and plusargs.
    assert runner.build_kwargs["build_args"] == ["-Wall"]
    assert runner.test_kwargs["plusargs"] == ["+ignore=1"]


def _disk_full_copy(real_copy):
    def copy(src, dst, **kwargs):
        if Path(dst).name == f"{rt.PROBE_MODULE}.py":
            raise OSError(errno.ENOSPC, os.strerror(errno.ENOSPC))
        return real_copy(src, dst, **kwargs)

    return copy


def _no_scratch_dir(*args, **kwargs):
    raise OSError(errno.EACCES, os.strerror(errno.EACCES))


@pytest.mark.parametrize("failing", ["copy-into-scratch", "create-scratch"])
def test_probe_setup_failure_lets_the_users_build_proceed(
    tmp_path, monkeypatch, failing
):
    """A full disk (or unwritable temp dir) costs the probe, not the run: the
    entry point goes on to the user's own build and test, with no exception."""
    request = _stage(tmp_path)
    runner = ProbingRunner(None)
    _stub_runner(monkeypatch, runner)
    if failing == "copy-into-scratch":
        monkeypatch.setattr(shutil, "copyfile", _disk_full_copy(shutil.copyfile))
    else:
        monkeypatch.setattr(tempfile, "mkdtemp", _no_scratch_dir)

    report = run_functional_verification(request)

    assert report["status"] == "pass"
    assert runner.calls == ["build", "test"]  # the user's only; no probe ran
    assert runner.probe_build is None


# -- the post-run wording ----------------------------------------------------


@pytest.mark.parametrize("engine", ["icarus", "verilator"])
def test_transcript_marker_is_a_lead_not_proof_of_a_wrong_wheel(engine):
    hint = rt._interpreter_mismatch_hint(engine, "FATAL: _embed_init_python failed")

    assert hint.startswith(f"probable cause: the {engine} transcript contains ")
    assert "'FATAL: _embed_init_python failed'" in hint
    assert "not proof that the wheel is wrong" in hint
    assert ("Python runtime probe" in hint) == (engine == "icarus")


def test_stubbed_run_keeps_its_marker_diagnostic(tmp_path, monkeypatch):
    """The ordinary run's own failure diagnostics survive an inconclusive
    probe: a missing ``results.xml`` plus a transcript marker still names
    the marker as a probable cause."""
    request = _stage(tmp_path)
    runner = ProbingRunner(None, results_xml_text=None)
    runner.extra_test_log = "ERROR gpi ... Unexpected sys.executable value (x)\n"
    _stub_runner(monkeypatch, runner)
    monkeypatch.setattr(
        rt, "_measure_vvp_python", lambda **kwargs: rt._inconclusive("x")
    )

    with pytest.raises(FunctionalVerificationError) as excinfo:
        run_functional_verification(request)

    message = str(excinfo.value)
    assert "produced no results file" in message
    assert "probable cause" in message
    assert "Unexpected sys.executable value" in message
