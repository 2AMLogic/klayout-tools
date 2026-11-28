"""Tests for `gen-compose` wiring a `diff_pair` row whose two nets share the
inter-unit column gap (issue #5764).

Split out of `test_gen_compose.py` to stay under that file's frozen per-file
size ceiling (the per-file size ratchet); mirrors its fixtures verbatim so
the tests run against the same fabricated PDK installs.

PDK resolution is exercised against a **fabricated** open_pdks-layout install
under ``tmp_path`` (mirrors `test_gen.py`/`test_pdk.py`) -- CI never
downloads a real PDK. The environment is scrubbed and the `pdk` module's
search-space constants are pointed away from the host by default (the
`_isolate` autouse fixture) so results are hermetic regardless of what is
installed on the machine running the suite.
"""

import pytest

from klayout_tools import gen, pdk
from klayout_tools.drc import run_drc
from klayout_tools.gen_compose import compose


def _make_install(root, variant):
    """Fabricate a minimal open_pdks-layout variant (just the layout probe)."""
    variant_dir = root / variant
    (variant_dir / "libs.tech").mkdir(parents=True)
    return variant_dir


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    """Scrub PDK env vars and empty the host search space -- see test_pdk.py."""
    monkeypatch.delenv("PDK_ROOT", raising=False)
    monkeypatch.delenv("PDK", raising=False)
    monkeypatch.setattr(pdk, "STORE_DIRS", [])
    monkeypatch.setattr(pdk, "CONVENTIONAL_PREFIXES", [])


@pytest.fixture()
def both_pdk_root(tmp_path):
    """Both PDK families the phase-2 generators support (see
    `klayout_tools.gen.PDK_ROLE_LAYERS`), under one install root -- used by
    the DRC-clean-on-both-decks tests below."""
    root = tmp_path / "pdk_install"
    _make_install(root, "sky130A")
    _make_install(root, "gf180mcuD")
    return root


def _gen_block_variant(tmp_path, pdk_root, variant, generator, cell_name, **params):
    """Like :func:`_gen_block`, but for an arbitrary PDK ``variant`` (e.g.
    ``"gf180mcuD"``) -- used by the dual-deck stub-widen (#496) tests below."""
    output = tmp_path / f"{cell_name}.gds"
    request = {
        "schema": gen.REQUEST_SCHEMA,
        "generator": generator,
        "pdk": {"variant": variant, "root": str(pdk_root)},
        "params": params,
        "options": {"cell_name": cell_name, "output": str(output)},
    }
    return gen.generate(request)


def _diff_pair_two_net_row_request(both_pdk_root, tmp_path, output, pitch_x_um=None):
    """Issue #5764's reproduction: one `diff_pair` block (splits=2) on
    gf180mcuD, its two rows wired so two *different* nets both own fixtures
    in the single inter-unit column gap -- OUTN on the column-0 drains
    (endpoint stubs extend east into the gap, its backbone threads the gap
    channel vertically) and TAIL on the column-1 sources (endpoint stubs
    extend west into the same gap, its backbone runs down the gap's east
    side). At the generator's default inter-unit pitch the router's own
    stub lane plus endpoint pads leave 0.19um where gf180mcu's deck demands
    0.28um (metal{1,2}.space.1); `params.pitch_x_um` is the knob that buys
    the room back."""
    params = {"splits": 2, "add_guard_ring": False}
    if pitch_x_um is not None:
        params["pitch_x_um"] = pitch_x_um
    block = _gen_block_variant(
        tmp_path, both_pdk_root, "gf180mcuD", "diff_pair", "dp", **params
    )
    return {
        "pdk": {"variant": "gf180mcuD", "root": str(both_pdk_root)},
        "blocks": [{"id": "dp", "generator_report": block}],
        "placement": {"strategy": "row", "order": ["dp"], "spacing_um": 1.0},
        "connectivity": [
            {
                "net": "OUTN",
                "pins": [
                    {"block": "dp", "port": "Q1_1_D"},
                    {"block": "dp", "port": "Q2_2_D"},
                ],
            },
            {
                "net": "TAIL",
                "pins": [
                    {"block": "dp", "port": "Q2_1_S"},
                    {"block": "dp", "port": "Q1_2_S"},
                ],
            },
        ],
        "routing": {"layer_role": "metal2", "width_um": 0.28},
        "options": {"cell_name": "diff_pair_two_net_row", "output": str(output)},
    }


def test_compose_gf180mcu_diff_pair_two_net_row_default_pitch_cannot_route(
    tmp_path, both_pdk_root
):
    # The same composition at the generator's default pitch must still fail,
    # so the widened-pitch regression below cannot pass vacuously: the
    # second net's stub lane comes within gf180mcu's same-layer spacing of
    # the first net's already-routed footprint inside the inter-unit gap.
    output = tmp_path / "diff_pair_two_net_row_default.gds"
    request = _diff_pair_two_net_row_request(both_pdk_root, tmp_path, output)
    report = compose(request)
    assert report["unrouted_nets"] == ["TAIL"]
    # Moving the second backbone up would leave its intermediate Metal2
    # landing too close to OUTN. The GDS must keep this spacing clean.
    assert run_drc(str(output), "gf180mcu")["status"] == "clean"


def test_compose_gf180mcu_diff_pair_two_net_row_widened_pitch_routes_drc_clean(
    tmp_path, both_pdk_root
):
    # Issue #5764 acceptance: at an effective pitch of at least
    # `unit extent + 0.49um` (delta = 0.09um over the legal minimum, derived
    # in the issue from the pure endpoint-pad arithmetic), the same two-net
    # composition composes fully routed and reports zero
    # metal{1,2,3,4}.space.1 violations from `klt drc`. This wiring asks a
    # little more than that arithmetic: OUTN's backbone also threads the gap
    # channel vertically between the two nets' fixtures, a constraint the
    # endpoint-only derivation does not count, so the test uses delta = 0.5
    # (effective pitch `unit extent + 0.9um` -- still "at least"
    # `unit extent + 0.49um`, with margin over every near-miss the router
    # actually draws; verified at delta = 0.09 the composition still refuses
    # with the issue's own `metal2.space.1` near-miss message).
    block = _gen_block_variant(
        tmp_path, both_pdk_root, "gf180mcuD", "diff_pair", "dp_probe", splits=2
    )
    min_col_pitch = next(
        p["x_um"] for p in block["ports"] if p["name"] == "Q1_2_S"
    ) - next(p["x_um"] for p in block["ports"] if p["name"] == "Q1_1_S")

    output = tmp_path / "diff_pair_two_net_row_wide.gds"
    request = _diff_pair_two_net_row_request(
        both_pdk_root, tmp_path, output, pitch_x_um=min_col_pitch + 0.5
    )
    report = compose(request)
    assert report["unrouted_nets"] == []
    assert all(net["routed"] for net in report["nets"])

    drc_report = run_drc(str(output), "gf180mcu")
    space_rules = {
        v["rule"]
        for v in drc_report["violations"]
        if v["rule"]
        in {
            "metal1.space.1",
            "metal2.space.1",
            "metal3.space.1",
            "metal4.space.1",
        }
    }
    assert space_rules == set(), drc_report["violations"]
    assert drc_report["status"] == "clean", drc_report["violations"]
