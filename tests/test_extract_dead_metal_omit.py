"""``klt extract --omit-dead-metal`` (issue #5545).

`dead_metal[]` is one entry per routing-stack cluster that joins no extracted
net, and under `--abstract-cells` that is one entry per *instance*: cell
abstraction erases a matched cell's device layers but keeps its
contact/metals/vias, so every abstracted cell's internal wiring is correctly
reported as dead. On a standard-cell block the list therefore scales with the
instance count and can dominate a report committed as a provenance record.

`--omit-dead-metal` is the opt-in compaction, mirroring `klt erc
--omit-antenna-tables`: `dead_metal` becomes `[]`, `dead_metal_omitted` is
`true`, and the always-present per-layer `dead_metal_summary` keeps the
counts. These tests pin the whole contract -- the summary is flag-independent,
nothing else in the report moves, the aggregate warning is rewritten rather
than dropped, and `--check --rerun` replays the flag from the committed echo
instead of reporting its own omission as drift.

Fixtures come from `test_extract.py` (the cross-module reuse
`test_characterize_osdi.py` and friends already use); this file is separate
from it only because `tests/test_extract.py` sits at its per-file line ratchet
ceiling (`scripts/check_file_size_baseline.py`).
"""

from __future__ import annotations

import json
from pathlib import Path

import klayout.db as kdb
import pytest

from klayout_tools.cli import main
from klayout_tools.extract import (
    check_extract_report,
    rerun_extract_report,
    run_extract,
)
from test_extract import (
    _make_abstract_cells_layout,
    _make_dead_metal_layout,
    _write_gds,
    _write_report,
)


@pytest.fixture
def tmp_path(tmp_path: Path) -> Path:
    """`tests/test_extract.py`'s own `tmp_path` override, repeated here for
    the same reason (issue #2659): seed a `.git` marker so the layout records
    a locatable `scope: "repo"` `file` envelope, which is what `klt extract
    --check`/`--rerun` resolve against. Importing a module does not import its
    fixtures, so it has to be redeclared."""
    (tmp_path / ".git").mkdir()
    return tmp_path


def _expected_dead_metal_summary(dead_metal):
    """The `dead_metal_summary` the per-cluster list implies, recomputed here
    rather than imported -- an independent oracle, not a restatement."""
    by_key = {}
    for entry in dead_metal:
        key = (entry["layer"], entry["datatype"], entry["role"])
        row = by_key.setdefault(
            key,
            {
                "role": entry["role"],
                "layer": entry["layer"],
                "datatype": entry["datatype"],
                "clusters": 0,
                "shapes": 0,
                "area_um2": 0.0,
            },
        )
        row["clusters"] += 1
        row["shapes"] += entry["shapes"]
        row["area_um2"] = round(row["area_um2"] + entry["area_um2"], 6)
    by_layer = [by_key[key] for key in sorted(by_key)]
    return {
        "clusters": sum(row["clusters"] for row in by_layer),
        "shapes": sum(row["shapes"] for row in by_layer),
        "area_um2": round(sum(row["area_um2"] for row in by_layer), 6),
        "by_layer": by_layer,
    }


def test_omit_dead_metal_blanks_the_list_and_keeps_the_summary(tmp_path):
    """`--omit-dead-metal` emits `dead_metal` as `[]` (still a list) and
    `dead_metal_omitted: true`, while `dead_metal_summary` carries the real
    counts computed from the clusters the unflagged run lists."""
    path = _write_gds(
        _make_dead_metal_layout(second_island_shape=True), tmp_path / "omit.gds"
    )
    full = run_extract(path, "sky130", output=str(tmp_path / "full.spice"))
    omitted = run_extract(
        path,
        "sky130",
        output=str(tmp_path / "omit.spice"),
        omit_dead_metal=True,
    )

    assert full["dead_metal"] != []
    assert omitted["dead_metal"] == []
    assert omitted["dead_metal_omitted"] is True
    assert full["dead_metal_omitted"] is False
    assert omitted["dead_metal_summary"] == _expected_dead_metal_summary(
        full["dead_metal"]
    )
    assert omitted["dead_metal_summary"] == full["dead_metal_summary"]


def test_dead_metal_summary_is_always_present_and_flag_independent(tmp_path):
    """Emitted on every run, `by_layer` sorted by `(layer, datatype)` and
    identical with and without the flag -- a reader never has to branch on
    `dead_metal_omitted` to find the totals."""
    path = _write_gds(_make_dead_metal_layout(), tmp_path / "summary.gds")
    full = run_extract(path, "sky130", output=str(tmp_path / "s_full.spice"))
    omitted = run_extract(
        path,
        "sky130",
        output=str(tmp_path / "s_omit.spice"),
        omit_dead_metal=True,
    )

    summary = full["dead_metal_summary"]
    assert set(summary) == {"clusters", "shapes", "area_um2", "by_layer"}
    assert summary["clusters"] == len(full["dead_metal"])
    assert summary["shapes"] == sum(e["shapes"] for e in full["dead_metal"])
    assert [(row["layer"], row["datatype"]) for row in summary["by_layer"]] == sorted(
        {(row["layer"], row["datatype"]) for row in summary["by_layer"]}
    )
    assert set(summary["by_layer"][0]) == {
        "role",
        "layer",
        "datatype",
        "clusters",
        "shapes",
        "area_um2",
    }
    assert omitted["dead_metal_summary"] == summary


def test_dead_metal_summary_is_zeroed_with_no_dead_metal(tmp_path):
    """A fully-netted layout reports the all-zero summary (not `null`, not a
    missing field) and no dead-metal warning, with and without the flag."""
    path = _write_gds(
        _make_dead_metal_layout(island_shapes=False), tmp_path / "clean.gds"
    )
    zeroed = {"clusters": 0, "shapes": 0, "area_um2": 0.0, "by_layer": []}
    for flag, stem in ((False, "c_full"), (True, "c_omit")):
        report = run_extract(
            path,
            "sky130",
            output=str(tmp_path / f"{stem}.spice"),
            omit_dead_metal=flag,
        )
        assert report["dead_metal"] == []
        assert report["dead_metal_summary"] == zeroed
        assert report["dead_metal_omitted"] is flag
        assert [w for w in report["warnings"] if "join no extracted net" in w] == []


def test_omit_dead_metal_leaves_default_output_unchanged(tmp_path):
    """Without the flag, `dead_metal[]` and `warnings[]` are exactly what they
    were before it existed: the two new fields are the only additions, and
    `dead_metal_omitted` is `false`."""
    path = _write_gds(
        _make_dead_metal_layout(second_island_shape=True), tmp_path / "default.gds"
    )
    report = run_extract(path, "sky130", output=str(tmp_path / "default.spice"))

    assert report["dead_metal_omitted"] is False
    (dead_warning,) = [w for w in report["warnings"] if "join no extracted net" in w]
    assert dead_warning.endswith(
        "see dead_metal[] for the per-cluster layer/bbox/shape count."
    )
    assert "dead_metal_summary" not in dead_warning


def test_omit_dead_metal_rewrites_only_the_warning_pointer(tmp_path):
    """Under the flag the aggregate warning keeps its counts and layers and
    changes only its tail: it names `dead_metal_summary` instead of
    `dead_metal[]`."""
    path = _write_gds(
        _make_dead_metal_layout(second_island_shape=True), tmp_path / "warn_omit.gds"
    )
    full = run_extract(path, "sky130", output=str(tmp_path / "w_full.spice"))
    omitted = run_extract(
        path,
        "sky130",
        output=str(tmp_path / "w_omit.spice"),
        omit_dead_metal=True,
    )

    (full_warning,) = [w for w in full["warnings"] if "join no extracted net" in w]
    (omitted_warning,) = [
        w for w in omitted["warnings"] if "join no extracted net" in w
    ]
    head = "1 routing-stack cluster (2 shapes on 69/20) join no extracted net"
    assert full_warning.startswith(head)
    assert omitted_warning.startswith(head)
    assert "dead_metal[]" not in omitted_warning
    assert "dead_metal_summary" in omitted_warning
    assert "re-run without --omit-dead-metal" in omitted_warning
    # Every *other* warning is untouched by the rewrite.
    assert [w for w in full["warnings"] if w != full_warning] == [
        w for w in omitted["warnings"] if w != omitted_warning
    ]


def test_omit_dead_metal_leaves_every_other_field_identical(tmp_path):
    """Every other field, and the written netlist, are identical with and
    without the flag -- only `dead_metal`, `dead_metal_omitted` and the one
    aggregate warning differ."""
    path = _write_gds(
        _make_dead_metal_layout(second_island_shape=True), tmp_path / "same.gds"
    )
    full = run_extract(path, "sky130", output=str(tmp_path / "same_a.spice"))
    omitted = run_extract(
        path,
        "sky130",
        output=str(tmp_path / "same_b.spice"),
        omit_dead_metal=True,
    )

    assert (tmp_path / "same_a.spice").read_text() == (
        tmp_path / "same_b.spice"
    ).read_text()
    volatile = {"dead_metal", "dead_metal_omitted", "warnings", "timing", "metrics"}
    assert set(full) == set(omitted)
    for key in set(full) - volatile:
        if key == "netlist_path":
            continue
        assert full[key] == omitted[key], key


def test_omit_dead_metal_on_abstracted_cells(tmp_path):
    """The reported case: with `--abstract-cells`, in-cell wiring joins no
    surviving net, so `dead_metal[]` grows one entry per *instance*. The flag
    drops those rows; the summary, netlist, `devices[]` and `nets[]` do not
    move."""
    layout = _make_abstract_cells_layout()
    # An in-cell met2 island inside the abstracted leaf: abstraction erases
    # the device layers but keeps contact/metals/vias, so this joins no
    # surviving net in either instance.
    layout.cell("SC_BUF").shapes(layout.layer(69, 20)).insert(
        kdb.Box(600, 1400, 1400, 1800)
    )
    path = _write_gds(layout, tmp_path / "abs_dead.gds")

    full = run_extract(
        path,
        "sky130",
        output=str(tmp_path / "abs_full.spice"),
        abstract_cell_patterns=("SC_BUF",),
    )
    omitted = run_extract(
        path,
        "sky130",
        output=str(tmp_path / "abs_omit.spice"),
        abstract_cell_patterns=("SC_BUF",),
        omit_dead_metal=True,
    )

    # One dead cluster per instance -- the "scales with instance count"
    # property that motivates the flag.
    assert len(full["dead_metal"]) == 2, full["dead_metal"]
    assert omitted["dead_metal"] == []
    assert omitted["dead_metal_summary"]["clusters"] == 2
    assert omitted["dead_metal_summary"] == full["dead_metal_summary"]
    assert omitted["nets"] == full["nets"]
    assert omitted["devices"] == full["devices"]
    assert omitted["abstracted_cells"] == full["abstracted_cells"]
    assert (tmp_path / "abs_full.spice").read_text() == (
        tmp_path / "abs_omit.spice"
    ).read_text()


def test_omit_dead_metal_request_document_matches_the_flag(tmp_path, capsys):
    """The request-document field `omit_dead_metal: true` behaves exactly like
    the CLI flag (issue #1867's both-forms-identical contract)."""
    path = _write_gds(_make_dead_metal_layout(), tmp_path / "req.gds")

    assert (
        main(
            [
                "extract",
                str(path),
                "--deck",
                "sky130",
                "--omit-dead-metal",
                "--output",
                str(tmp_path / "flag.spice"),
                "--format",
                "json",
            ]
        )
        == 0
    )
    from_flag = json.loads(capsys.readouterr().out)

    request = tmp_path / "req.json"
    request.write_text(
        json.dumps(
            {
                "schema": "klt.extract.request/1",
                "file": str(path),
                "deck": "sky130",
                "omit_dead_metal": True,
                "output": str(tmp_path / "doc.spice"),
            }
        )
    )
    assert main(["extract", str(request), "--format", "json"]) == 0
    from_doc = json.loads(capsys.readouterr().out)

    assert from_flag["dead_metal"] == []
    assert from_flag["dead_metal_omitted"] is True
    assert from_doc["dead_metal"] == from_flag["dead_metal"]
    assert from_doc["dead_metal_omitted"] is True
    assert from_doc["dead_metal_summary"] == from_flag["dead_metal_summary"]
    assert from_doc["warnings"] == from_flag["warnings"]


def test_omit_dead_metal_report_checks_and_reruns_without_drift(tmp_path):
    """A report committed with `--omit-dead-metal` passes `--check`, and
    `--check --rerun` replays the flag from the committed
    `dead_metal_omitted` echo, so it reports no drift."""
    path = _write_gds(
        _make_dead_metal_layout(second_island_shape=True), tmp_path / "rerun.gds"
    )
    report = run_extract(
        path,
        "sky130",
        output=str(tmp_path / "rerun.spice"),
        omit_dead_metal=True,
    )
    report_path = tmp_path / "rerun.extract.json"
    _write_report(report_path, report)

    assert check_extract_report(str(report_path))["status"] == "match"

    result = rerun_extract_report(str(report_path))
    assert result["status"] == "match", result["drift"]
    assert result["drift"] == []
    assert result["fresh"]["dead_metal"] == []
    assert result["fresh"]["dead_metal_omitted"] is True


def test_rerun_without_the_flag_still_replays_unflagged(tmp_path):
    """The replay is driven by the committed echo, not by a sticky default: a
    report committed *without* the flag reruns without it and keeps its
    per-cluster rows."""
    path = _write_gds(_make_dead_metal_layout(), tmp_path / "plain.gds")
    report_path = tmp_path / "plain.extract.json"
    _write_report(
        report_path,
        run_extract(path, "sky130", output=str(tmp_path / "plain.spice")),
    )

    result = rerun_extract_report(str(report_path))

    assert result["status"] == "match", result["drift"]
    assert result["fresh"]["dead_metal_omitted"] is False
    assert result["fresh"]["dead_metal"] != []
