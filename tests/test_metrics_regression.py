"""Golden-metrics regression net (issue #248).

Unlike `tests/test_corpus.py`/`tests/test_render_corpus.py` (byte/structure
comparison of a `klt` verb's full output), this module pins a *tolerance-
banded scalar-metric* snapshot -- device counts, cell/instance counts, DRC
violation counts -- of the sky130 5T OTA `klt gen-compose` -> `klt extract`
-> `klt drc` pipeline (the same worked example `docs/cli/gen-compose.md`
documents, and the same block shape `tests/test_gen_compose.py`'s
`test_compose_labeled_net_survives_extraction_as_named_pin` already
exercises) against `tests/golden_metrics/sky130_5t_ota_gen_compose.json`.

The helper itself (`tests/helpers/metrics_regression.py`) is also
unit-tested below, independent of any real `klt` verb.

Regenerating the golden file after a deliberate metric change:

    python tests/golden_metrics/generate_golden_metrics.py

See `tests/golden_metrics/README.md`.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from helpers.metrics_regression import (
    MetricsMismatchError,
    assert_metrics_match,
    diff_metrics,
    flatten_metrics,
    load_golden_metrics,
)
from klayout_tools import drc, extract, gen, gen_compose, pdk

GOLDEN_DIR = Path(__file__).parent / "golden_metrics"
OTA_GOLDEN_PATH = GOLDEN_DIR / "sky130_5t_ota_gen_compose.json"


# --------------------------------------------------------------------------- #
# Shared fixture builder -- also used by generate_golden_metrics.py, kept
# here (rather than duplicated) so the regenerator can never drift from what
# the regression test itself actually runs.
# --------------------------------------------------------------------------- #


def build_5t_ota_metrics(tmp_path: Path, pdk_root: Path) -> dict[str, float | int]:
    """Run the 5T OTA `gen-compose` -> `extract` -> `drc` pipeline and return
    a flattened metrics dict, merged across all three verbs' JSON responses
    under `gen_compose.`/`extract.`/`drc.` prefixes.
    """
    dp = gen.generate(
        {
            "schema": gen.REQUEST_SCHEMA,
            "generator": "diff_pair",
            "pdk": {"variant": "sky130A", "root": str(pdk_root)},
            "params": {"mirror": False, "add_guard_ring": False},
            "options": {"cell_name": "dp", "output": str(tmp_path / "dp.gds")},
        }
    )
    mir = gen.generate(
        {
            "schema": gen.REQUEST_SCHEMA,
            "generator": "diff_pair",
            "pdk": {"variant": "sky130A", "root": str(pdk_root)},
            "params": {"mirror": True, "add_guard_ring": False},
            "options": {"cell_name": "mir", "output": str(tmp_path / "mir.gds")},
        }
    )
    tail = gen.generate(
        {
            "schema": gen.REQUEST_SCHEMA,
            "generator": "mos_array",
            "pdk": {"variant": "sky130A", "root": str(pdk_root)},
            "params": {"rows": 1, "cols": 1},
            "options": {"cell_name": "tail", "output": str(tmp_path / "tail.gds")},
        }
    )

    output = tmp_path / "ota_top_0.gds"
    compose_report = gen_compose.compose(
        {
            "schema": gen_compose.REQUEST_SCHEMA,
            "pdk": {"variant": "sky130A", "root": str(pdk_root)},
            "blocks": [
                {"id": "diffpair", "generator_report": dp},
                {"id": "mirror", "generator_report": mir},
                {"id": "tail", "generator_report": tail},
            ],
            "placement": {
                "strategy": "row",
                "order": ["diffpair", "mirror", "tail"],
                "spacing_um": 2.0,
            },
            "connectivity": [
                {
                    "net": "VOUT",
                    "pins": [
                        {"block": "diffpair", "port": "Q1_1_D"},
                        {"block": "mirror", "port": "M1_1_S"},
                    ],
                },
            ],
            "routing": {"layer_role": "metal", "width_um": 0.17},
            "options": {"cell_name": "ota_top_0", "output": str(output)},
        }
    )

    extract_report = extract.run_extract(str(output), "sky130", top="ota_top_0")
    drc_report = drc.run_drc(str(output), "sky130")

    metrics: dict[str, float | int] = {}
    metrics.update(flatten_metrics(compose_report, prefix="gen_compose"))
    metrics.update(flatten_metrics(extract_report, prefix="extract"))
    metrics.update(flatten_metrics(drc_report, prefix="drc"))
    return metrics


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    """Scrub PDK env vars / host search space -- mirrors test_gen_compose.py."""
    monkeypatch.delenv("PDK_ROOT", raising=False)
    monkeypatch.delenv("PDK", raising=False)
    monkeypatch.setattr(pdk, "STORE_DIRS", [])
    monkeypatch.setattr(pdk, "CONVENTIONAL_PREFIXES", [])


@pytest.fixture()
def pdk_root(tmp_path):
    root = tmp_path / "pdk_install"
    variant_dir = root / "sky130A"
    (variant_dir / "libs.tech").mkdir(parents=True)
    return root


# --------------------------------------------------------------------------- #
# The regression test itself
# --------------------------------------------------------------------------- #


def test_5t_ota_metrics_match_golden(tmp_path, pdk_root):
    actual = build_5t_ota_metrics(tmp_path, pdk_root)
    assert_metrics_match(OTA_GOLDEN_PATH, actual)


def test_5t_ota_golden_file_declares_the_headline_metrics_from_the_issue():
    # #248's own acceptance bar: device counts, cell/instance counts, and a
    # DRC violation count are all present in the checked-in golden file (not
    # just whatever happened to flatten out of one run).
    golden = load_golden_metrics(OTA_GOLDEN_PATH)
    for key in (
        "extract.device_count",
        "extract.device_counts.nfet",
        "extract.net_count",
        "drc.violation_count",
    ):
        assert key in golden, f"expected golden metric {key!r} to be declared"
    assert golden["drc.violation_count"]["value"] == 0


# --------------------------------------------------------------------------- #
# Unit tests for the helper itself (flatten / load / diff / assert), fully
# independent of any real `klt` verb -- see #248's Test Plan.
# --------------------------------------------------------------------------- #


def test_flatten_metrics_recurses_nested_dicts():
    data = {"drc": {"violation_count": 0, "status": "clean"}}
    assert flatten_metrics(data) == {"drc.violation_count": 0}


def test_flatten_metrics_records_list_length_as_count_suffix():
    data = {"devices": [{"name": "M1"}, {"name": "M2"}], "warnings": []}
    assert flatten_metrics(data) == {"devices_count": 2, "warnings_count": 0}


def test_flatten_metrics_drops_non_numeric_scalars():
    data = {"file": "x.gds", "ok": True, "pdk": None, "dbu_um": 0.001}
    assert flatten_metrics(data) == {"dbu_um": 0.001}


def test_flatten_metrics_applies_a_prefix():
    assert flatten_metrics({"a": 1}, prefix="extract") == {"extract.a": 1}


def test_load_golden_metrics_normalizes_bare_number_to_tol_pct_zero(tmp_path):
    path = tmp_path / "g.json"
    path.write_text('{"metrics": {"device_count": 5}}')
    assert load_golden_metrics(path) == {"device_count": {"value": 5, "tol_pct": 0}}


def test_load_golden_metrics_preserves_declared_tol_pct(tmp_path):
    path = tmp_path / "g.json"
    path.write_text('{"metrics": {"area_um2": {"value": 812.4, "tol_pct": 5}}}')
    assert load_golden_metrics(path) == {"area_um2": {"value": 812.4, "tol_pct": 5}}


def test_diff_metrics_reports_missing_golden_metric():
    golden = {"device_count": {"value": 5, "tol_pct": 0}}
    diffs = diff_metrics(golden, {})
    assert len(diffs) == 1
    assert "missing metric 'device_count'" in diffs[0]


def test_diff_metrics_reports_unexpected_actual_metric():
    diffs = diff_metrics({}, {"device_count": 5})
    assert len(diffs) == 1
    assert "unexpected metric 'device_count'" in diffs[0]


def test_diff_metrics_exact_match_rejects_any_drift():
    golden = {"device_count": {"value": 5, "tol_pct": 0}}
    assert diff_metrics(golden, {"device_count": 5}) == []
    diffs = diff_metrics(golden, {"device_count": 6})
    assert len(diffs) == 1
    assert "mismatch" in diffs[0]


def test_diff_metrics_tolerance_boundary_is_inclusive():
    # golden=100, tol_pct=5 -> 105 is exactly 5% off: must pass.
    golden = {"area_um2": {"value": 100.0, "tol_pct": 5}}
    assert diff_metrics(golden, {"area_um2": 105.0}) == []
    # 105.01 is (barely) over 5%: must fail.
    diffs = diff_metrics(golden, {"area_um2": 105.01})
    assert len(diffs) == 1
    assert "out of tolerance" in diffs[0]


def test_diff_metrics_zero_golden_value_uses_absolute_tolerance():
    golden = {"violation_count": {"value": 0, "tol_pct": 2}}
    assert diff_metrics(golden, {"violation_count": 2}) == []
    diffs = diff_metrics(golden, {"violation_count": 3})
    assert len(diffs) == 1
    assert "out of tolerance" in diffs[0]


def test_assert_metrics_match_raises_with_a_readable_summary(tmp_path):
    path = tmp_path / "g.json"
    path.write_text('{"metrics": {"device_count": 5}}')
    with pytest.raises(MetricsMismatchError, match="device_count"):
        assert_metrics_match(path, {"device_count": 6})


def test_assert_metrics_match_passes_silently_when_in_tolerance(tmp_path):
    path = tmp_path / "g.json"
    path.write_text('{"metrics": {"device_count": 5}}')
    assert_metrics_match(path, {"device_count": 5})  # no raise
