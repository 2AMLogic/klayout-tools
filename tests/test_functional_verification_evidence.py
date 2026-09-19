"""Zero-execution and count-consistency regressions for issue #2096.

XML and envelopes here are synthetic; the runner never invokes a simulator.
Producer output is fed directly into both signoff entry points so a trusted
status alone cannot accidentally qualify an unchecked design.
"""

from __future__ import annotations

import copy
import json

import pytest

from klayout_tools.cli import main
from klayout_tools.functional_verification import (
    FunctionalVerificationError,
    run_functional_verification,
)
from klayout_tools.signoff import SignoffError, build_signoff, build_tier_report
from test_functional_verification import (
    _RESULTS_XML_EMPTY,
    _RESULTS_XML_WITH_FAILURE,
    _RESULTS_XML_WITH_SKIP,
    _base_request,
    _FakeRunner,
    _setup_inputs,
    _stub_runner,
    _write_request,
)
from test_signoff import _manifest, _write

_ALL_SKIPPED_XML = (
    '<testsuites><testsuite><testcase name="skipped">'
    "<skipped/></testcase></testsuite></testsuites>"
)


def _assert_signoff_verdict(tmp_path, envelope, *, passed):
    path = _write(tmp_path, "functional.json", envelope)
    aggregate = build_signoff([path])
    assert aggregate["status"] == ("pass" if passed else "fail")
    assert aggregate["checks"][0]["kind"] == "functional-verification"
    assert aggregate["checks"][0]["passed"] is passed

    # Item 5 accepts ordinary functional verification. Item 7 additionally
    # needs SDF annotation; neither may accept an otherwise invalid result.
    annotated = copy.deepcopy(envelope)
    annotated["environment"] = {"sdf": {"annotated": True, "corner": "typ"}}
    sdf_path = _write(tmp_path, "functional-sdf.json", annotated)
    report = build_tier_report(
        _manifest(kind="digital", evidence={"5": path, "7": sdf_path})
    )
    for item in (item for item in report["items"] if item["id"] in (5, 7)):
        assert item["status"] == ("met" if passed else "unmet")
        assert item["reason"] == (None if passed else "check_failed")


@pytest.mark.parametrize(
    ("xml", "reason"),
    [
        (_RESULTS_XML_EMPTY, "registered no tests"),
        (_ALL_SKIPPED_XML, "all tests were skipped.*zero executed tests"),
    ],
    ids=["empty", "all-skipped"],
)
def test_producer_refuses_zero_executed_tests(tmp_path, monkeypatch, xml, reason):
    _setup_inputs(tmp_path)
    request = _write_request(tmp_path / "request.json", _base_request())
    _stub_runner(monkeypatch, _FakeRunner(xml))

    with pytest.raises(FunctionalVerificationError, match=reason):
        run_functional_verification(request)


def test_all_skipped_cli_is_explicit_error(tmp_path, monkeypatch, capsys):
    _setup_inputs(tmp_path)
    request = _write_request(tmp_path / "request.json", _base_request())
    _stub_runner(monkeypatch, _FakeRunner(_ALL_SKIPPED_XML))

    assert main(["functional-verification", request, "--format", "json"]) == 1
    captured = capsys.readouterr()
    assert "all tests were skipped" in captured.err
    assert "zero executed tests" in captured.err
    assert not captured.out


@pytest.mark.parametrize(
    ("xml", "counts", "passed"),
    [
        (_RESULTS_XML_WITH_SKIP, (2, 0, 1), True),
        (_RESULTS_XML_WITH_SKIP.replace("<skipped />", ""), (3, 0, 0), True),
        (_RESULTS_XML_WITH_FAILURE, (2, 1, 0), False),
        (
            _RESULTS_XML_WITH_SKIP.replace(
                '<testsuite name="all" package="all">',
                '<testsuite name="all" tests="99" failures="99" skipped="99">',
            ),
            (2, 0, 1),
            True,
        ),
    ],
    ids=["mixed", "passing", "failing", "misleading-xml-summary"],
)
def test_producer_counts_qualify_consistently(
    tmp_path, monkeypatch, xml, counts, passed
):
    _setup_inputs(tmp_path)
    request = _write_request(tmp_path / "request.json", _base_request())
    _stub_runner(monkeypatch, _FakeRunner(xml))

    envelope = run_functional_verification(request)

    assert tuple(envelope[key] for key in _COUNT_FIELDS[1:]) == counts
    assert envelope["test_count"] == sum(counts) == len(envelope["tests"])
    assert envelope["coverage"] is None  # Code coverage is optional.
    _assert_signoff_verdict(tmp_path, envelope, passed=passed)


_COUNT_FIELDS = ("test_count", "passed_count", "failed_count", "skipped_count")


def _envelope(statuses):
    return {
        "schema_version": 1,
        "engine": "icarus",
        "status": "pass",
        "test_count": len(statuses),
        "passed_count": statuses.count("passed"),
        "failed_count": statuses.count("failed"),
        "skipped_count": statuses.count("skipped"),
        "tests": [
            {"name": f"test_{index}", "status": status}
            for index, status in enumerate(statuses)
        ],
    }


@pytest.mark.parametrize(
    "statuses", [[], ["skipped"], ["skipped", "skipped"], ["failed"]]
)
def test_external_pass_cannot_override_unchecked_or_failed_tests(tmp_path, statuses):
    _assert_signoff_verdict(tmp_path, _envelope(statuses), passed=False)


@pytest.mark.parametrize("field", _COUNT_FIELDS)
@pytest.mark.parametrize("value", [None, -1, 1.0, "1", True, False, [], {}])
def test_external_malformed_count_cannot_qualify(tmp_path, field, value):
    envelope = _envelope(["passed"])
    envelope[field] = value
    _assert_signoff_verdict(tmp_path, envelope, passed=False)


@pytest.mark.parametrize("field", _COUNT_FIELDS[1:])
def test_external_missing_count_cannot_qualify(tmp_path, field):
    envelope = _envelope(["passed"])
    del envelope[field]
    _assert_signoff_verdict(tmp_path, envelope, passed=False)


def test_external_missing_total_cannot_qualify(tmp_path):
    envelope = _envelope(["passed"])
    del envelope["test_count"]
    path = _write(tmp_path, "functional.json", envelope)
    with pytest.raises(SignoffError, match="unrecognized shape"):
        build_signoff([path])
    report = build_tier_report(_manifest(kind="digital", evidence={"5": path}))
    item = next(item for item in report["items"] if item["id"] == 5)
    assert item["status"] == "unmet"
    assert item["reason"] == "unrecognized_envelope"


@pytest.mark.parametrize(
    "updates",
    [
        {"test_count": 2},
        {"passed_count": 2},
        {"skipped_count": 1},
        {"failed_count": 1},
        {"tests": []},
        {"tests": [{"status": "skipped"}]},
        {"tests": [{"status": "failed"}]},
        {"tests": [{"status": "pass"}]},
        {"tests": [{}]},
        {"tests": [None]},
        {"tests": [{"status": []}]},
        {"tests": [{"status": {}}]},
    ],
)
def test_external_contradictory_counts_or_records_cannot_qualify(tmp_path, updates):
    envelope = _envelope(["passed"])
    envelope.update(updates)
    # Exercise a JSON round-trip, like external and legacy saved artifacts.
    _assert_signoff_verdict(tmp_path, json.loads(json.dumps(envelope)), passed=False)
