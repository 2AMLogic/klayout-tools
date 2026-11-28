"""Issue #4924: model-binding cause lines on ngspice ``Error on line N``.

Issue #7032: the continuation block under that header (the echoed offending
card and the cause line) reaches the message by position.
"""

from __future__ import annotations

import pytest

from klayout_tools import sim, sim_cause


def test_window_miss_is_recognised_from_the_message_the_suffix_builds():
    """Issue #7904: `reports_window_miss` is the consumer half of
    `_cause_suffix`, so a `klt pex` row can tell ngspice's "out of interval"
    (the analysis completed) from any other `.meas` failure reason."""
    base = "measurement 'tcross' produced no value"
    assert sim_cause.reports_window_miss(
        base + sim_cause._cause_suffix("out of interval")
    )
    assert not sim_cause.reports_window_miss(base)
    assert not sim_cause.reports_window_miss(
        base + sim_cause._cause_suffix("Expressions like 0.5*1.62 are not supported.")
    )
    assert not sim_cause.reports_window_miss("")


def test_window_miss_is_recognised_from_a_real_unreached_corner():
    """The corner's own `measurement` diagnostic, as `_no_value_measurement`
    builds it from ngspice's log, satisfies the predicate."""
    spec = {"name": "tcross", "spice": ".meas tran tcross WHEN v(mid)=5 CROSS=1"}
    _, diagnostic, _ = sim._no_value_measurement(
        spec, {"tcross": "out of interval"}, save_mode="all"
    )
    assert diagnostic["code"] == "measurement"
    assert sim_cause.reports_window_miss(diagnostic["message"])
    _, other, _ = sim._no_value_measurement(spec, {}, save_mode="all")
    assert not sim_cause.reports_window_miss(other["message"])


def test_error_on_line_message_carries_model_binding_causes():
    """Issue #4924: `Error on line N` gets bounded cause lines, model-binding
    lines first, and never repeats the matched line itself."""
    log = (
        "warning: something generic\n"
        "Unknown model type foo - ignored\n"
        "Error on line 12 or its substitute:\n"
        "Unable to find definition of model foo\n"
    )
    (diag,) = sim.classify_diagnostics(log)
    assert diag["code"] == "ngspice_fatal"
    assert diag["message"].startswith("Error on line 12")
    assert (
        "[cause: Unknown model type foo - ignored | "
        "Unable to find definition of model foo]"
    ) in diag["message"]
    assert "generic" not in diag["message"]


def test_error_on_line_without_cause_is_unchanged():
    (diag,) = sim.classify_diagnostics("Error on line 42 or its substitute:\n")
    assert diag["message"] == "Error on line 42 or its substitute:"


def test_error_on_line_message_carries_the_echoed_card_and_cause():
    """Issue #7032: the lines under an `Error on line N` header -- the echoed
    card and the cause ngspice prints under it -- reach the message in log
    order, so a remote unit's failure reason is readable without a rerun."""
    log = (
        "Error on line 1204 or its substitute:\n"
        "  m.x1 d g s b nfet w=1u l=0.15u\n"
        "  could not find a valid modelname\n"
        "Simulation interrupted due to error!\n"
    )
    (diag,) = sim.classify_diagnostics(log)
    assert diag["code"] == "ngspice_fatal"
    message = diag["message"]
    assert message.startswith("Error on line 1204")
    assert (
        "[cause: m.x1 d g s b nfet w=1u l=0.15u | "
        "could not find a valid modelname]" in message
    )


def test_error_on_line_context_is_bounded_and_excludes_following_blocks():
    """Issue #7032: blank lines are skipped, an overlong card is truncated per
    line without consuming the cause line's slot, and the block stops at the
    abort trailer so an unrelated following error is excluded."""
    card = "m.x1 d g s b nfet " + "x" * 500
    log = (
        "Error on line 5 or its substitute:\n"
        "\n"
        f"  {card}\n"
        "\n"
        "  could not find a valid modelname\n"
        "Simulation interrupted due to error!\n"
        "Error on line 9 or its substitute:\n"
        "  m.x2 d g s b nfet w=1u l=0.15u\n"
        "  another cause\n"
    )
    (diag,) = sim.classify_diagnostics(log)
    message = diag["message"]
    assert card[:200] in message
    assert "could not find a valid modelname" in message
    assert "m.x2" not in message
    assert "another cause" not in message


@pytest.mark.parametrize("warnings", [1, 2])
def test_error_on_line_prioritizes_card_and_cause_over_startup_warnings(warnings):
    card = "m.x1 d g s b nfet " + "x" * 500
    cause = "could not find a valid modelname"
    lines = [f"Warning: startup {n}" for n in range(warnings)]
    lines.extend(["Error on line 5 or its substitute:", card, cause])
    (diag,) = sim.classify_diagnostics("\n".join(lines))
    assert diag["message"].endswith(f"[cause: {card[:200]} | {cause}]")


@pytest.mark.parametrize("continuation", ["", "could not find a valid modelname"])
@pytest.mark.parametrize(
    "boundary",
    [
        "Simulation interrupted due to error!\nError: incomplete or empty netlist",
        "Error on line 9 or its substitute:",
        "Error in netlist line no. 9",
    ],
)
def test_error_on_line_does_not_borrow_a_later_error(continuation, boundary):
    header = "Error on line 5 : m.x1 d g s b nfet w=1u l=0.15u"
    log = "\n".join([header, continuation, boundary, "unrelated cause"])
    (diag,) = sim.classify_diagnostics(log)
    expected = f"{header} [cause: {continuation}]" if continuation else header
    assert diag["message"] == expected
