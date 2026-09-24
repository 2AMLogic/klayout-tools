"""Tests for `klt equiv`'s long-run operations contract (issue #2280):
stage-scoped ``--resume``, the stage-commit-record / ``partial: true``
artifact semantics, and the ``--check``/``--rerun`` committed-evidence
verification (issue #2224's shared machinery wired for this verb).

Three tiers, mirroring `tests/test_equiv.py`'s own structure:

- **Pure unit tests** (no subprocess): the stage-record loader's rejection
  table -- `partial: true`, a missing `partial` marker, a fingerprint
  mismatch, missing/mistyped fields, a missing committed log, and a log
  whose own bytes do not corroborate the recorded classification -- plus
  the atomic writer's `partial: false` guarantee and the
  partial-record-never-clobbers-a-commit rule. These are the negative
  controls: they pin the structural property that a partial or fabricated
  stage artifact can never satisfy the resume's verdict check.
- **Deterministic mocked tests** (mocked `subprocess.run`, no real
  `yosys`): a stage-1 process timeout produces an `inconclusive` envelope
  plus a `partial: true` record on disk (the real producer behind the
  `partial` marking), and a fabricated `all_proven` record pushed through
  the full `run_equiv(resume=True)` pipeline is discarded -- stage 1
  re-runs and the envelope says so, never inheriting the fabricated
  classification.
- **Real-Yosys integration tests** (`@pytest.mark.skipif` when `yosys` is
  not on `$PATH`): the acceptance criteria themselves. A staged sequential
  run killed mid-stage-2 leaves its committed stage-1 record on disk; a
  resumed run re-enters at stage 2 and produces an envelope byte-identical
  to an uninterrupted control run except in the declared run-scoped fields
  (`elapsed_s`, the additive `resume` block). The same identity is checked
  for the `all_proven` skip path (no Yosys invocation at all, verified by
  counting subprocess calls) and for the refinement-reconstruction path
  (blacklist artifact regenerated deterministically). Plus the
  `--check`/`--rerun` round-trip: cheap mode re-hashes without an engine,
  full mode re-runs and diffs with run-scoped bookkeeping excluded, and
  the CLI exit codes (0 match / 3 drifted / 1 refusal) match the shared
  #2224 contract.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from helpers.subprocess_fakes import fake_completed
from klayout_tools import _paths as paths_module
from klayout_tools import equiv
from klayout_tools.cli import main
from klayout_tools.equiv import (
    EquivError,
    check_equiv_report,
    rerun_equiv_report,
    run_equiv,
)

pytestmark = pytest.mark.usefixtures("real_build_identity_git")


@pytest.fixture(autouse=True)
def _bare_name_binary_resolution(monkeypatch):
    """Issue #2423: see the identically-named fixture in ``test_equiv.py``
    for the full rationale -- this module defines its own local copy of
    ``_mock_yosys_run`` (deliberately self-contained, per this module's own
    docstring) and needs the same bare-name resolution stub so those mocks'
    ``cmd[:2] == ["yosys", "-s"]``-shaped assertions keep holding."""
    real_which = shutil.which

    def fake_which(cmd, *args, **kwargs):
        if cmd in ("yosys", "iverilog", "vvp"):
            return cmd
        return real_which(cmd, *args, **kwargs)

    monkeypatch.setattr(paths_module.shutil, "which", fake_which)


# --------------------------------------------------------------------------- #
# Fixtures (RTL sources) -- deliberately local copies of the proven pairs in
# tests/test_equiv.py, so this module stays self-contained.
# --------------------------------------------------------------------------- #

_GOLD_AND = """\
module top(input a, input b, output y);
  assign y = a & b;
endmodule
"""

_GATE_OR_BROKEN = """\
module top(input a, input b, output y);
  assign y = a | b;
endmodule
"""

# Combinational-engine SAT-success text (mirrors tests/test_equiv.py's own
# `_SAT_SUCCESS_TEXT`) -- this module's own local copy, per its
# self-contained-fixtures convention above.
_SAT_SUCCESS_TEXT = """
Solving problem with 24 variables and 56 clauses..
SAT proof finished - no model found: SUCCESS!
"""

# Register-preserving buffer insertion (the survey's SS2.2 shape): stage 1
# proves it outright, so its committed record is `all_proven`.
_SEQ_GOLD_DFF_RTL = """\
module top(input clk, input rst, input [3:0] a, input [3:0] b, output reg [3:0] q);
  wire [3:0] sum = a + b;
  always @(posedge clk or posedge rst)
    if (rst) q <= 4'b0;
    else q <= sum;
endmodule
"""

_SEQ_GATE_DFF_RTL_BUFFERED = """\
module top(input clk, input rst, input [3:0] a, input [3:0] b, output reg [3:0] q);
  wire [3:0] sum_raw = a + b;
  wire [3:0] sum = ~(~sum_raw);
  always @(posedge clk or posedge rst)
    if (rst) q <= 4'b0;
    else q <= sum;
endmodule
"""

# D-input polarity inversion: stage 1 leaves cells unproven, stage 2 finds a
# genuine counterexample -- the pair that exercises the stage-2 re-entry
# resume path (and whose stage-1 record is therefore `unproven_cells`).
_SEQ_GOLD_SIMPLE_DFF_RTL = """\
module top(input clk, input rst, input d, output reg q);
  always @(posedge clk or posedge rst)
    if (rst) q <= 1'b0;
    else q <= d;
endmodule
"""

_SEQ_GATE_SIMPLE_DFF_RTL_INVERTED = """\
module top(input clk, input rst, input d, output reg q);
  always @(posedge clk or posedge rst)
    if (rst) q <= 1'b0;
    else q <= ~d;
endmodule
"""

# The #1353 miniature: same-named internal wire `n1` carrying opposite
# polarity -- stage 1 converges only after its cut-point refinement loop
# blacklists `n1`, so the committed record carries a nonempty blacklist.
_SEQ_GOLD_RENAMED_INTERNAL_RTL = """\
module top(input clk, input rst, input a, input b, input c,
           output reg y, output reg z);
  (* keep *) wire n1;
  assign n1 = a & b;
  always @(posedge clk) begin
    if (rst) begin y <= 1'b0; z <= 1'b0; end
    else begin y <= n1 | c; z <= n1 & c; end
  end
endmodule
"""

_SEQ_GATE_RENAMED_INTERNAL_RTL = """\
module top(input clk, input rst, input a, input b, input c,
           output reg y, output reg z);
  (* keep *) wire n1;
  assign n1 = ~(a & b);
  always @(posedge clk) begin
    if (rst) begin y <= 1'b0; z <= 1'b0; end
    else begin y <= (~n1) | c; z <= (~n1) & c; end
  end
endmodule
"""

# A stage-1 log whose bytes say "all cells proven" -- the only log content a
# genuine `all_proven` record can be corroborated by.
_ALL_PROVEN_LOG = """\
4. Executing EQUIV_STATUS pass.
Equivalence successfully proven!
"""

# A stage-1 log whose bytes say "cells left unproven" -- the only log content
# a genuine `unproven_cells` record can be corroborated by.
_UNPROVEN_LOG = """\
4. Executing EQUIV_STATUS pass.
Unproven $equiv $auto$equiv_make.cc:295:find_same_wires$10212: \\n1_gold \\n1_gate
Found a total of 1 unproven $equiv cells.
"""

HAVE_YOSYS = shutil.which("yosys") is not None

#: The envelope fields two runs of the same request are *allowed* to differ
#: in: wall-clock bookkeeping and the additive resume block (present only
#: when --resume was requested). Everything else -- status, counterexample,
#: diagnostics, artifacts, provenance hashes -- must be identical between an
#: uninterrupted run and an interrupted-then-resumed one. This is issue
#: #2280's "byte-identical (or drifts only in declared fields)" criterion,
#: stated as code.
_DECLARED_RUN_SCOPED_FIELDS = ("elapsed_s", "resume")


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _write(path: Path, text: str) -> str:
    path.write_text(text, encoding="utf-8")
    return str(path)


def _write_request(path: Path, request: dict) -> str:
    path.write_text(json.dumps(request), encoding="utf-8")
    return str(path)


def _side(sources: list[str], top: str = "top", **extra) -> dict:
    return {"sources": sources, "top": top, **extra}


def _seq_request(tmp_path: Path, *, gold: str, gate: str) -> str:
    _write(tmp_path / "gold.v", gold)
    _write(tmp_path / "gate.v", gate)
    return _write_request(
        tmp_path / "r.json",
        {
            "gold": _side(["gold.v"]),
            "gate": _side(["gate.v"]),
            "engine": "yosys-sequential",
        },
    )


def _equiv_dir(tmp_path: Path) -> str:
    return os.path.join(str(tmp_path), ".klt", "equiv")


def _record_path(tmp_path: Path) -> str:
    return os.path.join(_equiv_dir(tmp_path), "stage1.commit.json")


def _assert_verdict_identical(resumed: dict, control: dict) -> None:
    """Issue #2280's identity criterion: every envelope field outside
    :data:`_DECLARED_RUN_SCOPED_FIELDS` must match between an
    interrupted-then-resumed run and an uninterrupted control run."""
    assert set(resumed) >= set(control) - set(_DECLARED_RUN_SCOPED_FIELDS)
    for key, value in control.items():
        if key in _DECLARED_RUN_SCOPED_FIELDS:
            continue
        assert resumed[key] == value, f"envelope field {key!r} drifted on resume"


class _RecordingYosys:
    """A `subprocess.run` stand-in that records every invocation and
    delegates to the real runner -- lets a test count exactly how many
    `yosys -s` scripts a run executed."""

    def __init__(self):
        self.calls: list[list[str]] = []
        self._real = equiv.subprocess.run

    def __call__(self, cmd, **kwargs):
        self.calls.append(list(cmd))
        return self._real(cmd, **kwargs)


def _count_script_calls(recorder: _RecordingYosys) -> int:
    return sum(1 for cmd in recorder.calls if cmd[:2] == ["yosys", "-s"])


# --------------------------------------------------------------------------- #
# Pure unit tests: the stage-record contract (no subprocess)
# --------------------------------------------------------------------------- #


def _commit_record(
    tmp_path: Path,
    *,
    fingerprint: str = "fp",
    classification: str | None = "all_proven",
    partial: bool = False,
    log_text: str = _ALL_PROVEN_LOG,
    extra: dict | None = None,
    omit: tuple = (),
) -> str:
    """Write a stage record + its committed log directly, for loader tests."""
    out_dir = tmp_path / ".klt" / "equiv"
    out_dir.mkdir(parents=True, exist_ok=True)
    log_path = out_dir / "equiv_seq_stage1.log"
    log_path.write_text(log_text, encoding="utf-8")
    record_path = out_dir / "stage1.commit.json"
    record: dict = {
        "stage": 1,
        "fingerprint": fingerprint,
        "partial": partial,
        "classification": classification,
        "blacklist": [],
        "refinements": 0,
    }
    if extra:
        record.update(extra)
    for key in omit:
        record.pop(key, None)
    record_path.write_text(json.dumps(record), encoding="utf-8")
    return str(record_path)


def _committed_log_path(tmp_path: Path) -> str:
    return os.path.join(_equiv_dir(tmp_path), "equiv_seq_stage1.log")


def test_committed_record_roundtrips(tmp_path):
    """A record the committing path wrote (`partial` forced false, all
    fields present) with a corroborating log loads back intact."""
    record_path = _commit_record(tmp_path, fingerprint="fp")
    record, reason = equiv._load_committed_stage1(
        record_path,
        "fp",
        log_path=_committed_log_path(tmp_path),
        netlist_path="unused-for-all_proven",
    )
    assert reason is None
    assert record is not None
    assert record["classification"] == "all_proven"


def test_absent_record_is_not_a_discard(tmp_path):
    """No record on disk is the ordinary first-run shape: ``(None, None)`` --
    no diagnostic owed, stage 1 just runs."""
    record, reason = equiv._load_committed_stage1(
        str(tmp_path / "nope.commit.json"), "fp", log_path="x", netlist_path="x"
    )
    assert record is None
    assert reason is None


@pytest.mark.parametrize(
    "record_kwargs",
    [
        {"partial": True},
        {"omit": ("partial",)},  # a writer that never heard of the marker
        {"extra": {"classification": "equivalent"}},  # not a stage-1 classification
        {"extra": {"classification": None}},
        {"omit": ("classification",)},
        {"extra": {"blacklist": "n1"}},  # not a list
        {"extra": {"refinements": -1}},
        {"extra": {"refinements": "1"}},
        {"extra": {"stage": 2}},
    ],
)
def test_malformed_records_are_never_loaded(tmp_path, record_kwargs):
    """Every structural rejection in the loader's table: a record missing or
    mistyping anything the resume depends on is discarded with a reason --
    never best-effort interpreted (issue #2280's negative-control
    property)."""
    record_path = _commit_record(tmp_path, fingerprint="fp", **record_kwargs)
    record, reason = equiv._load_committed_stage1(
        record_path,
        "fp",
        log_path=_committed_log_path(tmp_path),
        netlist_path="unused-for-all_proven",
    )
    assert record is None
    assert reason


def test_fingerprint_mismatch_discards_record(tmp_path):
    """A record committed by a *different* request says nothing about this
    one -- discarded, named as a fingerprint mismatch."""
    record_path = _commit_record(tmp_path, fingerprint="old-request")
    record, reason = equiv._load_committed_stage1(
        record_path,
        "new-request",
        log_path=_committed_log_path(tmp_path),
        netlist_path="unused",
    )
    assert record is None
    assert "fingerprint" in reason


def test_missing_committed_log_discards_record(tmp_path):
    record_path = _commit_record(tmp_path, fingerprint="fp")
    os.unlink(_committed_log_path(tmp_path))
    record, reason = equiv._load_committed_stage1(
        record_path, "fp", log_path="does-not-exist", netlist_path="unused"
    )
    assert record is None
    assert "log" in reason


def test_all_proven_classification_requires_proven_log(tmp_path):
    """An `all_proven` record whose log does not contain Yosys's own success
    line -- e.g. a truncated or hand-fabricated record -- can never satisfy
    the resume: the log, not the record, is the primary artifact."""
    record_path = _commit_record(
        tmp_path,
        fingerprint="fp",
        classification="all_proven",
        log_text=_UNPROVEN_LOG,
    )
    record, reason = equiv._load_committed_stage1(
        record_path,
        "fp",
        log_path=_committed_log_path(tmp_path),
        netlist_path="unused",
    )
    assert record is None
    assert "corroborate" in reason


def test_unproven_classification_requires_unproven_log(tmp_path):
    """The mirror image: an `unproven_cells` record "corroborated" by a log
    that actually proves everything is also rejected."""
    record_path = _commit_record(
        tmp_path,
        fingerprint="fp",
        classification="unproven_cells",
        log_text=_ALL_PROVEN_LOG,
    )
    record, reason = equiv._load_committed_stage1(
        record_path,
        "fp",
        log_path=_committed_log_path(tmp_path),
        netlist_path=str(tmp_path / "netlist.v"),
    )
    assert record is None
    assert "corroborate" in reason


def test_unproven_cells_record_requires_the_netlist(tmp_path):
    """Stage 2 re-entry consumes the committed stage-1 netlist; without it
    the record is discarded so stage 1 re-runs (and rewrites it) rather
    than stage 2 failing on a missing input."""
    record_path = _commit_record(
        tmp_path,
        fingerprint="fp",
        classification="unproven_cells",
        log_text=_UNPROVEN_LOG,
    )
    record, reason = equiv._load_committed_stage1(
        record_path,
        "fp",
        log_path=_committed_log_path(tmp_path),
        netlist_path=str(tmp_path / "missing_netlist.v"),
    )
    assert record is None
    assert "netlist" in reason


def test_partial_record_never_clobbers_a_commit(tmp_path):
    """A later, killed run's partial marker must not destroy the one valid
    commit an earlier run left (the guard in `_write_partial_stage_record`)."""
    out_dir = tmp_path / ".klt" / "equiv"
    out_dir.mkdir(parents=True)
    record_path = str(out_dir / "stage1.commit.json")
    equiv._write_stage_record(
        record_path,
        {
            "stage": 1,
            "fingerprint": "fp",
            "classification": "all_proven",
            "blacklist": [],
            "refinements": 0,
        },
    )
    equiv._write_partial_stage_record(record_path, "fp", reason="process_timeout")
    with open(record_path, encoding="utf-8") as handle:
        data = json.load(handle)
    assert data["partial"] is False
    assert data["classification"] == "all_proven"


def test_stage_record_writer_forces_partial_false(tmp_path):
    """The committing writer always stamps `partial: false` explicitly, so a
    record's meaning never depends on a key's absence."""
    out_dir = tmp_path / ".klt" / "equiv"
    out_dir.mkdir(parents=True)
    record_path = str(out_dir / "stage1.commit.json")
    equiv._write_stage_record(
        record_path,
        {
            "stage": 1,
            "fingerprint": "fp",
            "classification": "all_proven",
            "blacklist": [],
            "refinements": 0,
        },
    )
    with open(record_path, encoding="utf-8") as handle:
        data = json.load(handle)
    assert data["partial"] is False
    # And the atomic write left no temp file behind.
    assert not os.path.isfile(record_path + ".tmp")


# --------------------------------------------------------------------------- #
# Deterministic mocked tests: the partial-record producer + the fabricated-
# record negative control through the full pipeline
# --------------------------------------------------------------------------- #


def _mock_yosys_run(*, run_stdout: str = "", raise_timeout: bool = False):
    """The same shape `tests/test_equiv.py`'s own mock uses: answer the
    `yosys -s <script>` invocation (or kill it) and the `yosys -V` probe
    from canned data."""

    def _run(cmd, **kwargs):
        if cmd[:2] == ["yosys", "-s"]:
            if raise_timeout:
                raise subprocess.TimeoutExpired(cmd=cmd, timeout=kwargs.get("timeout"))
            return fake_completed(stdout=run_stdout, returncode=0)
        if cmd == ["yosys", "-V"]:
            return fake_completed(stdout="Yosys 0.67 (git sha1 deadbeef)\n")
        raise AssertionError(f"unexpected subprocess.run call in mock: {cmd!r}")

    return _run


def test_stage1_timeout_writes_partial_record(tmp_path, monkeypatch):
    """A stage-1 process timeout is the *real producer* of a `partial: true`
    record: the envelope is inconclusive (never a verdict), and the on-disk
    trace of the attempt is explicitly partial -- which the loader can
    never adopt on a later resume."""
    request_path = _seq_request(
        tmp_path,
        gold=_SEQ_GOLD_SIMPLE_DFF_RTL,
        gate=_SEQ_GATE_SIMPLE_DFF_RTL_INVERTED,
    )
    monkeypatch.setattr(equiv.subprocess, "run", _mock_yosys_run(raise_timeout=True))

    report = run_equiv(request_path, resume=True)

    assert report["status"] == "inconclusive"
    assert report["diagnostics"][0]["code"] == "process_timeout"
    with open(_record_path(tmp_path), encoding="utf-8") as handle:
        record = json.load(handle)
    assert record["partial"] is True
    assert record["classification"] is None

    # And the structural negative control: that partial record can never
    # satisfy the resume.
    loaded, reason = equiv._load_committed_stage1(
        _record_path(tmp_path),
        record["fingerprint"],
        log_path=os.path.join(_equiv_dir(tmp_path), "equiv_seq_stage1.log"),
        netlist_path=os.path.join(_equiv_dir(tmp_path), "equiv_seq_netlist.v"),
    )
    assert loaded is None
    assert "partial" in reason


def test_fabricated_all_proven_record_is_discarded_and_never_verdict_bearing(
    tmp_path, monkeypatch
):
    """The full-pipeline negative control: a record claiming `all_proven`
    for a request that was never proven (wrong fingerprint, and a committed
    log that reports unproven cells) is discarded -- stage 1 re-runs, the
    envelope carries the `resume_stage_record_discarded` warning, and the
    final verdict comes from the live run, never from the fabrication."""
    request_path = _seq_request(
        tmp_path,
        gold=_SEQ_GOLD_DFF_RTL,
        gate=_SEQ_GATE_DFF_RTL_BUFFERED,
    )
    _commit_record(
        tmp_path,
        fingerprint="not-this-request",  # wrong fingerprint on purpose
        classification="all_proven",
        log_text=_UNPROVEN_LOG,  # a log that would not corroborate it either
    )
    monkeypatch.setattr(
        equiv.subprocess,
        "run",
        _mock_yosys_run(run_stdout="Equivalence successfully proven!\n"),
    )

    report = run_equiv(request_path, resume=True)

    assert report["status"] == "equivalent"
    codes = [diag["code"] for diag in report["diagnostics"]]
    assert "resume_stage_record_discarded" in codes
    discard = next(
        diag
        for diag in report["diagnostics"]
        if diag["code"] == "resume_stage_record_discarded"
    )
    assert "fingerprint" in discard["message"]
    assert report["resume"] == {
        "resumed_stage": 0,
        "record_path": _record_path(tmp_path),
    }


def test_partial_true_record_with_doctored_log_is_still_discarded(
    tmp_path, monkeypatch
):
    """The strongest fabrication: `partial: true` *and* a doctored log
    carrying Yosys's own success line. The partial gate fires before
    corroboration is even consulted, so the run re-runs stage 1 (here: the
    mocked live run) and the fabricated classification never reaches
    `status` through the resume path."""
    request_path = _seq_request(
        tmp_path,
        gold=_SEQ_GOLD_DFF_RTL,
        gate=_SEQ_GATE_DFF_RTL_BUFFERED,
    )
    out_dir = Path(_equiv_dir(tmp_path))
    out_dir.mkdir(parents=True)
    (out_dir / "equiv_seq_stage1.log").write_text(_ALL_PROVEN_LOG, encoding="utf-8")
    (out_dir / "stage1.commit.json").write_text(
        json.dumps(
            {
                "stage": 1,
                "fingerprint": "forged",
                "partial": True,
                "classification": "all_proven",
                "blacklist": [],
                "refinements": 0,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        equiv.subprocess,
        "run",
        _mock_yosys_run(run_stdout="Equivalence successfully proven!\n"),
    )

    report = run_equiv(request_path, resume=True)

    assert report["status"] == "equivalent"
    codes = [diag["code"] for diag in report["diagnostics"]]
    assert "resume_stage_record_discarded" in codes
    discard = next(
        diag
        for diag in report["diagnostics"]
        if diag["code"] == "resume_stage_record_discarded"
    )
    assert "partial" in discard["message"]


# --------------------------------------------------------------------------- #
# Real-Yosys integration tests: the acceptance criteria
# --------------------------------------------------------------------------- #


@pytest.mark.skipif(not HAVE_YOSYS, reason="yosys is not installed on this machine")
def test_resume_without_prior_run_reports_resumed_stage_zero(tmp_path):
    request_path = _seq_request(
        tmp_path, gold=_SEQ_GOLD_DFF_RTL, gate=_SEQ_GATE_DFF_RTL_BUFFERED
    )

    report = run_equiv(request_path, resume=True)

    assert report["status"] == "equivalent"
    assert report["resume"]["resumed_stage"] == 0
    assert report["diagnostics"] == []


@pytest.mark.skipif(not HAVE_YOSYS, reason="yosys is not installed on this machine")
def test_resume_without_flag_omits_block_entirely(tmp_path):
    """A run without --resume is byte-identical to pre-#2280 output: no
    `resume` key at all (the present-only-when-requested convention)."""
    request_path = _seq_request(
        tmp_path, gold=_SEQ_GOLD_DFF_RTL, gate=_SEQ_GATE_DFF_RTL_BUFFERED
    )

    report = run_equiv(request_path)

    assert "resume" not in report


@pytest.mark.skipif(not HAVE_YOSYS, reason="yosys is not installed on this machine")
def test_resume_all_proven_runs_no_yosys_and_matches_control(tmp_path, monkeypatch):
    """Acceptance criterion, `all_proven` flavor: after one full run, a
    resumed run adopts the committed stage-1 record, invokes *no* Yosys
    script at all (counted), and produces the control envelope byte-for-byte
    outside the declared run-scoped fields."""
    request_path = _seq_request(
        tmp_path, gold=_SEQ_GOLD_DFF_RTL, gate=_SEQ_GATE_DFF_RTL_BUFFERED
    )

    recorder = _RecordingYosys()
    monkeypatch.setattr(equiv.subprocess, "run", recorder)

    control = run_equiv(request_path)
    control_calls = _count_script_calls(recorder)
    assert control_calls >= 1

    resumed = run_equiv(request_path, resume=True)

    assert _count_script_calls(recorder) == control_calls, (
        "a resumed all_proven run must not re-run any Yosys script"
    )
    assert resumed["resume"]["resumed_stage"] == 1
    assert resumed["resume"]["record_path"] == _record_path(tmp_path)
    _assert_verdict_identical(resumed, control)


@pytest.mark.skipif(not HAVE_YOSYS, reason="yosys is not installed on this machine")
def test_resume_unproven_cells_reenters_stage2_and_matches_control(tmp_path):
    """Acceptance criterion, stage-2 re-entry flavor: the polarity-inversion
    pair's stage 1 leaves cells unproven (its record is `unproven_cells`);
    the resumed run re-enters at stage 2, re-derives the confirmed
    counterexample, and matches the uninterrupted control outside the
    declared fields."""
    request_path = _seq_request(
        tmp_path,
        gold=_SEQ_GOLD_SIMPLE_DFF_RTL,
        gate=_SEQ_GATE_SIMPLE_DFF_RTL_INVERTED,
    )

    control = run_equiv(request_path)
    assert control["status"] == "counterexample"

    with open(_record_path(tmp_path), encoding="utf-8") as handle:
        record = json.load(handle)
    assert record["partial"] is False
    assert record["classification"] == "unproven_cells"

    resumed = run_equiv(request_path, resume=True)

    assert resumed["resume"]["resumed_stage"] == 1
    assert resumed["status"] == "counterexample"
    assert resumed["counterexample"]["confirmed_by_simulation"] is True
    _assert_verdict_identical(resumed, control)


@pytest.mark.skipif(not HAVE_YOSYS, reason="yosys is not installed on this machine")
def test_killed_run_resumes_from_committed_stage_artifacts(tmp_path, monkeypatch):
    """The acceptance criterion verbatim: a staged sequential run killed
    mid-way (here: a SIGKILL-shaped death between stage 1's commit and
    stage 2's completion) leaves its committed stage-1 record on disk; an
    independent resumed run re-enters from that artifact and produces the
    uninterrupted envelope's verdict fields exactly."""
    request_path = _seq_request(
        tmp_path,
        gold=_SEQ_GOLD_SIMPLE_DFF_RTL,
        gate=_SEQ_GATE_SIMPLE_DFF_RTL_INVERTED,
    )

    control = run_equiv(request_path)

    # Run again from the same request, dying mid-stage-2 the way a reaped
    # session does: stage 1 completes and commits, then the process vanishes
    # (SystemExit standing in for SIGKILL -- nothing after it runs, so no
    # envelope is ever emitted for this "session").
    real_runner = equiv._run_yosys_subprocess

    def killed_mid_stage2(script_path, timeout_s, binary="yosys"):
        if "stage2" in script_path:
            raise SystemExit(137)
        return real_runner(script_path, timeout_s, binary)

    monkeypatch.setattr(equiv, "_run_yosys_subprocess", killed_mid_stage2)
    with pytest.raises(SystemExit):
        run_equiv(request_path, resume=True)
    monkeypatch.undo()

    with open(_record_path(tmp_path), encoding="utf-8") as handle:
        record = json.load(handle)
    assert record["partial"] is False
    assert record["classification"] == "unproven_cells"

    resumed = run_equiv(request_path, resume=True)

    assert resumed["resume"]["resumed_stage"] == 1
    _assert_verdict_identical(resumed, control)


@pytest.mark.skipif(not HAVE_YOSYS, reason="yosys is not installed on this machine")
def test_resume_reconstructs_refinement_diagnostic_and_blacklist(tmp_path):
    """The same-named-internal-wire pair converges only after stage 1's
    cut-point refinement loop blacklists `n1`, so its committed record
    carries the nonempty blacklist. A resumed run that lost the blacklist
    artifact reconstructs the exact refinement diagnostic from the record
    and regenerates the file deterministically, matching the control
    envelope's `artifacts.stage1_blacklist_path`."""
    request_path = _seq_request(
        tmp_path,
        gold=_SEQ_GOLD_RENAMED_INTERNAL_RTL,
        gate=_SEQ_GATE_RENAMED_INTERNAL_RTL,
    )

    control = run_equiv(request_path)
    assert control["status"] == "equivalent"
    codes = [diag["code"] for diag in control["diagnostics"]]
    assert "equiv_cutpoint_refinement" in codes
    blacklist_path = control["artifacts"]["stage1_blacklist_path"]
    assert blacklist_path is not None

    # Resume with the blacklist artifact deleted: the record still carries
    # the exact wire set, and the resumed run regenerates the file.
    os.unlink(blacklist_path)
    resumed = run_equiv(request_path, resume=True)

    assert resumed["resume"]["resumed_stage"] == 1
    assert resumed["artifacts"]["stage1_blacklist_path"] == blacklist_path
    assert os.path.isfile(blacklist_path)
    with open(blacklist_path, encoding="utf-8") as handle:
        assert handle.read().splitlines() == ["n1"]
    _assert_verdict_identical(resumed, control)


@pytest.mark.skipif(not HAVE_YOSYS, reason="yosys is not installed on this machine")
def test_resume_with_changed_request_discards_record(tmp_path):
    """Editing a source after the commit changes the request fingerprint:
    the record is discarded (with the warning naming why), stage 1 re-runs,
    and the envelope is truthful about nothing having been reused."""
    request_path = _seq_request(
        tmp_path, gold=_SEQ_GOLD_DFF_RTL, gate=_SEQ_GATE_DFF_RTL_BUFFERED
    )
    first = run_equiv(request_path, resume=True)
    assert first["resume"]["resumed_stage"] == 0

    _write(
        tmp_path / "gate.v",
        _SEQ_GATE_DFF_RTL_BUFFERED + "\n// a comment changes the bytes\n",
    )
    second = run_equiv(request_path, resume=True)

    assert second["status"] == "equivalent"
    assert second["resume"]["resumed_stage"] == 0
    codes = [diag["code"] for diag in second["diagnostics"]]
    assert "resume_stage_record_discarded" in codes
    discard = next(
        diag
        for diag in second["diagnostics"]
        if diag["code"] == "resume_stage_record_discarded"
    )
    assert "fingerprint" in discard["message"]


@pytest.mark.skipif(not HAVE_YOSYS, reason="yosys is not installed on this machine")
def test_combinational_engine_accepts_resume_and_reruns_its_single_stage(tmp_path):
    """The combinational engine is one Yosys subprocess: `--resume` is
    accepted (uniform agent-fleet retry command), re-runs the stage, and
    reports `resumed_stage: 0` truthfully."""
    _write(tmp_path / "gold.v", _GOLD_AND)
    _write(tmp_path / "gate.v", _GOLD_AND.replace("a & b", "b & a"))
    request_path = _write_request(
        tmp_path / "r.json",
        {"gold": _side(["gold.v"]), "gate": _side(["gate.v"])},
    )

    report = run_equiv(request_path, resume=True)

    assert report["status"] == "equivalent"
    assert report["resume"]["resumed_stage"] == 0


@pytest.mark.skipif(not HAVE_YOSYS, reason="yosys is not installed on this machine")
def test_cli_resume_flag_roundtrip(tmp_path, capsys):
    _write(tmp_path / "gold.v", _SEQ_GOLD_DFF_RTL)
    _write(tmp_path / "gate.v", _SEQ_GATE_DFF_RTL_BUFFERED)
    request_path = _write_request(
        tmp_path / "r.json",
        {
            "gold": _side(["gold.v"]),
            "gate": _side(["gate.v"]),
            "engine": "yosys-sequential",
        },
    )

    assert main(["equiv", request_path, "--resume", "--format", "json"]) == 0
    first = json.loads(capsys.readouterr().out)
    assert first["resume"]["resumed_stage"] == 0

    assert main(["equiv", request_path, "--resume", "--format", "json"]) == 0
    second = json.loads(capsys.readouterr().out)
    assert second["resume"]["resumed_stage"] == 1
    assert second["status"] == first["status"] == "equivalent"


# --------------------------------------------------------------------------- #
# --check / --rerun (issues #2224 + #2280)
# --------------------------------------------------------------------------- #


def _commit_envelope(tmp_path: Path, envelope: dict, name: str = "report.json") -> str:
    path = tmp_path / name
    path.write_text(json.dumps(envelope), encoding="utf-8")
    return str(path)


def _committed_envelope_with_hash(gold_hash: str) -> dict:
    return {
        "schema_version": 1,
        "status": "equivalent",
        "provenance": {"input": {"content_hash": gold_hash, "role": "source"}},
    }


def _resolved_sides(tmp_path: Path) -> tuple[dict, dict]:
    request_dir = str(tmp_path)
    gold = equiv._resolve_side(
        {"sources": ["gold.v"], "top": "top"}, request_dir, "gold"
    )
    gate = equiv._resolve_side(
        {"sources": ["gate.v"], "top": "top"}, request_dir, "gate"
    )
    return gold, gate


def _write_and_request_pair(tmp_path: Path) -> str:
    _write(tmp_path / "gold.v", _GOLD_AND)
    _write(tmp_path / "gate.v", _GATE_OR_BROKEN)
    return _write_request(
        tmp_path / "r.json",
        {"gold": _side(["gold.v"]), "gate": _side(["gate.v"])},
    )


def test_check_committed_report_matches_without_engine(tmp_path):
    """Cheap mode re-hashes only -- no Yosys, and a matching content hash
    means `match`. Works against a hand-committed envelope, which is
    exactly the retrieved-remote-envelope shape the guide's round-trip
    uses."""
    request_path = _write_and_request_pair(tmp_path)
    gold, gate = _resolved_sides(tmp_path)
    committed = _commit_envelope(
        tmp_path, _committed_envelope_with_hash(equiv._equiv_input_hash(gold, gate))
    )

    result = check_equiv_report(committed, request_path)

    assert result["status"] == "match"
    assert result["checks"][0]["match"] is True


def test_check_drifted_after_source_edit(tmp_path):
    request_path = _write_and_request_pair(tmp_path)
    gold, gate = _resolved_sides(tmp_path)
    committed = _commit_envelope(
        tmp_path, _committed_envelope_with_hash(equiv._equiv_input_hash(gold, gate))
    )

    _write(tmp_path / "gate.v", _GATE_OR_BROKEN + "\n// mutated\n")
    result = check_equiv_report(committed, request_path)

    assert result["status"] == "drifted"
    assert result["checks"][0]["match"] is False
    assert result["checks"][0]["expected"] != result["checks"][0]["actual"]


def test_check_missing_recorded_hash_is_never_a_pass(tmp_path):
    """A committed report predating the hash (or one that could not record
    it) renders `drifted`, never a false `match` -- the shared `hash_check`
    rule."""
    request_path = _write_and_request_pair(tmp_path)
    committed = _commit_envelope(tmp_path, {"schema_version": 1})

    result = check_equiv_report(committed, request_path)

    assert result["status"] == "drifted"
    assert result["checks"][0]["expected"] is None


def test_check_missing_report_is_a_clean_error(tmp_path):
    request_path = _write_and_request_pair(tmp_path)

    with pytest.raises(EquivError, match="not found"):
        check_equiv_report(str(tmp_path / "missing.json"), request_path)


@pytest.mark.skipif(not HAVE_YOSYS, reason="yosys is not installed on this machine")
def test_rerun_matches_and_excludes_run_scoped_bookkeeping(tmp_path):
    """Full mode re-runs the proof and matches a committed envelope whose
    run-scoped bookkeeping (elapsed_s, a resume block from a --resume run)
    differs -- those are exactly the declared non-evidence fields."""
    request_path = _write_and_request_pair(tmp_path)
    fresh = run_equiv(request_path)
    committed = dict(fresh)
    committed["elapsed_s"] = 9999.0
    committed["resume"] = {"resumed_stage": 1, "record_path": "/somewhere"}
    committed_path = _commit_envelope(tmp_path, committed)

    result = rerun_equiv_report(committed_path, request_path)

    assert result["status"] == "match"
    assert result["drift"] == []
    assert result["fresh"]["elapsed_s"] != 9999.0


@pytest.mark.skipif(not HAVE_YOSYS, reason="yosys is not installed on this machine")
def test_rerun_drifts_when_the_design_changed(tmp_path):
    request_path = _write_and_request_pair(tmp_path)
    committed_path = _commit_envelope(tmp_path, run_equiv(request_path))

    # XOR in place of OR: a genuinely different design (and a different
    # counterexample vector).
    _write(tmp_path / "gate.v", _GATE_OR_BROKEN.replace("a | b", "a ^ b"))
    result = rerun_equiv_report(committed_path, request_path)

    assert result["status"] == "drifted"
    drifted_fields = {entry["field"] for entry in result["drift"]}
    assert "provenance.input.content_hash" in drifted_fields


def test_rerun_excludes_yosys_binary_path_drift(tmp_path, monkeypatch):
    """Issue #2423: `yosys_binary` (a host-local absolute path) must not
    itself cause `--rerun` to report drift -- two hosts resolving the
    identical proof via a different yosys install path is not a change in
    what was proved, mirroring `klt lvs`'s `environment.netgen_binary`
    exclusion. Mocked (no real yosys/wall-clock needed), unlike the
    `HAVE_YOSYS`-gated rerun tests above."""
    _write(tmp_path / "gold.v", _GOLD_AND)
    _write(tmp_path / "gate.v", _GOLD_AND)
    request_path = _write_request(
        tmp_path / "r.json",
        {"gold": _side(["gold.v"]), "gate": _side(["gate.v"])},
    )
    monkeypatch.setattr(
        equiv.subprocess,
        "run",
        _mock_yosys_run(run_stdout=_SAT_SUCCESS_TEXT),
    )

    fresh = run_equiv(request_path)
    assert fresh["status"] == "equivalent"
    assert fresh["yosys_binary"] == "yosys"

    committed = dict(fresh)
    # Simulate a second host that resolved a *different* yosys install for
    # the same proof.
    committed["yosys_binary"] = "/opt/other-host/yosys"
    committed_path = _commit_envelope(tmp_path, committed)

    result = rerun_equiv_report(committed_path, request_path)

    assert result["status"] == "match"
    assert result["drift"] == []


@pytest.mark.skipif(not HAVE_YOSYS, reason="yosys is not installed on this machine")
def test_cli_check_and_rerun_exit_codes(tmp_path, capsys):
    _write(tmp_path / "gold.v", _GOLD_AND)
    _write(tmp_path / "gate.v", _GOLD_AND.replace("a & b", "b & a"))
    request_path = _write_request(
        tmp_path / "r.json",
        {"gold": _side(["gold.v"]), "gate": _side(["gate.v"])},
    )

    # Run once (proven equivalent), commit the report.
    assert main(["equiv", request_path, "--format", "json"]) == 0
    committed = json.loads(capsys.readouterr().out)
    committed_path = _commit_envelope(tmp_path, committed)

    # Cheap mode: match -> 0.
    assert (
        main(["equiv", request_path, "--check", committed_path, "--format", "json"])
        == 0
    )
    assert json.loads(capsys.readouterr().out)["status"] == "match"

    # Drift the source (AND -> OR: a real design change): cheap mode -> 3.
    _write(tmp_path / "gate.v", _GOLD_AND.replace("a & b", "a | b"))
    assert (
        main(["equiv", request_path, "--check", committed_path, "--format", "json"])
        == 3
    )
    assert json.loads(capsys.readouterr().out)["status"] == "drifted"

    # Full mode re-runs and reports the drift -> 3, with the drift named.
    assert (
        main(
            [
                "equiv",
                request_path,
                "--check",
                committed_path,
                "--rerun",
                "--format",
                "json",
            ]
        )
        == 3
    )
    rerun = json.loads(capsys.readouterr().out)
    assert rerun["status"] == "drifted"
    assert rerun["drift"]


def test_cli_rerun_without_check_is_a_clean_refusal(tmp_path, capsys):
    request_path = _write_and_request_pair(tmp_path)

    exit_code = main(["equiv", request_path, "--rerun", "--format", "json"])

    assert exit_code == 1
    err = capsys.readouterr().err
    assert "--rerun requires --check" in err
