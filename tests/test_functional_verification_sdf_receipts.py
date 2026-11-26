"""Public SDF observation gates, with a controlled simulator boundary (#2130)."""

import builtins
import hashlib
import io
import json
import subprocess
from pathlib import Path

import pytest

from helpers.tool_probes import SKIP_NO_ICARUS as needs_icarus
from klayout_tools import functional_verification as fv
from klayout_tools import functional_verification_sdf as sdf
from klayout_tools import signoff
from klayout_tools.cli import main
from test_functional_verification import (
    _RESULTS_XML_WITH_SKIP,
    _SDF_DUT_V,
    _SDF_TESTBENCH_PY,
    _base_request,
    _sdf_request,
    _sdf_text,
    _setup_inputs,
    _stub_runner,
    _write,
    _write_request,
)
from test_signoff import STA_MULTI_CORNER_CLEAN_ENVELOPE


class ReceiptRunner:
    def __init__(self, *, missing=(), build_text="clean\n", test_text="clean\n"):
        self.missing = missing
        self.text = {"build": build_text, "test": test_text}
        # Every runner step this spy was asked to take, in order (issue #7983).
        self.calls: list[str] = []

    def _capture(self, phase, path):
        if phase not in self.missing:
            Path(path).write_text(self.text[phase])

    def build(self, **kwargs):
        self.calls.append("build")
        Path(kwargs["build_dir"]).mkdir(parents=True, exist_ok=True)
        self._capture("build", kwargs["log_file"])

    def test(self, **kwargs):
        self.calls.append("test")
        self._capture("test", kwargs["log_file"])
        Path(kwargs["results_xml"]).write_text(_RESULTS_XML_WITH_SKIP)


def _tier_item(report, tmp_path, item_id):
    path = tmp_path / "evidence.json"
    path.write_text(json.dumps(report))
    aggregate = signoff.build_signoff([str(path)])
    assert aggregate["status"] == "pass"
    entry = str(path)
    if item_id == 5:
        # Digital item 5 needs STA plus the functional suite (issue #3741).
        sta_path = tmp_path / "sta.json"
        sta_path.write_text(json.dumps(STA_MULTI_CORNER_CLEAN_ENVELOPE))
        entry = [entry, str(sta_path)]
    tier = signoff.build_tier_report(
        {"kind": "digital", "evidence": {str(item_id): entry}}
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
    # Issue #5550: the provenance digest covers the user's RTL only -- not
    # the SDF file, and not the generated wrapper/shim this annotated build
    # substituted into the build list.
    assert report["provenance"]["input"] == {
        "content_hash": "sha256:"
        + hashlib.sha256((tmp_path / "gcd.v").read_bytes()).hexdigest(),
        "role": "source",
    }
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


# -- SDF TIMESCALE versus the model's unit (issue #7983) --------------------
#
# Icarus applies each SDF delay in the annotated module's own unit. cocotb's
# Icarus runner passes `options.timescale` as the compile-time default, which
# a module's own `timescale` declaration overrides, so only models that
# declare none take the request's unit.


def _unit_request(
    tmp_path,
    *,
    model_timescale: str | None = None,
    request_timescale: list[str] | None = None,
    sdf_timescale: str = "1ns",
    sdf_text: str | None = None,
    scope: str | None = None,
) -> str:
    """A cocotb request annotating ``u_inv``'s ``a -> y`` arc, with the cell
    model's own `` `timescale `` (``None``: none declared) and the request's
    ``options.timescale`` (``None``: the ``["1ns", "1ps"]`` default).
    ``sdf_text`` replaces the SDF and ``scope`` sets ``options.sdf.scope``."""
    models = _SDF_DUT_V
    if model_timescale is not None:
        directive = f"`timescale {model_timescale}\nmodule klt_inv"
        models = models.replace("module klt_inv", directive)
    _write(tmp_path / "dly.v", models)
    _write(tmp_path / "test_dly.py", _SDF_TESTBENCH_PY)
    if sdf_text is None:
        header = "(TIMESCALE 1ns)"
        sdf_text = _sdf_text().replace(header, f"(TIMESCALE {sdf_timescale})")
    _write(tmp_path / "route.sdf", sdf_text)
    sdf_option: dict = {"file": "route.sdf"}
    if scope is not None:
        sdf_option["scope"] = scope
    options = {"coverage": False, "sdf": sdf_option}
    if request_timescale is not None:
        options["timescale"] = request_timescale
    return _write_request(
        tmp_path / "request.json",
        {
            "sources": ["dly.v"],
            "hdl_toplevel": "dly",
            "testbench": {"module": "test_dly"},
            "options": options,
        },
    )


@pytest.mark.parametrize(
    ("model_timescale", "request_timescale"),
    [("1ps/1ps", None), (None, ["1ps", "1ps"])],
    ids=["model-declares-ps", "request-default-is-ps"],
)
def test_conflicting_unit_is_refused_and_never_built(
    tmp_path, monkeypatch, model_timescale, request_timescale
):
    """A matching request default cannot mask a conflicting model
    declaration: the model's own ``1ps`` wins over the request's ``1ns``, and
    the refusal arrives before the runner is asked to build anything. With no
    declaration the request's unit is the model's unit, compared the same
    way."""
    request = _unit_request(
        tmp_path,
        model_timescale=model_timescale,
        request_timescale=request_timescale,
    )
    runner = ReceiptRunner()
    _stub_runner(monkeypatch, runner)
    with pytest.raises(fv.FunctionalVerificationError) as excinfo:
        fv.run_functional_verification(request)
    message = str(excinfo.value)
    assert "the SDF file is in 1ns" in message
    assert "'u_inv' (CELLTYPE \"klt_inv\", `timescale 1ps)" in message
    assert runner.calls == []


@pytest.mark.parametrize(
    ("request_timescale", "sdf_timescale", "unit"),
    [(None, "1ns", "1ns"), (["1ps", "1ps"], "1ps", "1ps")],
    ids=["default-unit", "request-unit"],
)
def test_matching_cocotb_run_records_both_units_and_the_original_digest(
    tmp_path, monkeypatch, request_timescale, sdf_timescale, unit
):
    """The matching control: the receipt carries the SDF's ``TIMESCALE``, the
    unit its delays were applied in, and the digest of the caller's own SDF."""
    request = _unit_request(
        tmp_path, request_timescale=request_timescale, sdf_timescale=sdf_timescale
    )
    runner = ReceiptRunner()
    _stub_runner(monkeypatch, runner)
    report = fv.run_functional_verification(request)
    annotation = report["environment"]["sdf"]
    assert annotation["annotated"] is True
    assert (annotation["timescale"], annotation["time_unit"]) == (unit, unit)
    assert (
        annotation["file"]["sha256"]
        == "sha256:" + hashlib.sha256((tmp_path / "route.sdf").read_bytes()).hexdigest()
    )
    assert runner.calls == ["build", "test"]


def test_delay_outside_any_cell_under_an_unresolvable_scope_is_refused(
    tmp_path, monkeypatch
):
    """The annotation root is where ``options.sdf.scope`` points; when that
    names no instance in the sources, the root's unit cannot be established."""
    request = _unit_request(
        tmp_path,
        sdf_text=(
            "(DELAYFILE\n"
            '  (SDFVERSION "3.0")\n'
            "  (TIMESCALE 1ns)\n"
            "  (DELAY (ABSOLUTE (INTERCONNECT a u_inv.a (1:1:1))))\n"
            ")\n"
        ),
        scope="no_such_instance",
    )
    runner = ReceiptRunner()
    _stub_runner(monkeypatch, runner)
    with pytest.raises(fv.FunctionalVerificationError) as excinfo:
        fv.run_functional_verification(request)
    message = str(excinfo.value)
    assert "the DELAY clauses outside any CELL (the annotation root)" in message
    assert "options.sdf.scope does not resolve" in message
    assert runner.calls == []


@needs_icarus
def test_cocotb_preprocesses_the_builds_defines_and_includes(tmp_path, monkeypatch):
    request = Path(_unit_request(tmp_path))
    spec = json.loads(request.read_text())
    spec["options"].update(
        includes=["."], build_args=["-DFAST"], defines={"UNIT": "1ps"}
    )
    request.write_text(json.dumps(spec))
    (tmp_path / "unit.vh").write_text("`ifdef FAST\n`timescale `UNIT/1ps\n`endif\n")
    source = tmp_path / "dly.v"
    source.write_text(
        source.read_text().replace(
            "module klt_inv", '`include "unit.vh"\nmodule klt_inv'
        )
    )
    runner = ReceiptRunner()
    _stub_runner(monkeypatch, runner)
    with pytest.raises(fv.FunctionalVerificationError, match="1000x too small"):
        fv.run_functional_verification(str(request))
    assert runner.calls == []
    assert not (
        tmp_path / ".klt" / "functional-verification" / "sim_build_icarus"
    ).exists()
