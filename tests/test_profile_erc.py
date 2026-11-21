"""Smoke tests for `scripts/research/profile_erc.py` (issue #2229).

The profiler itself is a deliberate, manual research tool (its recorded
result is `docs/design/erc-runtime-profile.md`), but its bookkeeping is
easy to get subtly wrong -- nested sub-costs double-counted into the
top-level totals, or a wrapper left installed on `klayout_tools.erc`
after the run. These tests pin that bookkeeping on a tiny two-gate layout,
so they are cheap enough to run everywhere.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import klayout.db as kdb
import pytest

SCRIPTS_DIR = Path(__file__).parent.parent / "scripts" / "research"
sys.path.insert(0, str(SCRIPTS_DIR))

import profile_erc  # noqa: E402

from klayout_tools import erc  # noqa: E402

DBU = 0.001


def _um(v: float) -> int:
    return int(round(v / DBU))


def _layout(path: Path) -> None:
    """Two gate nets over an active (diffusion) strip: one strapped up to
    met1 and labelled, one bare poly bar."""
    layout = kdb.Layout()
    layout.dbu = DBU
    top = layout.create_cell("TOP")
    poly, active = layout.layer(1, 0), layout.layer(9, 0)
    licon, li1 = layout.layer(2, 0), layout.layer(3, 0)
    mcon, met1, met1_label = layout.layer(4, 0), layout.layer(5, 0), layout.layer(5, 5)

    top.shapes(active).insert(kdb.Box.new(_um(-1), _um(0.5), _um(12), _um(1.5)))
    top.shapes(poly).insert(kdb.Box.new(_um(0), _um(0), _um(1), _um(2)))
    top.shapes(licon).insert(kdb.Box.new(_um(0.2), _um(0.2), _um(0.4), _um(0.4)))
    top.shapes(li1).insert(kdb.Box.new(_um(0), _um(0), _um(2), _um(0.45)))
    top.shapes(mcon).insert(kdb.Box.new(_um(1.5), _um(0.1), _um(1.7), _um(0.3)))
    top.shapes(met1).insert(kdb.Box.new(_um(1.4), _um(0), _um(3), _um(0.4)))
    top.shapes(met1_label).insert(kdb.Text("A", kdb.Trans(_um(2), _um(0.2))))
    top.shapes(poly).insert(kdb.Box.new(_um(10), _um(0), _um(10.5), _um(2)))
    layout.write(str(path))


def _spec(path: Path) -> None:
    path.write_text(
        json.dumps(
            {
                "stackup": [
                    {
                        "name": "poly",
                        "layer": "1/0",
                        "role": "gate",
                        "active_layer": "9/0",
                    },
                    {"name": "li1", "layer": "3/0"},
                    {"name": "met1", "layer": "5/0", "label_layer": "5/5"},
                ],
                "vias": [
                    {"name": "licon", "layer": "2/0", "between": ["poly", "li1"]},
                    {"name": "mcon", "layer": "4/0", "between": ["li1", "met1"]},
                ],
            }
        )
    )


@pytest.mark.parametrize("findings_only", [False, True])
def test_phases_partition_end_to_end_and_report_is_unchanged(
    tmp_path, capsys, findings_only
):
    gds, spec = tmp_path / "t.gds", tmp_path / "s.json"
    _layout(gds)
    _spec(spec)
    originals = {name: getattr(erc, name) for name in dir(erc) if name.startswith("_")}

    argv = [str(gds), str(spec), "--check-identical", "--attribute-walk"]
    if findings_only:
        argv.append("--findings-only")
    assert profile_erc.main(argv) == 0
    out = json.loads(capsys.readouterr().out)

    # Every wrapper was removed again.
    for name, fn in originals.items():
        assert getattr(erc, name) is fn, name

    assert out["mode"] == ("findings_only" if findings_only else "full")
    assert out["identical_to_uninstrumented"] is True
    (run,) = out["runs"]
    top = run["top_level"]
    assert set(top) == {
        "primary_extraction",
        "candidate_walk",
        "tie_extraction",
        "remainder",
    }
    # Non-overlapping totals: they partition end-to-end (rounding aside).
    assert sum(v["seconds"] for v in top.values()) == pytest.approx(
        run["end_to_end_s"], abs=0.005
    )
    assert top["tie_extraction"]["seconds"] == 0.0  # no ties[] declared
    assert all(v["seconds"] >= 0 for v in top.values())

    # Nested walk sub-costs are contained in the walk, never added to it.
    walk = run["candidate_walk_breakdown"]
    nested = "_gate_is_floating" if findings_only else "_accumulated_levels"
    assert walk[nested]["calls"] == 2
    assert (
        sum(v["seconds"] for v in walk.values())
        <= top["candidate_walk"]["seconds"] + 0.005
    )
    assert walk["_region"]["calls"] == 1  # the active_layer region build

    assert run["counts"]["gates"] == 2
    assert run["counts"]["candidates"] == 2
    assert out["walk_attribution"]["gate_nets"] == 2
    assert out["walk_attribution"]["active_region_polygons"] == 1


# --- issue #2753: sampled attribution without a full profiling run ---


def _attribution_argv(gds: Path, spec: Path, limit: int) -> list[str]:
    return [
        str(gds),
        str(spec),
        "--repeat",
        "0",
        "--attribute-walk",
        "--attribute-walk-limit",
        str(limit),
    ]


def test_attribution_only_runs_no_erc(tmp_path, capsys, monkeypatch):
    """`--repeat 0 --attribute-walk` never runs a profile or plain ERC pass.

    The documented sampled-attribution recipe must not pay for a full
    unsampled `run_erc` first (issue #2753): every entry point that would
    run ERC is replaced by a fail-on-call sentinel while the real
    attribution function exercises the tiny layout.
    """
    gds, spec = tmp_path / "t.gds", tmp_path / "s.json"
    _layout(gds)
    _spec(spec)
    originals = {name: getattr(erc, name) for name in dir(erc) if name.startswith("_")}

    def _fail(name: str):
        raise AssertionError(f"{name} must not run in attribution-only mode")

    monkeypatch.setattr(
        profile_erc, "_one_profile", lambda args: _fail("profile_erc._one_profile")
    )
    monkeypatch.setattr(profile_erc, "_run", lambda args: _fail("profile_erc._run"))
    monkeypatch.setattr(erc, "run_erc", lambda *a, **k: _fail("erc.run_erc"))

    assert profile_erc.main(_attribution_argv(gds, spec, 1)) == 0
    out = json.loads(capsys.readouterr().out)

    # No profiling wrappers were installed, and none were left behind.
    for name, fn in originals.items():
        assert getattr(erc, name) is fn, name

    assert out["runs"] == []
    assert "identical_to_uninstrumented" not in out
    assert "cprofile_top_tottime" not in out
    walk = out["walk_attribution"]
    assert set(walk) == {
        "candidates_timed",
        "gate_nets",
        "active_layer",
        "active_region_polygons",
        "seconds",
        "ms_per_candidate",
    }
    assert set(walk["seconds"]) == {"gate_poly_merged", "and_active_merged", "area"}
    assert set(walk["ms_per_candidate"]) == set(walk["seconds"])
    assert walk["active_region_polygons"] == 1


@pytest.mark.parametrize("limit, expected", [(1, 1), (0, 2), (10, 2)])
def test_attribute_walk_limit_samples_in_cluster_id_order(
    tmp_path, capsys, monkeypatch, limit, expected
):
    """`--attribute-walk-limit N` times the first N candidates of the
    cluster_id sort (0 = all), even beyond the fixture's candidate count."""
    gds, spec = tmp_path / "t.gds", tmp_path / "s.json"
    _layout(gds)
    _spec(spec)

    timed_ids: list[int] = []
    real_extract = erc._extract_connectivity

    class _RecordingL2N:
        """Record the cluster_id of every net the replica times."""

        def __init__(self, l2n):
            self._l2n = l2n

        def polygons_of_net(self, net, gate_index):
            timed_ids.append(net.cluster_id)
            return self._l2n.polygons_of_net(net, gate_index)

    def recording_extract(*args, **kwargs):
        l2n, circuit, layer_index, extra = real_extract(*args, **kwargs)
        return _RecordingL2N(l2n), circuit, layer_index, extra

    monkeypatch.setattr(erc, "_extract_connectivity", recording_extract)

    assert profile_erc.main(_attribution_argv(gds, spec, limit)) == 0
    out = json.loads(capsys.readouterr().out)

    walk = out["walk_attribution"]
    assert walk["candidates_timed"] == expected
    # Both fixture candidates are gate nets over the active strip.
    assert walk["gate_nets"] == expected
    # Candidates only, sorted by cluster_id, sliced once to the limit.
    assert len(timed_ids) == expected
    assert all(cid != 0 for cid in timed_ids)
    assert timed_ids == sorted(timed_ids)
    assert len(set(timed_ids)) == expected
    assert out["runs"] == []


def test_attribute_walk_rejects_devices_and_deck(tmp_path, capsys, monkeypatch):
    """Unsupported devices[]/--deck configs keep their clear error, and the
    rejection happens without any ERC run."""
    gds, spec = tmp_path / "t.gds", tmp_path / "s.json"
    _layout(gds)
    _spec(spec)
    devices_spec = tmp_path / "dev.json"
    devices_spec.write_text(json.dumps({"devices": [{"name": "x"}]}))

    def _fail(name):
        raise AssertionError(f"{name} must not run before the rejection")

    monkeypatch.setattr(profile_erc, "_one_profile", lambda args: _fail("_one_profile"))
    monkeypatch.setattr(erc, "run_erc", lambda *a, **k: _fail("erc.run_erc"))

    with pytest.raises(SystemExit, match="does not support devices"):
        profile_erc.main(
            [str(gds), str(devices_spec), "--repeat", "0", "--attribute-walk"]
        )
    with pytest.raises(SystemExit, match="does not support devices"):
        profile_erc.main(
            [
                str(gds),
                str(spec),
                "--repeat",
                "0",
                "--attribute-walk",
                "--deck",
                "sky130",
            ]
        )
    capsys.readouterr()  # drain the argparse usage output
