"""Tests for the step-measurement name refusals (`klt sim`, issue #5446).

A step measurement's name becomes an ngspice vector name in that step's
plot, so naming one after a deck node replaces that node's waveform with a
one-point scalar and every *later* card in the step that reads the node
fails with `find(AT) : out of interval` -- reported against the innocent
later card. `src/klayout_tools/sim_step_shadow.py` refuses the collision at
request resolution; these tests pin both the refusals and, just as
importantly, the shapes that must keep working.

Resolution-level only: the check raises before any deck is written, so no
ngspice is needed here. The end-to-end behaviour (and the fact that
renaming the earlier measurement makes the same bench pass) is exercised by
`tests/test_sim_steps.py`'s integration tests.
"""

from __future__ import annotations

import copy

import pytest

from klayout_tools import sim, sim_step_shadow, sim_steps

pytestmark = pytest.mark.usefixtures(
    "real_build_identity_git", "bare_name_ngspice_resolution"
)


def _resolve(request: dict):
    return sim_steps.resolve_analysis_steps(copy.deepcopy(request))


#
# ngspice stores a measurement's result as a vector named after the
# measurement in the step's plot, so a measurement named after a deck node
# replaces that node's waveform with a one-point scalar. Later cards in the
# same step that read the node then fail with `find(AT) : out of interval`
# -- reported against the innocent later card. The collision is visible in
# the request alone, so it is refused at resolution, before any deck exists.


def _shadow_request(measurements: list[dict]) -> dict:
    return {
        "netlist": "body.spice",
        "analysis_steps": [
            {
                "name": "pulse",
                "analysis": {"kind": "tran", "args": "10u 1m"},
                "measurements": measurements,
            }
        ],
    }


def _meas(name: str, node: str, at: str = "0.2m") -> dict:
    return {"name": name, "spice": f".meas tran {name} find v({node}) at={at}"}


_SHADOW_REFUSALS = [
    (
        "both entries are .meas cards",
        [_meas("a", "A"), _meas("a_late", "A", at="0.8m")],
        "a_late",
        "a",
        "a",
    ),
    (
        "the earlier entry is an expr",
        [{"name": "b", "expr": "v(B)[5]"}, _meas("b_late", "B", at="0.8m")],
        "b_late",
        "b",
        "b",
    ),
    (
        "the earlier entry is an intermediate expr",
        [
            {"name": "b", "expr": "v(B)[5]", "intermediate": True},
            _meas("b_late", "B", at="0.8m"),
        ],
        "b_late",
        "b",
        "b",
    ),
    (
        "the later entry is an expr",
        [_meas("a", "A"), {"name": "a_late", "expr": "vecmax(v(A))"}],
        "a_late",
        "a",
        "a",
    ),
    (
        "both entries are exprs",
        [
            {"name": "a", "expr": "v(A)[5]"},
            {"name": "a_late", "expr": "vecmin(v(A))"},
        ],
        "a_late",
        "a",
        "a",
    ),
    (
        "the read is cased differently from the name",
        [_meas("a", "A"), {"name": "a_late", "expr": "vecmax(V(A))"}],
        "a_late",
        "a",
        "a",
    ),
    (
        "the name is cased differently from the read",
        [_meas("VOUT", "out"), {"name": "later", "expr": "vecmax(v(vout))"}],
        "later",
        "VOUT",
        "vout",
    ),
    (
        "the read is padded with whitespace",
        [_meas("a", "A"), {"name": "a_late", "expr": "vecmax(v( a ))"}],
        "a_late",
        "a",
        "a",
    ),
    (
        "the read is a two-node difference",
        [_meas("a", "A"), {"name": "a_late", "expr": "vecmax(v(x, a))"}],
        "a_late",
        "a",
        "a",
    ),
    (
        "the read is another voltage-family accessor",
        [_meas("a", "A"), {"name": "a_late", "expr": "vecmax(vdb(a))"}],
        "a_late",
        "a",
        "a",
    ),
    (
        "the shadowed node is read by a later .meas card's second argument",
        [_meas("a", "A"), _meas("c", "x,a", at="0.8m")],
        "c",
        "a",
        "a",
    ),
]


@pytest.mark.parametrize(
    "label,measurements,later,earlier,node",
    _SHADOW_REFUSALS,
    ids=[case[0] for case in _SHADOW_REFUSALS],
)
def test_sibling_measurement_shadowing_a_node_read_is_refused(
    label, measurements, later, earlier, node
):
    with pytest.raises(sim.SimError) as exc:
        _resolve(_shadow_request(measurements))
    message = str(exc.value)
    # The message must name all four things the ngspice error names none of:
    # the step, the innocent later card, the earlier card that caused it, and
    # the node whose waveform was replaced.
    assert "'pulse'" in message
    assert repr(later) in message
    assert repr(earlier) in message
    assert f"v({node})" in message
    assert "rename" in message.lower()


_SHADOW_ALLOWED = [
    (
        "a single card named after the node it reads",
        [_meas("a", "a")],
    ),
    (
        "a card reading the node it is itself named after",
        [_meas("a", "a"), _meas("b", "B", at="0.8m")],
    ),
    (
        "the colliding card is last in its step",
        [_meas("early", "A"), _meas("a", "A", at="0.8m")],
    ),
    (
        "a later bare sibling reference",
        [
            {"name": "slope", "expr": "v(A)", "intermediate": True},
            {"name": "top", "expr": "vecmax(slope)"},
        ],
    ),
    (
        "a later current read of the earlier name",
        [_meas("a", "A"), {"name": "a_i", "expr": "vecmax(i(a))"}],
    ),
    (
        "a later read of a node merely containing the earlier name",
        [_meas("a", "A"), {"name": "a_late", "expr": "vecmax(v(a_bus))"}],
    ),
    (
        "an earlier card reading a node a later card is named after",
        [_meas("early", "a"), _meas("a", "B", at="0.8m")],
    ),
]


@pytest.mark.parametrize(
    "label,measurements",
    _SHADOW_ALLOWED,
    ids=[case[0] for case in _SHADOW_ALLOWED],
)
def test_sibling_shadow_check_does_not_refuse_working_requests(label, measurements):
    analysis, specs = _resolve(_shadow_request(measurements))
    assert [s["local_name"] for s in specs if not s.get("intermediate")] == [
        m["name"] for m in measurements if not m.get("intermediate")
    ]
    assert analysis["kind"] == sim_steps.STEPS_KIND


def test_sibling_shadow_check_is_scoped_to_one_step():
    # The clobbered vector lives in its own step's plot, so the same pair of
    # names across two steps is harmless.
    request = {
        "netlist": "body.spice",
        "analysis_steps": [
            {
                "name": "first",
                "analysis": {"kind": "tran", "args": "10u 1m"},
                "measurements": [_meas("a", "A")],
            },
            {
                "name": "second",
                "analysis": {"kind": "tran", "args": "10u 1m"},
                "measurements": [_meas("a_late", "A", at="0.8m")],
            },
        ],
    }
    _, specs = _resolve(request)
    assert [s["name"] for s in specs] == ["first.a", "second.a_late"]


def test_sibling_shadow_refusal_does_not_change_generated_deck_lines():
    # The check is pure validation: a request that passes it emits exactly
    # the lines it emitted before.
    analysis, specs = _resolve(_shadow_request([_meas("vmid", "out")]))
    assert specs[0]["deck_lines"] == [
        "meas tran vmid find v(out) at=0.2m",
        "let pulse__vmid = vmid",
        "print pulse__vmid",
    ]


# --- The node extractor itself -------------------------------------------
#
# `refuse_sibling_vector_shadow` is only as good as what this reads out of a
# card, so pin the accessor set directly: the voltage family in, the current
# family and bare identifiers out.


@pytest.mark.parametrize(
    "text,expected",
    [
        ("v(a)", {"a"}),
        (".meas tran m find v(A) at=1n", {"a"}),
        ("v(x, a)", {"x", "a"}),
        ("v( a )", {"a"}),
        ("vdb(a) + vm(b) + vp(c) + vr(d) + vi(e)", {"a", "b", "c", "d", "e"}),
        ("v(x1.out)", {"x1.out"}),
        # The current family resolves to `<src>#branch`, never a bare name.
        ("i(vdd) + ir(vdd) + im(vdd)", set()),
        # A bare sibling reference is a documented feature, not a read.
        ("vecmax(slope)", set()),
        # `vecmax` must not be mistaken for a `v..` accessor.
        ("vecmax(v(out))", {"out"}),
        ("", set()),
    ],
)
def test_voltage_read_nodes_reads_only_the_voltage_family(text, expected):
    assert sim_step_shadow.voltage_read_nodes({"expr": text}) == expected


def test_voltage_read_nodes_covers_both_measurement_forms():
    assert sim_step_shadow.voltage_read_nodes({"spice": ".meas dc m find v(Q)"}) == {
        "q"
    }
    assert sim_step_shadow.voltage_read_nodes({"name": "m"}) == set()
    assert sim_step_shadow.voltage_read_nodes({"expr": None}) == set()
