"""Issue #4924: model-binding cause lines on ngspice ``Error on line N``."""

from __future__ import annotations

from klayout_tools import sim


def test_error_on_line_message_carries_model_binding_causes():
    """Issue #4924: `Error on line N` gets bounded cause lines, model-binding
    lines first, and never repeats the matched line itself."""
    log = (
        "warning: something generic\n"
        "Unknown model type foo - ignored\n"
        "Error on line 12 or its substitute:\n"
        "Unable to find definition of model foo\n"
    )
    (diag,) = sim._classify_diagnostics(log)
    assert diag["code"] == "ngspice_fatal"
    assert diag["message"].startswith("Error on line 12")
    assert (
        "[cause: Unknown model type foo - ignored | "
        "Unable to find definition of model foo]"
    ) in diag["message"]
    assert "generic" not in diag["message"]


def test_error_on_line_without_cause_is_unchanged():
    (diag,) = sim._classify_diagnostics("Error on line 42 or its substitute:\n")
    assert diag["message"] == "Error on line 42 or its substitute:"
