"""Tests for `scripts/bootstrap-gallery-blocks.py`'s render step (issue
#651, epic #650 "gallery visuals" phase 1).

Only the render-attachment helper is unit-tested here (not the whole
`bootstrap_block()`/`main()` flow, which shells out to `klt` and touches
the real `blocks/`/`tests/corpus/` trees) -- mirrors
`tests/test_ingest_canary.py`'s coverage of the same helper on the
canary-ingest side.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import klayout.db as kdb
import pytest

SCRIPTS_DIR = Path(__file__).parent.parent / "scripts"


def _load_bootstrap_gallery_blocks():
    spec = importlib.util.spec_from_file_location(
        "bootstrap_gallery_blocks", SCRIPTS_DIR / "bootstrap-gallery-blocks.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["bootstrap_gallery_blocks"] = module
    spec.loader.exec_module(module)
    return module


bg = _load_bootstrap_gallery_blocks()


def _make_gds(path: Path) -> None:
    layout = kdb.Layout()
    top = layout.create_cell("TOP")
    m1 = layout.layer(1, 0)
    top.shapes(m1).insert(kdb.Box(0, 0, 10, 10))
    layout.write(str(path))


def test_attach_overview_render_writes_overview_and_fixed_renders_key(tmp_path):
    gds_path = tmp_path / "block.gds"
    _make_gds(gds_path)
    block_dir = tmp_path / "blocks" / "some-block"
    layout: dict = {"slug": "some-block"}

    bg._attach_overview_render("some-block", block_dir, gds_path, layout)

    # Option A (#651): only the fixed "overview" key is recorded, not one
    # entry per per-layer PNG -- even though render_report also writes
    # per-layer PNGs alongside it (real files, just not listed here; they
    # stay `.gitignore`d).
    assert layout["renders"] == {"overview": "renders/overview.png"}
    assert (block_dir / "output" / "renders" / "overview.png").is_file()
    assert (block_dir / "output" / "renders" / "1_0.png").is_file()


def test_attach_overview_render_is_best_effort_on_failure(
    tmp_path, monkeypatch, capsys
):
    def _boom(*args, **kwargs):
        raise bg.RenderError("simulated render failure")

    monkeypatch.setattr(bg, "render_report", _boom)

    block_dir = tmp_path / "blocks" / "some-block"
    layout: dict = {"slug": "some-block"}
    bg._attach_overview_render(
        "some-block", block_dir, tmp_path / "missing.gds", layout
    )

    assert "renders" not in layout
    assert "skipping render" in capsys.readouterr().out


@pytest.mark.parametrize("slug", sorted(bg.NO_ARTIFACTS_SLUGS))
def test_no_artifacts_slugs_stay_out_of_committed_renders(slug):
    """Sanity check on the module's own data: a `NO_ARTIFACTS_SLUGS` entry
    never reaches `_attach_overview_render` (see `bootstrap_block`'s early
    return) -- this just documents/pins that set doesn't silently grow to
    include a block this test suite expects to have real renders."""
    assert slug not in {
        "gf180mcu_fd_sc_mcu9t5v0__and2_1",
        "gf180mcu_fd_sc_mcu9t5v0__dffnq_1",
        "sky130_fd_sc_hd__buf_4",
        "sky130_fd_sc_hd__dfxtp_2",
        "sky130_fd_sc_hd__inv_1",
        "sky130_fd_sc_hd__nand2_2",
    }
