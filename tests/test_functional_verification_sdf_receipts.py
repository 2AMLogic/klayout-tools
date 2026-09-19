"""Public SDF observation gates, with a controlled simulator boundary (#2130)."""

import builtins
import io
import json
import subprocess
from pathlib import Path

import pytest

from klayout_tools import functional_verification as fv
from klayout_tools import functional_verification_sdf as sdf
from klayout_tools import signoff
from klayout_tools.cli import main
from test_functional_verification import (
    _RESULTS_XML_WITH_SKIP,
    _base_request,
    _sdf_request,
    _setup_inputs,
    _stub_runner,
    _write_request,
)


class ReceiptRunner:
    def __init__(self, *, missing=(), build_text="clean\n", test_text="clean\n"):
        self.missing = missing
        self.text = {"build": build_text, "test": test_text}

    def _capture(self, phase, path):
        if phase not in self.missing:
            Path(path).write_text(self.text[phase])

    def build(self, **kwargs):
        Path(kwargs["build_dir"]).mkdir(parents=True, exist_ok=True)
        self._capture("build", kwargs["log_file"])

    def test(self, **kwargs):
        self._capture("test", kwargs["log_file"])
        Path(kwargs["results_xml"]).write_text(_RESULTS_XML_WITH_SKIP)


def _tier_item(report, tmp_path, item_id):
    path = tmp_path / "evidence.json"
    path.write_text(json.dumps(report))
    aggregate = signoff.build_signoff([str(path)])
    assert aggregate["status"] == "pass"
    tier = signoff.build_tier_report(
        {"kind": "digital", "evidence": {str(item_id): str(path)}}
    )
    return next(item for item in tier["items"] if item["id"] == item_id)


@pytest.mark.parametrize("missing", [("build",), ("test",), ("build", "test")])
def test_missing_sdf_transcript_refuses_api_and_cli_evidence(
    tmp_path, monkeypatch, capsys, missing
):
    request = _sdf_request(tmp_path)
    _stub_runner(monkeypatch, ReceiptRunner(missing=missing))
    with pytest.raises(
        fv.FunctionalVerificationError, match="could not read.*transcript"
    ):
        fv.run_functional_verification(request)

    exit_code = main(["functional-verification", request, "--format", "json"])
    captured = capsys.readouterr()
    assert exit_code == 1
    assert captured.out == ""
    assert "could not read" in captured.err
    assert "transcript" in captured.err
    assert f"{missing[0]}_icarus.log" in captured.err

    # Feed the actual CLI refusal's stdout/status through command evidence.
    completed = subprocess.CompletedProcess(
        ["captured-fv"], exit_code, stdout=captured.out, stderr=captured.err
    )
    monkeypatch.setattr(signoff.subprocess, "run", lambda *a, **kw: completed)
    tier = signoff.build_tier_report(
        {"kind": "digital", "evidence": {"7": {"command": ["captured-fv"]}}}
    )
    item = next(item for item in tier["items"] if item["id"] == 7)
    assert item["status"] == "unmet"
    assert item["reason"] == "command_failed"
    assert item["citation"] is None


@pytest.mark.parametrize("phase", ["build", "test"])
@pytest.mark.parametrize("failure", ["open", "read"])
def test_unreadable_or_incomplete_sdf_scan_refuses(
    tmp_path, monkeypatch, phase, failure
):
    request = _sdf_request(tmp_path)
    _stub_runner(monkeypatch, ReceiptRunner())

    class InterruptedRead(io.StringIO):
        def readlines(self, *args, **kwargs):
            self.readline()  # The read began successfully before the I/O failure.
            raise OSError("injected incomplete transcript read")

    def failing_open(path, *args, **kwargs):
        if Path(path).name == f"{phase}_icarus.log":
            if failure == "open":
                raise PermissionError("injected unreadable transcript")
            return InterruptedRead("clean prefix\nSDF ERROR: unread suffix\n")
        return builtins.open(path, *args, **kwargs)

    monkeypatch.setattr(sdf, "open", failing_open, raising=False)
    with pytest.raises(
        fv.FunctionalVerificationError, match="could not read.*transcript"
    ):
        fv.run_functional_verification(request)


@pytest.mark.parametrize(
    ("text", "partial"),
    [
        ("", False),
        ("clean transcript\n", False),
        ("SDF WARNING: route.sdf:7: TIMINGCHECK not supported.\n", True),
    ],
    ids=["captured-empty", "clean", "known-benign-partial"],
)
def test_captured_readable_sdf_logs_preserve_coverage(
    tmp_path, monkeypatch, text, partial
):
    request = _sdf_request(tmp_path)
    _stub_runner(monkeypatch, ReceiptRunner(build_text=text, test_text=text))
    report = fv.run_functional_verification(request)
    assert (report["passed_count"], report["skipped_count"]) == (2, 1)
    annotation = report["environment"]["sdf"]
    assert annotation["annotated"] is True
    assert annotation["partial"] is partial
    if partial:
        assert annotation["dropped"]["timingcheck"]["count"] == 2
    else:
        assert annotation["dropped"] == {}
    assert _tier_item(report, tmp_path, 7)["status"] == "met"


@pytest.mark.parametrize("phase", ["build", "test"])
def test_readable_actionable_sdf_error_still_refuses(tmp_path, monkeypatch, phase):
    request = _sdf_request(tmp_path)
    failure = "SDF ERROR: route.sdf:7: Unable to match ModPath D -> Q in gcd._1_\n"
    _stub_runner(monkeypatch, ReceiptRunner(**{f"{phase}_text": failure}))
    with pytest.raises(fv.FunctionalVerificationError, match="did not fully apply"):
        fv.run_functional_verification(request)


def test_non_sdf_regression_does_not_require_annotation_logs(tmp_path, monkeypatch):
    _setup_inputs(tmp_path)
    request = _write_request(tmp_path / "request.json", _base_request())
    _stub_runner(monkeypatch, ReceiptRunner(missing=("build", "test")))
    report = fv.run_functional_verification(request)
    assert report["status"] == "pass"
    assert report["environment"]["sdf"] is None
    assert _tier_item(report, tmp_path, 5)["status"] == "met"
    item = _tier_item(report, tmp_path, 7)
    assert item["status"] == "unmet"
    assert item["reason"] == "not_post_layout"
