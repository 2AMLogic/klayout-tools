"""`blocks[].source_digest` is the `klt stats --geometry-digest` value (#7540).

`source_digest` (#2065) is a digest of the *decoded geometry* of the source
stream, not of its bytes, so a raw-byte `sha256sum` never matches it. The
public recompute path is `klt stats --geometry-digest`; this module pins the
two surfaces together so they cannot silently diverge.

The fixture and helpers are local on purpose (the convention every sibling
``test_gen_compose_*.py`` module follows) so this stays out of the oversized
``test_gen_compose.py`` monolith, whose per-file size ratchet forbids growth.
"""

import pytest

from klayout_tools import pdk
from klayout_tools.gen_compose import compose
from klayout_tools.stats import stats_report


def _make_install(root, variant):
    (root / variant / "libs.tech").mkdir(parents=True)


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    monkeypatch.delenv("PDK_ROOT", raising=False)
    monkeypatch.delenv("PDK", raising=False)
    monkeypatch.setattr(pdk, "STORE_DIRS", [])
    monkeypatch.setattr(pdk, "CONVENTIONAL_PREFIXES", [])


@pytest.fixture()
def pdk_root(tmp_path):
    root = tmp_path / "pdk_install"
    _make_install(root, "sky130A")
    return root


def _write_boxed_gds(path, cell_name, boxes_um):
    """Write `cell_name` holding one li1 (67/20) rectangle per entry of
    `boxes_um` (each `(x0, y0, x1, y1)` in um)."""
    import klayout.db as kdb

    layout = kdb.Layout()
    layout.dbu = 0.001
    li1 = layout.layer(67, 20)
    cell = layout.create_cell(cell_name)
    for x0, y0, x1, y1 in boxes_um:
        cell.shapes(li1).insert(
            kdb.Box(
                int(round(x0 / layout.dbu)),
                int(round(y0 / layout.dbu)),
                int(round(x1 / layout.dbu)),
                int(round(y1 / layout.dbu)),
            )
        )
    layout.write(str(path))
    return str(path)


def _compose_one_cell_block(tmp_path, pdk_root, gds, cell_name, tag):
    """Compose the single library cell `cell_name` out of `gds`, and return
    the response's one `blocks[]` entry."""
    report = compose(
        {
            "pdk": {"variant": "sky130A", "root": str(pdk_root)},
            "blocks": [{"id": "u1", "cell": {"gds_path": gds, "cell_name": cell_name}}],
            "placement": {"strategy": "row", "order": ["u1"], "spacing_um": 1.0},
            "options": {
                "cell_name": f"{tag}_0",
                "output": str(tmp_path / f"{tag}.gds"),
            },
        }
    )
    return report["blocks"][0]


def test_compose_source_digest_equals_klt_stats_geometry_digest(tmp_path, pdk_root):
    # The public recompute path: `source_digest` is the same decoded-geometry
    # digest `klt stats --geometry-digest` reports for the source stream, so
    # a consumer holding the committed input re-verifies a committed compose
    # response with that one command (a raw-byte `sha256sum` never matches).
    # This pins the two public surfaces together -- they cannot silently
    # diverge without this test failing.
    lib = _write_boxed_gds(tmp_path / "lib.gds", "lib_inv", [(0.0, 0.0, 2.0, 1.2)])

    block = _compose_one_cell_block(tmp_path, pdk_root, lib, "lib_inv", "eq")

    assert block["source_digest"] is not None
    assert (
        block["source_digest"]
        == stats_report(lib, geometry_digest=True)["geometry_digest"]
    )
