"""`netlist_title_line`: a SPICE title line on the request's own netlist (#5433).

`klt sim` composes its own corner deck and pulls the request's `netlist` in
with `.include`, where SPICE has no title-line semantics -- every line of the
included file is circuit content. A deck written to the ordinary standalone
convention (title on line 1) therefore has that title parsed as a device card,
and ngspice aborts with a message that names neither the file nor the
convention: an empty model name, a "line 1" that is the *composed* deck's line
1, and a failure mode that depends on the title's first letter.

These tests pin the post-failure classification that names the cause. The
three failing log fixtures below are **captured verbatim** from ngspice 47 on
a 4-line top deck (`* top` / `.include body.cir` / `.op` / `.end`) whose body
carried each title -- not hand-synthesized -- so the echo shape the classifier
keys on is the real one. Each row of the issue's table is represented:

| title on line 1     | ngspice's cause line                 |
|---------------------|--------------------------------------|
| `Simple title here` | `Unable to find definition of model` |
| `my transient bench`| `could not find a valid modelname`   |
| `5T OTA bench`      | ` unknown device type - error `       |

A title that happens to parse as a *valid* card (`r2 vdd 0 1k`) runs with exit
0 and a wrong circuit; that is out of scope by construction -- see
`test_first_line_that_is_a_valid_card_is_indistinguishable` for what the
classifier can and cannot say about it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from klayout_tools import sim, sim_cause

# --------------------------------------------------------------------------- #
# Captured ngspice-47 logs (see the module docstring)
# --------------------------------------------------------------------------- #

_LOG_UNABLE_TO_FIND_MODEL = (
    "\nNote: No compatibility mode selected!\n\n\nCircuit: * top\n\n"
    "Error on line 1 or its substitute:\n"
    "  simple title here\n"
    "Unable to find definition of model \n"
    "    Simulation interrupted due to error!\n\n"
)

_LOG_NO_VALID_MODELNAME = (
    "\nNote: No compatibility mode selected!\n\n"
    "warning, can't find model 'bench' from line\n"
    "    my transient bench\n\nCircuit: * top\n\n"
    "Error on line 1 or its substitute:\n"
    "  my transient bench\n"
    "could not find a valid modelname\n"
    "    Simulation interrupted due to error!\n\n"
)

_LOG_UNKNOWN_DEVICE_TYPE = (
    "\nNote: No compatibility mode selected!\n\n\nCircuit: * top\n\n"
    "Error on line 1 or its substitute:\n"
    "  5t ota bench\n"
    " unknown device type - error \n"
    "    Simulation interrupted due to error!\n\n"
)

#: The three (captured log, line-1 title) pairs, keyed by ngspice's cause line.
_FAILING_SHAPES = [
    pytest.param(_LOG_UNABLE_TO_FIND_MODEL, "Simple title here", id="unable-to-find"),
    pytest.param(_LOG_NO_VALID_MODELNAME, "my transient bench", id="no-modelname"),
    pytest.param(_LOG_UNKNOWN_DEVICE_TYPE, "5T OTA bench", id="unknown-device"),
]

#: An older ngspice's location-line spelling, with an echo that is *not* the
#: netlist's first line -- the shape `tests/test_sim.py` already pins.
_OTHER_LINE_ABORT = (
    "Circuit: * big flat deck\n"
    "Error on line 42 :\n"
    "  m1 d g s b nfet_bad w=1u l=1u\n"
    "Simulation interrupted due to error!\n"
)

_BODY_TAIL = ".param vdd=3.3\nvdd vdd 0 dc {vdd}\nr1 vdd 0 1k\n"


def _write_netlist(tmp_path: Path, first_line: str, *, lead: str = "") -> str:
    path = tmp_path / "body.cir"
    path.write_text(f"{lead}{first_line}\n{_BODY_TAIL}")
    return str(path)


# --------------------------------------------------------------------------- #
# The classification fires on every shape the engine actually produces
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("log_text,title", _FAILING_SHAPES)
def test_title_line_is_the_only_cause_for_every_failing_shape(
    tmp_path, log_text, title
):
    """All three captured aborts classify as `netlist_title_line` alone: the
    message names the netlist file, the line number and the `*` fix, and
    neither `ngspice_fatal` nor `model_bin_range` survives for the same
    abort."""
    netlist = _write_netlist(tmp_path, title)

    diagnostics = sim._classify_diagnostics(log_text, netlist)

    assert [d["code"] for d in diagnostics] == ["netlist_title_line"]
    (cause,) = diagnostics
    assert cause["severity"] == "error"
    assert f"{netlist}:1" in cause["message"]
    assert f"`{title}`" in cause["message"]
    assert "`.include`" in cause["message"]
    assert "prefix it with `*`" in cause["message"]


def test_model_bin_range_reading_of_the_same_abort_is_suppressed(tmp_path):
    """A title beginning with `m` can make ngspice print `could not find a
    valid modelname` with its `Error:` prefix, which the `model_bin_range`
    pattern matches -- a BSIM bin-range reading that points even further from
    the cause than `ngspice_fatal` does. The title-line cause replaces it."""
    log_text = (
        "Circuit: * top\n"
        "Error on line 1 or its substitute:\n"
        "  my transient bench\n"
        "Error: could not find a valid modelname\n"
        "    Simulation interrupted due to error!\n"
    )
    netlist = _write_netlist(tmp_path, "my transient bench")

    # Without the netlist to compare against, this is the misleading reading.
    assert [d["code"] for d in sim._classify_diagnostics(log_text)] == [
        "model_bin_range"
    ]
    assert [d["code"] for d in sim._classify_diagnostics(log_text, netlist)] == [
        "netlist_title_line"
    ]


def test_unsupported_model_level_still_suppresses_model_bin_range(tmp_path):
    """The pre-existing suppression (#2814) is unaffected by #5433's addition
    to the same condition."""
    log_text = (
        "Only MOS device levels 1-6 are supported in this binary\n"
        "Error: could not find a valid modelname\n"
    )
    netlist = _write_netlist(tmp_path, ".model nfet nmos level=72")

    codes = [d["code"] for d in sim._classify_diagnostics(log_text, netlist)]

    assert codes == ["unsupported_model_level"]


def test_line_number_is_the_titles_own_line_not_line_one(tmp_path):
    """Leading blank lines: the reported line number is the title's actual
    1-based line in the caller's file (ngspice 47 reports `line 3` here)."""
    netlist = _write_netlist(tmp_path, "my transient bench", lead="\n\n")
    log_text = _LOG_NO_VALID_MODELNAME.replace("on line 1", "on line 3")

    (cause,) = sim._classify_diagnostics(log_text, netlist)

    assert cause["code"] == "netlist_title_line"
    assert f"{netlist}:3" in cause["message"]


def test_echo_matches_through_ngspices_whitespace_folding(tmp_path):
    """ngspice echoes the offending line lower-cased and with internal
    whitespace runs collapsed (verified on 47), so the comparison folds both
    sides rather than requiring byte equality."""
    netlist = _write_netlist(tmp_path, "My   Transient\tBench Here")
    log_text = (
        "Error on line 1 or its substitute:\n"
        "  my transient bench here\n"
        "could not find a valid modelname\n"
    )

    (cause,) = sim._classify_diagnostics(log_text, netlist)

    assert cause["code"] == "netlist_title_line"


# --------------------------------------------------------------------------- #
# ... and never otherwise: every fall-through keeps today's classification
# --------------------------------------------------------------------------- #


def test_abort_on_another_line_is_classified_exactly_as_before(tmp_path):
    """An `Error on line N` abort whose echo is not the netlist's first line
    keeps its `ngspice_fatal` classification, message and all."""
    netlist = _write_netlist(tmp_path, "my transient bench")

    with_netlist = sim._classify_diagnostics(_OTHER_LINE_ABORT, netlist)

    assert with_netlist == sim._classify_diagnostics(_OTHER_LINE_ABORT)
    assert [d["code"] for d in with_netlist] == ["ngspice_fatal"]


@pytest.mark.parametrize(
    "first_line",
    [
        pytest.param("* commented title", id="comment"),
        pytest.param(".param vdd=3.3", id="dot-card"),
        pytest.param("+ continuation", id="continuation"),
    ],
)
def test_comment_dot_and_continuation_first_lines_never_fire(tmp_path, first_line):
    """ngspice never parses one of these as a device, so the #5433 failure
    cannot be what happened even when the echo matches."""
    netlist = _write_netlist(tmp_path, first_line)
    log_text = (
        "Error on line 1 or its substitute:\n"
        f"  {first_line.lower()}\n"
        "Unable to find definition of model \n"
    )

    codes = [d["code"] for d in sim._classify_diagnostics(log_text, netlist)]

    assert "netlist_title_line" not in codes
    assert codes == ["ngspice_fatal"]


@pytest.mark.parametrize("netlist_path", ["", "/nonexistent/path/body.cir"])
def test_unreadable_netlist_falls_through_to_todays_codes(netlist_path):
    """Step 4 of the design: a netlist that cannot be read where
    classification runs (an off-host corner, a deleted scratch dir) leaves the
    result exactly as it was -- the check never raises."""
    codes = [
        d["code"]
        for d in sim._classify_diagnostics(_LOG_NO_VALID_MODELNAME, netlist_path)
    ]

    assert codes == ["ngspice_fatal"]


def test_blank_netlist_has_no_first_line(tmp_path):
    path = tmp_path / "empty.cir"
    path.write_text("\n\n   \n")

    assert sim_cause._first_netlist_line(str(path)) is None


def test_log_with_no_echo_after_the_location_line_falls_through(tmp_path):
    """`Error on line N` as the log's last line: nothing to compare, so no
    title-line claim is made."""
    netlist = _write_netlist(tmp_path, "my transient bench")

    assert sim_cause._netlist_title_line("Error on line 1 :\n", netlist) is None


def test_first_line_that_is_a_valid_card_is_indistinguishable(tmp_path):
    """The documented limitation. A title that parses as a valid card (the
    issue's `r2 vdd 0 1k` row) makes ngspice run a *wrong circuit* with exit
    0 and no error at all -- there is no abort to classify, so this code can
    never fire for it."""
    netlist = _write_netlist(tmp_path, "r2 vdd 0 1k")
    quiet_log = "Circuit: * top\nNo. of Data Rows : 1\n"

    assert sim._classify_diagnostics(quiet_log, netlist) == []


# --------------------------------------------------------------------------- #
# Contract surface: vocabulary, deck-abort semantics, docs
# --------------------------------------------------------------------------- #


def test_code_is_selectable_and_counts_as_a_deck_abort():
    assert "netlist_title_line" in sim.DIAGNOSTIC_CODES
    assert "netlist_title_line" in sim._NGSPICE_FATAL_CODES
    assert sim_cause.NETLIST_TITLE_LINE_CODE == "netlist_title_line"


def test_title_line_abort_marks_analysis_not_started(tmp_path):
    """Being in `_NGSPICE_FATAL_CODES` is what gives the cause
    `analysis_started: false` and collapses the measurement cascade."""
    netlist = _write_netlist(tmp_path, "my transient bench")
    diagnostics = sim._classify_diagnostics(_LOG_NO_VALID_MODELNAME, netlist)

    assert sim._mark_analysis_not_started(diagnostics, _LOG_NO_VALID_MODELNAME)

    (cause,) = diagnostics
    assert cause["analysis_started"] is False
    assert "no analysis started" in cause["message"]
    assert sim._demote_abort_cascade([{"code": "measurement"}], True) == []


def test_failure_classification_table_documents_the_code():
    doc = Path(__file__).resolve().parents[1] / "docs" / "cli" / "sim.md"
    text = doc.read_text()

    assert "| `netlist_title_line` |" in text
    assert "must not start with a SPICE title line" in text
