"""`request.requires_tools`: build-time tool pins resolved before any build
(issue #4473). Stub executables on a tmp ``PATH``; no real simulator."""

from __future__ import annotations

import hashlib
import json
import stat
from pathlib import Path

import pytest

from klayout_tools import functional_verification as fv
from klayout_tools.functional_verification import (
    FunctionalVerificationError,
    run_functional_verification,
)
from test_functional_verification import (
    _RESULTS_XML_WITH_SKIP,
    _base_request,
    _FakeRunner,
    _setup_inputs,
    _stub_runner,
    _write_request,
)


def _make_tool(bindir: Path, name: str, version_line: str, rc: int = 0) -> Path:
    bindir.mkdir(exist_ok=True)
    path = bindir / name
    path.write_text(f"#!/bin/sh\necho '{version_line}'\nexit {rc}\n", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _request(tmp_path: Path, bindir: Path, tools) -> str:
    _setup_inputs(tmp_path)
    return _write_request(
        tmp_path / "request.json",
        _base_request(env={"PATH": str(bindir)}, requires_tools=tools),
    )


@pytest.mark.parametrize(
    "tools",
    [
        "sail",
        {"name": "sail"},
        [["sail"]],
        [{}],
        [{"name": ""}],
        [{"name": 3}],
        [{"name": "a", "version": 1}],
        [{"name": "a", "sha256": 2}],
        [{"name": "a", "bogus": "x"}],
    ],
)
def test_malformed_requires_tools_is_a_request_error(tmp_path, tools):
    _setup_inputs(tmp_path)
    request_path = _write_request(
        tmp_path / "request.json", _base_request(requires_tools=tools)
    )
    with pytest.raises(FunctionalVerificationError, match="request.requires_tools"):
        run_functional_verification(request_path)


def test_all_unmet_tools_listed_and_no_build_started(tmp_path, monkeypatch):
    bindir = tmp_path / "bin"
    tool = _make_tool(bindir, "sail", "Sail 0.19")
    request_path = _request(
        tmp_path,
        bindir,
        [
            {"name": "definitely-not-a-tool"},
            {"name": "sail", "version": "9.9"},
            {"name": "sail", "sha256": "0" * 64},
            {"name": "also-missing"},
        ],
    )
    runner = _stub_runner(monkeypatch, _FakeRunner(_RESULTS_XML_WITH_SKIP))
    with pytest.raises(FunctionalVerificationError) as info:
        run_functional_verification(request_path)

    exc = info.value
    assert exc.code == "missing_tool"
    assert [u["name"] for u in exc.details["unmet_tools"]] == [
        "definitely-not-a-tool",
        "sail",
        "sail",
        "also-missing",
    ]
    assert exc.details["unmet_tools"][1]["found"]["version"] == "Sail 0.19"
    assert exc.details["unmet_tools"][2]["found"]["sha256"] == _sha(tool)
    for name in ("definitely-not-a-tool", "also-missing"):
        assert name in str(exc)
    assert not list(tmp_path.glob("**/build_*.log"))
    assert not hasattr(runner, "build_kwargs") or not runner.build_kwargs


def test_present_tools_are_echoed_and_path_from_request_env_is_used(
    tmp_path, monkeypatch
):
    bindir = tmp_path / "bin"
    tool = _make_tool(bindir, "sail", "Sail 0.19.1", rc=1)  # non-zero --version
    request_path = _request(
        tmp_path,
        bindir,
        [{"name": "sail", "version": "0.19", "sha256": _sha(tool).upper()}],
    )
    _stub_runner(monkeypatch, _FakeRunner(_RESULTS_XML_WITH_SKIP))

    response = run_functional_verification(request_path)

    assert response["environment"]["tools"] == [
        {
            "name": "sail",
            "path": str(tool),
            "version": "Sail 0.19.1",
            "sha256": _sha(tool),
        }
    ]


@pytest.mark.parametrize("omitted", [True, False])
def test_omitted_or_empty_requires_tools_only_adds_empty_tools(
    tmp_path, monkeypatch, omitted
):
    _setup_inputs(tmp_path)
    extra = {} if omitted else {"requires_tools": []}
    request_path = _write_request(tmp_path / "request.json", _base_request(**extra))
    _stub_runner(monkeypatch, _FakeRunner(_RESULTS_XML_WITH_SKIP))

    response = run_functional_verification(request_path)

    assert response["environment"]["tools"] == []


def test_mutations_run_checks_tools_before_anything_else(tmp_path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    request_path = _request(tmp_path, bindir, [{"name": "nope"}])
    proposals = tmp_path / "proposals.json"
    proposals.write_text(
        json.dumps({"scope": ["gcd.v"], "proposals": []}), encoding="utf-8"
    )
    with pytest.raises(FunctionalVerificationError) as info:
        fv.run_functional_verification_with_mutations(request_path, str(proposals))
    assert info.value.code == "missing_tool"
