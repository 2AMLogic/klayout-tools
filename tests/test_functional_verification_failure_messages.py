"""Portable RTL source locations in cocotb report diagnostics."""

from __future__ import annotations

import hashlib
from pathlib import Path
from xml.etree import ElementTree

import pytest

from klayout_tools.functional_verification import run_functional_verification
from test_functional_verification import (
    _base_request,
    _FakeRunner,
    _setup_inputs,
    _stub_runner,
    _write_request,
)
from test_functional_verification_initial_state import _request, _stage, _state


@pytest.mark.parametrize("message_field", ["error_msg", "message", "text"])
def test_failure_messages_survive_checkout_relocation(
    tmp_path, monkeypatch, message_field
):
    messages = []
    for host in ("one", "two"):
        checkout = tmp_path / host / "checkout"
        bench = checkout / "bench"
        bench.mkdir(parents=True)
        (checkout / ".git").mkdir()
        _setup_inputs(bench)
        rtl = checkout / "rtl files" / "gcd.v"
        rtl.parent.mkdir()
        (bench / "gcd.v").rename(rtl)
        external = checkout.parent / "cells.v"
        external.write_text("module cell; endmodule\n")
        request = _write_request(
            bench / "request.json",
            _base_request(sources=["../rtl files/gcd.v", "../../cells.v"]),
        )
        diagnostic = (
            f"assert dut.gcd.result == 6\nHierarchyObject(dut.gcd at {rtl}:17) "
            f"uses '{external}:3'"
        )
        suite = ElementTree.Element("testsuite")
        case = ElementTree.SubElement(suite, "testcase", name="test_gcd")
        failure = ElementTree.SubElement(case, "failure", error_type="AssertionError")
        if message_field == "text":
            failure.text = diagnostic
        else:
            failure.set(message_field, diagnostic)
        xml = ElementTree.tostring(suite, encoding="unicode")
        _stub_runner(monkeypatch, _FakeRunner(xml))

        report = run_functional_verification(request)

        assert report["status"] == "fail"
        assert (report["test_count"], report["failed_count"]) == (1, 1)
        (test,) = report["tests"]
        assert (test["name"], test["error_type"], test["status"]) == (
            "test_gcd",
            "AssertionError",
            "failed",
        )
        messages.append(test["error_message"])
        raw = Path(report.local_paths["results_xml"]).read_bytes()
        assert raw == xml.encode()
        assert report["environment"]["results_xml"]["sha256"] == (
            "sha256:" + hashlib.sha256(raw).hexdigest()
        )

    expected = (
        "assert dut.gcd.result == 6\n"
        "HierarchyObject(dut.gcd at rtl files/gcd.v:17) uses 'environment.sources[1]:3'"
    )
    assert messages == [expected, expected]


def test_only_known_source_references_change(tmp_path, monkeypatch):
    _setup_inputs(tmp_path)
    source = tmp_path / "gcd.v"
    other_paths = f"{source}.bak {source}~ /unrelated{source}"
    xml = f"""<testsuite>
      <testcase name="paths">
        <failure message="{source}:17 vs {other_paths}"/>
      </testcase>
      <testcase name="plain"><failure message="expected 42"/></testcase>
      <testcase name="missing"><failure/></testcase>
      <testcase name="passed"/><testcase name="skipped"><skipped/></testcase>
    </testsuite>"""
    _stub_runner(monkeypatch, _FakeRunner(xml))
    request = _write_request(tmp_path / "request.json", _base_request())

    report = run_functional_verification(request)

    paths, plain, missing, passed, skipped = report["tests"]
    assert paths["error_message"] == f"gcd.v:17 vs {other_paths}"
    assert plain["error_message"] == "expected 42"
    assert missing["error_message"] is None
    assert "error_message" not in passed and "error_message" not in skipped
    assert (
        report["passed_count"],
        report["failed_count"],
        report["skipped_count"],
    ) == (1, 3, 1)


def test_each_initial_state_seed_reports_portable_failures(tmp_path, monkeypatch):
    _stage(tmp_path)
    xml = f"""<testsuite><testcase name="test_top">
      <failure message="see {tmp_path / "net.v"}:8"/>
    </testcase></testsuite>"""
    _stub_runner(monkeypatch, _FakeRunner(xml))
    report = run_functional_verification(_request(tmp_path, _state(seeds=[3, 7])))
    assert [(t["initial_state_seed"], t["error_message"]) for t in report["tests"]] == [
        (3, "see net.v:8"),
        (7, "see net.v:8"),
    ]
