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
