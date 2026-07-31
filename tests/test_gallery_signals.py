"""Tests for `scripts/gallery_signals.py` -- the real, vendored-cell `klt
sim` pipeline behind `blocks/<slug>/output/layout.json`'s `signals` field
(issue #99, epic #90 phase 2).

Two tiers, mirroring `tests/test_sim.py`:

- **Unit tests** (always run) exercise `CELL_CONFIGS` shape/consistency and
  the pure netlist/request-generation helpers without invoking `ngspice`.
- **Integration test** (`@pytest.mark.skipif`, real `ngspice`, real vendored
  netlist) runs one full corner for one real sky130 cell -- the "`klt sim`
  invoked against at least one real vendored cell netlist must produce a
  passing/measurable result in CI" requirement from issue #99's test plan.
  Skipped (with a clear reason, never silently passing) when either
  `ngspice` is not installed or `pdks/cell-netlists/` has not been
  populated by `scripts/fetch-cell-netlists.sh` -- CI does both (see
  `.github/workflows/ci.yml`), so this always runs there.
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import gallery_signals as gs  # noqa: E402

HAVE_NGSPICE = shutil.which("ngspice") is not None
_SKIP_NO_NGSPICE = pytest.mark.skipif(
    not HAVE_NGSPICE, reason="ngspice is not installed on this machine"
)
_SKIP_NO_NETLISTS = pytest.mark.skipif(
    not gs.netlists_available(),
    reason=(
        "pdks/cell-netlists/ not populated -- run scripts/fetch-cell-netlists.sh first"
    ),
)

ALL_SLUGS = sorted(gs.CELL_CONFIGS)


# --------------------------------------------------------------------------- #
# Unit tests: CELL_CONFIGS shape (no ngspice, no vendored data needed)
# --------------------------------------------------------------------------- #


def test_seven_gallery_cells_configured():
    assert len(gs.CELL_CONFIGS) == 7
    sky130 = [s for s in ALL_SLUGS if gs.CELL_CONFIGS[s].pdk == "sky130"]
    gf180 = [s for s in ALL_SLUGS if gs.CELL_CONFIGS[s].pdk == "gf180mcu"]
    assert len(sky130) == 4
    assert len(gf180) == 3


@pytest.mark.parametrize("slug", ALL_SLUGS)
def test_cell_config_signal_roles_match_family(slug):
    cfg = gs.CELL_CONFIGS[slug]
    if cfg.family in ("comb1",):
        assert set(cfg.signal) == {"in", "out"}
    elif cfg.family == "comb2":
        assert set(cfg.signal) == {"in", "static", "out"}
    else:
        assert cfg.family == "dff"
        assert set(cfg.signal) == {"clk", "d", "out"}
    # Every signal role must name an actual formal port of the subckt.
    for pin in cfg.signal.values():
        assert pin in cfg.ports


@pytest.mark.parametrize("slug", ALL_SLUGS)
def test_build_request_shape(slug):
    cfg = gs.CELL_CONFIGS[slug]
    with tempfile.TemporaryDirectory() as work_dir:
        request = gs.build_request(cfg, work_dir)
        assert Path(request["netlist"]).is_file()
        assert Path(request["models"]["lib"]).is_file()
        assert request["corners"]["process"] == cfg.corners_process
        assert request["analysis"]["kind"] == "tran"
        assert request["measurements"]
        for meas in request["measurements"]:
            assert "name" in meas and "spice" in meas
        # Every request references the alterable `Vdd` source by name.
        netlist_text = Path(request["netlist"]).read_text()
        assert "\nVdd vdd_net 0 DC {vdd}" in netlist_text


def test_gf180_cells_get_documented_device_substitution():
    for slug in ALL_SLUGS:
        cfg = gs.CELL_CONFIGS[slug]
        if cfg.pdk != "gf180mcu":
            continue
        body = gs._cell_body_text(cfg)  # noqa: SLF001 -- unit-testing the helper directly
        assert "nfet_05v0" not in body
        assert "pfet_05v0" not in body
        assert "nmos_6p0" in body or "pmos_6p0" in body


def test_netlists_available_false_for_missing_root(tmp_path, monkeypatch):
    monkeypatch.setattr(gs, "NETLISTS_ROOT", str(tmp_path / "does-not-exist"))
    assert gs.netlists_available() is False


@pytest.mark.parametrize("slug", ALL_SLUGS)
def test_corner_matrix_is_extremes_plus_nominal(slug):
    """3 process x (4 PVT extremes + 1 nominal point) = 15 corners, with the
    six half-mixed supply/temperature combinations excluded."""
    from klayout_tools.sim import _expand_corners

    cfg = gs.CELL_CONFIGS[slug]
    with tempfile.TemporaryDirectory() as work_dir:
        request = gs.build_request(cfg, work_dir)
    points = _expand_corners(request["corners"], request["exclude"])
    ids = [p.corner_id for p in points]

    assert len(ids) == 15
    assert len(set(ids)) == 15
    # Every process corner contributes exactly one nominal point, and those
    # are precisely the ids `nominal_corner_ids` predicts without simulating.
    nominal = gs.nominal_corner_ids(cfg)
    assert len(nominal) == 3
    assert set(nominal) <= set(ids)
    # No half-mixed point survived: every non-nominal corner is at a supply
    # extreme AND a temperature extreme.
    for point in points:
        if point.corner_id in nominal:
            continue
        assert point.temperature_c in (-40, 125)
        assert point.supply_v["vdd"] != round(cfg.vdd_nominal, 4)


@pytest.mark.parametrize("slug", ALL_SLUGS)
def test_request_captures_waveforms_for_the_viewer(slug):
    """Issue #100's viewer needs real waveform artifacts, so the sweep must
    ask `klt sim` for them (see `docs/cli/sim.md`'s `options`)."""
    cfg = gs.CELL_CONFIGS[slug]
    with tempfile.TemporaryDirectory() as work_dir:
        request = gs.build_request(cfg, work_dir)
    assert request["options"]["waveforms"] is True
    assert request["options"]["keep_artifacts"] is True


def test_trim_waveform_projects_onto_testbench_pins(tmp_path):
    """The staged artifact keeps `klt sim`'s waveform shape but only the
    cell's own pins -- ngspice's rawfile also carries every internal
    device-body node (see the module docstring)."""
    cfg = gs.CELL_CONFIGS["sky130_fd_sc_hd__inv_1"]
    source = tmp_path / "waveform.raw.json"
    source.write_text(
        json.dumps(
            {
                "plotname": "Transient Analysis",
                "variables": [
                    {"index": 0, "name": "time", "type": "time"},
                    {"index": 1, "name": "v(a)", "type": "voltage"},
                    {"index": 2, "name": "v(m.x0.x0.mnfet#body)", "type": "voltage"},
                    {"index": 3, "name": "v(y)", "type": "voltage"},
                ],
                "points": [[0.0, 0.0, 0.0, 1.80000000001], [1e-10, 1.8, 0.0, 0.0]],
            }
        )
    )
    trimmed = gs._trim_waveform(str(source), cfg)  # noqa: SLF001 -- unit-testing the helper

    assert [v["name"] for v in trimmed["variables"]] == ["time", "v(a)", "v(y)"]
    # Re-indexed 0..n-1 so the artifact stays self-consistent.
    assert [v["index"] for v in trimmed["variables"]] == [0, 1, 2]
    assert trimmed["points"] == [[0.0, 0.0, 1.8], [1e-10, 1.8, 0.0]]


def test_trim_waveform_returns_none_for_unusable_artifact(tmp_path):
    cfg = gs.CELL_CONFIGS["sky130_fd_sc_hd__inv_1"]
    missing = tmp_path / "nope.json"
    assert gs._trim_waveform(str(missing), cfg) is None  # noqa: SLF001

    unrelated = tmp_path / "unrelated.json"
    unrelated.write_text(
        json.dumps(
            {
                "plotname": "Transient Analysis",
                "variables": [{"index": 0, "name": "time", "type": "time"}],
                "points": [[0.0]],
            }
        )
    )
    assert gs._trim_waveform(str(unrelated), cfg) is None  # noqa: SLF001


def test_stage_waveforms_writes_nominal_corners_and_drops_artifacts(tmp_path):
    """`_stage_waveforms` is the bridge to issue #100's viewer: nominal
    corners gain an `output/`-relative `waveform` path, every corner loses
    the response's machine-specific absolute `artifacts` paths."""
    cfg = gs.CELL_CONFIGS["sky130_fd_sc_hd__inv_1"]
    nominal_id = gs.nominal_corner_ids(cfg)[0]

    raw = tmp_path / "waveform.raw.json"
    raw.write_text(
        json.dumps(
            {
                "plotname": "Transient Analysis",
                "variables": [
                    {"index": 0, "name": "time", "type": "time"},
                    {"index": 1, "name": "v(a)", "type": "voltage"},
                    {"index": 2, "name": "v(y)", "type": "voltage"},
                ],
                "points": [[0.0, 0.0, 1.8]],
            }
        )
    )
    corners = [
        {"corner_id": nominal_id, "artifacts": {"waveform": str(raw), "log": "/tmp/x"}},
        {"corner_id": "tt/1.620V/-40C", "artifacts": {"waveform": str(raw)}},
    ]
    block_dir = tmp_path / "block"
    gs._stage_waveforms(cfg, corners, str(block_dir))  # noqa: SLF001

    assert corners[0]["waveform"] == f"signals/{nominal_id.replace('/', '_')}.json"
    assert "waveform" not in corners[1]  # a non-nominal corner is not staged
    assert all("artifacts" not in corner for corner in corners)
    staged = block_dir / "output" / corners[0]["waveform"]
    assert json.loads(staged.read_text())["variables"][0]["name"] == "time"


# --------------------------------------------------------------------------- #
# Integration: real ngspice against a real vendored cell netlist
# --------------------------------------------------------------------------- #


@_SKIP_NO_NGSPICE
@_SKIP_NO_NETLISTS
def test_real_sky130_inv_1_single_corner_produces_measurable_result():
    """The issue #99 test-plan requirement: `klt sim` against at least one
    real vendored cell netlist produces a passing/measurable result in CI.

    Trimmed to a single PVT corner (vs. the full 15-corner sweep
    `run_signals_for_block` runs for the actual gallery pipeline) so this
    stays fast in CI -- the point is proving the real transistor-level
    netlist + real vendored sky130 device models resolve and simulate
    correctly, not re-running the whole matrix on every PR.
    """
    cfg = gs.CELL_CONFIGS["sky130_fd_sc_hd__inv_1"]
    with tempfile.TemporaryDirectory() as work_dir:
        request = gs.build_request(cfg, work_dir)
        request["corners"]["process"] = ["tt"]
        request["corners"]["supply_v"] = {"vdd": [1.8]}
        request["corners"]["temperature_c"] = [27]
        request["exclude"] = []  # the full sweep's pruning is tested above
        request_path = str(Path(work_dir) / "request.json")
        with open(request_path, "w", encoding="utf-8") as handle:
            json.dump(request, handle)

        from klayout_tools.sim import run_sim

        response = run_sim(request_path)

    assert response["status"] == "pass"
    assert response["corner_count"] == 1
    # `corner_id()` predicts `klt sim`'s own corner naming (it has to: the
    # pipeline names `default_corner_id`/staged waveform files before the
    # sweep runs). Pin the two implementations together against real output.
    assert response["corners"][0]["corner_id"] == gs.corner_id("tt", 1.8, 27)
    by_name = {m["name"]: m for m in response["measurements"]}
    assert by_name["tphl"]["status"] == "pass"
    assert by_name["tplh"]["status"] == "pass"
    tphl_value = by_name["tphl"]["worst_case"]["value"]
    tplh_value = by_name["tplh"]["worst_case"]["value"]
    # Real sky130 inv_1 propagation delay is on the order of tens of
    # picoseconds at nominal PVT -- a loose sanity band, not a golden value
    # (no independent characterized reference is vendored here; see
    # gallery_signals.py's module docstring for why no `.meas` limits are
    # declared for the actual gallery sweep).
    assert 1e-12 < tphl_value < 1e-9
    assert 1e-12 < tplh_value < 1e-9


@_SKIP_NO_NGSPICE
@_SKIP_NO_NETLISTS
def test_real_gf180_and2_1_single_corner_produces_measurable_result():
    """Same as above, for one gf180mcu cell -- exercises the documented
    nfet_05v0/pfet_05v0 -> nmos_6p0/pmos_6p0 device substitution end to end
    against the real vendored gf180mcu primitive model deck."""
    cfg = gs.CELL_CONFIGS["gf180mcu_fd_sc_mcu9t5v0__and2_1"]
    with tempfile.TemporaryDirectory() as work_dir:
        request = gs.build_request(cfg, work_dir)
        request["corners"]["process"] = ["typical"]
        request["corners"]["supply_v"] = {"vdd": [5.0]}
        request["corners"]["temperature_c"] = [25]
        request["exclude"] = []
        request_path = str(Path(work_dir) / "request.json")
        with open(request_path, "w", encoding="utf-8") as handle:
            json.dump(request, handle)

        from klayout_tools.sim import run_sim

        response = run_sim(request_path)

    assert response["status"] == "pass"
    assert response["corner_count"] == 1
