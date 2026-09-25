"""Tests for `klt erc` and the `run_erc` library function: the layer-by-
layer connectivity model (issue #859, Phase 1a), the per-gate antenna-ratio
verdict (issue #860, Phase 1b), and the core ERC finding checks (issue
#861, Phase 1c) of the antenna + ERC signoff epic #713.

Fixtures are generated programmatically with `klayout.db` inside the tests,
mirroring `tests/test_power.py`'s convention (`klt power` is the closest
precedent: a sibling connectivity-only Phase 1a verb built on the same
`LayoutToNetlist` wire/via-connectivity API).
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import klayout.db as kdb
import pytest

from klayout_tools.cli import main
from klayout_tools.erc import ErcError, run_erc

DBU = 0.001

CORPUS_DIR = Path(__file__).parent / "corpus"


def _um(v: float) -> int:
    return int(round(v / DBU))


def _sha256_file(path) -> str:
    """Freshly computed sha256 hex digest of `path`, for cross-checking
    `provenance.input.content_hash` (issue #1968) against an independent
    computation rather than `klayout_tools._provenance.sha256_file` itself."""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


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

    # issue #1968: `status`/`provenance` are always present, with the
    # expected shape. `_basic_fixture`'s Gate B is unstrapped, so the
    # always-on `erc.floating_gate` check finds one violation here even with
    # no `nets`/`ties` declared -- `status` reflects that.
    assert report["status"] == "violations"
    assert report["erc_finding_count"] == 1
    expected_hash = f"sha256:{_sha256_file(gds)}"
    assert report["provenance"]["input"]["content_hash"] == expected_hash
    assert report["provenance"]["pdk"] is None
    assert report["provenance"]["deck"] is None
    assert "klt_version" in report["provenance"]
    assert "klayout_version" in report["provenance"]

    # issue #2036: `klt erc` is validated against two inputs, so the block
    # pins the spec's *contents* too -- not just the path echoed in the
    # top-level `spec` field -- in the same `sha256:`-prefixed form.
    assert report["provenance"]["spec"] == {
        "content_hash": f"sha256:{_sha256_file(spec)}"
    }


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


def _full_stack_spec(path) -> None:
    """Same gate/li1/met1/met2 stackup as `_basic_spec`, plus met3/met4/
    met5 -- roles sky130's own antenna-ratio limit table
    (`_SKY130_ANTENNA_RATIO_MAX_EGAR`) has no entries for at all (issue
    #1997's "met3-5-only" partial-coverage scenario; see docs/cli/erc.md's
    "Sky130 antenna-ratio limits" table). No vias connect met3-5 to the
    rest of the stack -- their own `step_area_um2` stays `0.0` regardless,
    which does not matter here: `verdict` is driven purely by whether the
    PDK table recognises the role `name` at all, not by geometry.
    """
    path.write_text(
        json.dumps(
            {
                "stackup": [
                    {"name": "poly", "layer": "1/0", "role": "gate"},
                    {"name": "li1", "layer": "3/0"},
                    {"name": "met1", "layer": "5/0"},
                    {"name": "met2", "layer": "7/0", "label_layer": "7/5"},
                    {"name": "met3", "layer": "20/0"},
                    {"name": "met4", "layer": "21/0"},
                    {"name": "met5", "layer": "22/0"},
                ],
                "vias": [
                    {"name": "licon", "layer": "2/0", "between": ["poly", "li1"]},
                    {"name": "mcon", "layer": "4/0", "between": ["li1", "met1"]},
                    {"name": "via1", "layer": "6/0", "between": ["met1", "met2"]},
                ],
            }
        )
    )


def test_antenna_verdict_pass_partial_when_met3_5_ungraded(tmp_path):
    """A full-stack spec declaring met3-5 (roles sky130's antenna-ratio
    table has no limit entries for at all) still gets a `levels[]` entry
    for each (`verdict: "unchecked"`) -- but the per-gate `antenna_verdict`
    rollup must not silently read as a plain `"pass"` just because every
    *graded* level (li1/met1/met2, the roles the table does cover) passed
    clean. `"pass_partial"` reports that distinction (issue #1997)."""
    gds = tmp_path / "full_stack_pass.gds"
    spec = tmp_path / "full_stack.erc.json"
    _full_stack_spec(spec)
    _antenna_fixture(gds, li1_um2=1.0, met1_um2=1.0, met2_um2=1.0)

    report = run_erc(str(gds), str(spec), pdk="sky130")
    gate = report["gates"][0]
    verdict_by_layer = {lvl["layer"]: lvl["verdict"] for lvl in gate["levels"]}

    assert verdict_by_layer["li1"] == "pass"
    assert verdict_by_layer["met1"] == "pass"
    assert verdict_by_layer["met2"] == "pass"
    assert verdict_by_layer["met3"] == "unchecked"
    assert verdict_by_layer["met4"] == "unchecked"
    assert verdict_by_layer["met5"] == "unchecked"
    assert gate["antenna_verdict"] == "pass_partial"


def test_antenna_verdict_violate_wins_over_partial_met3_5_coverage(tmp_path):
    """A real antenna-ratio violation on a graded level (li1) still reports
    `"violate"` even though met3-5 are simultaneously `"unchecked"` --
    coverage gaps never mask a genuine violation (issue #1997)."""
    gds = tmp_path / "full_stack_violate.gds"
    spec = tmp_path / "full_stack.erc.json"
    _full_stack_spec(spec)
    _antenna_fixture(gds, li1_um2=80.0, met1_um2=1.0, met2_um2=1.0)

    report = run_erc(str(gds), str(spec), pdk="sky130")
    gate = report["gates"][0]
    li1_level = next(lvl for lvl in gate["levels"] if lvl["layer"] == "li1")

    assert li1_level["verdict"] == "violate"
    assert gate["antenna_verdict"] == "violate"


def test_antenna_verdict_plain_pass_when_stackup_omits_ungraded_roles(tmp_path):
    """A genuinely fully-covered design -- a stackup that declares only
    roles the selected PDK's table actually has limits for (no met3-5 at
    all) -- must still report a plain `"pass"`, not `"pass_partial"`; the
    gate role itself (`stackup[0]`, always `"unchecked"` by construction)
    must never, on its own, trigger `"pass_partial"` (issue #1997)."""
    gds = tmp_path / "basic_full_coverage.gds"
    spec = tmp_path / "basic.erc.json"
    _basic_spec(spec)
    _antenna_fixture(gds, li1_um2=1.0, met1_um2=1.0, met2_um2=1.0)

    report = run_erc(str(gds), str(spec), pdk="sky130")
    gate = report["gates"][0]

    assert gate["levels"][0]["verdict"] == "unchecked"  # the gate role itself
    assert gate["antenna_verdict"] == "pass"


# --- run_erc: top-level `status` roll-up (issue #1968) -----------------------


def test_status_clean_when_no_findings_and_no_antenna_violation(tmp_path):
    """Both violation signals clean -> `status == "clean"`."""
    spec = tmp_path / "basic.erc.json"
    _basic_spec(spec)
    gds = tmp_path / "pass.gds"
    _antenna_fixture(gds, li1_um2=0.2, met1_um2=1.0, met2_um2=1.0)

    report = run_erc(str(gds), str(spec), pdk="sky130")
    assert report["erc_finding_count"] == 0
    assert not any(
        level["verdict"] == "violate"
        for gate in report["gates"]
        for level in gate["levels"]
    )
    assert report["status"] == "clean"


def test_status_reflects_antenna_violation_even_with_zero_erc_findings(tmp_path):
    """The key nuance issue #1968 calls out: `erc_finding_count == 0` (no
    `nets`/`ties` declared, so no `erc_findings` are computed at all) but a
    gate has an antenna `"violate"` level -- `status` must still read
    `"violations"`, not `"clean"`. A `status` derived only from
    `erc_finding_count` would miss this."""
    spec = tmp_path / "basic.erc.json"
    _basic_spec(spec)
    gds = tmp_path / "li1_violate.gds"
    _antenna_fixture(gds, li1_um2=80.0)

    report = run_erc(str(gds), str(spec), pdk="sky130")
    assert report["erc_finding_count"] == 0
    assert any(
        level["verdict"] == "violate"
        for gate in report["gates"]
        for level in gate["levels"]
    )
    assert report["status"] == "violations"


def test_status_reflects_erc_findings_even_with_no_antenna_check(tmp_path):
    """The inverse case: no `--pdk` (every antenna verdict is `"unchecked"`,
    never `"violate"`) but a real `erc_findings` violation (an unstrapped/
    floating gate) -- `status` must still read `"violations"`."""
    gds = tmp_path / "basic.gds"
    spec = tmp_path / "basic.erc.json"
    _basic_fixture(gds)
    _basic_spec(spec)

    report = run_erc(str(gds), str(spec))
    assert report["erc_finding_count"] > 0
    assert not any(
        level["verdict"] == "violate"
        for gate in report["gates"]
        for level in gate["levels"]
    )
    assert report["status"] == "violations"


def test_status_not_checked_when_no_pdk_is_given(tmp_path):
    """AC: the zero-check control (issue #2115/#2109). With no `--pdk`,
    every level's `verdict` is `"unchecked"` and the antenna scope's
    `coverage.nothing_checked` is known-`True` -- the common rollup rule's
    `zero` row, reported as `status: "not_checked"` (exit 4), matching this
    command's pre-#2115 behaviour for the same input. Uses a fully-strapped
    single-gate fixture so no `erc.floating_gate` finding intervenes and the
    zero-coverage row is what actually decides `status` here."""
    gds = tmp_path / "unchecked.gds"
    spec = tmp_path / "basic.erc.json"
    _basic_spec(spec)
    _antenna_fixture(gds, li1_um2=0.2, met1_um2=1.0, met2_um2=1.0)

    report = run_erc(str(gds), str(spec))
    assert report["erc_finding_count"] == 0
    assert report["coverage"]["nothing_checked"] is True
    assert report["status"] == "not_checked"


def test_status_clean_partial_when_met3_5_ungraded_but_all_graded_levels_pass(
    tmp_path,
):
    """AC: the partial-coverage control, migrating #1997's `pass_partial`
    scenario onto the common contract (issue #2109/#2115). A full-stack
    spec declaring met3-5 (roles sky130's antenna-ratio table has no limit
    entries for) alongside a clean li1/met1/met2 result must not report the
    unconditional `"clean"` -- `coverage.skipped` is nonempty (met3-5, each
    `missing_antenna_limit`), so the common rollup rule's `partial` row
    applies: `status: "clean_partial"`, still exit 0 (a real, successful
    run, just not this verb's unconditional success)."""
    gds = tmp_path / "full_stack_pass.gds"
    spec = tmp_path / "full_stack.erc.json"
    _full_stack_spec(spec)
    _antenna_fixture(gds, li1_um2=1.0, met1_um2=1.0, met2_um2=1.0)

    report = run_erc(str(gds), str(spec), pdk="sky130")
    assert report["erc_finding_count"] == 0
    assert not any(
        level["verdict"] == "violate"
        for gate in report["gates"]
        for level in gate["levels"]
    )
    skipped_ids = {entry["id"] for entry in report["coverage"]["skipped"]}
    assert len(skipped_ids) == 3  # met3, met4, met5
    assert all(
        entry["reason"] == "missing_antenna_limit"
        for entry in report["coverage"]["skipped"]
    )
    assert report["coverage"]["nothing_checked"] is False
    assert report["status"] == "clean_partial"


def test_status_violations_wins_over_partial_met3_5_coverage(tmp_path):
    """AC: the real-violation control (issue #2109/#2115) -- a genuine
    antenna-ratio violation on a graded level (li1) still reports
    `status: "violations"` even though met3-5 are simultaneously skipped
    (`coverage.skipped` nonempty): the common rollup rule decides `failed`
    before it ever consults coverage, so a coverage gap can never mask or
    be masked by a real defect. Companion to
    `test_antenna_verdict_violate_wins_over_partial_met3_5_coverage`, which
    checks the same fixture's per-gate `antenna_verdict`."""
    gds = tmp_path / "full_stack_violate.gds"
    spec = tmp_path / "full_stack.erc.json"
    _full_stack_spec(spec)
    _antenna_fixture(gds, li1_um2=80.0, met1_um2=1.0, met2_um2=1.0)

    report = run_erc(str(gds), str(spec), pdk="sky130")
    assert report["coverage"]["skipped"]  # met3-5 still ungraded
    assert report["status"] == "violations"


# --- run_erc: connectivity roll-up `erc_status` (issue #2179) ---------------
#
# `klt erc` answers two independent questions in one envelope. The antenna
# one needs a PDK limit table and `_ANTENNA_LIMITS_BY_PDK` has sky130 only,
# so on every other PDK (and every `--pdk`-less run) it is permanently
# ungradable -- `status: "not_checked"`, exit 4, for every layout. The
# connectivity one needs no PDK at all. These tests pin the second verdict's
# own field, and pin that adding it changed nothing about the first.


def _strapped_supply_layout():
    """A layout whose single gate is genuinely strapped up to a labelled
    ``li1`` rail -- ``_nets_fixture_layout``'s shape plus the ``licon`` via
    that actually connects the two roles, so no `erc.floating_gate` finding
    intervenes and the connectivity verdict under test is decided by the
    declared ``nets``/``ties`` alone. Returns the layer indices the tests
    below add their own geometry on.
    """
    layout = kdb.Layout()
    layout.dbu = DBU
    top = layout.create_cell("TOP")
    poly = layout.layer(1, 0)
    licon = layout.layer(2, 0)
    li1 = layout.layer(3, 0)
    label = layout.layer(3, 5)
    nwell = layout.layer(10, 0)
    tap = layout.layer(11, 0)
    top.shapes(poly).insert(kdb.Box.new(_um(0), _um(0), _um(1), _um(1)))
    top.shapes(licon).insert(kdb.Box.new(_um(0.2), _um(0.2), _um(0.4), _um(0.4)))
    top.shapes(li1).insert(kdb.Box.new(_um(0), _um(0), _um(5), _um(1)))
    top.shapes(label).insert(kdb.Text("VDD", kdb.Trans(_um(2.5), _um(0.5))))
    return layout, top, li1, label, nwell, tap


def _strapped_supply_spec(nets=None, ties=None):
    spec = _nets_spec(nets=nets, ties=ties)
    spec["vias"] = [{"name": "licon", "layer": "2/0", "between": ["poly", "li1"]}]
    return spec


def test_erc_status_clean_while_antenna_status_is_not_checked(tmp_path):
    """AC (issue #2179), the headline case: no `--pdk`, a declared net that
    resolves to exactly one island, no findings. The antenna half correctly
    reports `not_checked` (nothing was graded, and nothing ever could be) --
    and the connectivity half, which ran completely, reports `"clean"`
    without the caller re-deriving it from `erc_finding_count`."""
    layout, top, li1, label, nwell, tap = _strapped_supply_layout()

    gds = tmp_path / "conn_clean.gds"
    layout.write(str(gds))
    spec = tmp_path / "conn_clean.erc.json"
    _write_spec(spec, _strapped_supply_spec(nets=[{"name": "VDD", "kind": "supply"}]))

    report = run_erc(str(gds), str(spec))

    assert report["erc_finding_count"] == 0
    assert report["status"] == "not_checked"  # the antenna answer, unchanged
    assert report["coverage"]["nothing_checked"] is True
    assert report["erc_status"] == "clean"
    assert report["erc_coverage"]["scope"] == "connectivity"
    assert report["erc_coverage"]["nothing_checked"] is False


def test_erc_status_violations_without_any_pdk(tmp_path):
    """AC (issue #2179), the violating counterpart: the same table-less run
    with a genuine `erc.unconnected_net` reads `"violations"` on the new
    field, so "clean" and "not clean" are distinguishable there even though
    `status` cannot tell them apart from each other's antenna half."""
    layout, top, li1, label, nwell, tap = _strapped_supply_layout()
    # A second, disjoint island carrying the same declared supply name.
    top.shapes(li1).insert(kdb.Box.new(_um(8), _um(0), _um(9), _um(1)))
    top.shapes(label).insert(kdb.Text("VDD", kdb.Trans(_um(8.5), _um(0.5))))

    gds = tmp_path / "conn_violate.gds"
    layout.write(str(gds))
    spec = tmp_path / "conn_violate.erc.json"
    _write_spec(spec, _strapped_supply_spec(nets=[{"name": "VDD", "kind": "supply"}]))

    report = run_erc(str(gds), str(spec))

    assert [f["rule"] for f in report["erc_findings"]] == ["erc.unconnected_net"]
    assert report["erc_status"] == "violations"
    assert report["status"] == "violations"


def test_erc_status_ignores_an_antenna_violation(tmp_path):
    """The separation runs both ways: an antenna violation on a net with
    perfectly good connectivity turns `status` red but leaves `erc_status`
    `"clean"` -- the same split `docs/design-evidence-tiers.md` item 11
    already relies on ("those are the rules this item grades, not the
    report's overall `status`")."""
    spec = tmp_path / "basic.erc.json"
    _basic_spec(spec)
    gds = tmp_path / "li1_violate.gds"
    _antenna_fixture(gds, li1_um2=80.0)

    report = run_erc(str(gds), str(spec), pdk="sky130")

    assert report["erc_finding_count"] == 0
    assert report["status"] == "violations"
    assert report["erc_status"] == "clean"


def test_erc_status_reports_a_connectivity_finding_on_a_graded_pdk(tmp_path):
    """And a connectivity finding is still a connectivity finding when the
    antenna half *was* graded -- `erc_status` is not a "no PDK" fallback,
    it is the connectivity verdict in every run."""
    gds = tmp_path / "basic.gds"
    spec = tmp_path / "basic.erc.json"
    _basic_fixture(gds)
    _basic_spec(spec)

    report = run_erc(str(gds), str(spec), pdk="sky130")

    assert any(f["rule"] == "erc.floating_gate" for f in report["erc_findings"])
    assert report["erc_status"] == "violations"


def test_erc_coverage_names_the_gates_nets_and_ties_actually_checked(tmp_path):
    """The connectivity scope grades real work: one checked identity per
    discovered gate, per declared net, and per declared tie.

    The tie declares `tap_is_dedicated` (issue #2199) because this
    fixture's `11/0` really is a tap-only layer -- without that affirmation
    (or a `tap_requires` narrowing) the tie would be graded as skipped
    degenerate work, which the two tests below cover."""
    layout, top, li1, label, nwell, tap = _strapped_supply_layout()
    # A well with a tap inside it, sitting under the labelled li1 rail, so
    # the tap really does reach the declared "VDD" net.
    top.shapes(nwell).insert(kdb.Box.new(_um(3), _um(0), _um(4), _um(1)))
    top.shapes(tap).insert(kdb.Box.new(_um(3.2), _um(0.2), _um(3.8), _um(0.8)))

    gds = tmp_path / "coverage.gds"
    layout.write(str(gds))
    spec = tmp_path / "coverage.erc.json"
    _write_spec(
        spec,
        _strapped_supply_spec(
            nets=[{"name": "VDD", "kind": "supply"}],
            ties=[
                {
                    "name": "nwell_tie",
                    "well_layer": "10/0",
                    "tap_layer": "11/0",
                    "tap_is_dedicated": True,
                    "connect_to": "li1",
                    "net": "VDD",
                }
            ],
        ),
    )

    report = run_erc(str(gds), str(spec))
    checked = set(report["erc_coverage"]["checked"])

    assert 'erc.floating_gate:["gate0"]' in checked
    assert 'erc.net_connectivity:["VDD"]' in checked
    assert 'erc.missing_tie:["nwell_tie"]' in checked
    assert report["erc_coverage"]["inapplicable"] == []
    assert report["erc_coverage"]["skipped"] == []
    assert report["erc_status"] == "clean"


def test_erc_coverage_records_undeclared_rules_as_inapplicable(tmp_path):
    """A spec that declares no `nets`/`ties` never asked for those rules, so
    they are *inapplicable*, not skipped -- an undeclared rule must not make
    the connectivity scope partial, and a reader must be able to tell "no
    supply was declared, so `erc.supply_short` was never computed" from "the
    declared supplies came back clean" off the envelope alone."""
    gds = tmp_path / "basic.gds"
    spec = tmp_path / "basic.erc.json"
    _basic_fixture(gds)
    _basic_spec(spec)

    report = run_erc(str(gds), str(spec))
    reasons = {r["reason"] for r in report["erc_coverage"]["inapplicable"]}

    assert reasons == {"no_nets_declared", "no_ties_declared"}
    assert report["erc_coverage"]["skipped"] == []


def test_erc_status_does_not_change_the_antenna_status_on_sky130(tmp_path):
    """AC2 (issue #2179): the antenna `status` and its three reachable
    tokens are untouched on a PDK that does have a table."""
    basic = tmp_path / "basic.erc.json"
    _basic_spec(basic)
    full = tmp_path / "full_stack.erc.json"
    _full_stack_spec(full)

    clean_gds = tmp_path / "clean.gds"
    _antenna_fixture(clean_gds, li1_um2=0.2, met1_um2=1.0, met2_um2=1.0)
    partial_gds = tmp_path / "partial.gds"
    _antenna_fixture(partial_gds, li1_um2=1.0, met1_um2=1.0, met2_um2=1.0)
    violate_gds = tmp_path / "violate.gds"
    _antenna_fixture(violate_gds, li1_um2=80.0)

    assert run_erc(str(clean_gds), str(basic), pdk="sky130")["status"] == "clean"
    assert (
        run_erc(str(partial_gds), str(full), pdk="sky130")["status"] == "clean_partial"
    )
    assert run_erc(str(violate_gds), str(basic), pdk="sky130")["status"] == "violations"
    assert run_erc(str(clean_gds), str(basic))["status"] == "not_checked"


def test_provenance_pdk_populated_only_when_pdk_given(tmp_path):
    spec = tmp_path / "basic.erc.json"
    _basic_spec(spec)
    gds = tmp_path / "basic.gds"
    _basic_fixture(gds)

    no_pdk_report = run_erc(str(gds), str(spec))
    assert no_pdk_report["provenance"]["pdk"] is None

    pdk_report = run_erc(str(gds), str(spec), pdk="sky130")
    assert pdk_report["provenance"]["pdk"] == {
        "name": "sky130",
        "source": "built-in",
        "version": None,
    }


def test_provenance_spec_hash_tracks_spec_contents(tmp_path):
    """Issue #2036: editing the spec file changes
    `provenance.spec.content_hash` even though the layout (and therefore
    `provenance.input.content_hash`) is byte-identical. This is the whole
    point of the field: an ERC verdict is relative to the declarations it
    was run with, so a committed report must be re-verifiable against the
    spec as well as the layout."""
    gds = tmp_path / "basic.gds"
    spec = tmp_path / "basic.erc.json"
    _basic_fixture(gds)
    _basic_spec(spec)

    before = run_erc(str(gds), str(spec))
    assert before["provenance"]["spec"]["content_hash"] == (
        f"sha256:{_sha256_file(spec)}"
    )

    # A real declaration edit (the `met2` level is dropped from the
    # stackup), not a cosmetic one -- the layout is untouched.
    _write_spec(
        spec,
        {
            "stackup": [
                {"name": "poly", "layer": "1/0", "role": "gate"},
                {"name": "li1", "layer": "3/0"},
                {"name": "met1", "layer": "5/0"},
            ],
            "vias": [
                {"name": "licon", "layer": "2/0", "between": ["poly", "li1"]},
                {"name": "mcon", "layer": "4/0", "between": ["li1", "met1"]},
            ],
        },
    )
    after = run_erc(str(gds), str(spec))

    assert after["provenance"]["spec"]["content_hash"] == (
        f"sha256:{_sha256_file(spec)}"
    )
    assert (
        after["provenance"]["spec"]["content_hash"]
        != before["provenance"]["spec"]["content_hash"]
    )
    # The layout side is unchanged -- only the spec drifted.
    assert (
        after["provenance"]["input"]["content_hash"]
        == before["provenance"]["input"]["content_hash"]
    )


# --- run_erc: antenna-violation fix guidance (issue #908, epic #713 Phase 3)


def test_antenna_remedy_is_null_when_no_violation(tmp_path):
    spec = tmp_path / "basic.erc.json"
    _basic_spec(spec)
    gds = tmp_path / "pass.gds"
    _antenna_fixture(gds, li1_um2=0.2, met1_um2=1.0, met2_um2=1.0)

    report = run_erc(str(gds), str(spec), pdk="sky130")
    for level in report["gates"][0]["levels"]:
        assert level["verdict"] != "violate"
        assert level["remedy"] is None


def test_antenna_remedy_is_null_for_every_level_without_pdk(tmp_path):
    spec = tmp_path / "basic.erc.json"
    _basic_spec(spec)
    gds = tmp_path / "no_pdk.gds"
    # Would violate li1's limit if checked, but no --pdk means every
    # verdict is "unchecked", not "violate" -- so no remedy either.
    _antenna_fixture(gds, li1_um2=80.0)

    report = run_erc(str(gds), str(spec))
    for level in report["gates"][0]["levels"]:
        assert level["verdict"] == "unchecked"
        assert level["remedy"] is None


def test_antenna_remedy_layer_jumping_targets_higher_layer_with_margin(tmp_path):
    """li1 violates (ratio 80 > 75) but met1's own limit (400) is loose
    enough that the same carried-over cumulative area (80) still passes
    there -- a layer-local violation with margin one level up, so the
    standard remedy is to jump the routing forward onto met1 rather than
    diode insertion.
    """
    spec = tmp_path / "basic.erc.json"
    _basic_spec(spec)
    gds = tmp_path / "li1_violate.gds"
    _antenna_fixture(gds, li1_um2=80.0)

    report = run_erc(str(gds), str(spec), pdk="sky130")
    levels = {lvl["layer"]: lvl for lvl in report["gates"][0]["levels"]}
    assert levels["li1"]["verdict"] == "violate"
    assert levels["met1"]["verdict"] == "pass"

    remedy = levels["li1"]["remedy"]
    assert remedy is not None
    assert remedy["type"] == "layer_jumping"
    assert remedy["net"] == report["gates"][0]["net"]
    assert remedy["layer"] == "li1"
    assert remedy["target_layer"] == "met1"
    assert "li1" in remedy["justification"]
    assert "met1" in remedy["justification"]

    # The non-violating levels this violation carries into (met1, met2)
    # still get no remedy of their own -- they never violate.
    assert levels["met1"]["remedy"] is None
    assert levels["met2"]["remedy"] is None


def test_antenna_remedy_layer_jumping_targets_lower_layer_when_last_level(tmp_path):
    """met2 is the last stackup level (no level above it to jump forward
    to), but met1 -- immediately below -- has ample margin (ratio 1.2 vs.
    its own 400 limit), so the remedy targets it instead.
    """
    spec = tmp_path / "basic.erc.json"
    _basic_spec(spec)
    gds = tmp_path / "met2_violate.gds"
    _antenna_fixture(gds, li1_um2=0.2, met1_um2=1.0, met2_um2=450.0)

    report = run_erc(str(gds), str(spec), pdk="sky130")
    levels = {lvl["layer"]: lvl for lvl in report["gates"][0]["levels"]}
    assert levels["met2"]["verdict"] == "violate"
    assert levels["met1"]["verdict"] == "pass"

    remedy = levels["met2"]["remedy"]
    assert remedy is not None
    assert remedy["type"] == "layer_jumping"
    assert remedy["layer"] == "met2"
    assert remedy["target_layer"] == "met1"


def test_antenna_remedy_diode_insertion_when_violation_cascades(tmp_path):
    """li1 already violates (ratio 80 > 75); met1 and met2 both inherit
    and compound that excess (cumulative area only grows) and violate
    their own looser 400 limits too -- no adjacent level anywhere in the
    stack has margin, so every violating level falls back to diode
    insertion rather than a jump target that would not actually help.
    """
    spec = tmp_path / "basic.erc.json"
    _basic_spec(spec)
    gds = tmp_path / "cascading_violate.gds"
    _antenna_fixture(gds, li1_um2=80.0, met1_um2=350.0)

    report = run_erc(str(gds), str(spec), pdk="sky130")
    levels = {lvl["layer"]: lvl for lvl in report["gates"][0]["levels"]}
    assert levels["li1"]["verdict"] == "violate"
    assert levels["met1"]["verdict"] == "violate"
    assert levels["met2"]["verdict"] == "violate"

    for layer in ("li1", "met1", "met2"):
        remedy = levels[layer]["remedy"]
        assert remedy is not None, layer
        assert remedy["type"] == "diode_insertion", layer
        assert remedy["layer"] == layer
        assert remedy["target_layer"] is None
        assert layer in remedy["justification"]
        # `_antenna_fixture` never labels its net -- `remedy["net"]` is
        # `None`, and the justification prose should read naturally rather
        # than interpolating the bare Python `None` repr.
        assert remedy["net"] is None
        assert "unlabelled net" in remedy["justification"]
        assert "None" not in remedy["justification"]


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


# --- erc_findings: floating gate (issue #861) --------------------------------
#
# Golden pairs mirror the reality-grounding discipline used for the antenna
# check / deck compiler: every rule below ships one deliberately-violating
# fixture that must be flagged, and one clean counterpart that must not be.


def _nets_fixture_layout():
    """A layout with just a gate poly (required by `stackup[0]`) plus a
    labelled ``li1`` role, used by the ``nets``/``ties``-driven finding
    tests below. Returns ``(layout, poly, li1, label)`` layer indices."""
    layout = kdb.Layout()
    layout.dbu = DBU
    top = layout.create_cell("TOP")
    poly = layout.layer(1, 0)
    li1 = layout.layer(3, 0)
    label = layout.layer(3, 5)
    # A trivially strapped gate so `run_erc` always has at least one gate
    # net -- these tests are about `nets`/`ties` findings, not the gate
    # model itself.
    top.shapes(poly).insert(kdb.Box.new(_um(0), _um(0), _um(1), _um(1)))
    top.shapes(li1).insert(kdb.Box.new(_um(0), _um(0), _um(1), _um(1)))
    return layout, top, poly, li1, label


def _nets_spec(nets=None, ties=None):
    return {
        "stackup": [
            {"name": "poly", "layer": "1/0", "role": "gate"},
            {"name": "li1", "layer": "3/0", "label_layer": "3/5"},
        ],
        "nets": nets or [],
        "ties": ties or [],
    }


def test_floating_gate_flags_unstrapped_gate(tmp_path):
    gds = tmp_path / "basic.gds"
    spec = tmp_path / "basic.erc.json"
    _basic_fixture(gds)
    _basic_spec(spec)

    report = run_erc(str(gds), str(spec))
    floating = [f for f in report["erc_findings"] if f["rule"] == "erc.floating_gate"]

    assert len(floating) == 1
    assert floating[0]["net"] is None  # Gate B (the unstrapped one) has no label
    assert floating[0]["gate_id"] == "gate1"
    assert floating[0]["layer"] == "poly"
    assert floating[0]["bbox"] is not None


def test_floating_gate_does_not_flag_fully_strapped_gate(tmp_path):
    gds = tmp_path / "basic.gds"
    spec = tmp_path / "basic.erc.json"
    _basic_fixture(gds)
    _basic_spec(spec)

    report = run_erc(str(gds), str(spec))
    floating_nets = {
        f["net"] for f in report["erc_findings"] if f["rule"] == "erc.floating_gate"
    }

    assert "GATE_A" not in floating_nets


# --- erc_findings: unconnected / multiply-driven net (issue #861) ------------


def test_unconnected_net_flags_split_island(tmp_path):
    layout, top, poly, li1, label = _nets_fixture_layout()
    # Two disjoint li1 shapes both labelled "VDD" -- the same declared net
    # split into two electrical islands that never touch.
    top.shapes(li1).insert(kdb.Box.new(_um(5), _um(0), _um(6), _um(1)))
    top.shapes(label).insert(kdb.Text("VDD", kdb.Trans(_um(5.5), _um(0.5))))
    top.shapes(li1).insert(kdb.Box.new(_um(8), _um(0), _um(9), _um(1)))
    top.shapes(label).insert(kdb.Text("VDD", kdb.Trans(_um(8.5), _um(0.5))))

    gds = tmp_path / "split.gds"
    layout.write(str(gds))
    spec = tmp_path / "split.erc.json"
    _write_spec(spec, _nets_spec(nets=[{"name": "VDD", "kind": "supply"}]))

    report = run_erc(str(gds), str(spec))
    findings = [f for f in report["erc_findings"] if f["rule"] == "erc.unconnected_net"]

    assert len(findings) == 1
    assert findings[0]["net"] == "VDD"
    assert "2 disconnected electrical islands" in findings[0]["description"]

    # Issue #2194: the finding says *where* each island is, not just how
    # many there are. Both li1 bars, in ascending-x order (the islands are
    # emitted in `cluster_id` order, which follows the insertion order of
    # the geometry above).
    islands = findings[0]["islands"]
    assert [island["bbox"] for island in islands] == [
        {"left": _um(5), "bottom": _um(0), "right": _um(6), "top": _um(1)},
        {"left": _um(8), "bottom": _um(0), "right": _um(9), "top": _um(1)},
    ]
    assert [island["layer"] for island in islands] == ["li1", "li1"]
    assert [island["shape_count"] for island in islands] == [1, 1]
    # The per-net `bbox` spans every island, so a caller who only reads the
    # top-level box still lands on the right part of the layout.
    assert findings[0]["bbox"] == {
        "left": _um(5),
        "bottom": _um(0),
        "right": _um(9),
        "top": _um(1),
    }


def test_unconnected_net_islands_locate_each_of_three_islands(tmp_path):
    """Issue #2194: one `islands[]` entry per island for N > 2 as well --
    the motivating real case was a supply net resolving to three islands,
    where "which island did my fix resolve?" is unanswerable from a count.
    Each island here sits on a different role (li1-only, poly+li1, li1-only)
    so `layer`/`shape_count` are exercised too, not just `bbox`."""
    layout, top, poly, li1, label = _nets_fixture_layout()
    licon = layout.layer(2, 0)
    # Island 1: a lone li1 bar.
    top.shapes(li1).insert(kdb.Box.new(_um(5), _um(0), _um(6), _um(1)))
    top.shapes(label).insert(kdb.Text("VDD", kdb.Trans(_um(5.5), _um(0.5))))
    # Island 2: an li1 bar strapped down to a poly bar through a licon
    # via -- two roles on one island, and a bbox wider than either shape.
    top.shapes(li1).insert(kdb.Box.new(_um(8), _um(0), _um(9), _um(1)))
    top.shapes(poly).insert(kdb.Box.new(_um(8.5), _um(0), _um(11), _um(1)))
    top.shapes(licon).insert(kdb.Box.new(_um(8.6), _um(0.2), _um(8.8), _um(0.4)))
    top.shapes(label).insert(kdb.Text("VDD", kdb.Trans(_um(8.2), _um(0.5))))
    # Island 3: another lone li1 bar, far away.
    top.shapes(li1).insert(kdb.Box.new(_um(20), _um(0), _um(21), _um(1)))
    top.shapes(label).insert(kdb.Text("VDD", kdb.Trans(_um(20.5), _um(0.5))))

    gds = tmp_path / "three_islands.gds"
    layout.write(str(gds))
    spec = tmp_path / "three_islands.erc.json"
    spec_dict = _nets_spec(nets=[{"name": "VDD", "kind": "supply"}])
    spec_dict["vias"] = [{"name": "licon", "layer": "2/0", "between": ["poly", "li1"]}]
    _write_spec(spec, spec_dict)

    report = run_erc(str(gds), str(spec))
    findings = [f for f in report["erc_findings"] if f["rule"] == "erc.unconnected_net"]

    assert len(findings) == 1
    assert "3 disconnected electrical islands" in findings[0]["description"]

    islands = findings[0]["islands"]
    assert len(islands) == 3
    # Every island is locatable, and no two of them point at the same place
    # (a count-shaped finding repeated N times would not be actionable).
    assert all(island["bbox"] is not None for island in islands)
    boxes = [tuple(sorted(island["bbox"].items())) for island in islands]
    assert len(set(boxes)) == 3

    by_left = sorted(islands, key=lambda island: island["bbox"]["left"])
    assert by_left[0]["bbox"] == {
        "left": _um(5),
        "bottom": _um(0),
        "right": _um(6),
        "top": _um(1),
    }
    # The poly+li1 island's bbox covers both roles; `layer` names the role
    # carrying the most of its area (poly: 2 um^2 vs. li1: 1 um^2).
    assert by_left[1]["bbox"] == {
        "left": _um(8),
        "bottom": _um(0),
        "right": _um(11),
        "top": _um(1),
    }
    assert by_left[1]["layer"] == "poly"
    assert by_left[1]["shape_count"] == 2
    assert by_left[2]["bbox"] == {
        "left": _um(20),
        "bottom": _um(0),
        "right": _um(21),
        "top": _um(1),
    }


def _three_island_vdd_layout(tmp_path, name):
    """The issue #2194 three-island layout (one lone li1 bar, one
    poly+licon+li1 island, one far li1 bar, all labelled ``VDD``), written
    to ``<name>.gds`` and returned alongside a matching spec path. Shared
    by the declared-islands tests (issue #2400)."""
    layout, top, poly, li1, label = _nets_fixture_layout()
    licon = layout.layer(2, 0)
    top.shapes(li1).insert(kdb.Box.new(_um(5), _um(0), _um(6), _um(1)))
    top.shapes(label).insert(kdb.Text("VDD", kdb.Trans(_um(5.5), _um(0.5))))
    top.shapes(li1).insert(kdb.Box.new(_um(8), _um(0), _um(9), _um(1)))
    top.shapes(poly).insert(kdb.Box.new(_um(8.5), _um(0), _um(11), _um(1)))
    top.shapes(licon).insert(kdb.Box.new(_um(8.6), _um(0.2), _um(8.8), _um(0.4)))
    top.shapes(label).insert(kdb.Text("VDD", kdb.Trans(_um(8.2), _um(0.5))))
    top.shapes(li1).insert(kdb.Box.new(_um(20), _um(0), _um(21), _um(1)))
    top.shapes(label).insert(kdb.Text("VDD", kdb.Trans(_um(20.5), _um(0.5))))
    gds = tmp_path / f"{name}.gds"
    layout.write(str(gds))
    spec = tmp_path / f"{name}.erc.json"
    spec_dict = _nets_spec(nets=[{"name": "VDD", "kind": "supply"}])
    spec_dict["vias"] = [{"name": "licon", "layer": "2/0", "between": ["poly", "li1"]}]
    return gds, spec, spec_dict


def test_declared_islands_count_grades_multi_domain_net_clean(tmp_path):
    """Issue #2400: a `nets[]` entry may declare the number of electrical
    islands its name legitimately resolves to. The motivating shape: one
    library PG pin name (``VPWR``-style) instantiated on two or more
    deliberately separate supply domains -- three islands here -- which
    pre-#2400 was indistinguishable from a fragmented net and always
    reported `erc.unconnected_net`."""
    gds, spec, spec_dict = _three_island_vdd_layout(tmp_path, "declared3")

    spec_dict["nets"] = [{"name": "VDD", "kind": "supply", "islands": 3}]
    _write_spec(spec, spec_dict)
    report = run_erc(str(gds), str(spec))
    assert not any(f["rule"] == "erc.unconnected_net" for f in report["erc_findings"])

    # Regression guard against silently loosening the default: the same
    # layout, undeclared, still reports the multi-island finding.
    spec_dict["nets"] = [{"name": "VDD", "kind": "supply"}]
    _write_spec(spec, spec_dict)
    report = run_erc(str(gds), str(spec))
    findings = [f for f in report["erc_findings"] if f["rule"] == "erc.unconnected_net"]
    assert len(findings) == 1
    assert "3 disconnected electrical islands" in findings[0]["description"]


def test_declared_islands_mismatch_is_still_a_finding(tmp_path):
    """Issue #2400 falsifiability: the declaration grades against the
    actual island count, it does not silence the check. Declared 2 with 3
    actual islands, and declared 2 with 1 actual island, both still report
    `erc.unconnected_net` -- so a domain that later fragments (3 from a
    declared 2) or merges (1 from a declared 2) fails rather than passes."""
    gds, spec, spec_dict = _three_island_vdd_layout(tmp_path, "mismatch")

    spec_dict["nets"] = [{"name": "VDD", "kind": "supply", "islands": 2}]
    _write_spec(spec, spec_dict)
    report = run_erc(str(gds), str(spec))
    findings = [f for f in report["erc_findings"] if f["rule"] == "erc.unconnected_net"]
    assert len(findings) == 1
    assert "3 disconnected electrical islands" in findings[0]["description"]
    assert "expected exactly 2" in findings[0]["description"]

    # Declared 2, actual 1: a single island on a layout whose spec asserts
    # two domains is itself a mismatch (a domain has gone missing).
    layout, top, poly, li1, label = _nets_fixture_layout()
    top.shapes(li1).insert(kdb.Box.new(_um(5), _um(0), _um(6), _um(1)))
    top.shapes(label).insert(kdb.Text("VDD", kdb.Trans(_um(5.5), _um(0.5))))
    gds1 = tmp_path / "one_island.gds"
    layout.write(str(gds1))
    spec1 = tmp_path / "one_island.erc.json"
    _write_spec(
        spec1,
        _nets_spec(nets=[{"name": "VDD", "kind": "supply", "islands": 2}]),
    )
    report = run_erc(str(gds1), str(spec1))
    findings = [f for f in report["erc_findings"] if f["rule"] == "erc.unconnected_net"]
    assert len(findings) == 1
    assert "1 disconnected electrical island" in findings[0]["description"]
    assert "expected exactly 2" in findings[0]["description"]

    # Declared 1 with an actual 1 (explicit form of the default) is clean,
    # and declared 1 against the 3-island layout keeps today's wording.
    spec_dict["nets"] = [{"name": "VDD", "kind": "supply", "islands": 1}]
    _write_spec(spec, spec_dict)
    report = run_erc(str(gds), str(spec))
    findings = [f for f in report["erc_findings"] if f["rule"] == "erc.unconnected_net"]
    assert len(findings) == 1
    assert "(expected exactly one)" in findings[0]["description"]


def test_declared_islands_do_not_reach_the_short_detection(tmp_path):
    """Issue #2400 edge case: `islands` grades only the per-name island
    count; the cross-name short detection (`cluster_to_names`) must be
    unchanged. A two-domain ``VDD`` (declared ``islands: 2``, clean on the
    count) sharing one island with ``VSS`` still reports
    `erc.supply_short` exactly as an undeclared single-island short does."""
    layout, top, poly, li1, label = _nets_fixture_layout()
    # Domain 1: VDD and VSS labels on the same bar -- a short.
    top.shapes(li1).insert(kdb.Box.new(_um(5), _um(0), _um(7), _um(1)))
    top.shapes(label).insert(kdb.Text("VDD", kdb.Trans(_um(5.5), _um(0.5))))
    top.shapes(label).insert(kdb.Text("VSS", kdb.Trans(_um(6.5), _um(0.5))))
    # Domain 2: a far VDD-only island.
    top.shapes(li1).insert(kdb.Box.new(_um(20), _um(0), _um(21), _um(1)))
    top.shapes(label).insert(kdb.Text("VDD", kdb.Trans(_um(20.5), _um(0.5))))

    gds = tmp_path / "two_domain_short.gds"
    layout.write(str(gds))
    spec = tmp_path / "two_domain_short.erc.json"
    _write_spec(
        spec,
        _nets_spec(
            nets=[
                {"name": "VDD", "kind": "supply", "islands": 2},
                {"name": "VSS", "kind": "supply"},
            ]
        ),
    )

    report = run_erc(str(gds), str(spec))
    findings = report["erc_findings"]
    shorts = [f for f in findings if f["rule"] == "erc.supply_short"]
    assert len(shorts) == 1
    assert {shorts[0]["net"], shorts[0]["other_net"]} == {"VDD", "VSS"}
    assert not any(f["rule"] == "erc.unconnected_net" for f in findings)


@pytest.mark.parametrize(
    "bad",
    [0, -1, "two", 1.5, True, None],
)
def test_declared_islands_rejects_non_positive_integer_values(tmp_path, bad):
    """Issue #2400: `nets[].islands` must be an integer >= 1 -- a count of
    zero is the companion N=0 concern (issue #2401, a name matching no
    geometry at all), not a declarable partition, and a non-integer or
    boolean value cannot be a count. Validation fails loudly rather than
    silently grading against a coerced value."""
    layout, *_ = _nets_fixture_layout()
    gds = tmp_path / "bad_islands.gds"
    layout.write(str(gds))
    spec = tmp_path / "bad_islands.erc.json"
    _write_spec(spec, _nets_spec(nets=[{"name": "VDD", "islands": bad}]))
    with pytest.raises(ErcError, match=r"nets\[0\]\.islands"):
        run_erc(str(gds), str(spec))


def test_unconnected_net_finding_keys_are_uniform_across_rules(tmp_path):
    """Issue #2194 adds `islands` to the finding shape; it must be present
    (as `null`) on every other rule too, so the `erc_findings[]` key set
    stays identical for every rule id -- `islands` is populated only by the
    multi-island `erc.unconnected_net` call site."""
    layout, top, poly, li1, label = _nets_fixture_layout()
    # A split "VDD" (two islands -> populated `islands`) plus a shorted
    # A/B pair and an unmatched name (both -> `islands is None`).
    top.shapes(li1).insert(kdb.Box.new(_um(5), _um(0), _um(6), _um(1)))
    top.shapes(label).insert(kdb.Text("VDD", kdb.Trans(_um(5.5), _um(0.5))))
    top.shapes(li1).insert(kdb.Box.new(_um(8), _um(0), _um(9), _um(1)))
    top.shapes(label).insert(kdb.Text("VDD", kdb.Trans(_um(8.5), _um(0.5))))
    top.shapes(li1).insert(kdb.Box.new(_um(12), _um(0), _um(14), _um(1)))
    top.shapes(label).insert(kdb.Text("A", kdb.Trans(_um(12.5), _um(0.5))))
    top.shapes(label).insert(kdb.Text("B", kdb.Trans(_um(13.5), _um(0.5))))

    gds = tmp_path / "mixed.gds"
    layout.write(str(gds))
    spec = tmp_path / "mixed.erc.json"
    _write_spec(
        spec,
        _nets_spec(
            nets=[
                {"name": "VDD", "kind": "supply"},
                {"name": "A"},
                {"name": "B"},
                {"name": "MISSING"},
            ]
        ),
    )

    report = run_erc(str(gds), str(spec))
    findings = report["erc_findings"]
    assert findings
    for finding in findings:
        assert set(finding) == {
            "rule",
            "description",
            "net",
            "other_net",
            "gate_id",
            "layer",
            "bbox",
            "islands",
        }

    populated = [f for f in findings if f["islands"] is not None]
    assert [f["net"] for f in populated] == ["VDD"]
    # The zero-match `erc.unconnected_net` case has no geometry to point at
    # and is unchanged in shape.
    unmatched = next(f for f in findings if f["net"] == "MISSING")
    assert unmatched["islands"] is None
    assert unmatched["bbox"] is None


def test_unconnected_net_flags_unmatched_name(tmp_path):
    layout, top, poly, li1, label = _nets_fixture_layout()
    # No geometry anywhere carries a "VDD" label.

    gds = tmp_path / "unmatched.gds"
    layout.write(str(gds))
    spec = tmp_path / "unmatched.erc.json"
    _write_spec(spec, _nets_spec(nets=[{"name": "VDD", "kind": "supply"}]))

    report = run_erc(str(gds), str(spec))
    findings = [f for f in report["erc_findings"] if f["rule"] == "erc.unconnected_net"]

    assert len(findings) == 1
    assert findings[0]["net"] == "VDD"
    assert "matches no labelled geometry" in findings[0]["description"]


def test_unconnected_net_does_not_flag_single_island(tmp_path):
    layout, top, poly, li1, label = _nets_fixture_layout()
    top.shapes(li1).insert(kdb.Box.new(_um(5), _um(0), _um(6), _um(1)))
    top.shapes(label).insert(kdb.Text("VDD", kdb.Trans(_um(5.5), _um(0.5))))

    gds = tmp_path / "single.gds"
    layout.write(str(gds))
    spec = tmp_path / "single.erc.json"
    _write_spec(spec, _nets_spec(nets=[{"name": "VDD", "kind": "supply"}]))

    report = run_erc(str(gds), str(spec))
    findings = [f for f in report["erc_findings"] if f["rule"] == "erc.unconnected_net"]

    assert findings == []


def test_multiply_driven_net_flags_shorted_signal_nets(tmp_path):
    layout, top, poly, li1, label = _nets_fixture_layout()
    # One shape, two different declared (non-supply) net names -- shorted.
    top.shapes(li1).insert(kdb.Box.new(_um(10), _um(0), _um(12), _um(1)))
    top.shapes(label).insert(kdb.Text("A", kdb.Trans(_um(10.5), _um(0.5))))
    top.shapes(label).insert(kdb.Text("B", kdb.Trans(_um(11.5), _um(0.5))))

    gds = tmp_path / "shorted_signals.gds"
    layout.write(str(gds))
    spec = tmp_path / "shorted_signals.erc.json"
    _write_spec(
        spec,
        _nets_spec(
            nets=[
                {"name": "A", "kind": "signal"},
                {"name": "B", "kind": "signal"},
            ]
        ),
    )

    report = run_erc(str(gds), str(spec))
    findings = [
        f for f in report["erc_findings"] if f["rule"] == "erc.multiply_driven_net"
    ]

    assert len(findings) == 1
    assert {findings[0]["net"], findings[0]["other_net"]} == {"A", "B"}
    assert not any(f["rule"] == "erc.supply_short" for f in report["erc_findings"])


def test_multiply_driven_net_does_not_flag_separate_nets(tmp_path):
    layout, top, poly, li1, label = _nets_fixture_layout()
    top.shapes(li1).insert(kdb.Box.new(_um(10), _um(0), _um(11), _um(1)))
    top.shapes(label).insert(kdb.Text("A", kdb.Trans(_um(10.5), _um(0.5))))
    top.shapes(li1).insert(kdb.Box.new(_um(15), _um(0), _um(16), _um(1)))
    top.shapes(label).insert(kdb.Text("B", kdb.Trans(_um(15.5), _um(0.5))))

    gds = tmp_path / "separate_signals.gds"
    layout.write(str(gds))
    spec = tmp_path / "separate_signals.erc.json"
    _write_spec(
        spec,
        _nets_spec(
            nets=[
                {"name": "A", "kind": "signal"},
                {"name": "B", "kind": "signal"},
            ]
        ),
    )

    report = run_erc(str(gds), str(spec))
    assert not any(
        f["rule"] in ("erc.multiply_driven_net", "erc.supply_short")
        for f in report["erc_findings"]
    )


# --- erc_findings: supply short (issue #861) ----------------------------------


def test_supply_short_flags_shorted_supply_nets(tmp_path):
    layout, top, poly, li1, label = _nets_fixture_layout()
    top.shapes(li1).insert(kdb.Box.new(_um(10), _um(0), _um(12), _um(1)))
    top.shapes(label).insert(kdb.Text("VDD", kdb.Trans(_um(10.5), _um(0.5))))
    top.shapes(label).insert(kdb.Text("VSS", kdb.Trans(_um(11.5), _um(0.5))))

    gds = tmp_path / "shorted_supplies.gds"
    layout.write(str(gds))
    spec = tmp_path / "shorted_supplies.erc.json"
    _write_spec(
        spec,
        _nets_spec(
            nets=[
                {"name": "VDD", "kind": "supply"},
                {"name": "VSS", "kind": "supply"},
            ]
        ),
    )

    report = run_erc(str(gds), str(spec))
    findings = [f for f in report["erc_findings"] if f["rule"] == "erc.supply_short"]

    assert len(findings) == 1
    assert {findings[0]["net"], findings[0]["other_net"]} == {"VDD", "VSS"}
    assert not any(
        f["rule"] == "erc.multiply_driven_net" for f in report["erc_findings"]
    )


def test_supply_short_does_not_flag_separate_supplies(tmp_path):
    layout, top, poly, li1, label = _nets_fixture_layout()
    top.shapes(li1).insert(kdb.Box.new(_um(10), _um(0), _um(11), _um(1)))
    top.shapes(label).insert(kdb.Text("VDD", kdb.Trans(_um(10.5), _um(0.5))))
    top.shapes(li1).insert(kdb.Box.new(_um(15), _um(0), _um(16), _um(1)))
    top.shapes(label).insert(kdb.Text("VSS", kdb.Trans(_um(15.5), _um(0.5))))

    gds = tmp_path / "separate_supplies.gds"
    layout.write(str(gds))
    spec = tmp_path / "separate_supplies.erc.json"
    _write_spec(
        spec,
        _nets_spec(
            nets=[
                {"name": "VDD", "kind": "supply"},
                {"name": "VSS", "kind": "supply"},
            ]
        ),
    )

    report = run_erc(str(gds), str(spec))
    assert not any(
        f["rule"] in ("erc.multiply_driven_net", "erc.supply_short")
        for f in report["erc_findings"]
    )


# --- erc_findings: declared same-net tie (issue #2463) -----------------------


def _bar(top, li1, label, x0, x1, names):
    """One ``li1`` conductor from ``x0`` to ``x1`` micrometres carrying every
    label in ``names`` -- two names on one bar is a drawn tie/short, one name
    per bar is two separate nets."""
    top.shapes(li1).insert(kdb.Box.new(_um(x0), _um(0), _um(x1), _um(1)))
    for i, name in enumerate(names):
        top.shapes(label).insert(kdb.Text(name, kdb.Trans(_um(x0 + 0.5 + i), _um(0.5))))


def _same_net_as_report(tmp_path, name, nets, bars):
    """Run `klt erc` over a layout whose ``li1`` bars are ``bars``
    (``(x0, x1, [label, ...])`` triples) against a ``nets[]`` declaration.

    The fixture gate is contacted through a ``licon`` via here (unlike the
    bare :func:`_nets_fixture_layout`), so a clean run really does report an
    empty ``erc_findings`` -- these tests assert on the whole findings list
    rather than filtering one rule out of it."""
    layout, top, poly, li1, label = _nets_fixture_layout()
    licon = layout.layer(2, 0)
    top.shapes(licon).insert(kdb.Box.new(_um(0.2), _um(0.2), _um(0.4), _um(0.4)))
    for x0, x1, names in bars:
        _bar(top, li1, label, x0, x1, names)
    gds = tmp_path / f"{name}.gds"
    layout.write(str(gds))
    spec = tmp_path / f"{name}.erc.json"
    spec_dict = _nets_spec(nets=nets)
    spec_dict["vias"] = [{"name": "licon", "layer": "2/0", "between": ["poly", "li1"]}]
    _write_spec(spec, spec_dict)
    return run_erc(str(gds), str(spec))


def test_same_net_as_grades_a_drawn_supply_tie_as_clean(tmp_path):
    """Issue #2463: two declared names the spec says are intentionally one
    net, drawn as one conductor, are the design -- not an `erc.supply_short`.
    Pre-#2463 the only options were a permanent false positive (declare both)
    or losing all coverage of the second name (declare one)."""
    report = _same_net_as_report(
        tmp_path,
        "tie_drawn",
        nets=[
            {"name": "VSS", "kind": "supply"},
            {"name": "VSS_SUB", "kind": "supply", "same_net_as": "VSS"},
        ],
        bars=[(10, 12, ["VSS", "VSS_SUB"])],
    )

    assert report["erc_findings"] == []
    assert report["erc_finding_count"] == 0
    assert report["erc_status"] == "clean"


def test_same_net_as_reports_expected_short_missing_when_the_tie_is_absent(tmp_path):
    """Issue #2463: the declaration adds a check rather than suppressing one
    -- a declared tie that is *not* drawn is its own finding, so the spec
    that grades signoff can assert the tie is still there."""
    report = _same_net_as_report(
        tmp_path,
        "tie_absent",
        nets=[
            {"name": "VSS", "kind": "supply"},
            {"name": "VSS_SUB", "kind": "supply", "same_net_as": "VSS"},
        ],
        bars=[(10, 11, ["VSS"]), (15, 16, ["VSS_SUB"])],
    )

    findings = [
        f for f in report["erc_findings"] if f["rule"] == "erc.expected_short_missing"
    ]
    assert len(findings) == 1
    assert findings[0]["net"] == "VSS"
    assert findings[0]["other_net"] == "VSS_SUB"
    assert "same_net_as" in findings[0]["description"]
    assert "separate electrical nets" in findings[0]["description"]
    # The uniform 8-key finding shape (issue #2194) holds for the new rule.
    assert set(findings[0]) == {
        "rule",
        "description",
        "net",
        "other_net",
        "gate_id",
        "layer",
        "bbox",
        "islands",
    }
    assert findings[0]["islands"] is None
    # The missing tie is a finding, so the connectivity roll-up is red.
    assert report["erc_status"] == "violations"
    # ... and the per-name island counts are each still satisfied, which is
    # exactly why the absence needed its own rule.
    assert not any(f["rule"] == "erc.unconnected_net" for f in report["erc_findings"])


def test_same_net_as_grades_a_drawn_signal_tie_as_clean(tmp_path):
    """Issue #2463 edge case: the declaration is about the *pairing*, not the
    `kind` -- two tied ``"signal"`` names suppress `erc.multiply_driven_net`
    the same way two supplies suppress `erc.supply_short`."""
    report = _same_net_as_report(
        tmp_path,
        "signal_tie",
        nets=[
            {"name": "A", "kind": "signal"},
            {"name": "B", "kind": "signal", "same_net_as": "A"},
        ],
        bars=[(10, 12, ["A", "B"])],
    )

    assert report["erc_findings"] == []


def test_same_net_as_is_scoped_to_exactly_the_declared_pair(tmp_path):
    """Issue #2463 regression guard: declaring one tie must not loosen the
    short detection for any *other* pair. ``VSS``/``VSS_SUB`` are declared
    one net and drawn as one; ``VDD`` landing on that same conductor is
    still an `erc.supply_short` against both of them."""
    report = _same_net_as_report(
        tmp_path,
        "third_net_short",
        nets=[
            {"name": "VSS", "kind": "supply"},
            {"name": "VSS_SUB", "kind": "supply", "same_net_as": "VSS"},
            {"name": "VDD", "kind": "supply"},
        ],
        bars=[(10, 14, ["VSS", "VSS_SUB", "VDD"])],
    )

    shorts = [f for f in report["erc_findings"] if f["rule"] == "erc.supply_short"]
    assert {(f["net"], f["other_net"]) for f in shorts} == {
        ("VDD", "VSS"),
        ("VDD", "VSS_SUB"),
    }
    assert not any(
        f["rule"] == "erc.expected_short_missing" for f in report["erc_findings"]
    )


def test_same_net_as_may_name_an_entry_declared_later(tmp_path):
    """Issue #2463: the relation is symmetric, so the key may sit on either
    entry of the pair -- a forward reference to a name declared further down
    the array resolves the same way a backward one does."""
    report = _same_net_as_report(
        tmp_path,
        "forward_ref",
        nets=[
            {"name": "VSS", "kind": "supply", "same_net_as": "VSS_SUB"},
            {"name": "VSS_SUB", "kind": "supply"},
        ],
        bars=[(10, 12, ["VSS", "VSS_SUB"])],
    )

    assert report["erc_findings"] == []


def test_same_net_as_chains_into_one_declared_group(tmp_path):
    """Issue #2463: ``same_net_as`` is a pairwise key, and declaring A~B and
    C~B necessarily asserts A, B and C are one net -- so every pair inside
    the resulting group is expected, not just the two written down."""
    report = _same_net_as_report(
        tmp_path,
        "chain",
        nets=[
            {"name": "VSS", "kind": "supply"},
            {"name": "VSS_SUB", "kind": "supply", "same_net_as": "VSS"},
            {"name": "VSS_GUARD", "kind": "supply", "same_net_as": "VSS_SUB"},
        ],
        bars=[(10, 14, ["VSS", "VSS_SUB", "VSS_GUARD"])],
    )

    assert report["erc_findings"] == []


def test_same_net_as_reports_a_declared_partner_that_matches_no_geometry(tmp_path):
    """Issue #2463: a tie whose partner name is not in the layout at all
    cannot be satisfied either -- reported alongside the `erc.unconnected_net`
    for the absent name rather than silently passing."""
    report = _same_net_as_report(
        tmp_path,
        "absent_partner",
        nets=[
            {"name": "VSS", "kind": "supply"},
            {"name": "VSS_SUB", "kind": "supply", "same_net_as": "VSS"},
        ],
        bars=[(10, 11, ["VSS"])],
    )

    missing = [
        f for f in report["erc_findings"] if f["rule"] == "erc.expected_short_missing"
    ]
    assert len(missing) == 1
    assert "matches no labelled geometry" in missing[0]["description"]
    assert "VSS_SUB" in missing[0]["description"]
    assert any(
        f["rule"] == "erc.unconnected_net" and f["net"] == "VSS_SUB"
        for f in report["erc_findings"]
    )


def test_same_net_as_does_not_change_the_connectivity_coverage_ids(tmp_path):
    """Issue #2463: the new rule keys off the same `nets[]` declaration the
    existing ones do, so `erc_coverage` still records one
    `erc.net_connectivity` identity per declared entry -- no new work id."""
    report = _same_net_as_report(
        tmp_path,
        "coverage",
        nets=[
            {"name": "VSS", "kind": "supply"},
            {"name": "VSS_SUB", "kind": "supply", "same_net_as": "VSS"},
        ],
        bars=[(10, 12, ["VSS", "VSS_SUB"])],
    )

    checked = report["erc_coverage"]["checked"]
    assert 'erc.net_connectivity:["VSS"]' in checked
    assert 'erc.net_connectivity:["VSS_SUB"]' in checked
    assert not any(entry.startswith("erc.expected_short_missing") for entry in checked)


@pytest.mark.parametrize(
    ("bad", "match"),
    [
        ("VGND", r"nets\[1\]\.same_net_as"),
        ("VSS_SUB", r"nets\[1\]\.same_net_as"),
        (7, r"nets\[1\]\.same_net_as"),
        ("  ", r"nets\[1\]\.same_net_as"),
    ],
    ids=["undeclared-name", "self-reference", "not-a-string", "blank"],
)
def test_same_net_as_rejects_an_unusable_declaration(tmp_path, bad, match):
    """Issue #2463: a `same_net_as` that cannot name a declared partner is a
    spec error, not a silent no-op -- a typo'd partner would otherwise
    suppress nothing, check nothing, and read as a passing tie declaration."""
    layout, *_ = _nets_fixture_layout()
    gds = tmp_path / "bad_same_net_as.gds"
    layout.write(str(gds))
    spec = tmp_path / "bad_same_net_as.erc.json"
    _write_spec(
        spec,
        _nets_spec(
            nets=[
                {"name": "VSS", "kind": "supply"},
                {"name": "VSS_SUB", "kind": "supply", "same_net_as": bad},
            ]
        ),
    )
    with pytest.raises(ErcError, match=match):
        run_erc(str(gds), str(spec))


def test_same_net_as_null_is_the_undeclared_form(tmp_path):
    """Issue #2463: an explicit JSON ``null`` means "no tie declared", the
    same as omitting the key -- so a spec generator emitting the key
    unconditionally still gets today's `erc.supply_short` behaviour."""
    report = _same_net_as_report(
        tmp_path,
        "null_tie",
        nets=[
            {"name": "VDD", "kind": "supply", "same_net_as": None},
            {"name": "VSS", "kind": "supply", "same_net_as": None},
        ],
        bars=[(10, 12, ["VDD", "VSS"])],
    )

    shorts = [f for f in report["erc_findings"] if f["rule"] == "erc.supply_short"]
    assert len(shorts) == 1
    assert {shorts[0]["net"], shorts[0]["other_net"]} == {"VDD", "VSS"}


# --- erc_findings: missing substrate/well tie (issue #861) -------------------


def _ties_fixture_layout():
    """A layout with a gate poly, a labelled ``li1`` role, and an ``nwell``/
    ``tap`` layer pair for the ``ties`` finding tests below."""
    layout = kdb.Layout()
    layout.dbu = DBU
    top = layout.create_cell("TOP")
    poly = layout.layer(1, 0)
    li1 = layout.layer(3, 0)
    label = layout.layer(3, 5)
    nwell = layout.layer(10, 0)
    tap = layout.layer(11, 0)
    top.shapes(poly).insert(kdb.Box.new(_um(0), _um(0), _um(1), _um(1)))
    top.shapes(li1).insert(kdb.Box.new(_um(0), _um(0), _um(1), _um(1)))
    return layout, top, li1, label, nwell, tap


def _ties_spec():
    return _nets_spec(
        ties=[
            {
                "name": "nwell_tie",
                "well_layer": "10/0",
                "tap_layer": "11/0",
                "connect_to": "li1",
                "net": "VDD",
            }
        ]
    )


def test_missing_tie_flags_well_with_no_tap(tmp_path):
    layout, top, li1, label, nwell, tap = _ties_fixture_layout()
    # A well with no tap contact drawn inside it at all.
    top.shapes(nwell).insert(kdb.Box.new(_um(30), _um(0), _um(34), _um(4)))

    gds = tmp_path / "no_tap.gds"
    layout.write(str(gds))
    spec = tmp_path / "no_tap.erc.json"
    _write_spec(spec, _ties_spec())

    report = run_erc(str(gds), str(spec))
    findings = [f for f in report["erc_findings"] if f["rule"] == "erc.missing_tie"]

    assert len(findings) == 1
    assert findings[0]["net"] == "VDD"
    assert "no 'nwell_tie' tap contact drawn" in findings[0]["description"]


def test_missing_tie_flags_tap_connected_to_wrong_net(tmp_path):
    layout, top, li1, label, nwell, tap = _ties_fixture_layout()
    # A well with a tap present, but the tap's own net is labelled "VSS",
    # not the tie's declared "VDD".
    top.shapes(nwell).insert(kdb.Box.new(_um(40), _um(0), _um(44), _um(4)))
    top.shapes(tap).insert(kdb.Box.new(_um(41), _um(1), _um(42), _um(2)))
    top.shapes(li1).insert(kdb.Box.new(_um(40.5), _um(0.5), _um(42.5), _um(2.5)))
    top.shapes(label).insert(kdb.Text("VSS", kdb.Trans(_um(41), _um(1.5))))

    gds = tmp_path / "wrong_net.gds"
    layout.write(str(gds))
    spec = tmp_path / "wrong_net.erc.json"
    _write_spec(spec, _ties_spec())

    report = run_erc(str(gds), str(spec))
    findings = [f for f in report["erc_findings"] if f["rule"] == "erc.missing_tie"]

    assert len(findings) == 1
    assert findings[0]["net"] == "VDD"
    assert "not connected to declared net 'VDD'" in findings[0]["description"]


def test_missing_tie_does_not_flag_properly_connected_tap(tmp_path):
    layout, top, li1, label, nwell, tap = _ties_fixture_layout()
    top.shapes(nwell).insert(kdb.Box.new(_um(20), _um(0), _um(24), _um(4)))
    top.shapes(tap).insert(kdb.Box.new(_um(21), _um(1), _um(22), _um(2)))
    top.shapes(li1).insert(kdb.Box.new(_um(20.5), _um(0.5), _um(22.5), _um(2.5)))
    top.shapes(label).insert(kdb.Text("VDD", kdb.Trans(_um(21), _um(1.5))))

    gds = tmp_path / "connected.gds"
    layout.write(str(gds))
    spec = tmp_path / "connected.erc.json"
    _write_spec(spec, _ties_spec())

    report = run_erc(str(gds), str(spec))
    findings = [f for f in report["erc_findings"] if f["rule"] == "erc.missing_tie"]

    assert findings == []


# --- ties[]: well/tap connectivity scoping (issue #2169) --------------------


def _routed_tie_layout(
    *, vdd_tap: bool = True, vss_tap: bool = True, draw_pwell: bool = True
):
    """A miniature *routed* two-gate layout -- the shape issue #2169's
    reproduction collapses on, reduced to the smallest geometry that still
    reproduces it.

    Two CMOS-style gate nets (``A``/``B``) each run one ``li1`` strap from
    the p-well band, through the gap where their poly gate sits, up into
    the n-well band -- the ordinary "PMOS drain to NMOS drain" output net
    every standard cell draws. Each strap carries a *contact* inside each
    well, exactly like the real source/drain contacts that make a
    single-layer ``tap_layer`` over-broad. Genuine well taps (a contact
    plus a ``tap_implant`` marker, i.e. the ``Comp ∩ Nplus`` boolean the
    issue says no single layer can express) sit on the ``VDD``/``VSS``
    rails.

    Layer numbers (arbitrary, matching this file's convention): poly=1/0,
    licon=2/0, li1=3/0, li1 label=3/5, nwell=10/0, contact=11/0,
    tap_implant=12/0, pwell=13/0. 98/0 and 99/0 are deliberately left empty
    for the "no well geometry" / "no tap geometry" cases.

    ``draw_pwell=False`` (issue #2255) omits the pwell band entirely: the
    **native-substrate** shape, where the NMOS half sits in bulk and no
    drawn well/tub layer exists for a `ties[]` entry to name. Everything
    else -- the VSS rail, its real (implant-marked) tap, and the
    source/drain contacts that make a bare `tap_layer` over-broad -- is
    unchanged, so any difference between a `draw_pwell=True` run and a
    `draw_pwell=False` one is the missing well layer alone.
    """
    layout = kdb.Layout()
    layout.dbu = DBU
    top = layout.create_cell("TOP")
    poly = layout.layer(1, 0)
    licon = layout.layer(2, 0)
    li1 = layout.layer(3, 0)
    label = layout.layer(3, 5)
    nwell = layout.layer(10, 0)
    contact = layout.layer(11, 0)
    tap_implant = layout.layer(12, 0)
    pwell = layout.layer(13, 0)

    # Two blanket well bands spanning the whole row, with a routing gap in
    # between -- the "blanket well region spanning whole standard-cell
    # rows" the issue names.
    top.shapes(nwell).insert(kdb.Box.new(_um(0), _um(8), _um(20), _um(12)))
    if draw_pwell:
        top.shapes(pwell).insert(kdb.Box.new(_um(0), _um(0), _um(20), _um(4)))

    # Supply rails, each inside its own well band.
    top.shapes(li1).insert(kdb.Box.new(_um(0), _um(10), _um(20), _um(11)))
    top.shapes(label).insert(kdb.Text("VDD", kdb.Trans(_um(1), _um(10.5))))
    top.shapes(li1).insert(kdb.Box.new(_um(0), _um(1), _um(20), _um(2)))
    top.shapes(label).insert(kdb.Text("VSS", kdb.Trans(_um(1), _um(1.5))))

    if vdd_tap:
        top.shapes(contact).insert(kdb.Box.new(_um(2), _um(10.2), _um(2.6), _um(10.8)))
        top.shapes(tap_implant).insert(
            kdb.Box.new(_um(1.8), _um(10), _um(2.8), _um(11))
        )
    if vss_tap:
        top.shapes(contact).insert(kdb.Box.new(_um(2), _um(1.2), _um(2.6), _um(1.8)))
        top.shapes(tap_implant).insert(kdb.Box.new(_um(1.8), _um(1), _um(2.8), _um(2)))

    for x, name in ((7.0, "A"), (13.0, "B")):
        top.shapes(poly).insert(
            kdb.Box.new(_um(x - 2), _um(4.5), _um(x + 0.3), _um(5.5))
        )
        top.shapes(li1).insert(kdb.Box.new(_um(x), _um(2.5), _um(x + 0.6), _um(9.5)))
        top.shapes(licon).insert(
            kdb.Box.new(_um(x + 0.05), _um(4.7), _um(x + 0.25), _um(4.9))
        )
        # Source/drain contacts inside each well -- picked up by any
        # single-layer `tap_layer` naming the contact/diffusion layer.
        top.shapes(contact).insert(
            kdb.Box.new(_um(x + 0.1), _um(8.5), _um(x + 0.5), _um(8.9))
        )
        top.shapes(contact).insert(
            kdb.Box.new(_um(x + 0.1), _um(3.0), _um(x + 0.5), _um(3.4))
        )
        top.shapes(label).insert(kdb.Text(name, kdb.Trans(_um(x + 0.3), _um(6.0))))
    return layout, top


def _routed_tie_spec(ties=None):
    return {
        "stackup": [
            {"name": "poly", "layer": "1/0", "role": "gate"},
            {"name": "li1", "layer": "3/0", "label_layer": "3/5"},
        ],
        "vias": [{"name": "licon", "layer": "2/0", "between": ["poly", "li1"]}],
        "nets": [
            {"name": "VDD", "kind": "supply"},
            {"name": "VSS", "kind": "supply"},
            {"name": "A", "kind": "signal"},
            {"name": "B", "kind": "signal"},
        ],
        "ties": ties or [],
    }


def _routed_tie_entries(
    *,
    nwell_layer: str = "10/0",
    pwell_layer: str = "13/0",
    tap_layer: str = "11/0",
    tap_requires=None,
):
    entries = []
    for name, well_layer, net in (
        ("nwell_tie", nwell_layer, "VDD"),
        ("pwell_tie", pwell_layer, "VSS"),
    ):
        entry = {
            "name": name,
            "well_layer": well_layer,
            "tap_layer": tap_layer,
            "connect_to": "li1",
            "net": net,
        }
        if tap_requires is not None:
            entry["tap_requires"] = tap_requires
        entries.append(entry)
    return entries


def _run_routed_tie(tmp_path, stem, ties=None, *, vdd_tap=True, vss_tap=True):
    layout, _top = _routed_tie_layout(vdd_tap=vdd_tap, vss_tap=vss_tap)
    gds = tmp_path / f"{stem}.gds"
    layout.write(str(gds))
    spec = tmp_path / f"{stem}.erc.json"
    _write_spec(spec, _routed_tie_spec(ties=ties))
    return run_erc(str(gds), str(spec), pdk="sky130")


# Case (a) of issue #2169's four-case table: `ties` omitted -> clean.
def test_ties_omitted_is_the_clean_baseline(tmp_path):
    report = _run_routed_tie(tmp_path, "case_a")

    assert report["gate_count"] == 2
    assert report["erc_findings"] == []
    assert report["status"] == "clean"


# Case (b): a real well plus a `tap_layer` with no geometry -> honest
# `erc.missing_tie` findings, no short, gates untouched.
def test_ties_well_with_no_tap_geometry_reports_honest_missing_tie(tmp_path):
    report = _run_routed_tie(tmp_path, "case_b", _routed_tie_entries(tap_layer="99/0"))

    missing = [f for f in report["erc_findings"] if f["rule"] == "erc.missing_tie"]
    assert len(missing) == 2
    assert {f["net"] for f in missing} == {"VDD", "VSS"}
    assert all("no" in f["description"] for f in missing)
    assert not any(f["rule"] == "erc.supply_short" for f in report["erc_findings"])
    assert report["gate_count"] == 2


# Case (c): a tap layer but no well geometry -> the same absence-of-evidence
# state as a typo'd/never-drawn tub layer (issue #2377) -- both ties' wells
# resolve to an empty region, so their per-well loop runs zero times and the
# zero `erc.missing_tie` findings mean "nothing was examined", not "every
# well is tied". Graded skipped, not a clean pass -- see
# `test_ties_well_layer_with_no_geometry_is_skipped_not_clean` below for the
# dedicated coverage of this reason.
def test_ties_tap_without_well_geometry_is_clean(tmp_path):
    report = _run_routed_tie(
        tmp_path,
        "case_c",
        _routed_tie_entries(nwell_layer="98/0", pwell_layer="98/0"),
    )

    assert report["gate_count"] == 2
    assert report["erc_findings"] == []
    assert report["status"] == "clean"
    assert _tie_skips(report) == {
        'erc.missing_tie:["nwell_tie"]': "empty_well_region",
        'erc.missing_tie:["pwell_tie"]': "empty_well_region",
    }
    assert not any(
        identity.startswith("erc.missing_tie:")
        for identity in report["erc_coverage"]["checked"]
    )
    assert report["erc_status"] == "clean_partial"


# Case (d) -- the one that used to break: a real well plus a real tap must
# not collapse the design into one island, must not invent a supply short,
# and must not shrink `gates[]`.
def test_ties_real_well_and_tap_does_not_collapse_the_design(tmp_path):
    report = _run_routed_tie(tmp_path, "case_d", _routed_tie_entries())

    assert report["gate_count"] == 2
    assert not any(f["rule"] == "erc.supply_short" for f in report["erc_findings"])
    assert not any(
        f["rule"] == "erc.multiply_driven_net" for f in report["erc_findings"]
    )
    assert not any(f["rule"] == "erc.missing_tie" for f in report["erc_findings"])
    assert report["status"] == "clean"


def test_ties_never_alter_gates_or_net_findings(tmp_path):
    """A `ties[]` declaration -- even a pathological one whose `tap_layer`
    names the routing layer itself -- can never change `gates[]` or the
    `nets[]`-driven findings: the tie check runs on its own isolated
    connectivity graph (issue #2169 acceptance criterion 3)."""
    baseline = _run_routed_tie(tmp_path, "iso_a")
    real = _run_routed_tie(tmp_path, "iso_d", _routed_tie_entries())
    pathological = _run_routed_tie(
        tmp_path, "iso_x", _routed_tie_entries(tap_layer="3/0")
    )

    for report in (real, pathological):
        assert report["gates"] == baseline["gates"]
        assert report["gate_count"] == baseline["gate_count"]
        assert [
            f for f in report["erc_findings"] if f["rule"] != "erc.missing_tie"
        ] == baseline["erc_findings"]


def test_tap_requires_narrows_the_tap_to_a_boolean_layer(tmp_path):
    """`tap_requires` expresses the `Comp ∩ Nplus`-style boolean a real PDK
    tap is drawn as -- the derived tap keeps only contacts that also carry
    the tap implant, so the signal-net source/drain contacts inside the
    same well are not mistaken for taps."""
    report = _run_routed_tie(
        tmp_path, "requires", _routed_tie_entries(tap_requires=["12/0"])
    )

    assert report["erc_findings"] == []
    assert report["gate_count"] == 2


def test_tap_requires_reports_missing_tie_when_the_real_tap_is_absent(tmp_path):
    """With the derived tap, a well whose only contacts belong to signal
    nets is honestly reported as untied -- the single-layer `tap_layer`
    would have seen those source/drain contacts as taps."""
    report = _run_routed_tie(
        tmp_path,
        "requires_missing",
        _routed_tie_entries(tap_requires=["12/0"]),
        vdd_tap=False,
    )

    missing = [f for f in report["erc_findings"] if f["rule"] == "erc.missing_tie"]
    assert len(missing) == 1
    assert missing[0]["net"] == "VDD"
    assert "no 'nwell_tie' tap contact drawn" in missing[0]["description"]


def test_missing_tie_reported_when_only_untied_contacts_sit_in_the_well(tmp_path):
    """Without `tap_requires`, an over-broad `tap_layer` still finds the
    signal contacts inside the well -- but none of them reaches the
    declared net, so the well is reported as not tied rather than silently
    passing through a blanket well conductor."""
    report = _run_routed_tie(tmp_path, "untied", _routed_tie_entries(), vdd_tap=False)

    missing = [f for f in report["erc_findings"] if f["rule"] == "erc.missing_tie"]
    assert len(missing) == 1
    assert missing[0]["net"] == "VDD"
    assert "not connected to declared net 'VDD'" in missing[0]["description"]


# --- ties: the degenerate declaration (issue #2199) ---------------------------
#
# The false pass this guards: with no `tap_requires` narrowing, an ordinary
# PMOS *source* contact sitting on the VDD rail inside the n-well is
# indistinguishable from a tap, so `erc.missing_tie` reports the well as
# tied on the strength of geometry that is not a tap at all. The
# reproduction in issue #2199 is the same spec and the same GDS answering
# "tied" for `vdd` and "untied" for `vss`; the layout below is that shape,
# reduced.


def _degenerate_tie_layout(tmp_path, stem, ties):
    """An n-well whose only contact is a PMOS source contact on the VDD
    rail -- no tap implant drawn anywhere in it. The p-well keeps its
    genuine (implant-marked) tap, so any difference between the two ties is
    the declaration's, not the layout's."""
    layout, top = _routed_tie_layout(vdd_tap=False)
    top.shapes(layout.layer(11, 0)).insert(
        kdb.Box.new(_um(4), _um(10.2), _um(4.6), _um(10.8))
    )
    gds = tmp_path / f"{stem}.gds"
    layout.write(str(gds))
    spec = tmp_path / f"{stem}.erc.json"
    _write_spec(spec, _routed_tie_spec(ties=ties))
    return run_erc(str(gds), str(spec), pdk="sky130")


def _tie_skips(report):
    return {
        record["id"]: record["reason"] for record in report["erc_coverage"]["skipped"]
    }


# Case (e) of the `ties[]` table: a well plus a bare `tap_layer` that
# happens to match a source/drain contact already on the declared net. The
# findings are unchanged -- zero, as before -- but the envelope no longer
# reads as a clean missing-tie verdict.
def test_degenerate_tie_is_skipped_work_not_a_clean_missing_tie(tmp_path):
    report = _degenerate_tie_layout(tmp_path, "degenerate", _routed_tie_entries())

    # The mechanism itself is unchanged: the source contact reaches VDD, so
    # no finding is emitted. That is exactly why the coverage signal has to
    # carry the caveat.
    assert [f for f in report["erc_findings"] if f["rule"] == "erc.missing_tie"] == []
    assert _tie_skips(report) == {
        'erc.missing_tie:["nwell_tie"]': "degenerate_tap_declaration",
        'erc.missing_tie:["pwell_tie"]': "degenerate_tap_declaration",
    }
    assert not any(
        identity.startswith("erc.missing_tie:")
        for identity in report["erc_coverage"]["checked"]
    )
    assert report["erc_status"] == "clean_partial"


# The control that localises it: the same GDS, the same ties, with the
# narrowing the degenerate form lacks. The n-well has no tap implant in it,
# so the honest answer is a finding -- and that tie is graded as checked
# work, not skipped.
def test_tap_requires_makes_the_same_layout_report_an_honest_missing_tie(tmp_path):
    report = _degenerate_tie_layout(
        tmp_path, "narrowed", _routed_tie_entries(tap_requires=["12/0"])
    )

    missing = [f for f in report["erc_findings"] if f["rule"] == "erc.missing_tie"]
    assert [f["net"] for f in missing] == ["VDD"]
    assert report["erc_coverage"]["skipped"] == []
    assert 'erc.missing_tie:["nwell_tie"]' in report["erc_coverage"]["checked"]
    assert report["erc_status"] == "violations"


def test_tap_is_dedicated_keeps_a_bare_tap_layer_checked(tmp_path):
    """A PDK that draws taps on their own layer has nothing to narrow, so
    the spec affirms that instead -- and the tie is graded as the real
    check it is. The affirmation changes no geometry: the findings are
    identical to the degenerate run's."""
    ties = [{**entry, "tap_is_dedicated": True} for entry in _routed_tie_entries()]
    report = _degenerate_tie_layout(tmp_path, "dedicated", ties)

    assert [f for f in report["erc_findings"] if f["rule"] == "erc.missing_tie"] == []
    assert report["erc_coverage"]["skipped"] == []
    assert {
        'erc.missing_tie:["nwell_tie"]',
        'erc.missing_tie:["pwell_tie"]',
    } <= set(report["erc_coverage"]["checked"])
    assert report["erc_status"] == "clean"


def test_tap_requires_that_narrows_nothing_is_still_degenerate(tmp_path):
    """The test is geometric, not "is the key present": a `tap_requires`
    that covers every contact in the well (an implant drawn over the whole
    well, not only over the taps) narrows nothing and is exactly as
    unfalsifiable as omitting it."""
    ties = [
        {
            "name": "nwell_tie",
            "well_layer": "10/0",
            "tap_layer": "11/0",
            # The n-well layer itself -- inside the well, this intersection
            # removes nothing at all.
            "tap_requires": ["10/0"],
            "connect_to": "li1",
            "net": "VDD",
        }
    ]
    report = _degenerate_tie_layout(tmp_path, "no_narrowing", ties)

    assert _tie_skips(report) == {
        'erc.missing_tie:["nwell_tie"]': "degenerate_tap_declaration"
    }
    assert report["erc_status"] == "clean_partial"


def test_a_tie_whose_tap_matches_no_geometry_is_not_called_degenerate(tmp_path):
    """An empty tap region asserts nothing about taps either -- but it
    cannot produce a false pass: every well in it is reported as having no
    tap drawn. That is an honest finding, so the tie stays checked work and
    the skip list stays empty."""
    report = _run_routed_tie(
        tmp_path, "empty_tap", _routed_tie_entries(tap_layer="99/0")
    )

    assert report["erc_coverage"]["skipped"] == []
    assert 'erc.missing_tie:["nwell_tie"]' in report["erc_coverage"]["checked"]
    assert report["erc_status"] == "violations"


# --- ties: caller-asserted taps (issue #2234) ---------------------------
#
# The gap the tests above cannot close: `tap_requires` needs a PDK implant
# layer to actually be *drawn* (an implant-free full-custom stream draws
# none at all), and `tap_is_dedicated` needs a tap-only marker layer to
# exist (many PDKs, including gf180mcu, have none). `tap_boxes` is the
# third way -- naming the tap geometry directly -- and it is held to the
# same falsifiability bar #2199 already established for the other two.


def _routed_tie_entries_with_tap_boxes(vdd_box, vss_box):
    """Two tie declarations against `_routed_tie_layout`'s fixture, each
    asserting its tap directly via `tap_boxes` -- no `tap_requires`
    narrowing, no `tap_is_dedicated` affirmation, matching the reproduction
    this issue names: a stream with no distinguishing marker layer at
    all."""
    return [
        {
            "name": "nwell_tie",
            "well_layer": "10/0",
            "tap_layer": "11/0",
            "tap_boxes": [list(vdd_box)],
            "connect_to": "li1",
            "net": "VDD",
        },
        {
            "name": "pwell_tie",
            "well_layer": "13/0",
            "tap_layer": "11/0",
            "tap_boxes": [list(vss_box)],
            "connect_to": "li1",
            "net": "VSS",
        },
    ]


# `_routed_tie_layout`'s real tap contacts (see that fixture's docstring):
# VDD's is `Box(2, 10.2, 2.6, 10.8)`, VSS's is `Box(2, 1.2, 2.6, 1.8)`.
# These windows contain each real tap and nothing else on the same layer --
# in particular, neither of the ordinary source/drain contacts at
# x in {7.1..7.5, 13.1..13.5} falls inside either box.
_VDD_TAP_BOX = (1.5, 10.0, 3.0, 11.0)
_VSS_TAP_BOX = (1.5, 1.0, 3.0, 2.0)


def test_tap_boxes_resolves_real_taps_without_any_narrowing_marker(tmp_path):
    """Issue #2234's own fifth reproduction case: no `tap_requires`
    narrowing at all (the implant-free full-custom case the issue is about)
    and no `tap_is_dedicated` affirmation (no tap-only marker layer either)
    -- `tap_boxes` names the two contacted tap rings directly and resolves
    both cleanly, with zero `erc.missing_tie` findings."""
    ties = _routed_tie_entries_with_tap_boxes(_VDD_TAP_BOX, _VSS_TAP_BOX)
    report = _run_routed_tie(tmp_path, "tap_boxes_clean", ties)

    assert [f for f in report["erc_findings"] if f["rule"] == "erc.missing_tie"] == []
    assert report["erc_coverage"]["skipped"] == []
    assert set(report["erc_coverage"]["checked_by_assertion"]) == {
        'erc.missing_tie:["nwell_tie"]',
        'erc.missing_tie:["pwell_tie"]',
    }
    # `checked_by_assertion` names a subset of `checked`, never a parallel,
    # disjoint list -- an asserted tie is still real, evaluated work.
    assert set(report["erc_coverage"]["checked_by_assertion"]) <= set(
        report["erc_coverage"]["checked"]
    )
    assert report["erc_status"] == "clean"
    assert report["gate_count"] == 2


def test_tap_boxes_checked_by_assertion_is_per_tie_not_blanket(tmp_path):
    """`checked_by_assertion` names only the ties that actually used
    `tap_boxes` -- a sibling tie graded through ordinary `tap_requires`
    narrowing in the same run must not be swept in."""
    ties = [
        {
            "name": "nwell_tie",
            "well_layer": "10/0",
            "tap_layer": "11/0",
            "tap_boxes": [list(_VDD_TAP_BOX)],
            "connect_to": "li1",
            "net": "VDD",
        },
        {
            "name": "pwell_tie",
            "well_layer": "13/0",
            "tap_layer": "11/0",
            "tap_requires": ["12/0"],
            "connect_to": "li1",
            "net": "VSS",
        },
    ]
    report = _run_routed_tie(tmp_path, "tap_boxes_mixed", ties)

    assert report["erc_coverage"]["checked_by_assertion"] == [
        'erc.missing_tie:["nwell_tie"]'
    ]
    assert {
        'erc.missing_tie:["nwell_tie"]',
        'erc.missing_tie:["pwell_tie"]',
    } <= set(report["erc_coverage"]["checked"])


def test_tap_boxes_that_narrow_nothing_are_still_degenerate(tmp_path):
    """The falsifiability test is geometric (issue #2199), not "which
    narrowing key was given": a `tap_boxes` assertion spanning the whole
    well removes nothing from the drawn tap layer inside it, and is exactly
    as unfalsifiable as omitting `tap_requires` altogether."""
    ties = _routed_tie_entries_with_tap_boxes((0, 8, 20, 12), (0, 0, 20, 4))
    report = _run_routed_tie(tmp_path, "tap_boxes_degenerate", ties)

    assert [f for f in report["erc_findings"] if f["rule"] == "erc.missing_tie"] == []
    assert _tie_skips(report) == {
        'erc.missing_tie:["nwell_tie"]': "degenerate_tap_declaration",
        'erc.missing_tie:["pwell_tie"]': "degenerate_tap_declaration",
    }
    assert report["erc_coverage"]["checked_by_assertion"] == []
    assert report["erc_status"] == "clean_partial"


def test_tap_boxes_matching_no_geometry_is_an_honest_finding_not_a_silent_pass(
    tmp_path,
):
    """An assertion that names geometry the layout never drew is not a
    silent pass either: the well is honestly reported as untied, and the
    tie stays checked (asserted) work rather than being excused as
    degenerate."""
    empty_box = (15.0, 8.0, 16.0, 9.0)  # inside the n-well, no contact there
    ties = _routed_tie_entries_with_tap_boxes(empty_box, _VSS_TAP_BOX)
    report = _run_routed_tie(tmp_path, "tap_boxes_empty", ties)

    missing = [f for f in report["erc_findings"] if f["rule"] == "erc.missing_tie"]
    assert [f["net"] for f in missing] == ["VDD"]
    assert "no 'nwell_tie' tap contact drawn" in missing[0]["description"]
    assert report["erc_coverage"]["skipped"] == []
    assert (
        'erc.missing_tie:["nwell_tie"]'
        in report["erc_coverage"]["checked_by_assertion"]
    )
    assert report["erc_status"] == "violations"


def test_tap_boxes_rejects_a_malformed_entry(tmp_path):
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
            "ties": [
                {
                    "well_layer": "10/0",
                    "tap_layer": "11/0",
                    "tap_boxes": [[1.0, 2.0, 3.0]],
                    "connect_to": "li1",
                    "net": "VDD",
                }
            ],
        },
    )
    with pytest.raises(ErcError, match=r"tap_boxes\[0\]"):
        run_erc(str(gds), str(spec))


def test_tap_boxes_rejects_an_inverted_box(tmp_path):
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
            "ties": [
                {
                    "well_layer": "10/0",
                    "tap_layer": "11/0",
                    "tap_boxes": [[3.0, 2.0, 1.0, 4.0]],
                    "connect_to": "li1",
                    "net": "VDD",
                }
            ],
        },
    )
    with pytest.raises(ErcError, match="left < right and bottom < top"):
        run_erc(str(gds), str(spec))


# --- ties: caller-asserted substrate regions (issue #2255) ---------------
#
# The gap every test above leaves open: `tap_boxes` relaxes how the *tap*
# side is expressed, but `well_layer` still required drawn geometry, so a
# block sitting in a native substrate -- NMOS-in-bulk, no drawn pwell/tub
# anywhere in the stream -- could not declare its substrate tie at all, and
# only the drawn-well (nwell) half of such a design was ever graded.
# `well_layer: null` + `well_boxes` is the substitute, and unlike
# `tap_boxes` it replaces a required field rather than narrowing one -- so
# it carries its own falsifiability test and its own coverage bucket.


def _run_native_substrate(tmp_path, stem, ties, *, vdd_tap=True):
    """`_routed_tie_layout` with no pwell drawn -- the native-substrate
    stream this issue is about."""
    layout, _top = _routed_tie_layout(vdd_tap=vdd_tap, draw_pwell=False)
    gds = tmp_path / f"{stem}.gds"
    layout.write(str(gds))
    spec = tmp_path / f"{stem}.erc.json"
    _write_spec(spec, _routed_tie_spec(ties=ties))
    return run_erc(str(gds), str(spec), pdk="sky130")


def _substrate_tie(well_boxes, **overrides):
    """A substrate tie against the native-substrate fixture: no drawn well
    to name, the real (implant-marked) VSS tap narrowed by `tap_requires`
    so the tap side is not itself degenerate."""
    return {
        "name": "substrate_tie",
        "well_layer": None,
        "well_boxes": [list(box) for box in well_boxes],
        "tap_layer": "11/0",
        "tap_requires": ["12/0"],
        "connect_to": "li1",
        "net": "VSS",
        **overrides,
    }


# The fixture's own extent: the drawn geometry spans x in [0, 20] and y in
# [0, 12], so the substrate band below the routing gap is a third of it --
# a real claim about where the substrate tie applies, not "the whole die".
_SUBSTRATE_BAND = (0.0, 0.0, 20.0, 4.0)
_WHOLE_EXTENT = (-5.0, -5.0, 25.0, 25.0)


def test_native_substrate_tie_is_declarable_without_a_drawn_well(tmp_path):
    """Issue #2255's own reproduction: a block with no drawn pwell can now
    declare its substrate tie, and the declaration is graded as real,
    checked work -- distinguishable in `erc_coverage` from a tie derived
    from drawn well geometry."""
    report = _run_native_substrate(
        tmp_path, "native_clean", [_substrate_tie([_SUBSTRATE_BAND])]
    )

    identity = 'erc.missing_tie:["substrate_tie"]'
    assert [f for f in report["erc_findings"] if f["rule"] == "erc.missing_tie"] == []
    assert identity in report["erc_coverage"]["checked"]
    assert report["erc_coverage"]["skipped"] == []
    assert report["erc_coverage"]["checked_by_well_assertion"] == [identity]
    # The well was asserted; the *tap* was still derived from drawn markers,
    # so the tap-side assertion list stays empty. The two are separate
    # claims and must not be conflated.
    assert report["erc_coverage"]["checked_by_assertion"] == []
    # `checked_by_well_assertion` names a subset of `checked`, never a
    # parallel, differently-populated list.
    assert set(report["erc_coverage"]["checked_by_well_assertion"]) <= set(
        report["erc_coverage"]["checked"]
    )
    assert report["erc_status"] == "clean"


def test_native_substrate_well_assertion_can_fail(tmp_path):
    """The falsifiability that earns the assertion its `checked` grade: an
    asserted region the layout does not actually tie is reported, per
    asserted polygon, exactly as an untied drawn well would be. The left
    box holds the real implant-marked tap; the right box (disjoint from it,
    so the two stay separate polygons after merging) holds only source/drain
    contacts, which the `tap_requires` narrowing removes."""
    report = _run_native_substrate(
        tmp_path,
        "native_untied",
        [_substrate_tie([(0.0, 0.0, 9.0, 4.0), (11.0, 0.0, 20.0, 4.0)])],
    )

    missing = [f for f in report["erc_findings"] if f["rule"] == "erc.missing_tie"]
    assert len(missing) == 1
    assert missing[0]["net"] == "VSS"
    assert missing[0]["layer"] == "substrate_tie"
    assert "no 'substrate_tie' tap contact drawn" in missing[0]["description"]
    # The right-hand asserted polygon is the one reported, not the whole
    # asserted region: the finer the partition, the sharper the evidence.
    assert missing[0]["bbox"]["left"] == _um(11.0)
    assert report["erc_status"] == "violations"


def test_a_whole_extent_well_assertion_is_degenerate_not_evidence(tmp_path):
    """The #2199 bar, adapted to the well side: an assertion that covers the
    entire top-cell extent makes `erc.missing_tie` trivially satisfiable by
    any contact anywhere that reaches the declared net -- one die-sized
    "well" polygon, so the per-well loop asks nothing about taps. It is
    recorded as skipped work under its own reason, never as a pass."""
    report = _run_native_substrate(
        tmp_path, "native_degenerate", [_substrate_tie([_WHOLE_EXTENT])]
    )

    identity = 'erc.missing_tie:["substrate_tie"]'
    assert _tie_skips(report) == {identity: "degenerate_well_assertion"}
    assert identity not in report["erc_coverage"]["checked"]
    assert report["erc_coverage"]["checked_by_well_assertion"] == []
    # Findings are unchanged by the classification, exactly as for a
    # degenerate tap declaration -- what changes is that the envelope no
    # longer reads as a clean missing-tie verdict.
    assert [f for f in report["erc_findings"] if f["rule"] == "erc.missing_tie"] == []
    assert report["erc_status"] == "clean_partial"


def test_the_well_degeneracy_reason_is_distinct_from_the_tap_one(tmp_path):
    """Two different defects, two different remedies: "you claimed the whole
    die" must not render as "narrow your tap". The well test is applied
    first because the tap narrowing is measured *inside* the well region, so
    on a die-sized well the tap answer is about the die, not about a tap."""
    both_degenerate = _substrate_tie([_WHOLE_EXTENT])
    del both_degenerate["tap_requires"]  # tap-degenerate as well
    report = _run_native_substrate(tmp_path, "native_both", [both_degenerate])

    assert _tie_skips(report) == {
        'erc.missing_tie:["substrate_tie"]': "degenerate_well_assertion"
    }


def test_a_drawn_blanket_well_is_not_treated_as_a_degenerate_assertion(tmp_path):
    """The degeneracy test applies to *assertions* only. A drawn well layer
    that happens to cover most of the block is a fact about the stream, not
    an unverifiable claim, and grades exactly as it did before this issue --
    the whole existing `ties[]` corpus depends on that."""
    report = _run_routed_tie(
        tmp_path, "drawn_blanket", _routed_tie_entries(tap_requires=["12/0"])
    )

    assert report["erc_coverage"]["skipped"] == []
    assert report["erc_coverage"]["checked_by_well_assertion"] == []
    assert report["erc_status"] == "clean"


def test_an_asserted_substrate_tie_coexists_with_a_drawn_well_tie(tmp_path):
    """The mixed-substrate design the issue names: an n-well tie graded off
    drawn geometry beside a substrate tie graded off an assertion, in the
    same run. The two must stay independent -- including when the asserted
    region overlaps the drawn well in plan view, which it does here (the
    assertion reaches up to y=9, the n-well band starts at y=8)."""
    ties = [
        {
            "name": "nwell_tie",
            "well_layer": "10/0",
            "tap_layer": "11/0",
            "tap_requires": ["12/0"],
            "connect_to": "li1",
            "net": "VDD",
        },
        _substrate_tie([(0.0, 0.0, 20.0, 9.0)]),
    ]
    report = _run_native_substrate(tmp_path, "native_mixed", ties)

    assert [f for f in report["erc_findings"] if f["rule"] == "erc.missing_tie"] == []
    assert {
        'erc.missing_tie:["nwell_tie"]',
        'erc.missing_tie:["substrate_tie"]',
    } <= set(report["erc_coverage"]["checked"])
    # Only the asserted half is graded as asserted: the drawn n-well tie is
    # an ordinary geometrically-derived pass, and the overlap does not
    # promote it (or demote it) into the assertion bucket.
    assert report["erc_coverage"]["checked_by_well_assertion"] == [
        'erc.missing_tie:["substrate_tie"]'
    ]
    assert report["erc_status"] == "clean"


def test_well_and_tap_assertions_are_reported_in_separate_buckets(tmp_path):
    """A stream with neither a drawn well nor a distinguishing tap marker
    asserts both -- and each claim is reported under its own classification,
    so a reader can see exactly how much of the verdict rested on the
    caller's word."""
    tie = _substrate_tie([_SUBSTRATE_BAND], tap_boxes=[list(_VSS_TAP_BOX)])
    del tie["tap_requires"]
    report = _run_native_substrate(tmp_path, "native_both_asserted", [tie])

    identity = 'erc.missing_tie:["substrate_tie"]'
    assert [f for f in report["erc_findings"] if f["rule"] == "erc.missing_tie"] == []
    assert report["erc_coverage"]["checked_by_assertion"] == [identity]
    assert report["erc_coverage"]["checked_by_well_assertion"] == [identity]
    assert report["erc_status"] == "clean"


def test_a_spec_that_declares_no_ties_reports_no_well_assertions(tmp_path):
    """The additive-when-unused guarantee: the new list is present and empty
    for every spec that does not use it."""
    report = _run_routed_tie(tmp_path, "no_well_assertion")
    assert report["erc_coverage"]["checked_by_well_assertion"] == []


def _native_substrate_spec_error(tmp_path, stem, tie):
    gds = tmp_path / f"{stem}.gds"
    _basic_fixture(gds)
    spec = tmp_path / f"{stem}.json"
    _write_spec(
        spec,
        {
            "stackup": [
                {"name": "poly", "layer": "1/0", "role": "gate"},
                {"name": "li1", "layer": "3/0"},
            ],
            "ties": [tie],
        },
    )
    return gds, spec


def test_a_null_well_layer_without_well_boxes_is_rejected(tmp_path):
    """A "there is no well and I am not asserting one" entry is not a tie that
    quietly checks nothing -- that state is already expressible (omit the
    entry, disclose why), and unlike this one it cannot be mistaken for a
    graded check."""
    gds, spec = _native_substrate_spec_error(
        tmp_path,
        "null_no_boxes",
        {
            "well_layer": None,
            "tap_layer": "11/0",
            "connect_to": "li1",
            "net": "VSS",
        },
    )
    with pytest.raises(ErcError, match="well_boxes must assert the substrate region"):
        run_erc(str(gds), str(spec))


def test_an_empty_well_boxes_list_on_a_null_well_layer_is_rejected(tmp_path):
    """Same rule for an explicitly empty list: an assertion that names no
    region asserts nothing."""
    gds, spec = _native_substrate_spec_error(
        tmp_path,
        "null_empty_boxes",
        {
            "well_layer": None,
            "well_boxes": [],
            "tap_layer": "11/0",
            "connect_to": "li1",
            "net": "VSS",
        },
    )
    with pytest.raises(ErcError, match="well_boxes must assert the substrate region"):
        run_erc(str(gds), str(spec))


def test_well_boxes_alongside_a_drawn_well_layer_is_rejected(tmp_path):
    """The two well forms are mutually exclusive: a box list applied to a
    drawn well is either a tap narrowing (`tap_boxes` already expresses
    that, better) or a second, unstated claim."""
    gds, spec = _native_substrate_spec_error(
        tmp_path,
        "boxes_and_layer",
        {
            "well_layer": "10/0",
            "well_boxes": [[0.0, 0.0, 4.0, 4.0]],
            "tap_layer": "11/0",
            "connect_to": "li1",
            "net": "VSS",
        },
    )
    with pytest.raises(ErcError, match="well_layer.*must be null"):
        run_erc(str(gds), str(spec))


def test_an_omitted_well_layer_key_is_still_missing_not_asserted(tmp_path):
    """The absence of a drawn well must be *declared* (`well_layer: null`),
    never inferred from a key that was simply left out -- a typo must not
    silently become an assertion."""
    gds, spec = _native_substrate_spec_error(
        tmp_path,
        "no_well_key",
        {
            "well_boxes": [[0.0, 0.0, 4.0, 4.0]],
            "tap_layer": "11/0",
            "connect_to": "li1",
            "net": "VSS",
        },
    )
    with pytest.raises(ErcError, match="missing 'well_layer'"):
        run_erc(str(gds), str(spec))


def test_well_boxes_rejects_a_malformed_entry(tmp_path):
    gds, spec = _native_substrate_spec_error(
        tmp_path,
        "bad_well_box",
        {
            "well_layer": None,
            "well_boxes": [[1.0, 2.0, 3.0]],
            "tap_layer": "11/0",
            "connect_to": "li1",
            "net": "VSS",
        },
    )
    with pytest.raises(ErcError, match=r"well_boxes\[0\]"):
        run_erc(str(gds), str(spec))


def test_well_boxes_rejects_an_inverted_box(tmp_path):
    gds, spec = _native_substrate_spec_error(
        tmp_path,
        "inverted_well_box",
        {
            "well_layer": None,
            "well_boxes": [[3.0, 2.0, 1.0, 4.0]],
            "tap_layer": "11/0",
            "connect_to": "li1",
            "net": "VSS",
        },
    )
    with pytest.raises(ErcError, match="left < right and bottom < top"):
        run_erc(str(gds), str(spec))


# --- ties: well-side class selection (issue #2339) -----------------------
#
# Everything above narrows or asserts on the *tap* side, or replaces the
# well with an assertion. None of it reaches the case this section is
# about: one drawn tub layer carrying two differently-biased well
# *classes* -- device-body wells strapped to one supply beside a vertical
# bipolar's base tub strapped to the other, the ordinary shape of a
# bandgap/bias block on any PDK with a single n-tub layer. Each `ties[]`
# entry graded *every* merged shape of `well_layer` against its own single
# `net`, so whichever class was not declared reported a false
# `erc.missing_tie` -- one per correctly-tapped well of the other class,
# with the two declarations' finding sets disjoint and together covering
# every well. `well_requires`/`well_excludes` select which shapes an entry
# is about.


def _two_class_well_layout(*, base_tub_tap: bool = True):
    """`_routed_tie_layout`'s routed two-gate block plus a second n-tub on
    the **same** `nwell` layer (10/0), biased to the *other* rail: the
    base tub of a diode-connected vertical bipolar, sitting in the p-well
    band and strapped to VSS, beside the device-body n-well band strapped
    to VDD.

    Both tubs are real, correct, fully tapped wells on one drawn layer.
    The class marker (14/0, a PDK device marker over the bipolar) covers
    only *part* of the base tub, and deliberately does not cover its tap
    -- the marker sits at x in [5.2, 6.2] while the tap contact is at
    x in [4.0, 4.6] -- so a narrowing that intersected the marker into the
    well layer (rather than selecting whole shapes of it) would push that
    tub's real tap outside the graded region and report the very finding
    this feature removes.

    The base tub also carries one *unmarked* contact (the bipolar's own
    emitter contact, no tap implant on it), so the `tap_requires`
    narrowing removes something inside this well too and the tie is not
    itself tap-degenerate.

    `base_tub_tap=False` omits the tub's implant-marked tap, leaving the
    untied-well case for the falsifiability tests.
    """
    layout, top = _routed_tie_layout()
    nwell = layout.layer(10, 0)
    contact = layout.layer(11, 0)
    tap_implant = layout.layer(12, 0)
    bjt_marker = layout.layer(14, 0)

    top.shapes(nwell).insert(kdb.Box.new(_um(3), _um(0.4), _um(6.5), _um(2.8)))
    top.shapes(bjt_marker).insert(kdb.Box.new(_um(5.2), _um(0.8), _um(6.2), _um(2.4)))
    if base_tub_tap:
        top.shapes(contact).insert(kdb.Box.new(_um(4), _um(1.2), _um(4.6), _um(1.8)))
        top.shapes(tap_implant).insert(kdb.Box.new(_um(3.8), _um(1), _um(4.8), _um(2)))
    top.shapes(contact).insert(kdb.Box.new(_um(5.4), _um(1.2), _um(5.8), _um(1.8)))
    return layout, top


def _run_two_class_well(tmp_path, stem, ties, *, base_tub_tap=True):
    layout, _top = _two_class_well_layout(base_tub_tap=base_tub_tap)
    gds = tmp_path / f"{stem}.gds"
    layout.write(str(gds))
    spec = tmp_path / f"{stem}.erc.json"
    _write_spec(spec, _routed_tie_spec(ties=ties))
    return run_erc(str(gds), str(spec), pdk="sky130")


def _two_class_ties(*, body_overrides=None, base_overrides=None):
    """The two bias classes of the single 10/0 tub layer, declared as two
    `ties[]` entries -- identical but for their `net` and their well-side
    selection, which is what makes them two classes rather than two
    contradictory readings of the same one."""
    body = {
        "name": "device_body_wells",
        "well_layer": "10/0",
        "tap_layer": "11/0",
        "tap_requires": ["12/0"],
        "connect_to": "li1",
        "net": "VDD",
    }
    base = {
        "name": "bipolar_base_tub",
        "well_layer": "10/0",
        "tap_layer": "11/0",
        "tap_requires": ["12/0"],
        "connect_to": "li1",
        "net": "VSS",
    }
    body.update(body_overrides or {})
    base.update(base_overrides or {})
    return [body, base]


_BODY_ID = 'erc.missing_tie:["device_body_wells"]'
_BASE_ID = 'erc.missing_tie:["bipolar_base_tub"]'


def test_one_tub_layer_two_bias_classes_is_unsatisfiable_without_selection(tmp_path):
    """The reproduction, on today's vocabulary: both declarations are
    honest and the layout is correct, but each entry grades *every* shape
    of the shared `well_layer` against its own net, so each reports the
    other class's well. The two finding sets are disjoint and together
    account for every well -- which is exactly why no single report can
    say the layout is right."""
    report = _run_two_class_well(tmp_path, "two_class_before", _two_class_ties())

    missing = [f for f in report["erc_findings"] if f["rule"] == "erc.missing_tie"]
    assert len(missing) == 2
    assert {f["layer"] for f in missing} == {"device_body_wells", "bipolar_base_tub"}
    assert all("not connected to declared net" in f["description"] for f in missing)
    # Disjoint: the well each entry reports is the one the *other* entry
    # ties correctly.
    reported = {(f["layer"], f["bbox"]["bottom"]) for f in missing}
    assert len(reported) == 2
    assert report["erc_status"] == "violations"


def test_well_requires_and_well_excludes_partition_one_tub_layer(tmp_path):
    """Issue #2339's acceptance criterion: the same layout and the same two
    declarations, each scoped to its own bias class by the device marker --
    one naming it, the complementary one excluding it -- report **zero**
    `erc.missing_tie` findings, with both ties graded as real, checked
    work."""
    report = _run_two_class_well(
        tmp_path,
        "two_class_after",
        _two_class_ties(
            body_overrides={"well_excludes": ["14/0"]},
            base_overrides={"well_requires": ["14/0"]},
        ),
    )

    assert [f for f in report["erc_findings"] if f["rule"] == "erc.missing_tie"] == []
    assert {_BODY_ID, _BASE_ID} <= set(report["erc_coverage"]["checked"])
    assert report["erc_coverage"]["skipped"] == []
    # Drawn-geometry narrowing, not caller assertion: neither assertion
    # bucket is touched by a well-side selection.
    assert report["erc_coverage"]["checked_by_assertion"] == []
    assert report["erc_coverage"]["checked_by_well_assertion"] == []
    assert report["erc_status"] == "clean"
    assert report["gate_count"] == 2


def test_well_requires_selects_whole_shapes_not_the_marker_footprint(tmp_path):
    """The reason this narrows by *selection* rather than by intersection:
    the marker covers only part of the base tub and does not reach its tap
    at all. Intersecting it into `well_layer` would shrink the graded
    region to the marker's own footprint -- with the tub's real,
    correctly-wired tap outside it -- and report "no tap contact drawn
    inside it", re-introducing the false finding from the other
    direction."""
    report = _run_two_class_well(
        tmp_path,
        "two_class_whole_shape",
        [_two_class_ties(base_overrides={"well_requires": ["14/0"]})[1]],
    )

    assert [f for f in report["erc_findings"] if f["rule"] == "erc.missing_tie"] == []
    assert _BASE_ID in report["erc_coverage"]["checked"]
    assert report["erc_coverage"]["skipped"] == []


def test_a_selected_well_class_still_reports_its_own_untied_well(tmp_path):
    """The falsifiability that earns the selection its `checked` grade: it
    narrows *what is graded*, never what a finding is allowed to say. Drop
    the base tub's tap and the scoped entry reports that tub -- and only
    that tub, not the device-body wells it deliberately excluded."""
    report = _run_two_class_well(
        tmp_path,
        "two_class_untied",
        _two_class_ties(
            body_overrides={"well_excludes": ["14/0"]},
            base_overrides={"well_requires": ["14/0"]},
        ),
        base_tub_tap=False,
    )

    missing = [f for f in report["erc_findings"] if f["rule"] == "erc.missing_tie"]
    assert len(missing) == 1
    assert missing[0]["layer"] == "bipolar_base_tub"
    assert missing[0]["net"] == "VSS"
    assert "no 'bipolar_base_tub' tap contact drawn" in missing[0]["description"]
    # The reported shape is the tub itself, not the marker's footprint.
    assert missing[0]["bbox"]["left"] == _um(3.0)
    assert missing[0]["bbox"]["right"] == _um(6.5)
    assert report["erc_status"] == "violations"


def test_a_selection_that_keeps_every_well_is_degenerate(tmp_path):
    """The #2199 bar applied to the well-side selector: a "narrowing" that
    keeps every merged shape of the drawn layer partitions nothing, so the
    entry silently grades the other bias class against its own net exactly
    as an unselected tie would. Recorded as skipped work under its own
    reason, never as a pass. (Every well here contains a tap implant, so
    naming 12/0 selects all of them.)"""
    report = _run_two_class_well(
        tmp_path,
        "two_class_keeps_all",
        [_two_class_ties(base_overrides={"well_requires": ["12/0"]})[1]],
    )

    assert _tie_skips(report) == {_BASE_ID: "degenerate_well_selection"}
    assert _BASE_ID not in report["erc_coverage"]["checked"]
    # And the false finding is still reported, exactly as it would be
    # without the key: the device-body band is graded against VSS. The
    # skip is what tells a reader the declaration did not do what it
    # looks like it did.
    missing = [f for f in report["erc_findings"] if f["rule"] == "erc.missing_tie"]
    assert len(missing) == 1
    assert missing[0]["bbox"]["bottom"] == _um(8.0)
    assert report["erc_status"] == "violations"


def test_a_selection_that_keeps_no_well_is_degenerate(tmp_path):
    """The other endpoint, and the more dangerous one: a selector matching
    no well at all leaves the per-well loop with nothing to iterate, so the
    run emits zero `erc.missing_tie` findings because nothing was examined.
    That is the absence-of-evidence pass this whole family of tests exists
    to refuse."""
    report = _run_two_class_well(
        tmp_path,
        "two_class_keeps_none",
        [_two_class_ties(base_overrides={"well_requires": ["98/0"]})[1]],
    )

    assert [f for f in report["erc_findings"] if f["rule"] == "erc.missing_tie"] == []
    assert _tie_skips(report) == {_BASE_ID: "degenerate_well_selection"}
    assert report["erc_status"] == "clean_partial"


def test_an_excludes_that_removes_every_well_is_degenerate(tmp_path):
    """`well_excludes` is held to the same bar from the other side: an
    exclusion that removes every shape selects nothing."""
    report = _run_two_class_well(
        tmp_path,
        "two_class_excludes_all",
        [_two_class_ties(base_overrides={"well_excludes": ["12/0"]})[1]],
    )

    assert _tie_skips(report) == {_BASE_ID: "degenerate_well_selection"}


def test_the_selection_degeneracy_reason_is_distinct_from_the_tap_one(tmp_path):
    """Two different defects, two different remedies: "your marker does not
    partition this well layer" must not render as "narrow your tap". The
    well test is applied first, for the same reason #2255's is -- the tap
    narrowing is measured *inside* the well region, so on a well nobody
    selected the tap answer is about the wrong set of shapes."""
    tie = _two_class_ties(base_overrides={"well_requires": ["12/0"]})[1]
    del tie["tap_requires"]  # tap-degenerate as well
    report = _run_two_class_well(tmp_path, "two_class_both", [tie])

    assert _tie_skips(report) == {_BASE_ID: "degenerate_well_selection"}


def test_two_ties_on_one_well_layer_are_graded_independently(tmp_path):
    """Coverage is keyed on the tie `name`, not on the `well_layer` the two
    entries share: one well-formed selection and one degenerate selection in
    the same run land in different buckets."""
    report = _run_two_class_well(
        tmp_path,
        "two_class_mixed",
        _two_class_ties(
            body_overrides={"well_excludes": ["14/0"]},
            base_overrides={"well_requires": ["12/0"]},
        ),
    )

    assert _BODY_ID in report["erc_coverage"]["checked"]
    assert _tie_skips(report) == {_BASE_ID: "degenerate_well_selection"}


def test_a_tie_with_no_well_selection_is_never_selection_degenerate(tmp_path):
    """The additive-when-unused guarantee, and the reason this test is
    presence-gated rather than purely geometric: a tie with no well-side
    selector claims *every* shape on its layer, which is the strongest
    claim available -- not an unfalsifiable one. Every spec written before
    this key existed grades exactly as it did."""
    report = _run_routed_tie(
        tmp_path, "no_well_selection", _routed_tie_entries(tap_requires=["12/0"])
    )

    assert report["erc_coverage"]["skipped"] == []
    assert report["erc_status"] == "clean"


def test_well_requires_on_an_asserted_substrate_region_is_rejected(tmp_path):
    """A selection picks among *drawn* shapes. An asserted substrate region
    (`well_layer: null` + `well_boxes`) already names exactly the region it
    claims, box by box, so a layer-driven narrowing on top of it would be a
    second, unstated claim -- rejected rather than silently ignored."""
    gds, spec = _native_substrate_spec_error(
        tmp_path,
        "requires_on_assertion",
        {
            "well_layer": None,
            "well_boxes": [[0.0, 0.0, 4.0, 4.0]],
            "well_requires": ["14/0"],
            "tap_layer": "11/0",
            "connect_to": "li1",
            "net": "VSS",
        },
    )
    with pytest.raises(ErcError, match="well_requires selects among the drawn"):
        run_erc(str(gds), str(spec))


def test_well_excludes_on_an_asserted_substrate_region_is_rejected(tmp_path):
    gds, spec = _native_substrate_spec_error(
        tmp_path,
        "excludes_on_assertion",
        {
            "well_layer": None,
            "well_boxes": [[0.0, 0.0, 4.0, 4.0]],
            "well_excludes": ["14/0"],
            "tap_layer": "11/0",
            "connect_to": "li1",
            "net": "VSS",
        },
    )
    with pytest.raises(ErcError, match="well_excludes selects among the drawn"):
        run_erc(str(gds), str(spec))


@pytest.mark.parametrize("key", ["well_requires", "well_excludes"])
def test_a_non_array_well_selection_is_rejected(tmp_path, key):
    gds, spec = _native_substrate_spec_error(
        tmp_path,
        f"non_array_{key}",
        {
            "well_layer": "10/0",
            key: "14/0",
            "tap_layer": "11/0",
            "connect_to": "li1",
            "net": "VSS",
        },
    )
    with pytest.raises(ErcError, match=f"{key} must be an array"):
        run_erc(str(gds), str(spec))


@pytest.mark.parametrize("key", ["well_requires", "well_excludes"])
def test_a_malformed_well_selection_layer_is_rejected(tmp_path, key):
    """The same per-entry validation `tap_requires` applies: a
    silently-dropped layer would weaken a narrowing the caller believes
    they declared."""
    gds, spec = _native_substrate_spec_error(
        tmp_path,
        f"malformed_{key}",
        {
            "well_layer": "10/0",
            key: ["not-a-layer"],
            "tap_layer": "11/0",
            "connect_to": "li1",
            "net": "VSS",
        },
    )
    with pytest.raises(ErcError, match=rf"{key}\[0\]"):
        run_erc(str(gds), str(spec))


@pytest.mark.parametrize("key", ["well_requires", "well_excludes"])
def test_a_null_well_selection_is_the_same_as_omitting_it(tmp_path, key):
    """`null` is the documented "omitted" spelling for every optional list
    on this entry -- it must not become an empty selection that matches
    nothing."""
    report = _run_two_class_well(
        tmp_path,
        f"null_{key}",
        [_two_class_ties(base_overrides={key: None})[1]],
    )

    assert report["erc_coverage"]["skipped"] == []
    assert _BASE_ID in report["erc_coverage"]["checked"]


# --- ties: a well layer with no geometry at all (issue #2377) ------------
#
# The unselected form of `test_a_selection_that_keeps_no_well_is_degenerate`
# above: a `ties[]` entry naming a *drawn* `well_layer` that draws no
# geometry in this stream at all -- a typo'd layer/datatype, a PDK whose tub
# layer number changed, a GDS written without the tub layer -- collapses
# `_tie_findings`'s per-well loop to zero iterations exactly as an empty
# `well_requires`/`well_excludes` selection does. Before this issue, that
# state was graded as a clean pass (`test_ties_tap_without_well_geometry_is_
# clean` above asserted it deliberately); now it is skipped work, like every
# other member of this family.


def test_ties_well_layer_with_no_geometry_is_skipped_not_clean(tmp_path):
    """The reproduction from the issue: `well_layer` names a layer/datatype
    this stream never draws. No `erc.missing_tie` findings are emitted --
    the per-well loop never runs -- but the envelope must not read that as
    "every well is tied"."""
    report = _run_routed_tie(
        tmp_path,
        "empty_well",
        _routed_tie_entries(nwell_layer="98/0", pwell_layer="98/0"),
    )

    assert [f for f in report["erc_findings"] if f["rule"] == "erc.missing_tie"] == []
    assert _tie_skips(report) == {
        'erc.missing_tie:["nwell_tie"]': "empty_well_region",
        'erc.missing_tie:["pwell_tie"]': "empty_well_region",
    }
    assert not any(
        identity.startswith("erc.missing_tie:")
        for identity in report["erc_coverage"]["checked"]
    )
    assert report["erc_status"] == "clean_partial"


def test_a_real_well_layer_is_never_graded_empty(tmp_path):
    """The control: the same spec shape, but naming a well layer this
    stream actually draws. Real geometry must never be misread as an
    absent layer. (`tap_requires` keeps the tap side non-degenerate too, so
    the only variable between this test and the one above is the well
    layer's own geometry.)"""
    report = _run_routed_tie(
        tmp_path, "real_well", _routed_tie_entries(tap_requires=["12/0"])
    )

    assert report["erc_coverage"]["skipped"] == []
    assert {
        'erc.missing_tie:["nwell_tie"]',
        'erc.missing_tie:["pwell_tie"]',
    } <= set(report["erc_coverage"]["checked"])
    assert report["erc_status"] == "clean"


def test_asserted_substrate_region_is_never_graded_empty(tmp_path):
    """The scope boundary the issue calls out explicitly: an asserted
    region (`well_layer: null` + `well_boxes`) can never be empty -- boxes
    are validated non-degenerate at spec-parse time -- so this reason must
    never fire for it, even though its own `well_degenerate` test can (see
    `test_a_whole_extent_well_assertion_is_degenerate_not_evidence` in the
    #2255 section above)."""
    report = _run_native_substrate(
        tmp_path, "asserted_never_empty", [_substrate_tie([_SUBSTRATE_BAND])]
    )

    assert _tie_skips(report) == {}
    assert report["erc_status"] == "clean"


def test_the_empty_well_region_reason_is_distinct_from_the_tap_one(tmp_path):
    """Two different defects, two different remedies: "your well layer
    draws nothing" must not render as "narrow your tap". The well tests are
    applied before the tap test, for the same reason #2199's and #2339's
    are -- the tap narrowing is measured *inside* the well region, so on a
    well that draws nothing the tap answer is about the wrong set of
    shapes."""
    # No `tap_requires` declared, so this tie would *also* be tap-degenerate
    # if the well test did not win first.
    tie = _routed_tie_entries(nwell_layer="98/0", pwell_layer="98/0")[0]
    report = _run_routed_tie(tmp_path, "empty_well_and_tap", [tie])

    assert _tie_skips(report) == {'erc.missing_tie:["nwell_tie"]': "empty_well_region"}


# --- ties: disclosed-unexpressible taps (issue #2234) --------------------


def _run_routed_tie_with_spec_overrides(tmp_path, stem, spec_overrides):
    layout, _top = _routed_tie_layout()
    gds = tmp_path / f"{stem}.gds"
    layout.write(str(gds))
    spec = tmp_path / f"{stem}.erc.json"
    _write_spec(spec, {**_routed_tie_spec(ties=None), **spec_overrides})
    return run_erc(str(gds), str(spec), pdk="sky130")


def test_ties_disclosure_distinguishes_disclosed_from_plain_omission(tmp_path):
    """Two runs that both declare zero `ties[]` must not render
    identically once one of them discloses why it cannot express a tap --
    the whole point of the disclosure form."""
    omitted = _run_routed_tie(tmp_path, "omitted")
    disclosed = _run_routed_tie_with_spec_overrides(
        tmp_path,
        "disclosed",
        {"ties_disclosure": {"reason": "no implant layers are drawn on this stream"}},
    )

    omitted_reasons = {
        r["id"]: r["reason"] for r in omitted["erc_coverage"]["inapplicable"]
    }
    disclosed_reasons = {
        r["id"]: r["reason"] for r in disclosed["erc_coverage"]["inapplicable"]
    }
    assert omitted_reasons["erc.missing_tie:[]"] == "no_ties_declared"
    assert disclosed_reasons["erc.missing_tie:[]"] == "ties_disclosed_unexpressible"

    # Purely a coverage-reason distinction -- findings and status are
    # unaffected either way.
    assert omitted["erc_findings"] == disclosed["erc_findings"] == []
    assert omitted["erc_status"] == disclosed["erc_status"] == "clean"

    assert omitted["ties_disclosure"] is None
    assert disclosed["ties_disclosure"] == {
        "reason": "no implant layers are drawn on this stream"
    }


def test_ties_disclosure_omitted_is_none(tmp_path):
    report = _run_routed_tie(tmp_path, "no_disclosure")
    assert report["ties_disclosure"] is None


def test_ties_disclosure_rejects_missing_reason(tmp_path):
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
            "ties_disclosure": {},
        },
    )
    with pytest.raises(
        ErcError, match="ties_disclosure.reason must be a non-empty string"
    ):
        run_erc(str(gds), str(spec))


def test_ties_disclosure_rejects_non_object(tmp_path):
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
            "ties_disclosure": "no taps on this stream",
        },
    )
    with pytest.raises(ErcError, match="'ties_disclosure' must be a JSON object"):
        run_erc(str(gds), str(spec))


# --- ties: disclosed tool limitation (issue #2247) -----------------------


def test_ties_disclosure_kind_tool_limitation_gets_its_own_coverage_reason(tmp_path):
    """Issue #2247: a run that declares zero `ties[]` because the *build*
    cannot grade a declared tie safely (#2169's unisolated tie extraction
    producing a false `erc.supply_short`) is not the same state as a stream
    that has no tap to name at all -- the two have different remedies, so
    they must not collapse into one coverage reason."""
    unexpressible = _run_routed_tie_with_spec_overrides(
        tmp_path,
        "unexpressible",
        {"ties_disclosure": {"reason": "no implant layers are drawn on this stream"}},
    )
    tool_limited = _run_routed_tie_with_spec_overrides(
        tmp_path,
        "tool_limited",
        {
            "ties_disclosure": {
                "kind": "tool_limitation",
                "reason": (
                    "klayout-tools#2169: ties[] on the pinned release joins the "
                    "well/tap regions into the primary connectivity graph"
                ),
            }
        },
    )

    def _tie_reason(report):
        return {r["id"]: r["reason"] for r in report["erc_coverage"]["inapplicable"]}[
            "erc.missing_tie:[]"
        ]

    assert _tie_reason(unexpressible) == "ties_disclosed_unexpressible"
    assert _tie_reason(tool_limited) == "ties_disclosed_tool_limitation"

    # Still purely a coverage-reason distinction: a disclosure of either
    # kind changes no geometry, no finding, and no roll-up.
    assert unexpressible["erc_findings"] == tool_limited["erc_findings"] == []
    assert unexpressible["erc_status"] == tool_limited["erc_status"] == "clean"


def test_ties_disclosure_kind_is_echoed_only_when_declared(tmp_path):
    """The echo stays verbatim (issue #2247): a pre-#2247 spec that names no
    `kind` gets the byte-identical `{"reason": ...}` echo it always had, and
    one that does name a kind gets it back unchanged."""
    default = _run_routed_tie_with_spec_overrides(
        tmp_path,
        "kindless",
        {"ties_disclosure": {"reason": "no implant layers are drawn on this stream"}},
    )
    assert default["ties_disclosure"] == {
        "reason": "no implant layers are drawn on this stream"
    }

    explicit = _run_routed_tie_with_spec_overrides(
        tmp_path,
        "kinded",
        {
            "ties_disclosure": {
                "reason": "see klayout-tools#2169",
                "kind": "unexpressible",
            }
        },
    )
    assert explicit["ties_disclosure"] == {
        "reason": "see klayout-tools#2169",
        "kind": "unexpressible",
    }


def test_ties_disclosure_kind_unexpressible_is_the_default(tmp_path):
    """Omitting `kind` must keep the exact meaning #2234 gave the key --
    otherwise every already-committed disclosure would silently change
    which obstacle it claims."""
    report = _run_routed_tie_with_spec_overrides(
        tmp_path,
        "default_kind",
        {"ties_disclosure": {"reason": "no implant layers are drawn on this stream"}},
    )
    reasons = {r["id"]: r["reason"] for r in report["erc_coverage"]["inapplicable"]}
    assert reasons["erc.missing_tie:[]"] == "ties_disclosed_unexpressible"


@pytest.mark.parametrize("kind", ["tool-limitation", "", "unknown", True, 1, ["a"]])
def test_ties_disclosure_rejects_an_unknown_kind(tmp_path, kind):
    """A `kind` outside the two documented values is a spec error, not a
    silent fallback to the default: a typo that quietly downgraded a
    tool-limitation disclosure to an unexpressible one would misdirect the
    reader of the report of record."""
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
            "ties_disclosure": {"reason": "because", "kind": kind},
        },
    )
    with pytest.raises(ErcError, match="ties_disclosure.kind must be one of"):
        run_erc(str(gds), str(spec))


def test_ties_disclosure_kind_null_is_the_default(tmp_path):
    """An explicit JSON `null` is "not declared", matching every other
    optional key in this spec -- not a rejected value."""
    report = _run_routed_tie_with_spec_overrides(
        tmp_path,
        "null_kind",
        {"ties_disclosure": {"reason": "no implant layers are drawn", "kind": None}},
    )
    assert report["ties_disclosure"] == {"reason": "no implant layers are drawn"}
    reasons = {r["id"]: r["reason"] for r in report["erc_coverage"]["inapplicable"]}
    assert reasons["erc.missing_tie:[]"] == "ties_disclosed_unexpressible"


def test_cli_text_output_names_a_non_default_disclosure_kind(tmp_path, capsys):
    """Issue #2247: the courtesy view must not render the two disclosure
    kinds identically either -- a human reading the terminal should be able
    to tell a "redraw the layout" case from a "use a different build" one
    without opening the JSON."""
    gds = tmp_path / "basic.gds"
    _basic_fixture(gds)
    spec = tmp_path / "basic.erc.json"
    _write_spec(
        spec,
        {
            "stackup": [
                {"name": "poly", "layer": "1/0", "role": "gate"},
                {"name": "li1", "layer": "3/0"},
            ],
            "ties_disclosure": {
                "kind": "tool_limitation",
                "reason": "pinned klt cannot grade a declared tie (#2169)",
            },
        },
    )

    assert main(["erc", str(gds), str(spec)]) == 3
    out = capsys.readouterr().out
    assert (
        "ties_disclosure (tool_limitation): pinned klt cannot grade a "
        "declared tie (#2169)" in out
    )


def test_ties_rejects_a_non_boolean_tap_is_dedicated(tmp_path):
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
            "ties": [
                {
                    "well_layer": "10/0",
                    "tap_layer": "11/0",
                    "tap_is_dedicated": "yes",
                    "connect_to": "li1",
                    "net": "VDD",
                }
            ],
        },
    )
    with pytest.raises(ErcError, match="tap_is_dedicated must be true or false"):
        run_erc(str(gds), str(spec))


def test_multiple_taps_in_one_well_pass_when_any_reaches_the_declared_net(tmp_path):
    """A well holding several taps is tied as soon as *one* of them reaches
    the declared net -- the check must not depend on which tap it happens
    to look at first."""
    layout, top = _routed_tie_layout()
    # A second genuine VDD tap further along the same n-well.
    top.shapes(layout.layer(11, 0)).insert(
        kdb.Box.new(_um(17), _um(10.2), _um(17.6), _um(10.8))
    )
    top.shapes(layout.layer(12, 0)).insert(
        kdb.Box.new(_um(16.8), _um(10), _um(17.8), _um(11))
    )
    gds = tmp_path / "two_taps.gds"
    layout.write(str(gds))
    spec = tmp_path / "two_taps.erc.json"
    _write_spec(spec, _routed_tie_spec(ties=_routed_tie_entries()))

    report = run_erc(str(gds), str(spec), pdk="sky130")

    assert not any(f["rule"] == "erc.missing_tie" for f in report["erc_findings"])


# --- nets/ties: spec validation -----------------------------------------------


def test_nets_kind_must_be_signal_or_supply(tmp_path):
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
            "nets": [{"name": "VDD", "kind": "bogus"}],
        },
    )
    with pytest.raises(ErcError, match="must be 'signal' or 'supply'"):
        run_erc(str(gds), str(spec))


def test_nets_rejects_duplicate_names(tmp_path):
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
            "nets": [{"name": "VDD"}, {"name": "VDD"}],
        },
    )
    with pytest.raises(ErcError, match="duplicate net name"):
        run_erc(str(gds), str(spec))


def test_ties_requires_connect_to_naming_a_stackup_entry(tmp_path):
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
            "ties": [
                {
                    "well_layer": "10/0",
                    "tap_layer": "11/0",
                    "connect_to": "nope",
                    "net": "VDD",
                }
            ],
        },
    )
    with pytest.raises(ErcError, match="connect_to must name a 'stackup' entry"):
        run_erc(str(gds), str(spec))


def test_ties_rejects_non_array_tap_requires(tmp_path):
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
            "ties": [
                {
                    "well_layer": "10/0",
                    "tap_layer": "11/0",
                    "tap_requires": "12/0",
                    "connect_to": "li1",
                    "net": "VDD",
                }
            ],
        },
    )
    with pytest.raises(ErcError, match="tap_requires must be an array"):
        run_erc(str(gds), str(spec))


def test_ties_rejects_malformed_tap_requires_layer(tmp_path):
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
            "ties": [
                {
                    "well_layer": "10/0",
                    "tap_layer": "11/0",
                    "tap_requires": ["nope"],
                    "connect_to": "li1",
                    "net": "VDD",
                }
            ],
        },
    )
    with pytest.raises(ErcError, match=r"tap_requires\[0\]"):
        run_erc(str(gds), str(spec))


def test_ties_rejects_duplicate_names(tmp_path):
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
            "ties": [
                {
                    "name": "t0",
                    "well_layer": "10/0",
                    "tap_layer": "11/0",
                    "connect_to": "li1",
                    "net": "VDD",
                },
                {
                    "name": "t0",
                    "well_layer": "12/0",
                    "tap_layer": "13/0",
                    "connect_to": "li1",
                    "net": "VSS",
                },
            ],
        },
    )
    with pytest.raises(ErcError, match="duplicate tie name"):
        run_erc(str(gds), str(spec))


def test_ties_missing_required_field_raises(tmp_path):
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
            "ties": [{"well_layer": "10/0", "tap_layer": "11/0", "connect_to": "li1"}],
        },
    )
    with pytest.raises(ErcError, match="missing 'net'"):
        run_erc(str(gds), str(spec))


# --- CLI ----------------------------------------------------------------


def test_cli_json_contract(tmp_path, capsys):
    gds = tmp_path / "basic.gds"
    spec = tmp_path / "basic.erc.json"
    _basic_fixture(gds)
    _basic_spec(spec)

    assert main(["erc", str(gds), str(spec), "--format", "json"]) == 3
    data = json.loads(capsys.readouterr().out)

    assert set(data.keys()) == {
        "coverage",
        "erc_coverage",
        "schema_version",
        "file",
        "spec",
        "pdk",
        "findings_only",
        "gate_role",
        "gate_count",
        "gates",
        "erc_findings",
        "erc_finding_count",
        "erc_status",
        "status",
        "provenance",
        "ties_disclosure",
    }
    assert data["schema_version"] == 1
    assert data["pdk"] is None
    assert data["gate_count"] == 2
    assert data["ties_disclosure"] is None


def test_cli_text_output(tmp_path, capsys):
    gds = tmp_path / "basic.gds"
    spec = tmp_path / "basic.erc.json"
    _basic_fixture(gds)
    _basic_spec(spec)

    assert main(["erc", str(gds), str(spec)]) == 3
    out = capsys.readouterr().out
    assert "gates: 2" in out
    assert "GATE_A" in out
    assert "met2: step=" in out


def test_cli_text_output_prints_the_ties_disclosure(tmp_path, capsys):
    """Issue #2234: the courtesy view must not render a stream that
    disclosed why it cannot express a tap identically to one that never
    declared `ties` at all -- which is the whole point of the disclosure
    form, and would be lost if it lived only in the JSON payload."""
    gds = tmp_path / "basic.gds"
    _basic_fixture(gds)
    spec = tmp_path / "basic.erc.json"
    _write_spec(
        spec,
        {
            "stackup": [
                {"name": "poly", "layer": "1/0", "role": "gate"},
                {"name": "li1", "layer": "3/0"},
            ],
            "ties_disclosure": {"reason": "no implant layers are drawn"},
        },
    )

    assert main(["erc", str(gds), str(spec)]) == 3
    out = capsys.readouterr().out
    assert "ties_disclosure: no implant layers are drawn" in out

    # ... and a spec that declared none prints no such line at all.
    plain = tmp_path / "plain.erc.json"
    _basic_spec(plain)
    assert main(["erc", str(gds), str(plain)]) == 3
    assert "ties_disclosure:" not in capsys.readouterr().out


def test_cli_text_output_prints_island_locations(tmp_path, capsys):
    """Issue #2194: the text courtesy view carries the per-island boxes
    too, so a human reading the terminal output does not have to re-run
    with `--format json` just to find out where to look."""
    layout, top, poly, li1, label = _nets_fixture_layout()
    top.shapes(li1).insert(kdb.Box.new(_um(5), _um(0), _um(6), _um(1)))
    top.shapes(label).insert(kdb.Text("VDD", kdb.Trans(_um(5.5), _um(0.5))))
    top.shapes(li1).insert(kdb.Box.new(_um(8), _um(0), _um(9), _um(1)))
    top.shapes(label).insert(kdb.Text("VDD", kdb.Trans(_um(8.5), _um(0.5))))

    gds = tmp_path / "split.gds"
    layout.write(str(gds))
    spec = tmp_path / "split.erc.json"
    _write_spec(spec, _nets_spec(nets=[{"name": "VDD", "kind": "supply"}]))

    assert main(["erc", str(gds), str(spec)]) == 3
    out = capsys.readouterr().out
    assert "island 1: (5000,0)-(6000,1000)  layer=li1  shapes=1" in out
    assert "island 2: (8000,0)-(9000,1000)  layer=li1  shapes=1" in out


def test_cli_pdk_json_contract(tmp_path, capsys):
    gds = tmp_path / "basic.gds"
    spec = tmp_path / "basic.erc.json"
    _basic_fixture(gds)
    _basic_spec(spec)

    assert (
        main(["erc", str(gds), str(spec), "--pdk", "sky130", "--format", "json"]) == 3
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

    assert main(["erc", str(gds), str(spec), "--pdk", "sky130"]) == 3
    out = capsys.readouterr().out
    assert "pdk: sky130" in out
    assert "antenna_verdict=" in out
    assert "verdict=" in out


def test_cli_text_output_prints_remedy_for_violating_level(tmp_path, capsys):
    gds = tmp_path / "li1_violate.gds"
    spec = tmp_path / "basic.erc.json"
    _basic_spec(spec)
    _antenna_fixture(gds, li1_um2=80.0)

    assert main(["erc", str(gds), str(spec), "--pdk", "sky130"]) == 3
    out = capsys.readouterr().out
    assert "verdict=violate" in out
    assert "remedy: layer_jumping -> met1" in out


def test_cli_json_contract_includes_remedy_field(tmp_path, capsys):
    gds = tmp_path / "li1_violate.gds"
    spec = tmp_path / "basic.erc.json"
    _basic_spec(spec)
    _antenna_fixture(gds, li1_um2=80.0)

    assert (
        main(["erc", str(gds), str(spec), "--pdk", "sky130", "--format", "json"]) == 3
    )
    data = json.loads(capsys.readouterr().out)
    li1_level = next(lvl for lvl in data["gates"][0]["levels"] if lvl["layer"] == "li1")
    assert li1_level["verdict"] == "violate"
    assert li1_level["remedy"]["type"] == "layer_jumping"
    assert li1_level["remedy"]["target_layer"] == "met1"


def test_cli_exit_code_zero_for_clean_partial_status(tmp_path, capsys):
    """AC (issue #2109/#2115): `status: "clean_partial"` exits `0`, matching
    the common rollup rule's exit code for a partial-but-successful run --
    not `3` (findings) and not `4` (no verdict reached)."""
    gds = tmp_path / "full_stack_pass.gds"
    spec = tmp_path / "full_stack.erc.json"
    _full_stack_spec(spec)
    _antenna_fixture(gds, li1_um2=1.0, met1_um2=1.0, met2_um2=1.0)

    assert (
        main(["erc", str(gds), str(spec), "--pdk", "sky130", "--format", "json"]) == 0
    )
    data = json.loads(capsys.readouterr().out)
    assert data["status"] == "clean_partial"


def test_cli_exit_code_four_for_not_checked_status(tmp_path, capsys):
    """AC (issue #2109/#2115): the zero-check control's exit code, `4`."""
    gds = tmp_path / "unchecked.gds"
    spec = tmp_path / "basic.erc.json"
    _basic_spec(spec)
    _antenna_fixture(gds, li1_um2=0.2, met1_um2=1.0, met2_um2=1.0)

    assert main(["erc", str(gds), str(spec), "--format", "json"]) == 4
    data = json.loads(capsys.readouterr().out)
    assert data["status"] == "not_checked"


def test_cli_exit_code_is_still_four_for_a_connectivity_clean_run(tmp_path, capsys):
    """AC2 (issue #2179): the exit code is the *antenna* answer, and adding
    `erc_status` did not move it. A run with no antenna table still exits
    `4`; a caller that only wants the connectivity read reads `erc_status`
    off the payload rather than special-casing the exit code."""
    gds = tmp_path / "unchecked.gds"
    spec = tmp_path / "basic.erc.json"
    _basic_spec(spec)
    _antenna_fixture(gds, li1_um2=0.2, met1_um2=1.0, met2_um2=1.0)

    assert main(["erc", str(gds), str(spec), "--format", "json"]) == 4
    data = json.loads(capsys.readouterr().out)
    assert data["status"] == "not_checked"
    assert data["erc_status"] == "clean"


def test_cli_text_output_prints_the_connectivity_verdict(tmp_path, capsys):
    gds = tmp_path / "basic.gds"
    spec = tmp_path / "basic.erc.json"
    _basic_fixture(gds)
    _basic_spec(spec)

    assert main(["erc", str(gds), str(spec)]) == 3
    out = capsys.readouterr().out
    assert "erc_status: violations" in out


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


# --- run_erc: `poly ∩ diff` gate-area fix (issue #1979) ---------------------
#
# `klt erc`'s antenna check previously counted *any* net touching the
# declared gate-role layer as a gate, including a tie-cell/decap/filler-cell
# poly resistor body that never overlaps diffusion (no gate oxide, no
# antenna mechanism). `stackup[0].active_layer` (optional) fixes this by
# computing gate identification and the antenna-ratio denominator from
# `poly ∩ diff` instead of raw poly-net area. The fixtures below are
# constructed programmatically; the real checked-in
# `sky130_fd_sc_hd__conb_1` corpus cell is replayed in its own block further
# down ("the real corpus cell the constructed fixture stood in for",
# issue #1986).


def _antenna_spec_variant(*, active_layer: str | None) -> dict:
    """A minimal poly->li1 stackup (no met1/met2 needed -- every test below
    only cares about the li1-level antenna ratio, matching the real
    tie-cell defect the issue reports), with `stackup[0].active_layer` set
    to `active_layer` when given, omitted entirely otherwise.
    """
    stackup0: dict = {"name": "poly", "layer": "1/0", "role": "gate"}
    if active_layer is not None:
        stackup0["active_layer"] = active_layer
    return {
        "stackup": [
            stackup0,
            {"name": "li1", "layer": "3/0", "label_layer": "3/5"},
        ],
        "vias": [
            {"name": "licon", "layer": "2/0", "between": ["poly", "li1"]},
        ],
    }


def _tie_cell_and_real_gate_fixture(path, *, li1_um2: float = 80.0) -> None:
    """Two independent gate nets sharing the exact same (deliberately
    antenna-violating, `li1_um2` wide) li1 rail width, in separate y-bands
    so they never physically touch:

    - **`REAL`** (y = [0, 1]) -- poly gate fully covered by diffusion (layer
      8/0): a genuine transistor gate, `poly ∩ diff == poly`.
    - **`TIE`** (y = [10, 11]) -- poly resistor body with no diffusion
      anywhere: the tie-cell/decap/filler-cell case issue #1979 fixes,
      `poly ∩ diff == 0`.

    Without `active_layer`, both report the identical (false, for `TIE`)
    li1 antenna-ratio violation -- issue #1979's bug, preserved as
    documented legacy behaviour when the field is omitted. With it, `REAL`'s
    verdict is unchanged (the required positive control) while `TIE` is
    excluded from `gates[]` entirely.
    """
    layout = kdb.Layout()
    layout.dbu = DBU
    top = layout.create_cell("TOP")

    poly = layout.layer(1, 0)
    licon = layout.layer(2, 0)
    li1 = layout.layer(3, 0)
    li1_label = layout.layer(3, 5)
    diff = layout.layer(8, 0)

    # REAL: poly gate fully over diffusion.
    top.shapes(poly).insert(kdb.Box.new(_um(0), _um(0), _um(1), _um(1)))
    top.shapes(diff).insert(kdb.Box.new(_um(0), _um(0), _um(1), _um(1)))
    top.shapes(licon).insert(kdb.Box.new(_um(0.02), _um(0.02), _um(0.05), _um(0.05)))
    top.shapes(li1).insert(kdb.Box.new(_um(0), _um(0), _um(li1_um2), _um(1)))
    top.shapes(li1_label).insert(kdb.Text("REAL", kdb.Trans(_um(0.5), _um(0.5))))

    # TIE: poly resistor body, no diffusion anywhere -- offset in y so its
    # own li1 rail never touches REAL's.
    top.shapes(poly).insert(kdb.Box.new(_um(0), _um(10), _um(1), _um(11)))
    top.shapes(licon).insert(kdb.Box.new(_um(0.02), _um(10.02), _um(0.05), _um(10.05)))
    top.shapes(li1).insert(kdb.Box.new(_um(0), _um(10), _um(li1_um2), _um(11)))
    top.shapes(li1_label).insert(kdb.Text("TIE", kdb.Trans(_um(0.5), _um(10.5))))

    layout.write(str(path))


def test_active_layer_omitted_reproduces_false_tie_cell_violation(tmp_path):
    """Baseline/regression: without `active_layer`, a poly-only tie-cell
    resistor sharing a wide, genuinely-violating li1 rail is reported as a
    `gates[]` entry and produces the exact same false `violate` verdict as
    the real gate on the same rail width -- issue #1979's bug, preserved as
    documented legacy behaviour when the field is omitted.
    """
    gds = tmp_path / "fixture.gds"
    spec = tmp_path / "spec.json"
    _tie_cell_and_real_gate_fixture(gds, li1_um2=80.0)
    _write_spec(spec, _antenna_spec_variant(active_layer=None))

    report = run_erc(str(gds), str(spec), pdk="sky130")
    assert report["gate_count"] == 2

    tie = next(g for g in report["gates"] if g["net"] == "TIE")
    assert tie["gate_area_um2"] == pytest.approx(1.0)
    tie_li1 = next(lvl for lvl in tie["levels"] if lvl["layer"] == "li1")
    assert tie_li1["verdict"] == "violate"
    assert tie["antenna_verdict"] == "violate"

    real = next(g for g in report["gates"] if g["net"] == "REAL")
    real_li1 = next(lvl for lvl in real["levels"] if lvl["layer"] == "li1")
    assert real_li1["verdict"] == "violate"


def test_active_layer_excludes_tie_cell_resistor_from_gates(tmp_path):
    """AC: with `stackup[0].active_layer` supplied, the tie-cell's poly
    resistor (`poly ∩ diff == 0`) is no longer in `gates[]` and no longer
    produces a `violate` verdict.
    """
    gds = tmp_path / "fixture.gds"
    spec = tmp_path / "spec.json"
    _tie_cell_and_real_gate_fixture(gds, li1_um2=80.0)
    _write_spec(spec, _antenna_spec_variant(active_layer="8/0"))

    report = run_erc(str(gds), str(spec), pdk="sky130")

    assert report["gate_count"] == 1
    assert all(g["net"] != "TIE" for g in report["gates"])


def test_active_layer_leaves_positive_control_gate_violating_before_and_after(
    tmp_path,
):
    """AC: a real gate net (`poly ∩ diff > 0`) is unaffected -- a positive-
    control regression that must still violate whether or not
    `active_layer` is supplied, so the fix cannot silently stop catching
    genuine antenna violations.
    """
    gds = tmp_path / "fixture.gds"
    _tie_cell_and_real_gate_fixture(gds, li1_um2=80.0)

    no_active_spec = tmp_path / "no_active.json"
    _write_spec(no_active_spec, _antenna_spec_variant(active_layer=None))
    before = run_erc(str(gds), str(no_active_spec), pdk="sky130")
    real_before = next(g for g in before["gates"] if g["net"] == "REAL")
    assert real_before["antenna_verdict"] == "violate"

    active_spec = tmp_path / "active.json"
    _write_spec(active_spec, _antenna_spec_variant(active_layer="8/0"))
    after = run_erc(str(gds), str(active_spec), pdk="sky130")
    real_after = next(g for g in after["gates"] if g["net"] == "REAL")
    assert real_after["antenna_verdict"] == "violate"
    # Diffusion fully covers this gate's poly, so its area is unchanged.
    assert real_after["gate_area_um2"] == pytest.approx(real_before["gate_area_um2"])


def _conb1_like_fixture(path, *, poly_width_um: float, li1_width_um: float) -> None:
    """A single-net poly-resistor-plus-rail layout sized to land in the same
    order of magnitude as the real `sky130_fd_sc_hd__conb_1` measurements
    issue #1979 reports (net `'HI,VPWR'`, gate area ~1.2048 um^2, li1
    antenna ratio in the 81-87 band) -- not a replay of the literal corpus
    GDS (not checked into this repo), but the same shape of defect.
    """
    layout = kdb.Layout()
    layout.dbu = DBU
    top = layout.create_cell("TOP")

    poly = layout.layer(1, 0)
    licon = layout.layer(2, 0)
    li1 = layout.layer(3, 0)
    li1_label = layout.layer(3, 5)

    top.shapes(poly).insert(kdb.Box.new(_um(0), _um(0), _um(poly_width_um), _um(1)))
    top.shapes(licon).insert(kdb.Box.new(_um(0.02), _um(0.02), _um(0.05), _um(0.05)))
    top.shapes(li1).insert(kdb.Box.new(_um(0), _um(0), _um(li1_width_um), _um(1)))
    top.shapes(li1_label).insert(kdb.Text("HI,VPWR", kdb.Trans(_um(0.5), _um(0.5))))

    layout.write(str(path))


def test_active_layer_conb1_like_reproduction_violates_without_active_layer(tmp_path):
    gds = tmp_path / "conb1_like.gds"
    # `antenna_ratio` is `cumulative_area_um2 / gate_area_um2`, and
    # `cumulative_area_um2` at the li1 level includes the gate (poly) level's
    # own area too (see `run_erc`'s running-sum accumulation) -- so
    # `li1_width_um` here is sized to land the *combined* poly+li1
    # cumulative area, not the li1 rail area alone, in the reported 81-87
    # ratio band.
    _conb1_like_fixture(gds, poly_width_um=1.2048, li1_width_um=103.22)

    spec = tmp_path / "spec.json"
    _write_spec(spec, _antenna_spec_variant(active_layer=None))

    report = run_erc(str(gds), str(spec), pdk="sky130")
    gate = report["gates"][0]
    assert gate["net"] == "HI,VPWR"
    # DBU is 0.001 um, so the drawn 1.2048 um width snaps to the nearest
    # grid point (1.205) -- assert against that same tolerance.
    assert gate["gate_area_um2"] == pytest.approx(1.2048, abs=2e-3)
    li1_level = next(lvl for lvl in gate["levels"] if lvl["layer"] == "li1")
    assert 81.0 <= li1_level["antenna_ratio"] <= 87.0
    assert li1_level["verdict"] == "violate"


def test_active_layer_conb1_like_reproduction_excluded_with_active_layer(tmp_path):
    """No diffusion is drawn anywhere in this fixture -- with
    `active_layer` supplied, the poly resistor net is excluded entirely,
    leaving no gate net at all in this single-net layout.
    """
    gds = tmp_path / "conb1_like.gds"
    _conb1_like_fixture(gds, poly_width_um=1.2048, li1_width_um=103.22)

    spec = tmp_path / "spec.json"
    _write_spec(spec, _antenna_spec_variant(active_layer="8/0"))

    with pytest.raises(ErcError, match="no net"):
        run_erc(str(gds), str(spec), pdk="sky130")


# --- run_erc: the real corpus cell the constructed fixture stood in for -----
#
# Issue #1986: `sky130_fd_sc_hd__conb_1` itself, checked into
# `tests/corpus/sky130/` (verbatim from the same pinned upstream commit as
# the rest of that corpus -- see tests/corpus/README.md), replays the exact
# measurement issue #1979 reports from a real routed design: the tie cell's
# `HI` net -- a 1.2048 um^2 poly resistor with `poly ∩ diff` structurally 0
# (the cell draws no diffusion at all) -- joined to a synthetic VPWR rail
# reproduces the real design's false gate signature
# `('HI,VPWR', 1.2048, 86.66)` bit for bit under the raw-poly gate model.
# The synthetic `VPWR` rail is sized so the shared net's li1 cumulative
# area lands the li1 antenna ratio at 86.66, the top of the 56-87 band the
# issue measured on real one-tie-cell rails, and the merged net name
# `HI,VPWR` (in-cell `HI` label + synthetic `VPWR` label on one net) is the
# exact spelling the real design reported.


def _conb1_corpus_cell(path: Path) -> None:
    """One `TOP` cell instantiating the checked-in real `conb_1` verbatim,
    plus the two pieces of synthetic geometry the real design supplied
    around each tie cell:

    - a `VPWR`-labelled li1 rail extending the cell's own top li1 rail
      (the `HI` net) far enough to the right that the shared net's li1
      cumulative area puts the raw-poly antenna ratio at 86.66 -- the real
      design's row rail, shrunk to one tie cell's worth;
    - a `REAL` positive-control gate (poly fully over diffusion, its own
      80 um^2 li1 rail, ratio 81 against sky130's 75 limit) in a separate
      y-band, so a gate-model change cannot silently break genuine gates
      while the tie-cell nets come and go.
    """
    layout = kdb.Layout()
    layout.dbu = DBU
    top = layout.create_cell("TOP")

    cell_layout = kdb.Layout()
    cell_layout.read(str(CORPUS_DIR / "sky130" / "sky130_fd_sc_hd__conb_1.gds"))
    conb = layout.create_cell(cell_layout.top_cell().name)
    conb.copy_tree(cell_layout.top_cell())
    top.insert(kdb.CellInstArray(conb.cell_index(), kdb.Trans()))

    poly = layout.layer(66, 20)
    licon1 = layout.layer(66, 44)
    li1 = layout.layer(67, 20)
    li1_label = layout.layer(67, 5)
    diff = layout.layer(65, 20)

    # The cell's own top li1 rail (its `HI` net) spans x 0..1.38 um,
    # y 1.91..2.805 um (dbu, from the checked-in cell). Extending it by
    # 113.922 um of 0.895 um-tall rail adds 101.96019 um^2, which with the
    # in-cell li1 (1.24605 um^2) and the 1.2048 um^2 poly resistor lands
    # the li1 cumulative ratio at (1.2048 + 1.24605 + 101.96019) / 1.2048
    # = 86.66 (issue #1979's real-design measurement).
    top.shapes(li1).insert(kdb.Box(1380, 1910, 1380 + 113922, 2805))
    top.shapes(li1_label).insert(kdb.Text("VPWR", kdb.Trans(_um(1.5), _um(2.4))))

    # The REAL positive control, in its own y-band (um -10..-9).
    y0 = _um(-10)
    top.shapes(poly).insert(kdb.Box(_um(0), y0, _um(1), y0 + _um(1)))
    top.shapes(diff).insert(kdb.Box(_um(0), y0, _um(1), y0 + _um(1)))
    top.shapes(licon1).insert(
        kdb.Box(_um(0.02), y0 + _um(0.02), _um(0.05), y0 + _um(0.05))
    )
    top.shapes(li1).insert(kdb.Box(_um(0), y0, _um(80), y0 + _um(1)))
    top.shapes(li1_label).insert(kdb.Text("REAL", kdb.Trans(_um(0.5), y0 + _um(0.5))))

    layout.write(str(path))


def _conb1_corpus_spec(*, active_layer: str | None) -> dict:
    """The poly -> li1 stackup with the real cell's own sky130 layer
    numbers (poly 66/20, licon1 66/44, li1 67/20, li1 label 67/5), with
    `stackup[0].active_layer` set to the given diffusion layer (sky130's
    diff.drawing, 65/20) when given."""
    stackup0: dict = {"name": "poly", "layer": "66/20", "role": "gate"}
    if active_layer is not None:
        stackup0["active_layer"] = active_layer
    return {
        "stackup": [
            stackup0,
            {"name": "li1", "layer": "67/20", "label_layer": "67/5"},
        ],
        "vias": [
            {"name": "licon1", "layer": "66/44", "between": ["poly", "li1"]},
        ],
    }


def test_conb1_corpus_cell_reproduces_the_real_design_gate_signature(
    tmp_path,
):
    """The golden artifact (issue #1986): under the raw-poly gate model
    (no `active_layer`), the real checked-in tie cell joined to its
    synthetic row rail reports exactly the false gate the real design
    carried -- net `'HI,VPWR'`, gate area 1.2048 um^2, li1 antenna ratio
    86.66, `verdict: "violate"` against sky130's 75 limit. The `REAL`
    positive-control gate violates on the same run, so the fixture can
    never degrade into "the model catches nothing at all"."""
    gds = tmp_path / "conb1_corpus.gds"
    _conb1_corpus_cell(gds)
    spec = tmp_path / "spec.json"
    _write_spec(spec, _conb1_corpus_spec(active_layer=None))

    report = run_erc(str(gds), str(spec), pdk="sky130")

    gate = next(g for g in report["gates"] if g["net"] == "HI,VPWR")
    assert gate["gate_area_um2"] == pytest.approx(1.2048, abs=2e-3)
    li1_level = next(lvl for lvl in gate["levels"] if lvl["layer"] == "li1")
    assert li1_level["antenna_ratio"] == pytest.approx(86.66, abs=0.01)
    assert li1_level["verdict"] == "violate"
    assert gate["antenna_verdict"] == "violate"

    real = next(g for g in report["gates"] if g["net"] == "REAL")
    real_li1 = next(lvl for lvl in real["levels"] if lvl["layer"] == "li1")
    assert real_li1["verdict"] == "violate"


def test_conb1_corpus_cell_tie_nets_excluded_by_active_layer_real_gate_survives(
    tmp_path,
):
    """With `active_layer` supplied, the real cell's tie nets are excluded
    from `gates[]` -- the checked-in cell draws no diffusion at all, so
    *every* net it contributes has `poly ∩ diff == 0`, tie resistor and
    `LO` net alike -- while the `REAL` positive control survives with its
    verdict unchanged. The corpus cell, not the synthetic stand-in, is
    what pins this: it is the exact geometry a real design reported."""
    gds = tmp_path / "conb1_corpus.gds"
    _conb1_corpus_cell(gds)
    spec = tmp_path / "spec.json"
    _write_spec(spec, _conb1_corpus_spec(active_layer="65/20"))

    report = run_erc(str(gds), str(spec), pdk="sky130")

    assert [g["net"] for g in report["gates"]] == ["REAL"]
    real = report["gates"][0]
    real_li1 = next(lvl for lvl in real["levels"] if lvl["layer"] == "li1")
    assert real_li1["verdict"] == "violate"


def test_inv1_corpus_cell_real_gate_violates_before_and_after_active_layer(
    tmp_path,
):
    """The issue's second real-gate positive control (issue #1986): the
    real `sky130_fd_sc_hd__inv_1` corpus cell with a 102 um^2 li1 rail
    joined to its `A` input -- the shape the real design's *genuine* gate
    violations had. `A` is a real transistor gate (`poly ∩ diff > 0`), so
    it must stay in `gates[]` and keep violating whether or not
    `active_layer` is supplied -- a gate-model change cannot silently
    stop catching real gates. The computed gate area *does* move (raw
    poly 0.4689 um^2 -> `poly ∩ diff` 0.2475 um^2, the inverter's true
    gate oxide), which is exactly the fix working, not the gate
    disappearing."""
    gds = tmp_path / "inv1_corpus.gds"

    layout = kdb.Layout()
    layout.dbu = DBU
    top = layout.create_cell("TOP")
    cell_layout = kdb.Layout()
    cell_layout.read(str(CORPUS_DIR / "sky130" / "sky130_fd_sc_hd__inv_1.gds"))
    inv = layout.create_cell(cell_layout.top_cell().name)
    inv.copy_tree(cell_layout.top_cell())
    top.insert(kdb.CellInstArray(inv.cell_index(), kdb.Trans()))

    # The cell's `A` li1 pad is the (320,1075)..(650,1315) dbu strip;
    # extending it 425 um to the left at 0.24 um tall adds exactly
    # 102 um^2 of li1 to the `A` net -- the issue's own number.
    top.shapes(layout.layer(67, 20)).insert(kdb.Box(320 - 425000, 1075, 320, 1315))
    layout.write(str(gds))

    ratios = {}
    for label, active in (("before", None), ("after", "65/20")):
        spec = tmp_path / f"spec_{label}.json"
        _write_spec(spec, _conb1_corpus_spec(active_layer=active))
        report = run_erc(str(gds), str(spec), pdk="sky130")
        gate = next(g for g in report["gates"] if g["net"] == "A")
        li1_level = next(lvl for lvl in gate["levels"] if lvl["layer"] == "li1")
        assert li1_level["verdict"] == "violate"
        ratios[label] = (gate["gate_area_um2"], li1_level["antenna_ratio"])

    # Both runs violate; the areas differ exactly as the fix dictates.
    assert ratios["before"][0] == pytest.approx(0.4689, abs=2e-3)
    assert ratios["after"][0] == pytest.approx(0.2475, abs=2e-3)
    assert ratios["after"][1] > ratios["before"][1]


def _mixed_gate_and_resistor_fixture(path) -> None:
    """One net with two poly shapes tied together by a single continuous
    li1 strip: a real gate portion (0,0)-(0.5,1), over diffusion, and a
    separate resistor-body portion (2,0)-(2.5,1), with no diffusion under
    it. Raw poly area on this net is 1.0 um^2; `poly ∩ diff` is only
    0.5 um^2 -- the "mixed" edge case from issue #1979's test plan: a net
    with both a resistor body and a genuine gate tied together must still
    count as a gate, with its antenna ratio computed off the intersection
    only, not the combined raw poly area.
    """
    layout = kdb.Layout()
    layout.dbu = DBU
    top = layout.create_cell("TOP")

    poly = layout.layer(1, 0)
    licon = layout.layer(2, 0)
    li1 = layout.layer(3, 0)
    diff = layout.layer(8, 0)

    top.shapes(poly).insert(kdb.Box.new(_um(0), _um(0), _um(0.5), _um(1)))
    top.shapes(diff).insert(kdb.Box.new(_um(0), _um(0), _um(0.5), _um(1)))
    top.shapes(poly).insert(kdb.Box.new(_um(2), _um(0), _um(2.5), _um(1)))

    top.shapes(licon).insert(kdb.Box.new(_um(0.1), _um(0.1), _um(0.2), _um(0.2)))
    top.shapes(licon).insert(kdb.Box.new(_um(2.1), _um(0.1), _um(2.2), _um(0.2)))
    # One continuous li1 strip spans and connects both poly shapes into a
    # single net.
    top.shapes(li1).insert(kdb.Box.new(_um(0), _um(0), _um(2.5), _um(1)))

    layout.write(str(path))


def test_active_layer_mixed_gate_and_resistor_net_counts_only_intersection(tmp_path):
    gds = tmp_path / "mixed.gds"
    spec = tmp_path / "spec.json"
    _mixed_gate_and_resistor_fixture(gds)
    _write_spec(spec, _antenna_spec_variant(active_layer="8/0"))

    report = run_erc(str(gds), str(spec))
    assert report["gate_count"] == 1
    # Only the 0.5 um^2 gate portion overlaps diffusion -- the other 0.5
    # um^2 resistor-body portion of the same net contributes nothing.
    assert report["gates"][0]["gate_area_um2"] == pytest.approx(0.5)


def test_active_layer_omitted_mixed_net_uses_raw_combined_poly_area(tmp_path):
    """Without `active_layer`, the same net's `gate_area_um2` is the full
    1.0 um^2 combined raw poly area (both the gate and resistor-body
    portions) -- confirming the fixture change above is attributable to
    the fix, not the fixture itself.
    """
    gds = tmp_path / "mixed.gds"
    spec = tmp_path / "spec.json"
    _mixed_gate_and_resistor_fixture(gds)
    _write_spec(spec, _antenna_spec_variant(active_layer=None))

    report = run_erc(str(gds), str(spec))
    assert report["gate_count"] == 1
    assert report["gates"][0]["gate_area_um2"] == pytest.approx(1.0)


def test_active_layer_rejected_on_non_gate_stackup_entry(tmp_path):
    gds = tmp_path / "basic.gds"
    _basic_fixture(gds)
    spec = tmp_path / "spec.json"
    _write_spec(
        spec,
        {
            "stackup": [
                {"name": "poly", "layer": "1/0", "role": "gate"},
                {"name": "li1", "layer": "3/0", "active_layer": "8/0"},
            ],
        },
    )
    with pytest.raises(ErcError, match=r"active_layer is only valid on stackup\[0\]"):
        run_erc(str(gds), str(spec))


def test_active_layer_malformed_layer_string_raises(tmp_path):
    gds = tmp_path / "basic.gds"
    _basic_fixture(gds)
    spec = tmp_path / "spec.json"
    _write_spec(
        spec,
        {
            "stackup": [
                {
                    "name": "poly",
                    "layer": "1/0",
                    "role": "gate",
                    "active_layer": "nope",
                },
                {"name": "li1", "layer": "3/0"},
            ],
        },
    )
    with pytest.raises(ErcError, match="active_layer"):
        run_erc(str(gds), str(spec))


def test_active_layer_does_not_change_schema_version(tmp_path):
    gds = tmp_path / "fixture.gds"
    spec = tmp_path / "spec.json"
    _tie_cell_and_real_gate_fixture(gds, li1_um2=80.0)
    _write_spec(spec, _antenna_spec_variant(active_layer="8/0"))

    report = run_erc(str(gds), str(spec), pdk="sky130")
    assert report["schema_version"] == 1


# --- devices[]: drawn device bodies are not wires (issue #2183) --------------


def _resistor_divider_fixture(path) -> None:
    """A rail-to-rail poly-resistor string: two labelled `li1` supply rails
    joined *only* through a drawn poly resistor body, contacted at each end
    -- the defining topology of a power-on-reset divider, a brown-out
    detector, or a supply-referenced bias string.

    The layout is DRC-plausible and electrically correct: VDD and VSS are
    two distinct nodes with a resistor between them, not a short. But every
    shape on the path is on a declared conductor role, so without a
    `devices[]` declaration the connectivity model reads the resistor body
    as a wire and reports a false `erc.supply_short` -- the reproduction
    from issue #2183.

    Layer numbers: poly=1/0, contact=2/0, li1=3/0, li1 label=3/5, and the
    PDK device-body marker on 62/0 (gf180mcu's own `Resistor` layer number,
    kept recognisable).
    """
    layout = kdb.Layout()
    layout.dbu = DBU
    top = layout.create_cell("TOP")

    poly = layout.layer(1, 0)
    contact = layout.layer(2, 0)
    li1 = layout.layer(3, 0)
    label = layout.layer(3, 5)
    res_marker = layout.layer(62, 0)

    # The two supply rails, on li1, 6 um apart -- no metal runs between them.
    top.shapes(li1).insert(kdb.Box.new(_um(0), _um(0), _um(2), _um(1)))
    top.shapes(label).insert(kdb.Text("VDD", kdb.Trans(_um(1), _um(0.5))))
    top.shapes(li1).insert(kdb.Box.new(_um(8), _um(0), _um(10), _um(1)))
    top.shapes(label).insert(kdb.Text("VSS", kdb.Trans(_um(9), _um(0.5))))

    # The resistor: one poly bar from rail to rail, contacted at each head.
    top.shapes(poly).insert(kdb.Box.new(_um(1), _um(0.2), _um(9), _um(0.8)))
    top.shapes(contact).insert(kdb.Box.new(_um(1.2), _um(0.3), _um(1.4), _um(0.5)))
    top.shapes(contact).insert(kdb.Box.new(_um(8.6), _um(0.3), _um(8.8), _um(0.5)))

    # The PDK's own device-body marker over the resistor body (not the
    # heads): 6 um x 0.8 um = 4.8 um^2, drawn with the 0.1 um top/bottom
    # enclosure past the 0.6 um-tall poly bar that a PDK device marker
    # conventionally carries -- so only 6 um x 0.6 um = 3.6 um^2 of it is
    # actually subtracted from `poly` (issue #2226).
    top.shapes(res_marker).insert(kdb.Box.new(_um(2), _um(0.1), _um(8), _um(0.9)))

    layout.write(str(path))


#: The `devices[]` declaration matching `_resistor_divider_fixture`.
_RESISTOR_DEVICES = [{"name": "poly_resistor", "body_layer": "62/0", "on": "poly"}]

#: What `_RESISTOR_DEVICES` actually subtracts from `poly`: the 6 um x 0.8 um
#: marker (4.8 um^2 of its own) intersected with the 0.6 um-tall poly bar it
#: is drawn over -> 6 um x 0.6 um. The gap between the two numbers is
#: ordinary marker overhang, and reporting the *intersection* is the point of
#: issue #2226 -- see `test_device_body_area_is_the_intersection_with_its_role`.
_RESISTOR_SUBTRACTED_UM2 = 3.6

#: `_RESISTOR_DEVICES` with the same marker layer additionally declared `on` a
#: role it never touches (the `contact` via role: two 0.2 um x 0.2 um boxes
#: under the resistor heads, outside the marker's own x-range). The issue
#: #2226 reproduction shape: one correct declaration, one wrong-`on` one.
_RESISTOR_DEVICES_WITH_WRONG_ROLE = [
    {"name": "poly_resistor", "body_layer": "62/0", "on": "poly"},
    {"name": "wrong_role_resistor", "body_layer": "62/0", "on": "contact"},
]


def _resistor_divider_spec(devices=None):
    spec = {
        "stackup": [
            {"name": "poly", "layer": "1/0", "role": "gate"},
            {"name": "li1", "layer": "3/0", "label_layer": "3/5"},
        ],
        "vias": [{"name": "contact", "layer": "2/0", "between": ["poly", "li1"]}],
        "nets": [
            {"name": "VDD", "kind": "supply"},
            {"name": "VSS", "kind": "supply"},
        ],
    }
    if devices is not None:
        spec["devices"] = devices
    return spec


def _run_resistor_divider(tmp_path, devices=None, name="divider"):
    gds = tmp_path / f"{name}.gds"
    spec = tmp_path / f"{name}.erc.json"
    _resistor_divider_fixture(gds)
    _write_spec(spec, _resistor_divider_spec(devices))
    return run_erc(str(gds), str(spec))


def test_undeclared_device_body_reports_a_false_supply_short(tmp_path):
    """The bug, reproduced: with no `devices[]` declaration the drawn
    resistor body conducts, so the two rails resolve to one island."""
    report = _run_resistor_divider(tmp_path)

    assert [f["rule"] for f in report["erc_findings"]] == ["erc.supply_short"]
    finding = report["erc_findings"][0]
    assert {finding["net"], finding["other_net"]} == {"VDD", "VSS"}
    assert report["erc_status"] == "violations"


def test_declared_device_body_breaks_the_rail_to_rail_string(tmp_path):
    """The fix: declaring the device-body marker subtracts it from `poly`'s
    connectivity, so the same layout reports zero findings of any rule --
    matching the issue's own isolation experiment (delete only the
    resistor-marked poly -> clean)."""
    report = _run_resistor_divider(tmp_path, devices=_RESISTOR_DEVICES)

    assert report["erc_findings"] == []
    assert report["erc_finding_count"] == 0
    assert report["erc_status"] == "clean"


def test_declared_device_body_keeps_both_supplies_reachable(tmp_path):
    """The carve-out must break the *string*, not the rails: each supply
    still resolves to exactly one island (no `erc.unconnected_net`), and the
    resistor heads still reach their own rail through the contacts."""
    report = _run_resistor_divider(tmp_path, devices=_RESISTOR_DEVICES)

    assert not any(
        f["rule"] in ("erc.unconnected_net", "erc.floating_gate")
        for f in report["erc_findings"]
    )
    # Both resistor heads survive as gate-role geometry, each on its own
    # net now that the body between them is gone.
    assert report["gate_count"] == 2


def test_device_body_subtraction_is_reported_in_provenance(tmp_path):
    report = _run_resistor_divider(tmp_path, devices=_RESISTOR_DEVICES)

    assert report["provenance"]["devices"] == [
        {
            "name": "poly_resistor",
            "body_layer": "62/0",
            "on": "poly",
            "body_area_um2": _RESISTOR_SUBTRACTED_UM2,
        }
    ]


def test_device_body_area_is_the_intersection_with_its_role(tmp_path):
    """Issue #2226: `body_area_um2` is the area this declaration *actually*
    subtracted -- `marker ∩ on`'s own conductor region -- not the marker
    layer's own area.

    The fixture's RES marker is 6 um x 0.8 um (4.8 um^2) drawn over a 0.6
    um-tall poly bar, i.e. with the 0.1 um top/bottom overhang a PDK device
    marker conventionally carries. Only the 6 um x 0.6 um overlap is
    subtracted, so 3.6 is the honest number and 4.8 over-states it."""
    report = _run_resistor_divider(tmp_path, devices=_RESISTOR_DEVICES)
    entry = report["provenance"]["devices"][0]

    assert entry["body_area_um2"] == 3.6
    # The marker's own area, which the pre-#2226 implementation reported.
    assert entry["body_area_um2"] != 4.8
    # And the carve-out still bites: the false rail-to-rail short is gone.
    assert report["erc_findings"] == []


def test_device_body_declared_on_a_role_it_does_not_touch_reports_zero(tmp_path):
    """The issue #2226 reproduction: the *same* marker layer declared twice,
    once `on` the role it is drawn over and once `on` a role it never
    touches. Only the first subtracts anything, and the report now says so
    -- pre-fix both echoed the marker's identical non-zero area, which made
    the field useless as the wrong-`on` cross-check the docs promise."""
    report = _run_resistor_divider(tmp_path, devices=_RESISTOR_DEVICES_WITH_WRONG_ROLE)
    devices_by_name = {d["name"]: d for d in report["provenance"]["devices"]}

    assert devices_by_name["poly_resistor"]["body_area_um2"] == 3.6
    assert devices_by_name["wrong_role_resistor"]["body_area_um2"] == 0.0
    # The wrong-`on` entry is reported, not dropped, and still names the
    # role it was declared on.
    assert devices_by_name["wrong_role_resistor"]["on"] == "contact"
    # It also changed nothing: the `contact` via role is untouched, so the
    # resistor heads still reach their rails.
    assert not any(
        f["rule"] in ("erc.unconnected_net", "erc.floating_gate")
        for f in report["erc_findings"]
    )


def test_device_body_that_misses_its_declared_role_warns_on_stderr(tmp_path, capsys):
    """A marker that *is* drawn on this layout but subtracts nothing from
    the role it was declared `on` is a spec bug, not an unused layer -- so
    it gets a one-line stderr warning as well as a `0.0` in the report
    (issue #2226). JSON goes to stdout only, so the warning cannot corrupt
    a piped report."""
    _run_resistor_divider(tmp_path, devices=_RESISTOR_DEVICES_WITH_WRONG_ROLE)
    err = capsys.readouterr().err

    assert "wrong_role_resistor" in err
    assert "subtracted nothing" in err
    assert "62/0" in err
    # The correct declaration on `poly` is not warned about.
    assert "'poly_resistor'" not in err


# --- stackup label_layer diagnostics (issue #2401) ---------------------------


def test_empty_label_layer_warns_on_stderr(tmp_path, capsys):
    """A `stackup` entry whose declared `label_layer` carries zero text
    objects in the analysed top cell is almost always a spec typo (the
    wrong layer/datatype), but the report renders it exactly like a real
    supply defect -- `null` gate nets plus `erc.unconnected_net` on every
    declared net (issue #2401). One line on stderr names it, mirroring the
    #2226 device-marker warning precedent; JSON goes to stdout only, so a
    piped report stays uncorrupted."""
    layout, top, poly, li1, label = _nets_fixture_layout()
    top.shapes(li1).insert(kdb.Box.new(_um(5), _um(0), _um(6), _um(1)))
    # No text is ever written to `label` (3/5): the entry's declared label
    # layer is genuinely empty of text in this layout.

    gds = tmp_path / "empty-label.gds"
    layout.write(str(gds))
    spec = tmp_path / "empty-label.erc.json"
    _write_spec(spec, _nets_spec(nets=[{"name": "VDD", "kind": "supply"}]))

    report = run_erc(str(gds), str(spec))
    # The silent symptom the issue describes: the declared net matched zero
    # islands, because its label layer carried no text to match from.
    assert any(
        f["rule"] == "erc.unconnected_net" and f["net"] == "VDD"
        for f in report["erc_findings"]
    )
    err = capsys.readouterr().err

    assert "klt erc: warning:" in err
    assert "'li1'" in err
    assert "3/5" in err
    assert "no text" in err
    # Once per affected entry, not once per extraction.
    assert err.count("klt erc: warning:") == 1


def test_empty_label_layer_warns_once_even_with_ties(tmp_path, capsys):
    """The tie graph re-extracts the same `stackup` (issue #2169's second
    `_extract_connectivity` pass); the warning is per affected *entry*, not
    per extraction, so a ties-declaring spec still sees it exactly once
    (issue #2401)."""
    layout, top, poly, li1, label = _nets_fixture_layout()
    top.shapes(li1).insert(kdb.Box.new(_um(5), _um(0), _um(6), _um(1)))

    gds = tmp_path / "ties-empty-label.gds"
    layout.write(str(gds))
    spec = tmp_path / "ties-empty-label.erc.json"
    _write_spec(
        spec,
        _nets_spec(
            nets=[{"name": "VDD", "kind": "supply"}],
            ties=[
                {
                    "well_layer": "10/0",
                    "tap_layer": "11/0",
                    "tap_boxes": [[1.0, 2.0, 3.0, 4.0]],
                    "connect_to": "li1",
                    "net": "VDD",
                }
            ],
        ),
    )

    run_erc(str(gds), str(spec))
    err = capsys.readouterr().err

    assert err.count("klt erc: warning:") == 1


def test_populated_label_layer_does_not_warn_on_stderr(tmp_path, capsys):
    """The warning fires only when the declared label layer is genuinely
    empty of text in this layout: the same fixture with one VDD label on
    3/5 stays silent, so a normally matching declaration is not penalized
    (issue #2401)."""
    layout, top, poly, li1, label = _nets_fixture_layout()
    top.shapes(li1).insert(kdb.Box.new(_um(5), _um(0), _um(6), _um(1)))
    top.shapes(label).insert(kdb.Text("VDD", kdb.Trans(_um(5.5), _um(0.5))))

    gds = tmp_path / "labelled.gds"
    layout.write(str(gds))
    spec = tmp_path / "labelled.erc.json"
    _write_spec(spec, _nets_spec(nets=[{"name": "VDD", "kind": "supply"}]))

    report = run_erc(str(gds), str(spec))
    assert not any(
        f["rule"] == "erc.unconnected_net" and f["net"] == "VDD"
        for f in report["erc_findings"]
    )
    assert "warning" not in capsys.readouterr().err


def test_devices_omitted_reports_an_empty_provenance_list(tmp_path):
    report = _run_resistor_divider(tmp_path)

    assert report["provenance"]["devices"] == []


def test_device_body_layer_absent_from_layout_subtracts_nothing(tmp_path, capsys):
    """A `body_layer` this stream never carries is not an error (matching
    `stackup`/`vias`' own convention) -- but the measured `body_area_um2`
    says so, instead of leaving a caller to infer that the carve-out bit."""
    report = _run_resistor_divider(
        tmp_path,
        devices=[{"name": "poly_resistor", "body_layer": "99/0", "on": "poly"}],
    )

    assert report["provenance"]["devices"][0]["body_area_um2"] == 0.0
    # Nothing was subtracted, so the false short is still reported.
    assert [f["rule"] for f in report["erc_findings"]] == ["erc.supply_short"]
    # No stderr warning here (issue #2226): a layer absent from the stream
    # is the documented "this fixture doesn't draw it" case, unlike a marker
    # that is drawn but misses the role it was declared `on`.
    assert "subtracted nothing" not in capsys.readouterr().err


def test_devices_declaration_does_not_change_schema_version(tmp_path):
    report = _run_resistor_divider(tmp_path, devices=_RESISTOR_DEVICES)

    assert report["schema_version"] == 1


def test_device_body_does_not_disturb_an_unrelated_layout(tmp_path):
    """A `devices[]` declaration whose marker touches nothing on the
    declared role leaves every other finding exactly as it was: the basic
    fixture's own floating gate is still reported."""
    gds = tmp_path / "basic.gds"
    spec = tmp_path / "basic.erc.json"
    _basic_fixture(gds)
    _basic_spec(spec)
    baseline = run_erc(str(gds), str(spec))

    spec_dict = json.loads(spec.read_text())
    spec_dict["devices"] = [
        {"name": "poly_resistor", "body_layer": "62/0", "on": "poly"}
    ]
    with_devices = tmp_path / "basic_devices.erc.json"
    _write_spec(with_devices, spec_dict)
    report = run_erc(str(gds), str(with_devices))

    assert report["erc_findings"] == baseline["erc_findings"]
    assert report["gates"] == baseline["gates"]


# --- devices[] on a via role: a MiM/MOM cap bridging two metal roles ---------


def _mim_cap_fixture(path) -> None:
    """Two declared metal roles bridged *only* by the declared via role's
    own geometry -- the MiM/MOM capacitor shape the issue names by analogy
    (a poly resistor is not the only drawn device on a declared conductor).

    Layers: poly=1/0, contact=2/0, li1=3/0 (bottom plate + a labelled
    supply rail), li1 label=3/5, mimvia=4/0 (the plate-to-plate layer),
    met1=5/0 (top plate), met1 label=5/5, cap marker=63/0.
    """
    layout = kdb.Layout()
    layout.dbu = DBU
    top = layout.create_cell("TOP")

    poly = layout.layer(1, 0)
    contact = layout.layer(2, 0)
    li1 = layout.layer(3, 0)
    li1_label = layout.layer(3, 5)
    mimvia = layout.layer(4, 0)
    met1 = layout.layer(5, 0)
    met1_label = layout.layer(5, 5)
    cap_marker = layout.layer(63, 0)

    # Bottom plate on li1 (VDD) and top plate on met1 (VSS), overlapping.
    top.shapes(li1).insert(kdb.Box.new(_um(0), _um(0), _um(2), _um(1)))
    top.shapes(li1_label).insert(kdb.Text("VDD", kdb.Trans(_um(0.5), _um(0.5))))
    top.shapes(met1).insert(kdb.Box.new(_um(1), _um(0), _um(3), _um(1)))
    top.shapes(met1_label).insert(kdb.Text("VSS", kdb.Trans(_um(2.5), _um(0.5))))

    # The capacitor's own plate-to-plate geometry, on the declared via role.
    top.shapes(mimvia).insert(kdb.Box.new(_um(1.2), _um(0.2), _um(1.8), _um(0.8)))
    top.shapes(cap_marker).insert(kdb.Box.new(_um(1.1), _um(0.1), _um(1.9), _um(0.9)))

    # An unrelated, properly strapped gate, so `run_erc` has a gate net.
    top.shapes(poly).insert(kdb.Box.new(_um(20), _um(0), _um(21), _um(1)))
    top.shapes(contact).insert(kdb.Box.new(_um(20.2), _um(0.2), _um(20.4), _um(0.4)))
    top.shapes(li1).insert(kdb.Box.new(_um(20), _um(0), _um(21), _um(1)))

    layout.write(str(path))


def _mim_cap_spec(devices=None):
    spec = {
        "stackup": [
            {"name": "poly", "layer": "1/0", "role": "gate"},
            {"name": "li1", "layer": "3/0", "label_layer": "3/5"},
            {"name": "met1", "layer": "5/0", "label_layer": "5/5"},
        ],
        "vias": [
            {"name": "contact", "layer": "2/0", "between": ["poly", "li1"]},
            {"name": "mimvia", "layer": "4/0", "between": ["li1", "met1"]},
        ],
        "nets": [
            {"name": "VDD", "kind": "supply"},
            {"name": "VSS", "kind": "supply"},
        ],
    }
    if devices is not None:
        spec["devices"] = devices
    return spec


def _run_mim_cap(tmp_path, devices=None, name="mimcap"):
    gds = tmp_path / f"{name}.gds"
    spec = tmp_path / f"{name}.erc.json"
    _mim_cap_fixture(gds)
    _write_spec(spec, _mim_cap_spec(devices))
    return run_erc(str(gds), str(spec))


def test_undeclared_cap_body_on_a_via_role_shorts_two_supplies(tmp_path):
    report = _run_mim_cap(tmp_path)

    assert [f["rule"] for f in report["erc_findings"]] == ["erc.supply_short"]


def test_device_declared_on_a_via_role_breaks_the_plate_to_plate_bridge(tmp_path):
    """`on` may name a `vias` entry, not just a `stackup` role: a MiM/MOM
    cap's bridge between two declared metal roles *is* the via-role
    geometry, so that is where the carve-out has to apply."""
    report = _run_mim_cap(
        tmp_path,
        devices=[{"name": "mim_cap", "body_layer": "63/0", "on": "mimvia"}],
    )

    assert report["erc_findings"] == []
    assert report["provenance"]["devices"][0]["on"] == "mimvia"


# --- devices[]: spec validation (issue #2183) --------------------------------


def _run_devices_spec(tmp_path, devices):
    gds = tmp_path / "divider.gds"
    spec = tmp_path / "divider.erc.json"
    _resistor_divider_fixture(gds)
    _write_spec(spec, _resistor_divider_spec(devices))
    return run_erc(str(gds), str(spec))


def test_devices_must_be_an_array(tmp_path):
    with pytest.raises(ErcError, match="'devices' must be an array"):
        _run_devices_spec(tmp_path, {"name": "poly_resistor"})


def test_devices_entry_must_be_an_object(tmp_path):
    with pytest.raises(ErcError, match=r"devices\[0\] must be a JSON object"):
        _run_devices_spec(tmp_path, ["62/0"])


def test_devices_entry_requires_body_layer(tmp_path):
    with pytest.raises(ErcError, match=r"devices\[0\] missing 'body_layer'"):
        _run_devices_spec(tmp_path, [{"on": "poly"}])


def test_devices_entry_requires_on(tmp_path):
    with pytest.raises(ErcError, match=r"devices\[0\] missing 'on'"):
        _run_devices_spec(tmp_path, [{"body_layer": "62/0"}])


def test_devices_on_must_name_a_declared_conductor(tmp_path):
    with pytest.raises(ErcError, match=r"devices\[0\]\.on must name a 'stackup'"):
        _run_devices_spec(tmp_path, [{"body_layer": "62/0", "on": "met9"}])


def test_devices_body_layer_must_parse(tmp_path):
    with pytest.raises(ErcError, match=r"devices\[0\]\.body_layer"):
        _run_devices_spec(tmp_path, [{"body_layer": "sixty-two", "on": "poly"}])


def test_devices_names_must_be_unique(tmp_path):
    with pytest.raises(ErcError, match="duplicate device name 'poly_resistor'"):
        _run_devices_spec(
            tmp_path,
            [
                {"name": "poly_resistor", "body_layer": "62/0", "on": "poly"},
                {"name": "poly_resistor", "body_layer": "62/0", "on": "li1"},
            ],
        )


def test_devices_null_is_treated_as_omitted(tmp_path):
    gds = tmp_path / "divider.gds"
    spec = tmp_path / "divider.erc.json"
    _resistor_divider_fixture(gds)
    spec_dict = _resistor_divider_spec()
    spec_dict["devices"] = None
    _write_spec(spec, spec_dict)

    report = run_erc(str(gds), str(spec))
    assert report["provenance"]["devices"] == []


def test_two_devices_on_the_same_role_are_unioned(tmp_path):
    """Two declarations sharing one `on` role must both apply -- the second
    cannot replace the first."""
    gds = tmp_path / "divider.gds"
    spec = tmp_path / "divider.erc.json"
    _resistor_divider_fixture(gds)
    _write_spec(
        spec,
        _resistor_divider_spec(
            [
                {"name": "poly_resistor", "body_layer": "62/0", "on": "poly"},
                {"name": "poly_fuse", "body_layer": "99/0", "on": "poly"},
            ]
        ),
    )

    report = run_erc(str(gds), str(spec))
    assert report["erc_findings"] == []
    assert [d["name"] for d in report["provenance"]["devices"]] == [
        "poly_resistor",
        "poly_fuse",
    ]


# --- --deck: auto-apply a curated deck's own device-marker layers (issue #2204) ----
#
# Fixtures below are redrawn on gf180mcu's *real* curated-deck device layers
# (`src/klayout_tools/decks/gf180mcu.py`'s `EXTRACTION_DECK`), not the
# `62/0`/`63/0` stand-in markers the #2183 fixtures above use -- the whole
# point of this feature is that `--deck` reads the *actual* deck
# declarations, so a test against invented layer numbers would not exercise
# the real matching rule at all.

# `ppolyf_u`: body=Poly2 (30/0, also this deck's gate role), marker=RES_MK
# (110/5), requires=(Pplus 31/0, SAB 49/0).
_GF180MCU_POLY = "30/0"
_GF180MCU_CONTACT = "33/0"
_GF180MCU_MET1 = "34/0"
_GF180MCU_MET1_LABEL = "34/10"

# `cap_mim_2f0_m4m5_noshield`: top_plate=FuseTop (75/0, requires CAP_MK
# 117/5 + MIM_L_MK 117/10), bottom_plate=Metal4 (46/0), top_plate_via=Via4
# (41/0, gf180mcu's *own* ordinary Metal4<->Metal5 routing via layer too),
# top_plate_via_metal=Metal5 (81/0).
_GF180MCU_MET4 = "46/0"
_GF180MCU_MET4_LABEL = "46/10"
_GF180MCU_MET5 = "81/0"
_GF180MCU_MET5_LABEL = "81/10"
_GF180MCU_VIA4 = "41/0"


def _gf180mcu_resistor_divider_fixture(path, *, narrow_requires=True) -> None:
    """The #2183 rail-to-rail poly-resistor fixture (`_resistor_divider_
    fixture`), redrawn on gf180mcu's *real* `ppolyf_u` resistor layers so
    `--deck gf180mcu` auto-detects it with no `devices[]` declaration at
    all: body=Poly2 (30/0, doubling as this deck's own gate layer, exactly
    as real silicon draws it), marker=RES_MK (110/5), narrowed by Pplus
    (31/0) + SAB (49/0).

    `narrow_requires=False` (issue #2204's own edge-case test) omits the
    Pplus/SAB narrowing entirely, so `ppolyf_u`'s own `requires` never see
    a marker to narrow: the auto-detected candidate region comes out empty
    (`body & marker & Pplus & SAB == body & marker & {} & {}`), and nothing
    is subtracted -- exactly the same "no PDK device recognised here"
    outcome as a layout that draws no resistor marker at all.
    """
    layout = kdb.Layout()
    layout.dbu = DBU
    top = layout.create_cell("TOP")

    poly = layout.layer(30, 0)
    contact = layout.layer(33, 0)
    met1 = layout.layer(34, 0)
    label = layout.layer(34, 5)
    res_mk = layout.layer(110, 5)
    pplus = layout.layer(31, 0)
    sab = layout.layer(49, 0)

    # Two labelled supply rails, 6 um apart -- no metal runs between them.
    top.shapes(met1).insert(kdb.Box.new(_um(0), _um(0), _um(2), _um(1)))
    top.shapes(label).insert(kdb.Text("VDD", kdb.Trans(_um(1), _um(0.5))))
    top.shapes(met1).insert(kdb.Box.new(_um(8), _um(0), _um(10), _um(1)))
    top.shapes(label).insert(kdb.Text("VSS", kdb.Trans(_um(9), _um(0.5))))

    # The resistor: one Poly2 bar from rail to rail, contacted at each head.
    top.shapes(poly).insert(kdb.Box.new(_um(1), _um(0.2), _um(9), _um(0.8)))
    top.shapes(contact).insert(kdb.Box.new(_um(1.2), _um(0.3), _um(1.4), _um(0.5)))
    top.shapes(contact).insert(kdb.Box.new(_um(8.6), _um(0.3), _um(8.8), _um(0.5)))

    # The PDK's own RES_MK marker over the resistor body (not the heads):
    # 6 um x 0.8 um = 4.8 um^2.
    top.shapes(res_mk).insert(kdb.Box.new(_um(2), _um(0.1), _um(8), _um(0.9)))
    if narrow_requires:
        top.shapes(pplus).insert(kdb.Box.new(_um(2), _um(0.1), _um(8), _um(0.9)))
        top.shapes(sab).insert(kdb.Box.new(_um(2), _um(0.1), _um(8), _um(0.9)))

    layout.write(str(path))


def _gf180mcu_resistor_divider_spec(devices=None):
    spec = {
        "stackup": [
            {"name": "poly", "layer": _GF180MCU_POLY, "role": "gate"},
            {
                "name": "met1",
                "layer": _GF180MCU_MET1,
                "label_layer": "34/5",
            },
        ],
        "vias": [
            {"name": "contact", "layer": _GF180MCU_CONTACT, "between": ["poly", "met1"]}
        ],
        "nets": [
            {"name": "VDD", "kind": "supply"},
            {"name": "VSS", "kind": "supply"},
        ],
    }
    if devices is not None:
        spec["devices"] = devices
    return spec


def _run_gf180mcu_resistor_divider(
    tmp_path, *, deck=None, devices=None, narrow_requires=True, name="gf_divider"
):
    gds = tmp_path / f"{name}.gds"
    spec = tmp_path / f"{name}.erc.json"
    _gf180mcu_resistor_divider_fixture(gds, narrow_requires=narrow_requires)
    _write_spec(spec, _gf180mcu_resistor_divider_spec(devices))
    return run_erc(str(gds), str(spec), deck=deck)


def test_no_deck_still_reports_the_false_supply_short_on_a_real_deck_layout(tmp_path):
    """Without `--deck`, this module's own auto-detection never runs -- the
    #2183 baseline behaviour (a false `erc.supply_short`) is unchanged, even
    though the drawn resistor happens to use a real curated deck's own
    layers."""
    report = _run_gf180mcu_resistor_divider(tmp_path)

    assert [f["rule"] for f in report["erc_findings"]] == ["erc.supply_short"]
    assert report["provenance"]["devices"] == []
    assert report["provenance"]["deck"] is None


def test_deck_auto_detects_resistor_body_and_breaks_the_rail_to_rail_string(tmp_path):
    """`--deck gf180mcu` with no `devices[]` at all auto-carves the
    recognised `ppolyf_u` body out of `poly`, clearing the false short."""
    report = _run_gf180mcu_resistor_divider(tmp_path, deck="gf180mcu")

    assert report["erc_findings"] == []
    assert report["erc_finding_count"] == 0
    assert report["erc_status"] == "clean"


def test_deck_auto_detected_resistor_reported_in_provenance(tmp_path):
    report = _run_gf180mcu_resistor_divider(tmp_path, deck="gf180mcu")

    assert report["provenance"]["devices"] == [
        {
            "name": "ppolyf_u",
            "body_layer": _GF180MCU_POLY,
            "on": "poly",
            # `poly & RES_MK & Pplus & SAB`, not the marker's own raw area
            # (unlike a hand-declared `devices[]` entry, which subtracts
            # its `body_layer` region directly -- see `_device_body_cuts`):
            # poly spans x=[1,9]/y=[0.2,0.8] (8 x 0.6 um), the requires-
            # narrowed marker spans x=[2,8]/y=[0.1,0.9] (6 x 0.8 um), so
            # the overlap is 6 x 0.6 = 3.6 um^2.
            "body_area_um2": 3.6,
            "source": "deck",
            "superseded_by": None,
        }
    ]


def test_deck_selected_populates_provenance_deck(tmp_path):
    report = _run_gf180mcu_resistor_divider(tmp_path, deck="gf180mcu")

    deck_provenance = report["provenance"]["deck"]
    assert deck_provenance["name"] == "gf180mcu"
    assert deck_provenance["content_hash"].startswith("sha256:")


def test_deck_selected_does_not_change_schema_version(tmp_path):
    report = _run_gf180mcu_resistor_divider(tmp_path, deck="gf180mcu")

    assert report["schema_version"] == 1


def test_explicit_devices_entry_wins_over_deck_auto_detection(tmp_path):
    """An explicit `devices[]` entry for `poly` wins: it is the one that
    actually cuts, and the deck's own `ppolyf_u` match for the same role is
    listed but not applied (`body_area_um2: 0.0`, `superseded_by` naming
    the declared entry that won)."""
    report = _run_gf180mcu_resistor_divider(
        tmp_path,
        deck="gf180mcu",
        devices=[{"name": "my_resistor", "body_layer": "110/5", "on": "poly"}],
    )

    assert report["erc_findings"] == []
    devices_by_name = {d["name"]: d for d in report["provenance"]["devices"]}
    assert devices_by_name["my_resistor"]["source"] == "declared"
    # 3.6, not the RES_MK marker's own 4.8: the declared entry's reported
    # area is the marker intersected with `poly`'s own drawn region, and
    # this fixture's marker overhangs the poly bar by 0.1 um top and bottom
    # (issue #2226).
    assert devices_by_name["my_resistor"]["body_area_um2"] == 3.6
    assert devices_by_name["my_resistor"]["superseded_by"] is None
    assert devices_by_name["ppolyf_u"]["source"] == "deck"
    assert devices_by_name["ppolyf_u"]["body_area_um2"] == 0.0
    assert devices_by_name["ppolyf_u"]["superseded_by"] == "my_resistor"


def test_requires_narrowed_resistor_removes_only_the_narrowed_region(tmp_path):
    """gf180mcu's `ppolyf_u` requires *both* Pplus and SAB. With neither
    drawn, the candidate body (`body & marker & Pplus & SAB`) is empty, so
    nothing is subtracted and the false short survives -- the deck's own
    `requires` narrowing is honoured, not just its `body`/`marker` pair."""
    report = _run_gf180mcu_resistor_divider(
        tmp_path, deck="gf180mcu", narrow_requires=False
    )

    assert [f["rule"] for f in report["erc_findings"]] == ["erc.supply_short"]
    # Nothing was recognised at all -- no entry to list (this deck ships
    # over a dozen resistor/capacitor flavours; listing every one that
    # matches no geometry on every `--deck`-selected run would bury the
    # signal AC4 exists to surface -- see `_deck_device_cuts`'s docstring).
    assert report["provenance"]["devices"] == []


def _gf180mcu_mim_cap_fixture(path) -> None:
    """A MiM cap drawn on gf180mcu's *real* layers: bottom plate on Metal4
    (VDD), top plate (FuseTop, CAP_MK/MIM_L_MK-narrowed) bridged to Metal5
    (VSS) through Via4 -- the *same* physical layer gf180mcu also uses for
    ordinary Metal4<->Metal5 routing vias (`ExtractionDeck.vias[3]`), so a
    spec that declares a `via4` role bridging `met4`/`met5` reads the cap's
    own via as an ordinary short between the two plates without
    auto-detection -- issue #2204's own worked "MiM cap on a via role"
    example, now against the real deck rather than the #2183 fixture's
    invented `4/0`/`63/0` stand-ins.

    Also draws an unrelated, properly strapped gate (so `run_erc` has a
    gate net) and a *second*, unrelated Metal4<->Metal5 Via4 stack far from
    the cap, labelled `ROUTE` -- the regression guard for issue #364/#1388's
    "not the whole via layer" guarantee, reused here by issue #2204's own
    auto-detection (see `test_deck_auto_detection_does_not_disconnect_
    ordinary_vias_on_the_same_layer`).
    """
    layout = kdb.Layout()
    layout.dbu = DBU
    top = layout.create_cell("TOP")

    poly = layout.layer(30, 0)
    contact = layout.layer(33, 0)
    met4 = layout.layer(46, 0)
    met4_label = layout.layer(46, 5)
    met5 = layout.layer(81, 0)
    met5_label = layout.layer(81, 5)
    via4 = layout.layer(41, 0)
    fusetop = layout.layer(75, 0)
    cap_mk = layout.layer(117, 5)
    mim_l_mk = layout.layer(117, 10)

    # Bottom plate on met4 (VDD), top-plate stack (FuseTop+Via4+Met5, VSS).
    top.shapes(met4).insert(kdb.Box.new(_um(0), _um(0), _um(3), _um(2)))
    top.shapes(met4_label).insert(kdb.Text("VDD", kdb.Trans(_um(0.5), _um(1))))

    top.shapes(fusetop).insert(kdb.Box.new(_um(1), _um(0.5), _um(2), _um(1.5)))
    top.shapes(cap_mk).insert(kdb.Box.new(_um(0.8), _um(0.3), _um(2.2), _um(1.7)))
    top.shapes(mim_l_mk).insert(kdb.Box.new(_um(0.8), _um(0.3), _um(2.2), _um(1.7)))
    top.shapes(via4).insert(kdb.Box.new(_um(1.2), _um(0.7), _um(1.8), _um(1.3)))
    top.shapes(met5).insert(kdb.Box.new(_um(1), _um(0.5), _um(2), _um(1.5)))
    top.shapes(met5_label).insert(kdb.Text("VSS", kdb.Trans(_um(1.5), _um(1))))

    # An unrelated, properly strapped gate.
    top.shapes(poly).insert(kdb.Box.new(_um(20), _um(0), _um(21), _um(1)))
    top.shapes(contact).insert(kdb.Box.new(_um(20.2), _um(0.2), _um(20.4), _um(0.4)))
    top.shapes(met4).insert(kdb.Box.new(_um(20), _um(0), _um(21), _um(1)))

    # An unrelated, ordinary Via4 stack -- no cap marker anywhere near it.
    top.shapes(met4).insert(kdb.Box.new(_um(30), _um(0), _um(32), _um(1)))
    top.shapes(via4).insert(kdb.Box.new(_um(30.4), _um(0.2), _um(30.6), _um(0.4)))
    top.shapes(met5).insert(kdb.Box.new(_um(30), _um(0), _um(32), _um(1)))
    top.shapes(met5_label).insert(kdb.Text("ROUTE", kdb.Trans(_um(31), _um(0.5))))

    layout.write(str(path))


def _gf180mcu_mim_cap_spec(devices=None, extra_nets=None):
    spec = {
        "stackup": [
            {"name": "poly", "layer": _GF180MCU_POLY, "role": "gate"},
            {"name": "met4", "layer": _GF180MCU_MET4, "label_layer": "46/5"},
            {"name": "met5", "layer": _GF180MCU_MET5, "label_layer": "81/5"},
        ],
        "vias": [
            {
                "name": "contact",
                "layer": _GF180MCU_CONTACT,
                "between": ["poly", "met4"],
            },
            {"name": "via4", "layer": _GF180MCU_VIA4, "between": ["met4", "met5"]},
        ],
        "nets": [
            {"name": "VDD", "kind": "supply"},
            {"name": "VSS", "kind": "supply"},
            *(extra_nets or []),
        ],
    }
    if devices is not None:
        spec["devices"] = devices
    return spec


def _run_gf180mcu_mim_cap(
    tmp_path, *, deck=None, devices=None, extra_nets=None, name="gf_mimcap"
):
    gds = tmp_path / f"{name}.gds"
    spec = tmp_path / f"{name}.erc.json"
    _gf180mcu_mim_cap_fixture(gds)
    _write_spec(spec, _gf180mcu_mim_cap_spec(devices, extra_nets))
    return run_erc(str(gds), str(spec), deck=deck)


def test_no_deck_reports_a_false_supply_short_through_the_ordinary_via_role(tmp_path):
    report = _run_gf180mcu_mim_cap(tmp_path)

    assert [f["rule"] for f in report["erc_findings"]] == ["erc.supply_short"]


def test_deck_auto_detects_cap_via_overlap_and_breaks_the_plate_to_plate_bridge(
    tmp_path,
):
    report = _run_gf180mcu_mim_cap(tmp_path, deck="gf180mcu")

    assert report["erc_findings"] == []


def test_deck_auto_detected_cap_reported_in_provenance(tmp_path):
    report = _run_gf180mcu_mim_cap(tmp_path, deck="gf180mcu")

    devices_by_layer = {d["body_layer"]: d for d in report["provenance"]["devices"]}
    # FuseTop (the cap's own `top_plate`) matches no declared role in this
    # spec -- listed, not silently dropped (issue #2204's own AC4).
    fusetop_entry = devices_by_layer["75/0"]
    assert fusetop_entry["name"] == "cap_mim_2f0_m4m5_noshield"
    assert fusetop_entry["on"] is None
    assert fusetop_entry["body_area_um2"] == 0.0
    assert fusetop_entry["source"] == "deck"
    # Via4 matches the declared `via4` role -- only the cap's own
    # via-to-bottom-plate overlap is cut, not the whole via layer.
    via4_entry = devices_by_layer[_GF180MCU_VIA4]
    assert via4_entry["name"] == "cap_mim_2f0_m4m5_noshield"
    assert via4_entry["on"] == "via4"
    assert via4_entry["body_area_um2"] > 0.0
    assert via4_entry["superseded_by"] is None


def test_deck_auto_detection_does_not_disconnect_ordinary_vias_on_the_same_layer(
    tmp_path,
):
    """The cut is the cap's own via-to-bottom-plate *overlap* only (issue
    #364/#1388's own derivation, reused here) -- an ordinary Metal4<->
    Metal5 via elsewhere on the same physical Via4 layer, unrelated to any
    capacitor, must still connect normally."""
    report = _run_gf180mcu_mim_cap(
        tmp_path, deck="gf180mcu", extra_nets=[{"name": "ROUTE", "kind": "signal"}]
    )

    assert not any(f["net"] == "ROUTE" for f in report["erc_findings"])


def _gf180mcu_real_short_fixture(path) -> None:
    """Two supply rails joined by a plain metal wire -- a genuine short,
    with no device-marker geometry anywhere. `--deck`'s auto-detection
    (issue #2204) must not touch this: it only ever cuts where a curated
    device's own conducting-body layer matches a declared role *and* that
    device's own region is actually drawn, neither of which applies here."""
    layout = kdb.Layout()
    layout.dbu = DBU
    top = layout.create_cell("TOP")

    poly = layout.layer(30, 0)
    contact = layout.layer(33, 0)
    met1 = layout.layer(34, 0)
    label = layout.layer(34, 5)

    top.shapes(met1).insert(kdb.Box.new(_um(0), _um(0), _um(10), _um(1)))
    top.shapes(label).insert(kdb.Text("VDD", kdb.Trans(_um(1), _um(0.5))))
    top.shapes(label).insert(kdb.Text("VSS", kdb.Trans(_um(9), _um(0.5))))

    top.shapes(poly).insert(kdb.Box.new(_um(20), _um(0), _um(21), _um(1)))
    top.shapes(contact).insert(kdb.Box.new(_um(20.2), _um(0.2), _um(20.4), _um(0.4)))
    top.shapes(met1).insert(kdb.Box.new(_um(20), _um(0), _um(21), _um(1)))

    layout.write(str(path))


def test_deck_selected_still_reports_a_genuine_short(tmp_path):
    """A real rail-to-rail metal short -- no device body involved at all --
    must still be reported with `--deck` selected: the auto carve-out only
    ever removes a curated device's own recognised body/via-overlap region,
    never ordinary routing (issue #2204's own regression guard against an
    over-broad carve-out silently hiding a real short)."""
    gds = tmp_path / "gf_real_short.gds"
    spec = tmp_path / "gf_real_short.erc.json"
    _gf180mcu_real_short_fixture(gds)
    _write_spec(
        spec,
        {
            "stackup": [
                {"name": "poly", "layer": _GF180MCU_POLY, "role": "gate"},
                {"name": "met1", "layer": _GF180MCU_MET1, "label_layer": "34/5"},
            ],
            "vias": [
                {
                    "name": "contact",
                    "layer": _GF180MCU_CONTACT,
                    "between": ["poly", "met1"],
                }
            ],
            "nets": [
                {"name": "VDD", "kind": "supply"},
                {"name": "VSS", "kind": "supply"},
            ],
        },
    )

    report = run_erc(str(gds), str(spec), deck="gf180mcu")

    assert [f["rule"] for f in report["erc_findings"]] == ["erc.supply_short"]


def test_unknown_deck_name_is_a_clean_error(tmp_path):
    gds = tmp_path / "divider.gds"
    spec = tmp_path / "divider.erc.json"
    _resistor_divider_fixture(gds)
    _write_spec(spec, _resistor_divider_spec())

    with pytest.raises(ErcError, match="unknown deck 'not_a_real_deck'"):
        run_erc(str(gds), str(spec), deck="not_a_real_deck")


def test_cli_unknown_deck_exits_one_with_clean_message(tmp_path, capsys):
    gds = tmp_path / "basic.gds"
    spec = tmp_path / "basic.erc.json"
    _basic_fixture(gds)
    _basic_spec(spec)

    assert main(["erc", str(gds), str(spec), "--deck", "not_a_real_deck"]) == 1
    err = capsys.readouterr().err
    assert "unknown deck" in err
    assert "Traceback" not in err


def test_deck_omitted_is_byte_identical_to_pre_2204_output(tmp_path):
    """AC5: with no `--deck`, output is byte-identical to before this
    feature existed -- `provenance.devices` entries keep their pre-#2204
    4-key shape (no `source`/`superseded_by`), and `provenance.deck` stays
    `None`, exactly as `test_device_body_subtraction_is_reported_in_
    provenance` above (the #2183 baseline) already asserts."""
    report = _run_resistor_divider(tmp_path, devices=_RESISTOR_DEVICES)

    assert report["provenance"]["deck"] is None
    assert report["provenance"]["devices"] == [
        {
            "name": "poly_resistor",
            "body_layer": "62/0",
            "on": "poly",
            "body_area_um2": _RESISTOR_SUBTRACTED_UM2,
        }
    ]


def test_cli_deck_selected_via_flag_reports_no_findings(tmp_path, capsys):
    """The `--deck` CLI flag itself plumbs through to `run_erc` (not just
    the library function) -- no findings on the same gf180mcu resistor
    fixture the library-level tests above exercise. No `--pdk` is given, so
    the exit code (which follows the *antenna* question, per
    `erc_cmd.py`'s own docstring) is `4` (`not_checked`), not `0` -- read
    `erc_status`, the connectivity-only roll-up, for this check instead."""
    gds = tmp_path / "gf_divider_cli.gds"
    spec = tmp_path / "gf_divider_cli.erc.json"
    _gf180mcu_resistor_divider_fixture(gds)
    _write_spec(spec, _gf180mcu_resistor_divider_spec())

    assert (
        main(["erc", str(gds), str(spec), "--deck", "gf180mcu", "--format", "json"])
        == 4
    )
    out = json.loads(capsys.readouterr().out)
    assert out["erc_findings"] == []
    assert out["erc_status"] == "clean"
    assert out["provenance"]["deck"]["name"] == "gf180mcu"


# --- --findings-only: skip the antenna accumulation (issue #2219) ------------


class _CountingL2N:
    """A thin recording proxy around ``klayout.db.LayoutToNetlist``.

    ``polygons_of_net`` is *the* expensive call in `klt erc` -- one merged
    region per gate net per stackup role -- so the issue-#2219 skip is only
    real if it makes fewer of them. Counting is done here, on the object
    `run_erc` actually calls, rather than by timing: a wall-clock assertion
    on a tiny fixture would be noise, while a call count is exact.
    """

    def __init__(self, inner, layer_index):
        self._inner = inner
        self.layer_index = layer_index
        self.layer_calls: list[int] = []

    def calls_for(self, role: str) -> int:
        """How many times this graph was asked for a net's geometry on the
        `stackup` role named `role`."""
        return self.layer_calls.count(self.layer_index[role])

    def polygons_of_net(self, net, layer, *rest):
        self.layer_calls.append(layer)
        return self._inner.polygons_of_net(net, layer, *rest)

    def __getattr__(self, name):
        return getattr(self._inner, name)


def _record_polygons_of_net(monkeypatch):
    """Wrap `_extract_connectivity` so every `LayoutToNetlist` `run_erc`
    builds is a counting proxy. Returns the list the proxies land in, in
    creation order (the primary graph first, the tie graph -- when the spec
    declares `ties[]` -- second)."""
    from klayout_tools import erc as erc_module

    proxies: list[_CountingL2N] = []
    real = erc_module._extract_connectivity

    def counting(*args, **kwargs):
        l2n, circuit, layer_index, tie_layers = real(*args, **kwargs)
        proxy = _CountingL2N(l2n, layer_index)
        proxies.append(proxy)
        return proxy, circuit, layer_index, tie_layers

    monkeypatch.setattr(erc_module, "_extract_connectivity", counting)
    return proxies


def _assert_findings_identical(gds, spec):
    """The contract the whole flag rests on: a findings-only run's
    `erc_findings` (and the connectivity roll-up over them) are identical,
    field for field, to the full run's."""
    full = run_erc(str(gds), str(spec))
    lean = run_erc(str(gds), str(spec), findings_only=True)

    assert json.dumps(lean["erc_findings"], sort_keys=True) == json.dumps(
        full["erc_findings"], sort_keys=True
    )
    assert lean["erc_finding_count"] == full["erc_finding_count"]
    assert lean["erc_status"] == full["erc_status"]
    assert lean["erc_coverage"] == full["erc_coverage"]
    assert lean["gate_count"] == full["gate_count"]
    assert [g["gate_id"] for g in lean["gates"]] == [
        g["gate_id"] for g in full["gates"]
    ]
    assert [g["gate_area_um2"] for g in lean["gates"]] == [
        g["gate_area_um2"] for g in full["gates"]
    ]
    return full, lean


def test_findings_only_keeps_floating_gate_findings_identical(tmp_path):
    """`erc.floating_gate` is the one rule that reads the per-level model,
    so it is the one the skip could plausibly break."""
    gds = tmp_path / "basic.gds"
    spec = tmp_path / "basic.erc.json"
    _basic_fixture(gds)
    _basic_spec(spec)

    full, lean = _assert_findings_identical(gds, spec)

    floating = [f for f in lean["erc_findings"] if f["rule"] == "erc.floating_gate"]
    assert len(floating) == 1
    assert floating[0]["gate_id"] == "gate1"
    assert floating[0]["bbox"] is not None


def test_findings_only_keeps_supply_read_findings_identical(tmp_path):
    """The T1 item-11 supply read: `erc.unconnected_net` (a declared supply
    that matches nothing) plus `erc.missing_tie` (wells whose declared tap
    layer draws nothing), on the same routed two-gate layout issue #2169's
    tie tests use."""
    layout, _top = _routed_tie_layout()
    gds = tmp_path / "supply_read.gds"
    layout.write(str(gds))
    spec_doc = _routed_tie_spec(ties=_routed_tie_entries(tap_layer="99/0"))
    spec_doc["nets"].append({"name": "VDDA", "kind": "supply"})
    spec = tmp_path / "supply_read.erc.json"
    _write_spec(spec, spec_doc)

    _full, lean = _assert_findings_identical(gds, spec)

    rules = {f["rule"] for f in lean["erc_findings"]}
    assert "erc.missing_tie" in rules
    assert "erc.unconnected_net" in rules
    assert {
        f["net"] for f in lean["erc_findings"] if f["rule"] == "erc.missing_tie"
    } == {"VDD", "VSS"}


def test_findings_only_keeps_supply_short_findings_identical(tmp_path):
    layout, top, _poly, li1, label = _nets_fixture_layout()
    top.shapes(li1).insert(kdb.Box.new(_um(10), _um(0), _um(12), _um(1)))
    top.shapes(label).insert(kdb.Text("VDD", kdb.Trans(_um(10.5), _um(0.5))))
    top.shapes(label).insert(kdb.Text("VSS", kdb.Trans(_um(11.5), _um(0.5))))

    gds = tmp_path / "shorted_supplies.gds"
    layout.write(str(gds))
    spec = tmp_path / "shorted_supplies.erc.json"
    _write_spec(
        spec,
        _nets_spec(
            nets=[
                {"name": "VDD", "kind": "supply"},
                {"name": "VSS", "kind": "supply"},
            ]
        ),
    )

    _full, lean = _assert_findings_identical(gds, spec)

    shorts = [f for f in lean["erc_findings"] if f["rule"] == "erc.supply_short"]
    assert len(shorts) == 1
    assert {shorts[0]["net"], shorts[0]["other_net"]} == {"VDD", "VSS"}


def test_findings_only_skips_the_per_level_accumulation(tmp_path, monkeypatch):
    """The point of the flag: materially fewer `polygons_of_net` calls, and
    *none at all* for the roles a strapped gate's accumulation would have
    walked past its first connected level."""
    gds = tmp_path / "basic.gds"
    spec = tmp_path / "basic.erc.json"
    _basic_fixture(gds)
    _basic_spec(spec)

    proxies = _record_polygons_of_net(monkeypatch)
    full = run_erc(str(gds), str(spec))
    full_graph = proxies[0]

    proxies.clear()
    lean = run_erc(str(gds), str(spec), findings_only=True)
    lean_graph = proxies[0]

    assert len(lean_graph.layer_calls) < len(full_graph.layer_calls)

    # Both gate nets are walked up the whole four-role stackup by the full
    # run. The findings-only run stops at each gate's first connected role
    # instead: gate A straps up through li1, so the roles above it are
    # probed only for the floating gate B.
    for role in ("li1", "met1", "met2"):
        assert full_graph.calls_for(role) == 2, role
    assert lean_graph.calls_for("li1") == 2
    assert lean_graph.calls_for("met1") == 1
    assert lean_graph.calls_for("met2") == 1

    # And the skip is not achieved by dropping a gate on the floor.
    assert lean["gate_count"] == full["gate_count"] == 2


def test_findings_only_nulls_the_accumulation_and_says_so(tmp_path):
    gds = tmp_path / "basic.gds"
    spec = tmp_path / "basic.erc.json"
    _basic_fixture(gds)
    _basic_spec(spec)

    report = run_erc(str(gds), str(spec), findings_only=True)

    assert report["findings_only"] is True
    assert report["pdk"] is None
    for gate in report["gates"]:
        assert gate["antenna_verdict"] == "unchecked"
        assert [level["layer"] for level in gate["levels"]] == [
            "poly",
            "li1",
            "met1",
            "met2",
        ]
        for level in gate["levels"]:
            assert level["step_area_um2"] is None
            assert level["cumulative_area_um2"] is None
            assert level["antenna_ratio"] is None
            assert level["antenna_ratio_max"] is None
            assert level["antenna_ratio_source"] is None
            assert level["verdict"] == "unchecked"
            assert level["remedy"] is None

    # Nothing antenna-side was graded, and the envelope names why.
    assert report["coverage"]["checked"] == []
    assert {entry["reason"] for entry in report["coverage"]["skipped"]} == {
        "findings_only"
    }
    # The connectivity half is untouched -- fully graded, with its own
    # roll-up still reporting the floating gate this fixture carries.
    assert report["erc_status"] == "violations"
    assert report["status"] == "violations"


def test_findings_only_clean_run_reports_not_checked(tmp_path):
    """With no finding to report, a findings-only run's antenna answer is
    `not_checked` -- the same answer a `--pdk`-less run already gives, not
    a new status token."""
    layout, _top = _routed_tie_layout()
    gds = tmp_path / "clean.gds"
    layout.write(str(gds))
    spec = tmp_path / "clean.erc.json"
    ties = [{**entry, "tap_is_dedicated": True} for entry in _routed_tie_entries()]
    _write_spec(spec, _routed_tie_spec(ties=ties))

    report = run_erc(str(gds), str(spec), findings_only=True)

    assert report["erc_findings"] == []
    assert report["erc_status"] == "clean"
    assert report["status"] == "not_checked"


def test_findings_only_with_pdk_raises(tmp_path):
    gds = tmp_path / "basic.gds"
    spec = tmp_path / "basic.erc.json"
    _basic_fixture(gds)
    _basic_spec(spec)

    with pytest.raises(ErcError, match="--findings-only"):
        run_erc(str(gds), str(spec), pdk="sky130", findings_only=True)


def test_default_run_is_unchanged_by_the_flag(tmp_path):
    """The flag is opt-in: an invocation that does not pass it accumulates
    exactly as before, nulls nowhere."""
    gds = tmp_path / "basic.gds"
    spec = tmp_path / "basic.erc.json"
    _basic_fixture(gds)
    _basic_spec(spec)

    report = run_erc(str(gds), str(spec), pdk="sky130")

    assert report["findings_only"] is False
    gate_a = next(g for g in report["gates"] if g["net"] == "GATE_A")
    assert all(level["step_area_um2"] is not None for level in gate_a["levels"])
    # `levels[0]` is the gate role's own merged region, reused rather than
    # re-extracted (issue #2219) -- still exactly the gate area.
    assert gate_a["levels"][0]["step_area_um2"] == pytest.approx(
        gate_a["gate_area_um2"]
    )
    assert gate_a["levels"][0]["cumulative_area_um2"] == pytest.approx(
        gate_a["gate_area_um2"]
    )


def test_cli_findings_only_json(tmp_path, capsys):
    layout, _top = _routed_tie_layout()
    gds = tmp_path / "clean.gds"
    layout.write(str(gds))
    spec = tmp_path / "clean.erc.json"
    ties = [{**entry, "tap_is_dedicated": True} for entry in _routed_tie_entries()]
    _write_spec(spec, _routed_tie_spec(ties=ties))

    exit_code = main(
        ["erc", str(gds), str(spec), "--findings-only", "--format", "json"]
    )
    data = json.loads(capsys.readouterr().out)

    assert exit_code == 4
    assert data["findings_only"] is True
    assert data["erc_status"] == "clean"
    assert data["status"] == "not_checked"


def test_cli_findings_only_text_says_accumulation_skipped(tmp_path, capsys):
    gds = tmp_path / "basic.gds"
    spec = tmp_path / "basic.erc.json"
    _basic_fixture(gds)
    _basic_spec(spec)

    assert main(["erc", str(gds), str(spec), "--findings-only"]) == 3
    out = capsys.readouterr().out

    assert "findings_only: True" in out
    assert "met2: accumulation skipped (--findings-only)" in out
    assert "step=" not in out
    assert "[erc.floating_gate]" in out


def test_cli_findings_only_with_pdk_exits_1(tmp_path, capsys):
    gds = tmp_path / "basic.gds"
    spec = tmp_path / "basic.erc.json"
    _basic_fixture(gds)
    _basic_spec(spec)

    exit_code = main(["erc", str(gds), str(spec), "--findings-only", "--pdk", "sky130"])

    assert exit_code == 1
    assert "--findings-only" in capsys.readouterr().err


# --- `erc_coverage.layers_in_stream_without_declaration` (issue #2389) ------
#
# The inverse-direction disclosure `klt drc`'s
# `coverage.layers_in_stream_without_rules` already makes: which layers this
# stream actually draws that the spec never names, so a committed supply spec
# cannot silently narrow as its layout is re-routed.


def test_undeclared_stream_layers_are_empty_when_the_spec_names_everything(
    tmp_path,
):
    """AC: empty when every drawn layer is declared -- the same convention
    `klt drc`'s own empty `layers_in_stream_without_rules` carries.

    `_strapped_supply_layout` draws poly (1/0), licon (2/0), li1 (3/0) and
    the li1 label (3/5); the spec names all four (the label via
    `stackup[].label_layer`, which is a declaration even though it carries
    no conductor role)."""
    layout, top, li1, label, nwell, tap = _strapped_supply_layout()

    gds = tmp_path / "fully_declared.gds"
    layout.write(str(gds))
    spec = tmp_path / "fully_declared.erc.json"
    _write_spec(spec, _strapped_supply_spec(nets=[{"name": "VDD", "kind": "supply"}]))

    report = run_erc(str(gds), str(spec))

    assert report["erc_coverage"]["layers_in_stream_without_declaration"] == []


def test_undeclared_stream_layer_is_named_without_changing_any_verdict(tmp_path):
    """AC, the headline case: a conductor layer the layout draws and the
    spec's `stackup`/`vias` never declare is named -- and nothing else in
    the report moves. This is the re-route scenario the issue describes: the
    rail still resolves to one island through the declared roles, so the
    report is clean, but the connectivity model is now narrower than the
    layout and the envelope finally says so."""
    layout, top, li1, label, nwell, tap = _strapped_supply_layout()
    # A met1 (5/0) strap the committed spec never declared -- drawn over the
    # li1 rail, so the declared net still resolves to exactly one island.
    met1 = layout.layer(5, 0)
    top.shapes(met1).insert(kdb.Box.new(_um(1), _um(0), _um(4), _um(1)))

    gds = tmp_path / "rerouted.gds"
    layout.write(str(gds))
    spec = tmp_path / "rerouted.erc.json"
    _write_spec(spec, _strapped_supply_spec(nets=[{"name": "VDD", "kind": "supply"}]))

    report = run_erc(str(gds), str(spec))

    assert report["erc_coverage"]["layers_in_stream_without_declaration"] == ["5/0"]
    # Reported, never graded: the verdicts are exactly the clean ones the
    # same layout produces without the undeclared strap.
    assert report["erc_finding_count"] == 0
    assert report["erc_status"] == "clean"
    assert report["status"] == "not_checked"  # the antenna half, no --pdk
    assert report["erc_coverage"]["skipped"] == []
    assert report["erc_coverage"]["inapplicable"] == [
        {"id": "erc.missing_tie:[]", "reason": "no_ties_declared"}
    ]


def test_undeclared_stream_layers_are_sorted_layer_then_datatype(tmp_path):
    """Deterministic output, `klt drc`'s `layer/datatype` formatting, sorted
    by `(layer, datatype)` -- so two runs of the same stream are diffable."""
    layout, top, li1, label, nwell, tap = _strapped_supply_layout()
    for layer, datatype in ((9, 0), (5, 2), (5, 0)):
        index = layout.layer(layer, datatype)
        top.shapes(index).insert(kdb.Box.new(_um(20), _um(0), _um(21), _um(1)))

    gds = tmp_path / "sorted.gds"
    layout.write(str(gds))
    spec = tmp_path / "sorted.erc.json"
    _write_spec(spec, _strapped_supply_spec())

    report = run_erc(str(gds), str(spec))

    assert report["erc_coverage"]["layers_in_stream_without_declaration"] == [
        "5/0",
        "5/2",
        "9/0",
    ]


def test_undeclared_stream_layers_exclude_every_layer_the_spec_names(tmp_path):
    """A layer named anywhere in the spec -- a `ties[]` well/tap layer, a
    `tap_requires` narrowing marker -- is declared, not undisclosed: the
    field must not train a reader to ignore it with false positives."""
    layout, top, li1, label, nwell, tap = _strapped_supply_layout()
    top.shapes(nwell).insert(kdb.Box.new(_um(3), _um(0), _um(4), _um(1)))
    top.shapes(tap).insert(kdb.Box.new(_um(3.2), _um(0.2), _um(3.8), _um(0.8)))
    marker = layout.layer(12, 0)
    top.shapes(marker).insert(kdb.Box.new(_um(3.2), _um(0.2), _um(3.8), _um(0.8)))

    gds = tmp_path / "declared_elsewhere.gds"
    layout.write(str(gds))
    spec = tmp_path / "declared_elsewhere.erc.json"
    _write_spec(
        spec,
        _strapped_supply_spec(
            nets=[{"name": "VDD", "kind": "supply"}],
            ties=[
                {
                    "name": "nwell_tie",
                    "well_layer": "10/0",
                    "tap_layer": "11/0",
                    "tap_requires": ["12/0"],
                    "connect_to": "li1",
                    "net": "VDD",
                }
            ],
        ),
    )

    report = run_erc(str(gds), str(spec))

    assert report["erc_coverage"]["layers_in_stream_without_declaration"] == []


def test_undeclared_stream_layers_ignore_geometry_outside_the_selected_top(
    tmp_path,
):
    """Scoped to the top cell actually analysed, exactly as `klt drc`'s own
    `coverage` block is scoped by `--top`: a layer drawn only in a cell this
    run never looked at is not a gap in *this* run's connectivity model."""
    layout, top, li1, label, nwell, tap = _strapped_supply_layout()
    other = layout.create_cell("OTHER")
    other.shapes(layout.layer(7, 0)).insert(kdb.Box.new(_um(0), _um(0), _um(1), _um(1)))

    gds = tmp_path / "two_tops.gds"
    layout.write(str(gds))
    spec = tmp_path / "two_tops.erc.json"
    _write_spec(spec, _strapped_supply_spec())

    report = run_erc(str(gds), str(spec), top="TOP")

    assert report["erc_coverage"]["layers_in_stream_without_declaration"] == []


def test_undeclared_stream_layers_unfiltered_without_a_deck(tmp_path):
    """Without `--deck` there is no PDK-agnostic way to know which drawn
    layers are conductors, so the list is unfiltered -- implants and markers
    included. Strictly more information than silence, and the honest
    behaviour to document rather than a guess at which layers matter."""
    report = _run_gf180mcu_resistor_divider(tmp_path)

    # RES_MK (110/5), Pplus (31/0) and SAB (49/0) are drawn by the fixture
    # and named nowhere in the spec.
    assert report["erc_coverage"]["layers_in_stream_without_declaration"] == [
        "31/0",
        "49/0",
        "110/5",
    ]


def test_deck_narrows_undeclared_stream_layers_to_the_routing_stack(tmp_path):
    """AC: with `--deck`, the curated deck's own `metals`/`vias` (plus the
    device-level conductors it recognises) narrow the list, so implants and
    markers stop being reported as coverage gaps."""
    report = _run_gf180mcu_resistor_divider(tmp_path, deck="gf180mcu")

    assert report["erc_coverage"]["layers_in_stream_without_declaration"] == []


def test_deck_still_names_an_undeclared_metal_level(tmp_path):
    """The narrowing filters noise, not signal: a Metal2 (36/0) strap the
    spec never declared is a real gap in the connectivity model and is still
    named under `--deck`."""
    gds = tmp_path / "gf_met2.gds"
    spec = tmp_path / "gf_met2.erc.json"
    _gf180mcu_resistor_divider_fixture(gds)
    layout = kdb.Layout()
    layout.read(str(gds))
    top = layout.top_cell()
    top.shapes(layout.layer(36, 0)).insert(kdb.Box.new(_um(0), _um(2), _um(10), _um(3)))
    layout.write(str(gds))
    _write_spec(spec, _gf180mcu_resistor_divider_spec())

    report = run_erc(str(gds), str(spec), deck="gf180mcu")

    assert report["erc_coverage"]["layers_in_stream_without_declaration"] == ["36/0"]
    assert report["erc_status"] == "clean"


def test_cli_json_carries_undeclared_stream_layers(tmp_path, capsys):
    """The field reaches the JSON envelope (the contract), not just the
    in-process return value."""
    layout, top, li1, label, nwell, tap = _strapped_supply_layout()
    top.shapes(layout.layer(5, 0)).insert(kdb.Box.new(_um(1), _um(0), _um(4), _um(1)))

    gds = tmp_path / "cli_undeclared.gds"
    layout.write(str(gds))
    spec = tmp_path / "cli_undeclared.erc.json"
    _write_spec(spec, _strapped_supply_spec(nets=[{"name": "VDD", "kind": "supply"}]))

    main(["erc", str(gds), str(spec), "--format", "json"])
    data = json.loads(capsys.readouterr().out)

    assert data["erc_coverage"]["layers_in_stream_without_declaration"] == ["5/0"]
