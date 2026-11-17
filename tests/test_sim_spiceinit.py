"""`klt sim`'s `environment.ngspice_init` init-file provenance (issue #5648)."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from klayout_tools import sim
from test_sim import (
    _stub_subprocess_run,
    _write_body,
    _write_request,
)

# Issue #5648: `environment.ngspice_init` records the init file ngspice read.


def _init_report(tmp_path, monkeypatch, *, home_init=None, options=None, **kw):
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    if home_init is not None:
        (home / ".spiceinit").write_text(home_init)
    monkeypatch.setenv("HOME", str(home))
    _write_body(tmp_path)
    request = _write_request(
        tmp_path,
        {
            "netlist": "body.spice",
            "analysis": {"kind": "tran", "args": "1n 1u"},
            "options": options or {},
        },
    )
    _stub_subprocess_run(monkeypatch)
    return sim.run_sim(str(request), **kw)


def _sha(text):
    import hashlib

    return "sha256:" + hashlib.sha256(text.encode()).hexdigest()


def test_ngspice_init_env_request_source(tmp_path, monkeypatch):
    report = _init_report(
        tmp_path, monkeypatch, options={"ngspice_init": ["set wnflag=1"]}
    )
    assert report["environment"]["ngspice_init"] == {
        "source": "request",
        "lines": ["set wnflag=1"],
        "sha256": _sha("set wnflag=1\n"),
    }


def test_ngspice_init_env_thread_pin_source(tmp_path, monkeypatch):
    report = _init_report(tmp_path, monkeypatch, options={"threads_per_worker": 2})
    assert report["environment"]["ngspice_init"] == {
        "source": "thread_pin",
        "lines": ["set num_threads=2"],
        "sha256": _sha("set num_threads=2\n"),
    }


def test_ngspice_init_env_home_source_and_inherited_warning(
    tmp_path, monkeypatch, capsys
):
    report = _init_report(tmp_path, monkeypatch, home_init="set wnflag=1\n")
    block = report["environment"]["ngspice_init"]
    assert block == {
        "source": "home",
        "lines": None,
        "sha256": _sha("set wnflag=1\n"),
    }
    err = capsys.readouterr().err
    assert err.count("klt: warning:") == 1
    assert "options.ngspice_init" in err and ".spiceinit" in err
    assert str(tmp_path) not in json.dumps(block)


def test_ngspice_init_env_none_source_and_no_warning(tmp_path, monkeypatch, capsys):
    report = _init_report(tmp_path, monkeypatch)
    assert report["environment"]["ngspice_init"] == {
        "source": "none",
        "lines": None,
        "sha256": None,
    }
    assert "klt: warning:" not in capsys.readouterr().err


def test_ngspice_init_env_empty_home_file_is_home(tmp_path, monkeypatch):
    report = _init_report(tmp_path, monkeypatch, home_init="")
    assert report["environment"]["ngspice_init"]["source"] == "home"
    assert report["environment"]["ngspice_init"]["sha256"] == _sha("")


def test_ngspice_init_env_differs_between_home_files(tmp_path, monkeypatch):
    one = _init_report(tmp_path, monkeypatch, home_init="set wnflag=1\n")
    two = _init_report(tmp_path, monkeypatch, home_init="set wnflag=0\n")
    assert (
        one["environment"]["ngspice_init"]["sha256"]
        != two["environment"]["ngspice_init"]["sha256"]
    )


def test_ngspice_init_suppressed_home_warning(tmp_path, monkeypatch, capsys):
    report = _init_report(
        tmp_path,
        monkeypatch,
        home_init="set wnflag=1\n",
        options={"threads_per_worker": 1},
    )
    assert report["environment"]["ngspice_init"]["source"] == "thread_pin"
    err = capsys.readouterr().err
    assert err.count("klt: warning:") == 1
    assert "replaces" in err


def test_ngspice_init_env_null_for_xyce_and_runner_offhost():
    from klayout_tools import sim_spiceinit

    assert sim_spiceinit.effective_init((), (), engine="xyce", offhost=False) is None
    assert sim_spiceinit.effective_init(
        ("a",), ("a",), engine="ngspice", offhost=True
    ) == {"source": "runner", "lines": None, "sha256": None}


# Issue #6047: the fail-fast probe reads the same init file as the corners.


@pytest.mark.parametrize(
    ("backend", "corners", "options", "expected"),
    [
        # Explicit pin, `ngspice_init` undeclared (issue #6047's repro).
        ("local", {}, {"threads_per_worker": 1}, "set num_threads=1\n"),
        # Implicit pin: local-parallel, >1 worker and >1 unit.
        (
            "local-parallel",
            {"temperature_c": [10, 20]},
            {"max_workers": 2},
            "set num_threads=1\n",
        ),
        # Pin resolves to None: no probe-dir file, behaviour unchanged.
        ("local", {}, {}, None),
        # A declared init still follows the pin line, as for the corners.
        (
            "local",
            {},
            {"threads_per_worker": 1, "ngspice_init": ["set wnflag=1"]},
            "set num_threads=1\nset wnflag=1\n",
        ),
    ],
)
def test_fail_fast_probe_spiceinit_matches_corner_pin(
    tmp_path, monkeypatch, backend, corners, options, expected
):
    """The probe and the corners it screens read one effective `.spiceinit`
    (issue #6047): a run-dir file replaces `$HOME`'s, so an unpinned probe
    would otherwise read the ambient home file the pinned corners do not."""
    _write_body(tmp_path)
    request = _write_request(
        tmp_path,
        {
            "netlist": "body.spice",
            "backend": backend,
            "corners": corners,
            "analysis": {"kind": "tran", "args": "1n 1u"},
            "options": {"keep_artifacts": True, "fail_fast_probe": True, **options},
        },
    )
    _stub_subprocess_run(monkeypatch)

    report = sim.run_sim(str(request), artifacts_dir=str(tmp_path / "artifacts"))

    probe_dirs = sorted(Path(tmp_path, "artifacts").glob("*__probe"))
    assert len(probe_dirs) == 1
    probe_init = probe_dirs[0] / ".spiceinit"
    corner_dir = Path(os.path.dirname(report["corners"][0]["artifacts"]["log"]))
    corner_init = corner_dir / ".spiceinit"
    if expected is None:
        assert not probe_init.exists()
        assert not corner_init.exists()
    else:
        assert probe_init.read_text() == expected
        assert probe_init.read_text() == corner_init.read_text()
