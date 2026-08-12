"""Tests for `klt erc` and the `run_erc` library function: the layer-by-
layer connectivity model (issue #859, Phase 1a) and the per-gate antenna-
ratio verdict (issue #860, Phase 1b) of the antenna + ERC signoff epic
#713.

Fixtures are generated programmatically with `klayout.db` inside the tests,
mirroring `tests/test_power.py`'s convention (`klt power` is the closest
precedent: a sibling connectivity-only Phase 1a verb built on the same
`LayoutToNetlist` wire/via-connectivity API).
"""

from __future__ import annotations

import json

import klayout.db as kdb
import pytest

from klayout_tools.cli import main
from klayout_tools.erc import ErcError, run_erc

DBU = 0.001


def _um(v: float) -> int:
    return int(round(v / DBU))


def _basic_fixture(path) -> None:
    """One gate net threaded through a four-level stack (poly -> li1 ->
    met1 -> met2 via licon/mcon/via1), plus a second, entirely separate
    gate net (a different poly bar, connected only up to li1, with no
    metal at all) -- exercising both "reaches every level" and "stops
    partway" accumulation in the same layout.

    Layer numbers (arbitrary, not sky130's real ones -- kept small and
    distinct for readability, matching `tests/test_mom.py`'s convention):
    poly=1/0, licon=2/0, li1=3/0, mcon=4/0, met1=5/0, via1=6/0, met2=7/0,
    met2 label=7/5.
    """
    layout = kdb.Layout()
    layout.dbu = DBU
    top = layout.create_cell("TOP")

    poly = layout.layer(1, 0)
    licon = layout.layer(2, 0)
    li1 = layout.layer(3, 0)
    mcon = layout.layer(4, 0)
    met1 = layout.layer(5, 0)
    via1 = layout.layer(6, 0)
    met2 = layout.layer(7, 0)
    met2_label = layout.layer(7, 5)

    # Gate A: poly (1x2 um = 2 um^2) -> li1 (2x1 = 2 um^2) -> met1
    # (3x0.5 = 1.5 um^2) -> met2 (0.4x4 = 1.6 um^2), labelled on met2.
    top.shapes(poly).insert(kdb.Box.new(_um(0), _um(0), _um(1), _um(2)))
    top.shapes(licon).insert(kdb.Box.new(_um(0.2), _um(0.2), _um(0.4), _um(0.4)))
    top.shapes(li1).insert(kdb.Box.new(_um(0), _um(0), _um(2), _um(1)))
    top.shapes(mcon).insert(kdb.Box.new(_um(0.5), _um(0.3), _um(0.7), _um(0.5)))
    top.shapes(met1).insert(kdb.Box.new(_um(0), _um(0), _um(3), _um(0.5)))
    top.shapes(via1).insert(kdb.Box.new(_um(1), _um(0.1), _um(1.2), _um(0.3)))
    top.shapes(met2).insert(kdb.Box.new(_um(0.9), _um(0), _um(1.3), _um(4)))
    top.shapes(met2_label).insert(kdb.Text("GATE_A", kdb.Trans(_um(1.0), _um(1.0))))

    # Gate B: isolated poly bar (0.5x1 = 0.5 um^2) with no contact/metal at
    # all -- an unstrapped gate, whose accumulation should simply stop at
    # the gate level (every level above reports 0).
    top.shapes(poly).insert(kdb.Box.new(_um(10), _um(0), _um(10.5), _um(1)))

    layout.write(str(path))


def _basic_spec(path) -> None:
    path.write_text(
        json.dumps(
            {
                "stackup": [
                    {"name": "poly", "layer": "1/0", "role": "gate"},
                    {"name": "li1", "layer": "3/0"},
                    {"name": "met1", "layer": "5/0"},
                    {"name": "met2", "layer": "7/0", "label_layer": "7/5"},
                ],
                "vias": [
                    {"name": "licon", "layer": "2/0", "between": ["poly", "li1"]},
                    {"name": "mcon", "layer": "4/0", "between": ["li1", "met1"]},
                    {"name": "via1", "layer": "6/0", "between": ["met1", "met2"]},
                ],
            }
        )
    )


def _write_spec(path, spec: dict) -> None:
    path.write_text(json.dumps(spec))


def _antenna_fixture(
    path,
    *,
    li1_um2: float = 0.0,
    met1_um2: float = 0.0,
    met2_um2: float = 0.0,
) -> None:
    """Single-gate-net fixture with a fixed 1.0 um^2 poly gate and
    independently configurable li1/met1/met2 areas, used to build golden
    antenna-ratio violate/pass pairs against the real sky130 MAX_EGAR
    limits (li1: 75, met1: 400, met2: 400 -- see
    ``klayout_tools.erc._SKY130_ANTENNA_RATIO_MAX_EGAR``).

    Every configured level's box starts at the origin with a fixed 1um
    height, so its area equals its width directly
    (``step_area_um2 == <level>_um2``) -- the same overlapping-box
    connectivity-threading pattern ``_basic_fixture`` uses (poly -> licon ->
    li1 -> mcon -> met1 -> via1 -> met2), just with each level's width made
    a parameter instead of a fixed literal. Leaving a level at its default
    ``0.0`` omits that level *and* every level above it, mirroring
    ``_basic_fixture``'s unstrapped "Gate B" case -- e.g. passing only
    ``li1_um2`` produces a two-level net (poly, li1) with met1/met2 both
    reporting ``step_area_um2: 0.0``.
    """
    layout = kdb.Layout()
    layout.dbu = DBU
    top = layout.create_cell("TOP")

    poly = layout.layer(1, 0)
    licon = layout.layer(2, 0)
    li1 = layout.layer(3, 0)
    mcon = layout.layer(4, 0)
    met1 = layout.layer(5, 0)
    via1 = layout.layer(6, 0)
    met2 = layout.layer(7, 0)

    top.shapes(poly).insert(kdb.Box.new(_um(0), _um(0), _um(1), _um(1)))

    if li1_um2 > 0:
        top.shapes(licon).insert(
            kdb.Box.new(_um(0.02), _um(0.02), _um(0.05), _um(0.05))
        )
        top.shapes(li1).insert(kdb.Box.new(_um(0), _um(0), _um(li1_um2), _um(1)))

    if met1_um2 > 0:
        assert li1_um2 >= 0.09, "li1 must be wide enough to host mcon"
        top.shapes(mcon).insert(kdb.Box.new(_um(0.06), _um(0.06), _um(0.09), _um(0.09)))
        top.shapes(met1).insert(kdb.Box.new(_um(0), _um(0), _um(met1_um2), _um(1)))

    if met2_um2 > 0:
        assert met1_um2 >= 0.13, "met1 must be wide enough to host via1"
        top.shapes(via1).insert(kdb.Box.new(_um(0.10), _um(0.10), _um(0.13), _um(0.13)))
        top.shapes(met2).insert(kdb.Box.new(_um(0), _um(0), _um(met2_um2), _um(1)))

    layout.write(str(path))


# --- run_erc: connectivity model accumulation -------------------------------


def test_run_erc_reports_two_gates(tmp_path):
    gds = tmp_path / "basic.gds"
    spec = tmp_path / "basic.erc.json"
    _basic_fixture(gds)
    _basic_spec(spec)

    report = run_erc(str(gds), str(spec))

    assert report["schema_version"] == 1
    assert report["file"] == str(gds)
    assert report["spec"] == str(spec)
    assert report["gate_role"] == "poly"
    assert report["gate_count"] == 2


def test_run_erc_accumulates_connected_area_layer_by_layer(tmp_path):
    gds = tmp_path / "basic.gds"
    spec = tmp_path / "basic.erc.json"
    _basic_fixture(gds)
    _basic_spec(spec)

    report = run_erc(str(gds), str(spec))
    gate_a = next(g for g in report["gates"] if g["net"] == "GATE_A")

    assert gate_a["gate_area_um2"] == pytest.approx(2.0)
    assert [level["layer"] for level in gate_a["levels"]] == [
        "poly",
        "li1",
        "met1",
        "met2",
    ]

    step_areas = {level["layer"]: level["step_area_um2"] for level in gate_a["levels"]}
    assert step_areas["poly"] == pytest.approx(2.0)
    assert step_areas["li1"] == pytest.approx(2.0)
    assert step_areas["met1"] == pytest.approx(1.5)
    assert step_areas["met2"] == pytest.approx(1.6)

    # Cumulative is a running sum across fabrication steps, in stackup order.
    cumulative = [level["cumulative_area_um2"] for level in gate_a["levels"]]
    assert cumulative == [
        pytest.approx(2.0),
        pytest.approx(4.0),
        pytest.approx(5.5),
        pytest.approx(7.1),
    ]


def test_run_erc_unstrapped_gate_stops_accumulating_after_gate_level(tmp_path):
    gds = tmp_path / "basic.gds"
    spec = tmp_path / "basic.erc.json"
    _basic_fixture(gds)
    _basic_spec(spec)

    report = run_erc(str(gds), str(spec))
    gate_b = next(g for g in report["gates"] if g["net"] is None)

    assert gate_b["gate_area_um2"] == pytest.approx(0.5)
    step_areas = {level["layer"]: level["step_area_um2"] for level in gate_b["levels"]}
    assert step_areas["poly"] == pytest.approx(0.5)
    assert step_areas["li1"] == 0.0
    assert step_areas["met1"] == 0.0
    assert step_areas["met2"] == 0.0
    # Cumulative never grows past the gate level itself.
    cumulative = [level["cumulative_area_um2"] for level in gate_b["levels"]]
    assert cumulative == [pytest.approx(0.5)] * 4


def test_run_erc_gate_ids_are_stable_ascending(tmp_path):
    gds = tmp_path / "basic.gds"
    spec = tmp_path / "basic.erc.json"
    _basic_fixture(gds)
    _basic_spec(spec)

    report = run_erc(str(gds), str(spec))
    assert [g["gate_id"] for g in report["gates"]] == ["gate0", "gate1"]


# --- run_erc: no matching geometry ------------------------------------------


def test_run_erc_no_gate_geometry_raises(tmp_path):
    gds = tmp_path / "no_gate.gds"
    layout = kdb.Layout()
    layout.dbu = DBU
    top = layout.create_cell("TOP")
    # Metal geometry only -- no poly (gate-role) shapes anywhere.
    met1 = layout.layer(5, 0)
    top.shapes(met1).insert(kdb.Box.new(_um(0), _um(0), _um(1), _um(1)))
    layout.write(str(gds))

    spec = tmp_path / "basic.erc.json"
    _basic_spec(spec)

    with pytest.raises(ErcError, match="no net"):
        run_erc(str(gds), str(spec))


# --- run_erc: spec validation ------------------------------------------------


def test_spec_file_not_found(tmp_path):
    gds = tmp_path / "basic.gds"
    _basic_fixture(gds)
    with pytest.raises(ErcError, match="spec file not found"):
        run_erc(str(gds), str(tmp_path / "nope.json"))


def test_spec_requires_stackup_with_at_least_two_entries(tmp_path):
    gds = tmp_path / "basic.gds"
    _basic_fixture(gds)
    spec = tmp_path / "spec.json"
    _write_spec(spec, {"stackup": [{"name": "poly", "layer": "1/0", "role": "gate"}]})
    with pytest.raises(ErcError, match="at least two entries"):
        run_erc(str(gds), str(spec))


def test_spec_requires_stackup_zero_to_be_gate_role(tmp_path):
    gds = tmp_path / "basic.gds"
    _basic_fixture(gds)
    spec = tmp_path / "spec.json"
    _write_spec(
        spec,
        {
            "stackup": [
                {"name": "poly", "layer": "1/0"},
                {"name": "li1", "layer": "3/0"},
            ]
        },
    )
    with pytest.raises(ErcError, match="stackup\\[0\\] must set"):
        run_erc(str(gds), str(spec))


def test_spec_rejects_a_second_gate_role(tmp_path):
    gds = tmp_path / "basic.gds"
    _basic_fixture(gds)
    spec = tmp_path / "spec.json"
    _write_spec(
        spec,
        {
            "stackup": [
                {"name": "poly", "layer": "1/0", "role": "gate"},
                {"name": "li1", "layer": "3/0", "role": "gate"},
            ]
        },
    )
    with pytest.raises(ErcError, match="only stackup\\[0\\] may set"):
        run_erc(str(gds), str(spec))


def test_spec_rejects_duplicate_stackup_names(tmp_path):
    gds = tmp_path / "basic.gds"
    _basic_fixture(gds)
    spec = tmp_path / "spec.json"
    _write_spec(
        spec,
        {
            "stackup": [
                {"name": "poly", "layer": "1/0", "role": "gate"},
                {"name": "poly", "layer": "3/0"},
            ]
        },
    )
    with pytest.raises(ErcError, match="duplicate stackup name"):
        run_erc(str(gds), str(spec))


def test_spec_rejects_malformed_layer_string(tmp_path):
    gds = tmp_path / "basic.gds"
    _basic_fixture(gds)
    spec = tmp_path / "spec.json"
    _write_spec(
        spec,
        {
            "stackup": [
                {"name": "poly", "layer": "not-a-layer", "role": "gate"},
                {"name": "li1", "layer": "3/0"},
            ]
        },
    )
    with pytest.raises(ErcError, match="must be '<layer>/<datatype>'"):
        run_erc(str(gds), str(spec))


def test_spec_via_between_must_name_two_distinct_stackup_entries(tmp_path):
    gds = tmp_path / "basic.gds"
    _basic_fixture(gds)
    spec = tmp_path / "spec.json"
    _write_spec(
        spec,
        {
            "stackup": [
                {"name": "poly", "layer": "1/0", "role": "gate"},
                {"name": "li1", "layer": "3/0"},
            ],
            "vias": [{"layer": "2/0", "between": ["poly", "nope"]}],
        },
    )
    with pytest.raises(ErcError, match="must name two distinct"):
        run_erc(str(gds), str(spec))


def test_top_required_when_multiple_top_cells(tmp_path):
    gds = tmp_path / "two_tops.gds"
    layout = kdb.Layout()
    layout.dbu = DBU
    layout.create_cell("TOP_A")
    layout.create_cell("TOP_B")
    layout.write(str(gds))

    spec = tmp_path / "basic.erc.json"
    _basic_spec(spec)

    with pytest.raises(ErcError, match="needs exactly one top cell"):
        run_erc(str(gds), str(spec))


# --- run_erc: antenna-ratio verdict (issue #860) -----------------------------


def test_antenna_ratio_computed_but_unchecked_without_pdk(tmp_path):
    gds = tmp_path / "basic.gds"
    spec = tmp_path / "basic.erc.json"
    _basic_fixture(gds)
    _basic_spec(spec)

    report = run_erc(str(gds), str(spec))
    assert report["pdk"] is None
    gate_a = next(g for g in report["gates"] if g["net"] == "GATE_A")

    assert gate_a["antenna_verdict"] == "unchecked"
    for level in gate_a["levels"]:
        assert level["verdict"] == "unchecked"
        assert level["antenna_ratio_max"] is None
        assert level["antenna_ratio_source"] is None
        # antenna_ratio is still derived even with no PDK to compare
        # against -- cumulative_area_um2 / gate_area_um2 (gate_area == 2.0).
        assert level["antenna_ratio"] == pytest.approx(
            level["cumulative_area_um2"] / gate_a["gate_area_um2"]
        )


def test_antenna_gate_role_level_is_never_pdk_checked(tmp_path):
    gds = tmp_path / "basic.gds"
    spec = tmp_path / "basic.erc.json"
    _basic_fixture(gds)
    _basic_spec(spec)

    report = run_erc(str(gds), str(spec), pdk="sky130")
    gate_a = next(g for g in report["gates"] if g["net"] == "GATE_A")
    poly_level = next(lvl for lvl in gate_a["levels"] if lvl["layer"] == "poly")

    # cumulative == gate area at the gate's own level -> ratio trivially 1.0,
    # and it is never compared against a PDK limit (see run_erc's docstring
    # on why the gate role itself is excluded).
    assert poly_level["antenna_ratio"] == pytest.approx(1.0)
    assert poly_level["antenna_ratio_max"] is None
    assert poly_level["antenna_ratio_source"] is None
    assert poly_level["verdict"] == "unchecked"


def test_unknown_pdk_raises(tmp_path):
    gds = tmp_path / "basic.gds"
    spec = tmp_path / "basic.erc.json"
    _basic_fixture(gds)
    _basic_spec(spec)

    with pytest.raises(ErcError, match="unknown --pdk"):
        run_erc(str(gds), str(spec), pdk="nope")


@pytest.mark.parametrize(
    ("layer", "limit", "violate_kwargs", "pass_kwargs"),
    [
        ("li1", 75.0, {"li1_um2": 80.0}, {"li1_um2": 1.0}),
        (
            "met1",
            400.0,
            {"li1_um2": 0.1, "met1_um2": 450.0},
            {"li1_um2": 0.1, "met1_um2": 1.0},
        ),
        (
            "met2",
            400.0,
            {"li1_um2": 0.2, "met1_um2": 1.0, "met2_um2": 450.0},
            {"li1_um2": 0.2, "met1_um2": 1.0, "met2_um2": 1.0},
        ),
    ],
)
def test_antenna_golden_violate_pass_pair_per_layer(
    tmp_path, layer, limit, violate_kwargs, pass_kwargs
):
    """AC #1: at least one golden violate/pass pair per checked metal layer
    (li1, met1, met2), each correctly classified against sky130's real
    MAX_EGAR limit for that layer.
    """
    spec = tmp_path / "basic.erc.json"
    _basic_spec(spec)

    violate_gds = tmp_path / f"{layer}_violate.gds"
    _antenna_fixture(violate_gds, **violate_kwargs)
    violate_report = run_erc(str(violate_gds), str(spec), pdk="sky130")
    violate_level = next(
        lvl for lvl in violate_report["gates"][0]["levels"] if lvl["layer"] == layer
    )
    assert violate_level["antenna_ratio"] > limit
    assert violate_level["antenna_ratio_max"] == pytest.approx(limit)
    assert violate_level["verdict"] == "violate"
    assert violate_report["gates"][0]["antenna_verdict"] == "violate"
    # AC #2: cites the specific PDK limit, traced to source.
    assert "skywater-pdk" in violate_level["antenna_ratio_source"]
    assert "Max EA/A w/o diode" in violate_level["antenna_ratio_source"]

    pass_gds = tmp_path / f"{layer}_pass.gds"
    _antenna_fixture(pass_gds, **pass_kwargs)
    pass_report = run_erc(str(pass_gds), str(spec), pdk="sky130")
    pass_level = next(
        lvl for lvl in pass_report["gates"][0]["levels"] if lvl["layer"] == layer
    )
    assert pass_level["antenna_ratio"] <= limit
    assert pass_level["antenna_ratio_max"] == pytest.approx(limit)
    assert pass_level["verdict"] == "pass"
    assert pass_report["gates"][0]["antenna_verdict"] == "pass"


# --- run_erc: cross-check against klayout's own built-in antenna engine -----


def _klayout_builtin_antenna_violates(
    gds_path, *, through_layer: str, limit: float
) -> bool:
    """Independent cross-check (AC #3): run klayout's own built-in
    ``LayoutToNetlist.antenna_check`` -- a separate engine implemented in
    klayout's C++ core, not a re-derivation of this module's own
    arithmetic -- against the same fixture geometry, and report whether it
    flags a violation.

    Threads the same poly -> li1 -> met1 -> met2 connectivity
    ``_antenna_fixture`` builds, accumulating a "metal so far" region up
    through ``through_layer`` (inclusive of the gate/poly level itself,
    matching ``run_erc``'s own ``cumulative_area_um2`` semantics) and
    registering/connecting it into the gate's own net cluster so
    ``antenna_check`` sees it as part of the same net -- a synthetic
    boolean-union region has to be threaded into the net this way, or
    ``antenna_check`` treats it as an unrelated, always-zero-overlap
    cluster (verified against a real klayout run; a disconnected union
    region silently reports zero violations regardless of area).
    """
    layout = kdb.Layout()
    layout.read(str(gds_path))
    top = layout.top_cell()

    layer_order = ["poly", "li1", "met1", "met2"]
    layer_numbers = {"poly": (1, 0), "li1": (3, 0), "met1": (5, 0), "met2": (7, 0)}
    via_numbers = [
        ("licon", (2, 0), "poly", "li1"),
        ("mcon", (4, 0), "li1", "met1"),
        ("via1", (6, 0), "met1", "met2"),
    ]

    l2n = kdb.LayoutToNetlist(top.name, layout.dbu)
    regions = {}
    for name in layer_order:
        idx = layout.find_layer(*layer_numbers[name])
        region = (
            kdb.Region(top.begin_shapes_rec(idx)) if idx is not None else kdb.Region()
        )
        regions[name] = region
        l2n.register(region, name)
        l2n.connect(region)

    for via_name, via_layer, role_a, role_b in via_numbers:
        idx = layout.find_layer(*via_layer)
        via_region = (
            kdb.Region(top.begin_shapes_rec(idx)) if idx is not None else kdb.Region()
        )
        l2n.register(via_region, via_name)
        l2n.connect(via_region)
        l2n.connect(regions[role_a], via_region)
        l2n.connect(via_region, regions[role_b])

    cumulative = kdb.Region()
    for name in layer_order:
        cumulative += regions[name]
        if name == through_layer:
            break
    l2n.register(cumulative, "cumulative")
    l2n.connect(cumulative)
    l2n.connect(regions["poly"], cumulative)

    l2n.extract_netlist()

    violations = l2n.antenna_check(regions["poly"], cumulative, limit)
    return not violations.is_empty()


@pytest.mark.parametrize(
    ("layer", "limit", "violate_kwargs", "pass_kwargs"),
    [
        ("li1", 75.0, {"li1_um2": 80.0}, {"li1_um2": 1.0}),
        (
            "met1",
            400.0,
            {"li1_um2": 0.1, "met1_um2": 450.0},
            {"li1_um2": 0.1, "met1_um2": 1.0},
        ),
        (
            "met2",
            400.0,
            {"li1_um2": 0.2, "met1_um2": 1.0, "met2_um2": 450.0},
            {"li1_um2": 0.2, "met1_um2": 1.0, "met2_um2": 1.0},
        ),
    ],
)
def test_antenna_verdict_agrees_with_klayout_builtin_antenna_check(
    tmp_path, layer, limit, violate_kwargs, pass_kwargs
):
    """AC #3: cross-checked against klayout's built-in antenna check.

    The epic's named corpus (Tiny Tapeout, issue #520) is not usable for
    this cross-check: #520 is itself an unimplemented, `loom:operator-only`
    epic (no ingestion harness, no locally cached GDS anywhere in this repo
    as of 2026-08-12 -- `gh issue view 520` shows Phase 1 "ingest and mass
    regression" has not been built). There is no corpus to run against.
    This test does the next best headless thing: it runs klayout's own,
    independently-implemented antenna engine
    (`klayout.db.LayoutToNetlist.antenna_check`) directly against the same
    golden fixtures used for AC #1 above, and asserts it agrees with
    `klt erc`'s own per-level verdict on every case.
    """
    spec = tmp_path / "basic.erc.json"
    _basic_spec(spec)

    for kwargs, want_violate in [(violate_kwargs, True), (pass_kwargs, False)]:
        gds = tmp_path / f"{layer}_{want_violate}.gds"
        _antenna_fixture(gds, **kwargs)

        report = run_erc(str(gds), str(spec), pdk="sky130")
        level = next(
            lvl for lvl in report["gates"][0]["levels"] if lvl["layer"] == layer
        )
        klt_erc_violates = level["verdict"] == "violate"
        assert klt_erc_violates == want_violate

        builtin_violates = _klayout_builtin_antenna_violates(
            gds, through_layer=layer, limit=limit
        )
        assert builtin_violates == want_violate == klt_erc_violates


# --- CLI ----------------------------------------------------------------


def test_cli_json_contract(tmp_path, capsys):
    gds = tmp_path / "basic.gds"
    spec = tmp_path / "basic.erc.json"
    _basic_fixture(gds)
    _basic_spec(spec)

    assert main(["erc", str(gds), str(spec), "--format", "json"]) == 0
    data = json.loads(capsys.readouterr().out)

    assert set(data.keys()) == {
        "schema_version",
        "file",
        "spec",
        "pdk",
        "gate_role",
        "gate_count",
        "gates",
    }
    assert data["schema_version"] == 1
    assert data["pdk"] is None
    assert data["gate_count"] == 2


def test_cli_text_output(tmp_path, capsys):
    gds = tmp_path / "basic.gds"
    spec = tmp_path / "basic.erc.json"
    _basic_fixture(gds)
    _basic_spec(spec)

    assert main(["erc", str(gds), str(spec)]) == 0
    out = capsys.readouterr().out
    assert "gates: 2" in out
    assert "GATE_A" in out
    assert "met2: step=" in out


def test_cli_pdk_json_contract(tmp_path, capsys):
    gds = tmp_path / "basic.gds"
    spec = tmp_path / "basic.erc.json"
    _basic_fixture(gds)
    _basic_spec(spec)

    assert (
        main(["erc", str(gds), str(spec), "--pdk", "sky130", "--format", "json"]) == 0
    )
    data = json.loads(capsys.readouterr().out)

    assert data["pdk"] == "sky130"
    gate_a = next(g for g in data["gates"] if g["net"] == "GATE_A")
    li1_level = next(lvl for lvl in gate_a["levels"] if lvl["layer"] == "li1")
    assert li1_level["antenna_ratio_max"] == pytest.approx(75.0)
    assert li1_level["verdict"] in ("pass", "violate")


def test_cli_pdk_text_output(tmp_path, capsys):
    gds = tmp_path / "basic.gds"
    spec = tmp_path / "basic.erc.json"
    _basic_fixture(gds)
    _basic_spec(spec)

    assert main(["erc", str(gds), str(spec), "--pdk", "sky130"]) == 0
    out = capsys.readouterr().out
    assert "pdk: sky130" in out
    assert "antenna_verdict=" in out
    assert "verdict=" in out


def test_cli_unknown_pdk_exits_one_with_clean_message(tmp_path, capsys):
    gds = tmp_path / "basic.gds"
    spec = tmp_path / "basic.erc.json"
    _basic_fixture(gds)
    _basic_spec(spec)

    assert main(["erc", str(gds), str(spec), "--pdk", "nope"]) == 1
    err = capsys.readouterr().err
    assert "unknown --pdk" in err
    assert "Traceback" not in err


def test_cli_error_exits_one_with_clean_message(tmp_path, capsys):
    gds = tmp_path / "basic.gds"
    _basic_fixture(gds)

    assert main(["erc", str(gds), str(tmp_path / "nope.json")]) == 1
    err = capsys.readouterr().err
    assert "spec file not found" in err
    assert "Traceback" not in err


def test_cli_json_error_shape(tmp_path, capsys):
    gds = tmp_path / "basic.gds"
    _basic_fixture(gds)

    assert main(["erc", str(gds), str(tmp_path / "nope.json"), "--format", "json"]) == 1
    err = json.loads(capsys.readouterr().err)
    assert err["schema_version"] == 1
    assert err["error"]["command"] == "erc"
    assert "spec file not found" in err["error"]["message"]
