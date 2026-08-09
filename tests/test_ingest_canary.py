"""Tests for `scripts/ingest-canary.py` (issue #62, "block content pipeline").

Two tiers:

- **Unit tests** (always run, no network): the public-repo gate (mocked
  `gh api` calls), the markdown parsers (`parse_spec_summary`,
  `build_signals`'s bullet/table nominal extraction and suite-summary
  rollup) against inline fixture text mirroring the two real canary repos'
  actual record formats, and the forward-compat GDS-found upgrade path
  against a synthetic corpus-style GDS.
- **Integration tests** (`@pytest.mark.skipif`, real network + `gh`):
  the actual public-repo gate against a real private canary, dynamically
  selected at test time from `blocks/README.md`'s "still-private" canary
  list (falling back to a `pytest.skip` -- not a false pass or a failure --
  if every candidate has since gone public), and a full end-to-end ingest
  of both real public canaries (`2AMLogic/gf180-bandgap`,
  `2AMLogic/sky130-bandgap`). Skipped (with a clear reason, never silently
  passing) when `gh`/network access isn't available -- mirrors
  `tests/test_gallery_signals.py`'s `HAVE_NGSPICE` skip convention.
"""

from __future__ import annotations

import importlib.util
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import klayout.db as kdb
import pytest

SCRIPTS_DIR = Path(__file__).parent.parent / "scripts"


def _load_ingest_canary():
    spec = importlib.util.spec_from_file_location(
        "ingest_canary", SCRIPTS_DIR / "ingest-canary.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["ingest_canary"] = module
    spec.loader.exec_module(module)
    return module


ic = _load_ingest_canary()


def _have_gh_network() -> bool:
    """True if `gh api` can reach GitHub right now (real network + a `gh`
    binary that is at least unauthenticated-usable for public repo reads)."""
    if shutil.which("gh") is None:
        return False
    result = subprocess.run(
        ["gh", "api", "repos/2AMLogic/klayout-tools", "--jq", ".visibility"],
        capture_output=True,
        text=True,
        timeout=15,
    )
    return result.returncode == 0 and result.stdout.strip() == "public"


HAVE_GH_NETWORK = _have_gh_network()
_SKIP_NO_NETWORK = pytest.mark.skipif(
    not HAVE_GH_NETWORK,
    reason="gh CLI + network access to GitHub not available in this environment",
)


# --------------------------------------------------------------------------- #
# Public-repo gate (mocked)
# --------------------------------------------------------------------------- #


def test_assert_public_repo_allows_public(monkeypatch):
    monkeypatch.setattr(ic, "repo_visibility", lambda repo: "public")
    ic.assert_public_repo("2AMLogic/gf180-bandgap")  # does not raise


def test_assert_public_repo_refuses_private(monkeypatch):
    monkeypatch.setattr(ic, "repo_visibility", lambda repo: "private")
    with pytest.raises(ic.IngestError, match='not "public"'):
        ic.assert_public_repo("2AMLogic/gf180-trng")


def test_assert_public_repo_fails_closed_on_api_error(monkeypatch):
    def _raise(repo):
        raise ic.IngestError("gh api failed: 404 Not Found")

    monkeypatch.setattr(ic, "repo_visibility", _raise)
    with pytest.raises(ic.IngestError):
        ic.assert_public_repo("2AMLogic/gf180-trng")


def test_repo_visibility_raises_on_nonzero_exit(monkeypatch):
    class FakeResult:
        returncode = 1
        stdout = ""
        stderr = "gh: not found"

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: FakeResult())
    with pytest.raises(ic.IngestError):
        ic.repo_visibility("2AMLogic/gf180-trng")


def test_ingest_writes_nothing_when_gate_refuses(tmp_path, monkeypatch):
    monkeypatch.setattr(ic, "repo_visibility", lambda repo: "private")
    blocks_dir = tmp_path / "blocks"
    with pytest.raises(ic.IngestError):
        ic.ingest(
            "2AMLogic/gf180-trng",
            blocks_dir=blocks_dir,
            cache_dir=tmp_path / "cache",
        )
    assert not blocks_dir.exists()


# --------------------------------------------------------------------------- #
# `parse_spec_summary`
# --------------------------------------------------------------------------- #

_GF180_README = """\
# gf180-bandgap

Intro paragraph.

## Target specification (RATIFIED 2026-07-31, see issue #1 and #35)

Some prose about ratification.

| Parameter | Target | Stretch | Corner binding |
|---|---|---|---|
| Output reference | 1.20 V +/-2% untrimmed | +/-0.5% trimmed | temperature extremes |
| Supply | 3.3 V +/-10% | also 5 V flavor | headroom binds at SS |

Rows marked TBD are open items.

## Layout

more stuff
"""

_SKY130_README = """\
# sky130-bandgap

## Target specification (DRAFT — engineering to ratify, see issue #1)

| Parameter | Target | Stretch |
|---|---|---|
| Output reference | 1.20 V +/-1% untrimmed | +/-0.5% with trim |
| Iq | < 50 uA | < 20 uA |

## Environment setup
"""


def test_parse_spec_summary_gf180_shape():
    result = ic.parse_spec_summary(_GF180_README)
    assert result is not None
    assert result["status_note"] == "RATIFIED 2026-07-31, see issue #1 and #35"
    assert len(result["rows"]) == 2
    row = result["rows"][0]
    assert row["parameter"] == "Output reference"
    assert row["target"] == "1.20 V +/-2% untrimmed"
    assert row["corner_binding"] == "temperature extremes"


def test_parse_spec_summary_sky130_shape_no_corner_binding_column():
    result = ic.parse_spec_summary(_SKY130_README)
    assert result is not None
    assert result["status_note"].startswith("DRAFT")
    assert len(result["rows"]) == 2
    assert "corner_binding" not in result["rows"][0]


def test_parse_spec_summary_absent_returns_none():
    assert ic.parse_spec_summary("# no spec section here\n\nJust prose.\n") is None


# --------------------------------------------------------------------------- #
# `build_signals`: bullet-format record (sky130-bandgap style)
# --------------------------------------------------------------------------- #

_BULLET_RECORD = """\
# Record 20260731-043353-a8c4147

- **Record ID**: 20260731-043353-a8c4147
- **Experiment**: `pnp-characterization` — Substrate PNP VBE
- **Tools**: ngspice-46 : Circuit level simulation program
- **Result**:
  - tt_-40c_3.30v: PASS (veb_small_100n=0.638694V; dvbe_100n=0.00545338V)
  - tt_27c_3.30v: PASS (veb_small_100n=0.48467V; dvbe_100n=0.0143955V)
  - ss_125c_3.30v: PASS (veb_small_100n=0.261922V; dvbe_100n=0.0375146V)
  - corner-sensitivity check on `veb_small_1u`: PASS (spread 0.34, req >= 0.01)
  - **Overall: PASS**
"""


def test_build_signals_bullet_format(tmp_path):
    exp_dir = tmp_path / "sim" / "pnp-characterization" / "records"
    exp_dir.mkdir(parents=True)
    (exp_dir / "20260731-043353-a8c4147.md").write_text(_BULLET_RECORD)

    signals = ic.build_signals(tmp_path / "sim")
    assert signals is not None
    assert signals["schema_version"] == 1
    assert signals["engine"] == "ngspice"
    assert signals["engine_version"] == "46"
    assert signals["status"] == "pass"
    assert signals["passed"] == 1
    assert signals["failed"] == 0
    assert signals["corner_count"] == 1

    corner = signals["corners"][0]
    assert corner["corner_id"] == "pnp-characterization/tt_27c_3.30v"
    assert corner["status"] == "pass"
    names = {m["name"] for m in corner["measurements"]}
    assert "veb_small_100n" in names
    veb = next(m for m in corner["measurements"] if m["name"] == "veb_small_100n")
    assert veb["value"] == pytest.approx(0.48467)
    assert veb["unit"] == "V"

    # No suite/summaries dir -> falls back to a per-experiment measurement.
    assert signals["measurements"][0]["name"] == "Pnp Characterization"
    assert signals["measurements"][0]["status"] == "pass"


# --------------------------------------------------------------------------- #
# `build_signals`: table-format record + suite summary (gf180-bandgap style)
# --------------------------------------------------------------------------- #

_TABLE_RECORD = """\
# Record 20260801-032252-82ea7db

- **Record ID**: 20260801-032252-82ea7db
- **Tools**: ngspice-46 : Circuit level simulation program
- **Result**:

  | corner-id | vref | tc_ppm | sweep_points | pass/fail |
  |---|---|---|---|---|
  | `tt_-40c_2.97v` | 1.21886 | 103.368 | 166 | FAIL — tc_ppm max=50 (got 103.368) |
  | `tt_27c_3.30v` | 1.22912 | 96.8127 | 166 | FAIL — vref max=1.224 (got 1.22912) |
  | `bjt_ss_125c_2.97v` | 1.2523 | 135.125 | 166 | FAIL — vref max=1.224 (got 1.2523) |

  - **Overall: FAIL**
"""

_SUITE_SUMMARY = """\
# Suite summary 20260801-032248-82ea7db

**NOT simulation-complete**: 6 of 6 claimed spec lines FAIL against the ratified table.

## Per-spec-line verdict

| Spec row | Target | Bench | Worst measured | At corner | Verdict |
|---|---|---|---|---|---|
| Output reference | 1.2V+/-2% | `ovt` | 1.2523 V (lim<=1.224) | `bjt_ss_125c` | FAIL |
| Quiescent current | <50uA | `iq` | 73.9483 uA (lim<=50) | `ff_125c` | FAIL |

## Benches and their evidence

| Bench | Corners run | Record | DUT | Runner status |
|---|---|---|---|---|
| `output-voltage-tc` | 81 | x | y | z |
"""


def test_build_signals_table_format_and_suite_summary(tmp_path):
    sim_dir = tmp_path / "sim"
    records_dir = sim_dir / "output-voltage-tc" / "records"
    records_dir.mkdir(parents=True)
    (records_dir / "20260801-032252-82ea7db.md").write_text(_TABLE_RECORD)

    summaries_dir = sim_dir / "suite" / "summaries"
    summaries_dir.mkdir(parents=True)
    (summaries_dir / "20260801-032248-82ea7db.md").write_text(_SUITE_SUMMARY)

    signals = ic.build_signals(sim_dir)
    assert signals is not None
    assert signals["status"] == "fail"
    assert signals["failed"] == 1

    corner = signals["corners"][0]
    assert corner["corner_id"] == "output-voltage-tc/tt_27c_3.30v"
    assert corner["status"] == "fail"
    vref = next(m for m in corner["measurements"] if m["name"] == "vref")
    assert vref["value"] == pytest.approx(1.22912)

    # Suite summary present -> measurements rollup uses it, not the fallback.
    names = [m["name"] for m in signals["measurements"]]
    assert names == ["Output reference", "Quiescent current"]
    m0 = signals["measurements"][0]
    assert m0["status"] == "fail"
    assert m0["worst_case"]["corner_id"] == "bjt_ss_125c"
    assert m0["worst_case"]["value"] == pytest.approx(1.2523)
    assert m0["unit"] == "V"


def test_build_signals_excludes_reference_only_experiments(tmp_path):
    """A record with no `**Overall: ...**` verdict (device-characterization
    reference data) is excluded, not miscoded as pass/fail/error."""
    sim_dir = tmp_path / "sim"
    records_dir = sim_dir / "device-pnp-vbe" / "records"
    records_dir.mkdir(parents=True)
    (records_dir / "20260731-030932-8fb0ea6.md").write_text(
        "# Record\n\n- **Claim**: measured device data, no spec comparison.\n"
        "\n| Corner | T | V |\n|---|---|---|\n| bjt_ss | -40 | 0.79 |\n"
    )
    assert ic.build_signals(sim_dir) is None


def test_build_signals_no_sim_dir_returns_none(tmp_path):
    assert ic.build_signals(tmp_path / "sim") is None


# --------------------------------------------------------------------------- #
# GDS-found upgrade path (forward-compat, synthetic fixture)
# --------------------------------------------------------------------------- #


def _make_layout() -> kdb.Layout:
    layout = kdb.Layout()
    top = layout.create_cell("TOP")
    m1 = layout.layer(1, 0)
    top.shapes(m1).insert(kdb.Box(0, 0, 10, 10))
    return layout


def test_gds_found_upgrades_to_ok_status(tmp_path):
    repo_dir = tmp_path / "repo"
    (repo_dir / "layout").mkdir(parents=True)
    _make_layout().write(str(repo_dir / "layout" / "block.gds"))

    block_dir = tmp_path / "blocks" / "some-block"
    layout = ic.build_layout_json(
        repo_dir,
        repo="2AMLogic/some-block",
        ref="deadbeef",
        slug="some-block",
        block_dir=block_dir,
    )
    assert layout["status"] == "ok"
    assert layout["layer_count"] == 1
    assert layout["layout_file"] == "block.gds"
    assert (block_dir / "output" / "block.gds").is_file()

    # Issue #651, Option A: a found GDS also gets the gallery-thumbnail
    # composite rendered and attached -- only the fixed "overview" key, not
    # one entry per per-layer PNG (those stay `.gitignore`d on disk).
    assert layout["renders"] == {"overview": "renders/overview.png"}
    assert (block_dir / "output" / "renders" / "overview.png").is_file()


def test_gds_found_dry_run_skips_render(tmp_path):
    """`--dry-run` must not write any files -- including render output."""
    repo_dir = tmp_path / "repo"
    (repo_dir / "layout").mkdir(parents=True)
    _make_layout().write(str(repo_dir / "layout" / "block.gds"))

    block_dir = tmp_path / "blocks" / "some-block"
    layout = ic.build_layout_json(
        repo_dir,
        repo="2AMLogic/some-block",
        ref="deadbeef",
        slug="some-block",
        block_dir=block_dir,
        dry_run=True,
    )
    assert layout["status"] == "ok"
    assert "renders" not in layout
    assert not (block_dir / "output").exists()


def test_render_failure_is_best_effort(tmp_path, monkeypatch):
    """A render failure must not fail the whole ingest or downgrade status
    -- mirrors the DRC/signals fields' opt-in, best-effort treatment."""
    repo_dir = tmp_path / "repo"
    (repo_dir / "layout").mkdir(parents=True)
    _make_layout().write(str(repo_dir / "layout" / "block.gds"))

    def _boom(*args, **kwargs):
        raise ic.RenderError("simulated render failure")

    monkeypatch.setattr(ic, "render_report", _boom)

    block_dir = tmp_path / "blocks" / "some-block"
    layout = ic.build_layout_json(
        repo_dir,
        repo="2AMLogic/some-block",
        ref="deadbeef",
        slug="some-block",
        block_dir=block_dir,
    )
    assert layout["status"] == "ok"
    assert "renders" not in layout


def test_no_gds_yields_in_design_status(tmp_path):
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    block_dir = tmp_path / "blocks" / "some-block"
    layout = ic.build_layout_json(
        repo_dir,
        repo="2AMLogic/some-block",
        ref="deadbeef",
        slug="some-block",
        block_dir=block_dir,
    )
    assert layout["status"] == ic.IN_DESIGN_STATUS
    assert "layout_file" not in layout
    assert layout["source"] == {
        "repo": "2AMLogic/some-block",
        "ref": "deadbeef",
        "path": ".",
    }
    assert layout["downloadable"] is True


# --------------------------------------------------------------------------- #
# Integration: real network + gh CLI
# --------------------------------------------------------------------------- #


# Candidates for the "real private canary" integration fixture below, per
# `blocks/README.md`'s "still-private canaries" list. Deliberately not a
# single hardcoded repo: any one of these graduates to public on its own
# lifecycle schedule (that is the point of the pipeline), so this test picks
# whichever candidate is *actually* still private right now rather than
# hard-depending on one repo's mutable visibility state (see #489).
_PRIVATE_CANARY_CANDIDATES = (
    "2AMLogic/gf180-trng",
    "2AMLogic/gf180-sar-adc",
    "2AMLogic/gf180-pll",
    "2AMLogic/gf180-temp-por",
    "2AMLogic/gf180-ldo",
)


def _try_repo_visibility(repo: str) -> str | None:
    """`ic.repo_visibility(repo)`, or `None` on any failure (e.g. no access).

    Used only to *select* a fixture repo from a candidate list -- an API
    error for one candidate (permissions, rate limit, etc.) should not fail
    the whole test, just rule that candidate out.
    """
    try:
        return ic.repo_visibility(repo)
    except ic.IngestError:
        return None


@_SKIP_NO_NETWORK
def test_gate_refuses_real_private_canary(tmp_path):
    repo = next(
        (r for r in _PRIVATE_CANARY_CANDIDATES if _try_repo_visibility(r) == "private"),
        None,
    )
    if repo is None:
        pytest.skip(
            "no candidate in _PRIVATE_CANARY_CANDIDATES is currently private "
            "(all have gone public) -- see #489; this is a fixture-"
            "availability gap, not a gate-logic failure, and does not "
            "exercise the real-network private-repo path right now"
        )

    blocks_dir = tmp_path / "blocks"
    with pytest.raises(ic.IngestError, match=re.escape(repo.split("/")[-1])):
        ic.ingest(
            repo,
            blocks_dir=blocks_dir,
            cache_dir=tmp_path / "cache",
        )
    assert not blocks_dir.exists()


@_SKIP_NO_NETWORK
@pytest.mark.parametrize("repo", ["2AMLogic/gf180-bandgap", "2AMLogic/sky130-bandgap"])
def test_full_ingest_real_public_canary(tmp_path, repo):
    blocks_dir = tmp_path / "blocks"
    layout = ic.ingest(repo, blocks_dir=blocks_dir, cache_dir=tmp_path / "cache")

    slug = repo.split("/")[-1]
    out_path = blocks_dir / slug / "output" / "layout.json"
    assert out_path.is_file()
    on_disk = json.loads(out_path.read_text())
    assert on_disk == layout

    assert layout["schema_version"] == 1
    assert layout["slug"] == slug
    assert layout["status"] == ic.IN_DESIGN_STATUS
    assert layout["source"]["repo"] == repo
    assert len(layout["source"]["ref"]) == 40  # a full git sha
    assert layout["downloadable"] is True
    # Both wave-1 canaries ship a Target specification table.
    assert "spec_summary" in layout
    assert layout["spec_summary"]["rows"]
