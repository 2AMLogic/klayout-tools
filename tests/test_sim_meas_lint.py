"""Tests for the `.meas` signal-position expression lint (issue #5651).

A bare arithmetic expression in a `.meas` signal position fails every
corner with ngspice's "no such vector"; these cover the refusal (raised
before any corner runs, on the pre-run path ``--dry-run`` includes), the
known-good shapes that must stay unflagged, the ``par('...')``/two-node
rewrites the message quotes, and the control-scope remedy (a ``let``
helper from an intermediate ``expr`` measurement, not the file-scope
``par('...')`` wrapper ngspice rejects inside a ``.control`` block).
"""

from __future__ import annotations

import pytest

from klayout_tools import sim
from test_sim import _write_body, _write_request


@pytest.mark.parametrize(
    "card",
    [
        # Every signal position, from the measured failing fragments in
        # issue #5651 (a): ngspice resolves no such vector for each.
        ".meas tran t when v(a)-v(b)=0.3",
        ".meas tran t when v(a) - v(b)=0.3",
        ".meas tran t when v(a,b)=0.3",
        ".meas tran t find v(a)-v(b) at=2u",
        ".meas tran t avg v(a)-v(b) from=0 to=20u",
        ".meas tran t max v(a)-v(b) from=0 to=20u",
        ".meas tran t trig v(a)-v(b) val=0.3 rise=1 targ v(a) val=0.9 rise=1",
        # an operator inside a par('...') span and another outside it
        ".meas tran t when par('v(a)-v(b)')-v(c)=0.3",
        # a lone reference bound to a non-reference operand
        ".meas tran t when 2*v(a)=0.5",
        ".meas tran t when v(a)*2=0.5",
        # a call around the expression: the rewrite must wrap the whole
        # call, not splice par('...') inside it (measured on ngspice-47:
        # abs(par('v(a)-v(b)')) fatals; par('abs(v(a)-v(b))') works)
        ".meas tran t when abs(v(a)-v(b))=0.1",
        ".meas tran t when v(a)*abs(v(b))=0.1",
        ".meas tran t when v(a)*2*par('v(b)')=0",
        # a parenthesised difference straight after the signal-position
        # keyword: the group is not a call, and the rewrite must keep the
        # keyword outside the par('...') span (see the rewrite-text test
        # below -- `par('when (v(a)-v(b))')` fatals ngspice-47)
        ".meas tran t when (v(a)-v(b))=0.1",
        ".meas tran t avg (v(a)-v(b))",
    ],
)
def test_validate_meas_card_rejects_signal_position_expression(card):
    # Issue #5651: a bare arithmetic expression in a .meas signal position
    # fails every corner ("no such vector"); refuse it before any corner
    # runs, naming the par('...') form that works in the same card.
    with pytest.raises(sim.SimError, match=r"par\("):
        sim._validate_meas_card("t", card)


@pytest.mark.parametrize(
    "card",
    [
        ".meas tran t when v(a)=0.5",  # the single-vector baseline
        ".meas tran t when a=0.5",  # a bare identifier LHS is legal
        ".meas tran t when i(V1)=-0.0005",  # the - binds a number
        ".MEAS tran t WHEN V(A)=0.5",  # case-insensitive
        ".measure tran t find v(out) at=1n",  # long spelling
        ".meas tran t when par('v(a)-v(b)')=0.3",
        ".meas tran t when par('v(a)-v(b)')=0.3 rise=1",
        ".meas tran t when par('v(a)-v(b)')=0.3 fall=1",
        ".meas tran t when par('v(a)-v(b)')=0.3 cross=2",
        ".meas tran t trig v(a) val=0.3 rise=1 targ v(b) val=0.9 rise=1",
        ".meas tran t when v(a)=0.5*1.62",  # #2826 RHS: runtime diagnostic
        ".meas tran t find v(a) at='1u+2n'",  # #2826: pinned accepted
        ".meas tran t max i(vout) from=0 to=1u",
        ".meas tran t when v(a)=0.5\n+ rise=1",  # + continuation line
        "print v(a)-v(b)",  # not a .meas card: left alone
    ],
)
def test_validate_meas_card_accepts_single_vector_signal_positions(card):
    # Issue #5651: the lint must fire on the measured failures and nothing
    # else -- no false positive on any known-good card shape.
    sim._validate_meas_card("t", card)  # must not raise


def test_validate_meas_card_rewrites_expression_in_message():
    # The refusal quotes the author's own expression and the exact
    # par('...') rewrite of their card (issue #5651 message contract).
    with pytest.raises(sim.SimError) as excinfo:
        sim._validate_meas_card("t_x", ".meas tran t_x when v(a)-v(b)=0.3")
    assert "v(a)-v(b)" in str(excinfo.value)
    assert ".meas tran t_x when par('v(a)-v(b)')=0.3" in str(excinfo.value)
    # a crossings entry's value is a count, not a time -- stated, so an
    # author picks the right form
    assert "measurements[].crossings" in str(excinfo.value)
    assert "count" in str(excinfo.value)


def test_validate_meas_card_rewrites_call_expression_as_whole_par_span():
    # A flagged expression inside or beside a call must be quoted whole:
    # the rewrite splices par('...') around the call (or the whole operand
    # run), never inside it -- the spliced-in forms are cards ngspice
    # fatals or crashes on (measured on ngspice-47, issue #5651 verdict).
    cases = [
        (
            ".meas tran t_x when abs(v(a)-v(b))=0.1",
            ".meas tran t_x when par('abs(v(a)-v(b))')=0.1",
        ),
        (
            ".meas tran t_x when v(a)*abs(v(b))=0.1",
            ".meas tran t_x when par('v(a)*abs(v(b))')=0.1",
        ),
        (
            ".meas tran t_x when v(a)*2*par('v(b)')=0",
            ".meas tran t_x when par('v(a)*2*v(b)')=0",
        ),
    ]
    for card, rewrite in cases:
        with pytest.raises(sim.SimError) as excinfo:
            sim._validate_meas_card("t_x", card)
        message = str(excinfo.value)
        assert rewrite in message
        # the spliced-into-the-call forms must never be suggested
        assert "abs(par(" not in message
        assert "par('v(a)*abs')" not in message
        assert "par('v(a)*2*par')" not in message


#: Every `.meas` signal-position keyword that can sit right before a
#: parenthesised flagged expression (issue #5651 verdict on b8297e5cf: the
#: lint read each as the callee of a call and widened the span over it).
_SIGNAL_POSITION_KEYWORDS = [
    "when", "find", "avg", "max", "min", "rms",
    "integ", "pp", "deriv", "trig", "targ", "param",
]  # fmt: skip


@pytest.mark.parametrize("keyword", _SIGNAL_POSITION_KEYWORDS)
def test_validate_meas_card_rewrite_keeps_keyword_outside_par_span(keyword):
    # A parenthesised difference right after a signal-position keyword is a
    # bare group, not a call: the working rewrite wraps the group *with* its
    # parens -- `when par('(v(a)-v(b))')=0.1` -- and leaves the keyword
    # outside. `par('when (v(a)-v(b))')` is a card ngspice-47 fatals on
    # ("no such function 'when'"), and the quoted expression must be the
    # author's own `(v(a)-v(b))`, not one that swallows the keyword.
    card = f".meas tran t_x {keyword} (v(a)-v(b))=0.1"
    with pytest.raises(sim.SimError) as excinfo:
        sim._validate_meas_card("t_x", card)
    message = str(excinfo.value)
    assert f".meas tran t_x {keyword} par('(v(a)-v(b))')=0.1" in message
    assert "expression '(v(a)-v(b))'" in message
    assert f"par('{keyword} " not in message
    assert f"({keyword} (" not in message


def test_validate_meas_card_rewrite_keyword_is_case_insensitive():
    # ngspice card syntax is case-insensitive; so is the keyword guard.
    with pytest.raises(sim.SimError) as excinfo:
        sim._validate_meas_card("t_x", ".MEAS TRAN t_x WHEN (V(A)-V(B))=0.1")
    message = str(excinfo.value)
    assert ".MEAS TRAN t_x WHEN par('(V(A)-V(B))')=0.1" in message
    assert "par('WHEN" not in message


def test_validate_meas_card_rewrite_separates_keyword_from_par_span():
    # `when(v(a)-v(b))` has no space to keep the keyword apart; the rewrite
    # must not glue them into the unknown token `whenpar(`.
    with pytest.raises(sim.SimError) as excinfo:
        sim._validate_meas_card("t_x", ".meas tran t_x when(v(a)-v(b))=0.1")
    message = str(excinfo.value)
    assert ".meas tran t_x when par('(v(a)-v(b))')=0.1" in message
    assert "whenpar(" not in message


def test_validate_meas_card_rewrite_widens_operand_across_keyword_group():
    # The lone-reference widening reaches the same keyword-adjacent group
    # through _token_start: `when (v(a))-v(b)` is one expression and the
    # keyword stays outside the quote.
    with pytest.raises(sim.SimError) as excinfo:
        sim._validate_meas_card("t_x", ".meas tran t_x when (v(a))-v(b)=0.1")
    message = str(excinfo.value)
    assert ".meas tran t_x when par('(v(a))-v(b)')=0.1" in message
    assert "expression '(v(a))-v(b)'" in message


def test_validate_meas_card_rewrite_wraps_group_nested_in_call():
    # A bare group inside a call widens outward through both: the call and
    # the group's own parens end up inside one par('...') span.
    with pytest.raises(sim.SimError) as excinfo:
        sim._validate_meas_card("t_x", ".meas tran t_x when abs((v(a)-v(b)))=0.1")
    assert ".meas tran t_x when par('abs((v(a)-v(b)))')=0.1" in str(excinfo.value)


def test_validate_meas_card_control_scope_keeps_keyword_for_group():
    # Control scope shares the span: the reference handed back must keep the
    # `when` keyword (it used to read `.meas tran c_x vdiff=0.1`), and the
    # let helper holds the author's whole parenthesised expression.
    with pytest.raises(sim.SimError) as excinfo:
        sim._validate_meas_card(
            "c_x", ".meas tran c_x when (v(a)-v(b))=0.1", control_scope=True
        )
    message = str(excinfo.value)
    assert "`.meas tran c_x when vdiff=0.1`" in message
    assert '"expr": "(v(a)-v(b))"' in message


def test_validate_meas_card_control_scope_call_expression_names_vdiff():
    # The control-scope remedy replaces the same widened span: the let
    # helper holds the whole call expression and the card references it by
    # name, so the quoted expr is the author's whole local expression.
    with pytest.raises(sim.SimError) as excinfo:
        sim._validate_meas_card(
            "c_x", ".meas tran c_x when abs(v(a)-v(b))=0.1", control_scope=True
        )
    message = str(excinfo.value)
    assert '"expr": "abs(v(a)-v(b))"' in message
    assert "when vdiff=0.1" in message


def test_validate_meas_card_rewrites_two_node_voltage_as_difference():
    # v(a,b) resolves no such vector; the expressible equivalent of the
    # two-node voltage is v(a)-v(b) inside the par('...') wrapper.
    with pytest.raises(sim.SimError) as excinfo:
        sim._validate_meas_card("t", ".meas tran t when v(a,b)=0.3")
    assert "par('v(a)-v(b)')" in str(excinfo.value)


def test_validate_meas_card_control_scope_names_let_not_par():
    # Issue #5651 (c): par('...') is file-scope only -- inside a .control
    # block ngspice rejects it, and the working route is a let helper line.
    # In request terms: an intermediate expr measurement declared before
    # the card (sim_steps emits it as `let <name> = <expr>`).
    with pytest.raises(sim.SimError, match="let ") as excinfo:
        sim._validate_meas_card(
            "c_x", ".meas tran c_x when v(a)-v(b)=0.3", control_scope=True
        )
    assert "par(" not in str(excinfo.value)
    assert '"intermediate": true' in str(excinfo.value)


def test_run_sim_refuses_signal_position_expression_before_any_corner(tmp_path):
    # Issue #5651 (f): --dry-run passes today (exit 0, warnings: []) and
    # every corner then fails; run_sim must refuse the request up front,
    # before any corner is dispatched, naming the par('...') rewrite.
    _write_body(tmp_path)
    request = _write_request(
        tmp_path,
        {
            "netlist": "body.spice",
            "analysis": {"kind": "tran", "args": "1n 10u"},
            "measurements": [
                {"name": "t_x", "spice": ".meas tran t_x when v(out)-v(vdd)=0.3"}
            ],
        },
    )
    with pytest.raises(sim.SimError, match=r"par\('v\(out\)-v\(vdd\)'\)"):
        sim.run_sim(str(request))


def test_run_sim_dry_run_refuses_signal_position_expression(tmp_path):
    # The lint sits on the pre-run path, so --dry-run (the documented
    # pre-submit gate) fails on the same request instead of reporting
    # valid with no warnings.
    _write_body(tmp_path)
    request = _write_request(
        tmp_path,
        {
            "netlist": "body.spice",
            "analysis": {"kind": "tran", "args": "1n 10u"},
            "measurements": [
                {"name": "t_x", "spice": ".meas tran t_x when v(out)-v(vdd)=0.3"}
            ],
        },
    )
    with pytest.raises(sim.SimError, match=r"par\("):
        sim.run_sim(str(request), dry_run=True)


def test_run_sim_refuses_signal_position_expression_in_step_measurement():
    # Issue #5651 (c): the same defect on an analysis_steps[] card is
    # refused too, and the message names the let-helper route (an
    # intermediate expr) rather than par('...').
    request = {
        "netlist": "x",
        "analysis_steps": [
            {
                "name": "s1",
                "analysis": {"kind": "tran", "args": "1n 10u"},
                "measurements": [
                    {"name": "c_x", "spice": ".meas tran c_x when v(out)-v(vdd)=0.3"}
                ],
            }
        ],
    }
    with pytest.raises(sim.SimError, match="let ") as excinfo:
        sim._resolve_analysis_and_measurements(request)
    assert "par(" not in str(excinfo.value)
