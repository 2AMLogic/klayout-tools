"""Tests for the ``klt kb`` verb: the library module (``klayout_tools.kb``)
and its CLI wiring (``cli/kb_cmd.py`` + ``cli/parser.py``).

Fixtures build an isolated ``kb/`` tree under ``tmp_path`` (real copy of the
project's own ``kb/schema/entry.schema.json``, synthetic entries) so these
tests never depend on -- or risk being broken by -- the checked-in ``kb/``
corpus under ``kb/entries/`` (that corpus has its own coverage in
``tests/test_kb.py``).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from klayout_tools import kb
from klayout_tools.cli import main

_REPO_SCHEMA_PATH = (
    Path(__file__).resolve().parent.parent / "kb" / "schema" / "entry.schema.json"
)


def _valid_entry(entry_id: str, **overrides) -> dict:
    entry = {
        "id": entry_id,
        "title": f"Title for {entry_id}",
        "topology": "some topology",
        "spec_class": "example spec class",
        "source": {"citation": 'Example, A., "An example paper," 2020.'},
    }
    entry.update(overrides)
    return entry


def _make_kb(tmp_path: Path, entries: dict[str, dict | str]) -> Path:
    """Write a ``kb/`` tree at ``tmp_path/kb`` with the real project schema
    and one file per ``entries`` item. A string value is written verbatim
    (for malformed-JSON fixtures); a dict is ``json.dump``-ed."""
    root = tmp_path / "kb"
    (root / "schema").mkdir(parents=True)
    (root / "schema" / "entry.schema.json").write_text(_REPO_SCHEMA_PATH.read_text())
    entries_dir = root / "entries"
    entries_dir.mkdir()
    for entry_id, content in entries.items():
        path = entries_dir / f"{entry_id}.json"
        if isinstance(content, str):
            path.write_text(content)
        else:
            path.write_text(json.dumps(content))
    return root


# --------------------------------------------------------------------------- #
# library: list_entries
# --------------------------------------------------------------------------- #


def test_list_entries_sorted_and_summarized(tmp_path):
    root = _make_kb(
        tmp_path,
        {
            "zebra-design": _valid_entry("zebra-design", spec_class="zed"),
            "alpha-design": _valid_entry("alpha-design", spec_class="alpha"),
        },
    )

    report = kb.list_entries(root=root)

    assert report["schema_version"] == 1
    assert report["count"] == 2
    ids = [entry["id"] for entry in report["entries"]]
    assert ids == ["alpha-design", "zebra-design"]  # id-sorted
    assert set(report["entries"][0].keys()) == {"id", "title", "spec_class"}


def test_list_entries_missing_dir_raises(tmp_path):
    with pytest.raises(kb.KbError, match="entries directory not found"):
        kb.list_entries(root=tmp_path / "does-not-exist")


def test_list_entries_empty_dir_is_success(tmp_path):
    root = _make_kb(tmp_path, {})
    report = kb.list_entries(root=root)
    assert report["count"] == 0
    assert report["entries"] == []


# --------------------------------------------------------------------------- #
# library: show_entry
# --------------------------------------------------------------------------- #


def test_show_entry_returns_full_entry(tmp_path):
    root = _make_kb(tmp_path, {"my-design": _valid_entry("my-design")})

    report = kb.show_entry("my-design", root=root)

    assert report["schema_version"] == 1
    assert report["entry"]["id"] == "my-design"
    assert report["entry"]["topology"] == "some topology"


def test_show_entry_missing_raises(tmp_path):
    root = _make_kb(tmp_path, {"my-design": _valid_entry("my-design")})

    with pytest.raises(kb.KbError, match="not found"):
        kb.show_entry("no-such-entry", root=root)


# --------------------------------------------------------------------------- #
# library: search_entries
# --------------------------------------------------------------------------- #


def test_search_matches_title_case_insensitively(tmp_path):
    root = _make_kb(
        tmp_path,
        {
            "bandgap": _valid_entry("bandgap", title="Curvature-Corrected Bandgap"),
            "inductor": _valid_entry("inductor", title="Spiral Inductor"),
        },
    )

    report = kb.search_entries("BANDGAP", root=root)

    assert report["query"] == "BANDGAP"
    assert report["count"] == 1
    assert report["entries"][0]["id"] == "bandgap"


@pytest.mark.parametrize(
    "field,value",
    [
        ("topology", "a differential folded-cascode topology"),
        ("spec_class", "low-noise amplifier spec class"),
        ("notes", "watch out for substrate noise here"),
    ],
)
def test_search_matches_scalar_fields(tmp_path, field, value):
    root = _make_kb(tmp_path, {"entry-a": _valid_entry("entry-a", **{field: value})})

    report = kb.search_entries(
        "substrate" if field == "notes" else value.split()[0], root=root
    )

    assert report["count"] == 1


def test_search_matches_layout_idioms_array(tmp_path):
    root = _make_kb(
        tmp_path,
        {
            "entry-a": _valid_entry(
                "entry-a", layout_idioms=["common-centroid matching", "guard rings"]
            )
        },
    )

    report = kb.search_entries("guard ring", root=root)

    assert report["count"] == 1


def test_search_no_match_returns_empty_success(tmp_path):
    root = _make_kb(tmp_path, {"entry-a": _valid_entry("entry-a")})

    report = kb.search_entries("nonexistent-keyword-xyz", root=root)

    assert report["count"] == 0
    assert report["entries"] == []


def test_search_tolerates_null_optional_fields(tmp_path):
    entry = _valid_entry("entry-a")
    entry["notes"] = None
    entry["layout_idioms"] = None
    root = _make_kb(tmp_path, {"entry-a": entry})

    report = kb.search_entries("topology", root=root)

    assert report["count"] == 1


# --------------------------------------------------------------------------- #
# library: search_entries numeric filters (--where / --pdk)
# --------------------------------------------------------------------------- #


def _measured_entry(entry_id: str, pdk: str, figures: list[dict]) -> dict:
    return _valid_entry(
        entry_id,
        measured={
            "pdk": pdk,
            "corner": "tt, 1.8V, 27C",
            "figures": figures,
        },
    )


def test_search_empty_query_matches_everything_before_filters(tmp_path):
    root = _make_kb(
        tmp_path,
        {"entry-a": _valid_entry("entry-a"), "entry-b": _valid_entry("entry-b")},
    )

    report = kb.search_entries(root=root)

    assert report["query"] == ""
    assert report["count"] == 2


def test_search_where_filters_on_numeric_figure(tmp_path):
    root = _make_kb(
        tmp_path,
        {
            "high-gain": _measured_entry(
                "high-gain",
                "sky130",
                [{"name": "av_db", "value": 55, "unit": "dB", "testbench": "x.json"}],
            ),
            "low-gain": _measured_entry(
                "low-gain",
                "sky130",
                [{"name": "av_db", "value": 20, "unit": "dB", "testbench": "x.json"}],
            ),
        },
    )

    report = kb.search_entries(where=["av_db>=40"], root=root)

    assert report["where"] == ["av_db>=40"]
    assert report["count"] == 1
    assert report["entries"][0]["id"] == "high-gain"


@pytest.mark.parametrize(
    "expr,expected_ids",
    [
        ("av_db==40", ["exact"]),
        ("av_db!=40", ["other"]),
        ("av_db>40", []),
        ("av_db<40", ["other"]),
        ("av_db<=40", ["exact", "other"]),
    ],
)
def test_search_where_supports_every_operator(tmp_path, expr, expected_ids):
    root = _make_kb(
        tmp_path,
        {
            "exact": _measured_entry(
                "exact",
                "sky130",
                [{"name": "av_db", "value": 40, "unit": "dB", "testbench": "x.json"}],
            ),
            "other": _measured_entry(
                "other",
                "sky130",
                [{"name": "av_db", "value": 10, "unit": "dB", "testbench": "x.json"}],
            ),
        },
    )

    report = kb.search_entries(where=[expr], root=root)

    assert sorted(entry["id"] for entry in report["entries"]) == sorted(expected_ids)


def test_search_where_multiple_conditions_are_anded(tmp_path):
    root = _make_kb(
        tmp_path,
        {
            "both": _measured_entry(
                "both",
                "sky130",
                [
                    {
                        "name": "av_db",
                        "value": 55,
                        "unit": "dB",
                        "testbench": "x.json",
                    },
                    {
                        "name": "idd_a",
                        "value": 1e-6,
                        "unit": "A",
                        "testbench": "x.json",
                    },
                ],
            ),
            "only-gain": _measured_entry(
                "only-gain",
                "sky130",
                [{"name": "av_db", "value": 55, "unit": "dB", "testbench": "x.json"}],
            ),
        },
    )

    report = kb.search_entries(where=["av_db>=40", "idd_a<=1e-6"], root=root)

    assert report["count"] == 1
    assert report["entries"][0]["id"] == "both"


def test_search_where_entry_without_measured_never_matches(tmp_path):
    root = _make_kb(tmp_path, {"entry-a": _valid_entry("entry-a")})

    report = kb.search_entries(where=["av_db>=0"], root=root)

    assert report["count"] == 0


def test_search_where_missing_figure_name_never_matches(tmp_path):
    root = _make_kb(
        tmp_path,
        {
            "entry-a": _measured_entry(
                "entry-a",
                "sky130",
                [{"name": "pm_deg", "value": 60, "unit": "deg", "testbench": "x.json"}],
            )
        },
    )

    report = kb.search_entries(where=["av_db>=0"], root=root)

    assert report["count"] == 0


def test_search_pdk_filter_is_case_insensitive_exact_match(tmp_path):
    root = _make_kb(
        tmp_path,
        {
            "sky": _measured_entry(
                "sky",
                "sky130",
                [{"name": "av_db", "value": 1, "unit": "dB", "testbench": "x.json"}],
            ),
            "gf": _measured_entry(
                "gf",
                "gf180mcu",
                [{"name": "av_db", "value": 1, "unit": "dB", "testbench": "x.json"}],
            ),
        },
    )

    report = kb.search_entries(pdk="SKY130", root=root)

    assert report["pdk"] == "SKY130"
    assert report["count"] == 1
    assert report["entries"][0]["id"] == "sky"


def test_search_pdk_filter_entry_without_measured_never_matches(tmp_path):
    root = _make_kb(tmp_path, {"entry-a": _valid_entry("entry-a")})

    report = kb.search_entries(pdk="sky130", root=root)

    assert report["count"] == 0


@pytest.mark.parametrize(
    "expr",
    ["no-operator-here", "av_db>=not-a-number", ">=40", "  >=40"],
)
def test_search_where_malformed_expression_raises(tmp_path, expr):
    root = _make_kb(tmp_path, {"entry-a": _valid_entry("entry-a")})

    with pytest.raises(kb.KbError):
        kb.search_entries(where=[expr], root=root)


# --------------------------------------------------------------------------- #
# library: validate_entries
# --------------------------------------------------------------------------- #


def test_validate_entries_all_valid(tmp_path):
    root = _make_kb(
        tmp_path,
        {
            "entry-a": _valid_entry("entry-a"),
            "entry-b": _valid_entry("entry-b"),
        },
    )

    report = kb.validate_entries(root=root)

    assert report["schema_version"] == 1
    assert report["valid"] is True
    assert report["entry_count"] == 2
    assert all(entry["valid"] for entry in report["entries"])
    assert all(entry["errors"] == [] for entry in report["entries"])


def test_validate_entries_reports_schema_violation(tmp_path):
    broken = _valid_entry("broken-entry")
    del broken["source"]["citation"]
    root = _make_kb(tmp_path, {"broken-entry": broken})

    report = kb.validate_entries(root=root)

    assert report["valid"] is False
    assert report["entry_count"] == 1
    (result,) = report["entries"]
    assert result["id"] == "broken-entry"
    assert result["valid"] is False
    assert result["errors"]  # structured, non-empty per-entry errors


def test_validate_entries_reports_id_mismatch(tmp_path):
    entry = _valid_entry("wrong-id-inside")
    root = _make_kb(tmp_path, {"filename-id": entry})

    report = kb.validate_entries(root=root)

    assert report["valid"] is False
    (result,) = report["entries"]
    assert result["id"] == "filename-id"
    assert any("does not match filename stem" in error for error in result["errors"])


def test_validate_entries_reports_invalid_json(tmp_path):
    root = _make_kb(tmp_path, {"malformed": "{not valid json"})

    report = kb.validate_entries(root=root)

    assert report["valid"] is False
    (result,) = report["entries"]
    assert result["id"] == "malformed"
    assert any("invalid JSON" in error for error in result["errors"])


def test_validate_entries_mixed_valid_and_invalid(tmp_path):
    root = _make_kb(
        tmp_path,
        {
            "good-entry": _valid_entry("good-entry"),
            "bad-entry": {"id": "bad-entry"},  # missing several required fields
        },
    )

    report = kb.validate_entries(root=root)

    assert report["valid"] is False
    assert report["entry_count"] == 2
    by_id = {entry["id"]: entry for entry in report["entries"]}
    assert by_id["good-entry"]["valid"] is True
    assert by_id["bad-entry"]["valid"] is False


def test_validate_missing_schema_raises(tmp_path):
    root = tmp_path / "kb"
    (root / "entries").mkdir(parents=True)

    with pytest.raises(kb.KbError, match="schema not found"):
        kb.validate_entries(root=root)


# --------------------------------------------------------------------------- #
# library: validate_entries -- artifacts path-existence check (#126)
# --------------------------------------------------------------------------- #


def test_validate_entries_passes_when_artifact_path_exists(tmp_path):
    """Artifact paths are resolved relative to the repository root -- `kb/`'s
    parent directory, i.e. `root.parent` -- matching how every other
    `examples/...`-shaped path in this repo is written."""
    root = _make_kb(
        tmp_path,
        {
            "entry-a": _valid_entry(
                "entry-a", artifacts={"netlist": "examples/entry-a/testbench.spice"}
            )
        },
    )
    netlist_path = tmp_path / "examples" / "entry-a" / "testbench.spice"
    netlist_path.parent.mkdir(parents=True)
    netlist_path.write_text("* fixture netlist\n")

    report = kb.validate_entries(root=root)

    assert report["valid"] is True
    (result,) = report["entries"]
    assert result["valid"] is True
    assert result["errors"] == []


def test_validate_entries_fails_when_artifact_path_missing(tmp_path):
    root = _make_kb(
        tmp_path,
        {
            "entry-a": _valid_entry(
                "entry-a",
                artifacts={"netlist": "examples/entry-a/does-not-exist.spice"},
            )
        },
    )
    # Deliberately do not create examples/entry-a/does-not-exist.spice.

    report = kb.validate_entries(root=root)

    assert report["valid"] is False
    (result,) = report["entries"]
    assert result["id"] == "entry-a"
    assert result["valid"] is False
    assert any(
        "artifacts/netlist" in error and "does-not-exist.spice" in error
        for error in result["errors"]
    ), result["errors"]


def test_validate_entries_checks_both_artifact_fields_independently(tmp_path):
    root = _make_kb(
        tmp_path,
        {
            "entry-a": _valid_entry(
                "entry-a",
                artifacts={
                    "netlist": "examples/entry-a/testbench.spice",
                    "layout": "examples/entry-a/layout.gds",
                },
            )
        },
    )
    netlist_path = tmp_path / "examples" / "entry-a" / "testbench.spice"
    netlist_path.parent.mkdir(parents=True)
    netlist_path.write_text("* fixture netlist\n")
    # layout.gds deliberately absent.

    report = kb.validate_entries(root=root)

    assert report["valid"] is False
    (result,) = report["entries"]
    assert len(result["errors"]) == 1
    assert "artifacts/layout" in result["errors"][0]


def test_validate_entries_tolerates_absent_artifacts_field(tmp_path):
    root = _make_kb(tmp_path, {"entry-a": _valid_entry("entry-a")})

    report = kb.validate_entries(root=root)

    assert report["valid"] is True


def test_validate_entries_passes_when_layout_artifact_exists(tmp_path):
    root = _make_kb(
        tmp_path,
        {
            "entry-a": _valid_entry(
                "entry-a", artifacts={"layout": "examples/entry-a/layout.gds"}
            )
        },
    )
    layout_path = tmp_path / "examples" / "entry-a" / "layout.gds"
    layout_path.parent.mkdir(parents=True)
    layout_path.write_bytes(b"\x00\x06\x00\x02\x00\x07")  # not parsed, only stat-ed

    report = kb.validate_entries(root=root)

    assert report["valid"] is True
    (result,) = report["entries"]
    assert result["errors"] == []


def test_validate_entries_rejects_absolute_artifact_path(tmp_path):
    """An absolute path silently wins the `repo_root / rel_path` join, so an
    entry could otherwise "verify" against a file outside the repository that
    no other checkout can see."""
    outside = tmp_path / "outside.spice"
    outside.write_text("* fixture netlist\n")
    root = _make_kb(
        tmp_path,
        {"entry-a": _valid_entry("entry-a", artifacts={"netlist": str(outside)})},
    )

    report = kb.validate_entries(root=root)

    assert report["valid"] is False
    (result,) = report["entries"]
    assert any(
        "artifacts/netlist" in error and "repository-relative" in error
        for error in result["errors"]
    ), result["errors"]


def test_validate_entries_rejects_parent_traversal_artifact_path(tmp_path):
    outside = tmp_path.parent / f"{tmp_path.name}-outside.gds"
    outside.write_bytes(b"\x00")
    root = _make_kb(
        tmp_path,
        {
            "entry-a": _valid_entry(
                "entry-a", artifacts={"layout": f"../{outside.name}"}
            )
        },
    )

    report = kb.validate_entries(root=root)

    assert report["valid"] is False
    (result,) = report["entries"]
    assert any(
        "artifacts/layout" in error and "'..'" in error for error in result["errors"]
    ), result["errors"]
    outside.unlink()


def test_validate_entries_never_treats_artifact_notes_as_a_path(tmp_path):
    """`artifacts.notes` is prose. It must not be stat-ed, even when it reads
    like a path -- otherwise every note mentioning a filename would fail."""
    root = _make_kb(
        tmp_path,
        {
            "entry-a": _valid_entry(
                "entry-a",
                artifacts={
                    "netlist": "examples/entry-a/testbench.spice",
                    "notes": "examples/entry-a/no-such-file.spice is NOT a path",
                },
            )
        },
    )
    netlist_path = tmp_path / "examples" / "entry-a" / "testbench.spice"
    netlist_path.parent.mkdir(parents=True)
    netlist_path.write_text("* fixture netlist\n")

    report = kb.validate_entries(root=root)

    assert report["valid"] is True
    (result,) = report["entries"]
    assert result["errors"] == []


def test_validate_entries_rejects_artifacts_naming_only_notes(tmp_path):
    """`notes` alone is not a verification link -- the schema's `anyOf`
    requires at least one of `netlist`/`layout` when `artifacts` is given."""
    root = _make_kb(
        tmp_path,
        {"entry-a": _valid_entry("entry-a", artifacts={"notes": "see the paper"})},
    )

    report = kb.validate_entries(root=root)

    assert report["valid"] is False
    (result,) = report["entries"]
    assert result["errors"], result


def test_validate_entries_resolves_artifacts_against_explicit_repo_root(tmp_path):
    """`repo_root=` overrides the `root.parent` default, so a KB root that
    is not a direct child of the tree holding the artifacts still validates."""
    root = _make_kb(
        tmp_path / "nested" / "deeper",
        {
            "entry-a": _valid_entry(
                "entry-a", artifacts={"netlist": "examples/entry-a/testbench.spice"}
            )
        },
    )
    netlist_path = tmp_path / "examples" / "entry-a" / "testbench.spice"
    netlist_path.parent.mkdir(parents=True)
    netlist_path.write_text("* fixture netlist\n")

    # Default resolution (root.parent == tmp_path/nested/deeper) cannot see it.
    assert kb.validate_entries(root=root)["valid"] is False

    report = kb.validate_entries(root=root, repo_root=tmp_path)

    assert report["valid"] is True
    (result,) = report["entries"]
    assert result["errors"] == []


# --------------------------------------------------------------------------- #
# validate_entries: measured.figures[].testbench (mirrors artifacts above)
# --------------------------------------------------------------------------- #


def _measured_block(testbench: str) -> dict:
    return {
        "pdk": "sky130",
        "corner": "tt, 1.8V, 27C",
        "figures": [
            {"name": "av_db", "value": 55, "unit": "dB", "testbench": testbench}
        ],
    }


def test_validate_entries_tolerates_absent_measured_field(tmp_path):
    root = _make_kb(tmp_path, {"entry-a": _valid_entry("entry-a")})

    report = kb.validate_entries(root=root)

    assert report["valid"] is True


def test_validate_entries_passes_when_measured_testbench_path_exists(tmp_path):
    root = _make_kb(
        tmp_path,
        {
            "entry-a": _valid_entry(
                "entry-a", measured=_measured_block("examples/entry-a/request.json")
            )
        },
    )
    netlist_path = tmp_path / "examples" / "entry-a" / "request.json"
    netlist_path.parent.mkdir(parents=True)
    netlist_path.write_text("{}")

    report = kb.validate_entries(root=root)

    assert report["valid"] is True


def test_validate_entries_fails_when_measured_testbench_path_missing(tmp_path):
    root = _make_kb(
        tmp_path,
        {
            "entry-a": _valid_entry(
                "entry-a",
                measured=_measured_block("examples/entry-a/does-not-exist.json"),
            )
        },
    )

    report = kb.validate_entries(root=root)

    assert report["valid"] is False
    (result,) = report["entries"]
    assert any(
        "measured/figures/0/testbench" in error and "does not exist" in error
        for error in result["errors"]
    ), result["errors"]


def test_validate_entries_rejects_absolute_measured_testbench_path(tmp_path):
    root = _make_kb(
        tmp_path,
        {"entry-a": _valid_entry("entry-a", measured=_measured_block("/etc/passwd"))},
    )

    report = kb.validate_entries(root=root)

    assert report["valid"] is False
    (result,) = report["entries"]
    assert any("repository-relative path" in error for error in result["errors"])


def test_validate_entries_rejects_parent_traversal_measured_testbench_path(tmp_path):
    root = _make_kb(
        tmp_path,
        {
            "entry-a": _valid_entry(
                "entry-a", measured=_measured_block("../outside/request.json")
            )
        },
    )

    report = kb.validate_entries(root=root)

    assert report["valid"] is False
    (result,) = report["entries"]
    assert any("repository-relative path" in error for error in result["errors"])


# --------------------------------------------------------------------------- #
# validate_entries(recheck_measured=True): re-run the testbench via `klt sim`
# (issue #1726). `kb.run_sim` is monkeypatched to a canned report rather than
# invoking a real ngspice subprocess -- these tests exercise the comparison
# logic (`_sim_candidate_values`/`_closest_relative_diff`/
# `_recheck_measured_errors`), not `run_sim` itself (covered by
# `tests/test_sim.py`).
# --------------------------------------------------------------------------- #


def _fake_sim_report(measurements: dict[str, float]) -> dict:
    """Minimal stand-in for a `run_sim` report -- just the fields
    `kb._sim_candidate_values` reads: one corner carrying each measurement's
    raw value, plus the same value as that measurement's rolled-up
    `worst_case` (a single-corner sweep's rollup equals its own corner)."""
    return {
        "corners": [
            {
                "corner_id": "tt/1.8V/27C",
                "measurements": [
                    {"name": name, "value": value}
                    for name, value in measurements.items()
                ],
            }
        ],
        "measurements": [
            {"name": name, "worst_case": {"value": value}}
            for name, value in measurements.items()
        ],
    }


def _entry_with_one_figure(
    testbench: str, *, name: str = "av_db", value: float = 55.0
) -> dict:
    return _valid_entry(
        "entry-a",
        measured={
            "pdk": "sky130",
            "corner": "tt, 1.8V, 27C",
            "figures": [
                {"name": name, "value": value, "unit": "dB", "testbench": testbench}
            ],
        },
    )


def _write_testbench(tmp_path: Path) -> str:
    rel_path = "examples/entry-a/request.json"
    path = tmp_path / rel_path
    path.parent.mkdir(parents=True)
    path.write_text("{}")
    return rel_path


def test_recheck_measured_off_by_default_never_runs_sim(tmp_path, monkeypatch):
    rel_path = _write_testbench(tmp_path)
    root = _make_kb(tmp_path, {"entry-a": _entry_with_one_figure(rel_path)})

    def _boom(*_args, **_kwargs):
        raise AssertionError("run_sim must not run without --recheck-measured")

    monkeypatch.setattr(kb, "run_sim", _boom)

    report = kb.validate_entries(root=root)

    assert report["valid"] is True


def test_recheck_measured_passes_when_value_matches_fresh_sim(tmp_path, monkeypatch):
    rel_path = _write_testbench(tmp_path)
    root = _make_kb(tmp_path, {"entry-a": _entry_with_one_figure(rel_path, value=55.0)})
    monkeypatch.setattr(
        kb, "run_sim", lambda *a, **k: _fake_sim_report({"av_db": 55.0})
    )

    report = kb.validate_entries(root=root, recheck_measured=True)

    assert report["valid"] is True


def test_recheck_measured_tolerates_small_relative_difference(tmp_path, monkeypatch):
    rel_path = _write_testbench(tmp_path)
    root = _make_kb(tmp_path, {"entry-a": _entry_with_one_figure(rel_path, value=55.0)})
    # 0.5% off -- within the 1% default tolerance.
    monkeypatch.setattr(
        kb, "run_sim", lambda *a, **k: _fake_sim_report({"av_db": 55.25})
    )

    report = kb.validate_entries(root=root, recheck_measured=True)

    assert report["valid"] is True


def test_recheck_measured_fails_when_value_drifted(tmp_path, monkeypatch):
    rel_path = _write_testbench(tmp_path)
    root = _make_kb(tmp_path, {"entry-a": _entry_with_one_figure(rel_path, value=55.0)})
    monkeypatch.setattr(
        kb, "run_sim", lambda *a, **k: _fake_sim_report({"av_db": 40.0})
    )

    report = kb.validate_entries(root=root, recheck_measured=True)

    assert report["valid"] is False
    (result,) = report["entries"]
    assert any(
        "measured/figures/0/av_db" in error and "drifted" in error
        for error in result["errors"]
    ), result["errors"]


def test_recheck_measured_reports_corner_diagnostics_when_run_yields_no_values(
    tmp_path, monkeypatch
):
    """`run_sim` does not raise for a corner that times out or whose `.meas`
    statements produce nothing -- it returns `status: "error"` with the
    reason in `corners[].diagnostics`. The recheck error must quote that
    reason, or a nightly failure reads as "no values" and the operator has to
    re-run the testbench by hand to learn the request's own
    `options.timeout_s` was too small (exactly what
    `examples/kb/pfd-charge-pump-tri-state/request.json` did)."""
    rel_path = _write_testbench(tmp_path)
    root = _make_kb(tmp_path, {"entry-a": _entry_with_one_figure(rel_path)})
    errored_report = {
        "status": "error",
        "corners": [
            {
                "corner_id": "tt/1.8V/27C",
                "measurements": [{"name": "av_db", "value": None}],
                "diagnostics": [
                    {
                        "severity": "error",
                        "code": "timeout",
                        "message": "ngspice did not complete within 60s, killed",
                    },
                    {
                        "severity": "error",
                        "code": "measurement",
                        "message": "measurement 'av_db' produced no value",
                    },
                ],
            }
        ],
        "measurements": [{"name": "av_db", "worst_case": {"value": None}}],
    }
    monkeypatch.setattr(kb, "run_sim", lambda *a, **k: errored_report)

    report = kb.validate_entries(root=root, recheck_measured=True)

    assert report["valid"] is False
    (result,) = report["entries"]
    (error,) = [e for e in result["errors"] if "produced no measurement" in e]
    assert "timeout: ngspice did not complete within 60s" in error, error


def test_sim_error_diagnostics_dedupes_and_caps_the_message_list():
    report = {
        "corners": [
            {
                "diagnostics": [
                    {
                        "severity": "warning",
                        "code": "convergence",
                        "message": "retried",
                    },
                    {"severity": "error", "code": "measurement", "message": "a"},
                    {"severity": "error", "code": "measurement", "message": "a"},
                    {"severity": "error", "code": "measurement", "message": "b"},
                    {"severity": "error", "code": "measurement", "message": "c"},
                    {"severity": "error", "code": "measurement", "message": "d"},
                ]
            }
        ]
    }

    summary = kb._sim_error_diagnostics(report)

    # Warnings excluded, the duplicate 'a' collapsed, the 4th of 4 rolled up.
    assert summary == ("measurement: a; measurement: b; measurement: c (+1 more)")


def test_recheck_measured_no_values_without_diagnostics_omits_detail(
    tmp_path, monkeypatch
):
    rel_path = _write_testbench(tmp_path)
    root = _make_kb(tmp_path, {"entry-a": _entry_with_one_figure(rel_path)})
    monkeypatch.setattr(kb, "run_sim", lambda *a, **k: _fake_sim_report({}))

    report = kb.validate_entries(root=root, recheck_measured=True)

    assert report["valid"] is False
    (result,) = report["entries"]
    (error,) = [e for e in result["errors"] if "produced no measurement" in e]
    assert error.endswith("to recheck this figure against"), error


def test_recheck_measured_matches_via_reciprocal_duality(tmp_path, monkeypatch):
    """A `freq_hz` figure recorded alongside a `period_s` one (see
    `kb/entries/rc-relaxation-oscillator.json`) is the *reciprocal* of a raw
    measurement, not a distinct one -- `_sim_candidate_values` must still
    recheck it without drift."""
    rel_path = _write_testbench(tmp_path)
    root = _make_kb(
        tmp_path,
        {"entry-a": _entry_with_one_figure(rel_path, name="freq_hz", value=2.0)},
    )
    monkeypatch.setattr(
        kb, "run_sim", lambda *a, **k: _fake_sim_report({"period": 0.5})
    )

    report = kb.validate_entries(root=root, recheck_measured=True)

    assert report["valid"] is True


def test_recheck_measured_reports_sim_failure(tmp_path, monkeypatch):
    rel_path = _write_testbench(tmp_path)
    root = _make_kb(tmp_path, {"entry-a": _entry_with_one_figure(rel_path)})

    def _raise(*_args, **_kwargs):
        raise kb.SimError("ngspice exited nonzero")

    monkeypatch.setattr(kb, "run_sim", _raise)

    report = kb.validate_entries(root=root, recheck_measured=True)

    assert report["valid"] is False
    (result,) = report["entries"]
    assert any(
        "failed to reproduce this testbench" in error for error in result["errors"]
    ), result["errors"]


def test_recheck_measured_skips_when_testbench_path_already_missing(
    tmp_path, monkeypatch
):
    """A missing/absolute/escaping testbench path is already reported by the
    plain path-existence check -- `--recheck-measured` must not also try
    (and fail) to run it."""
    root = _make_kb(
        tmp_path,
        {"entry-a": _entry_with_one_figure("examples/entry-a/does-not-exist.json")},
    )

    def _boom(*_args, **_kwargs):
        raise AssertionError("run_sim must not run for a missing testbench path")

    monkeypatch.setattr(kb, "run_sim", _boom)

    report = kb.validate_entries(root=root, recheck_measured=True)

    assert report["valid"] is False
    (result,) = report["entries"]
    assert any("does not exist" in error for error in result["errors"])


def test_recheck_measured_caches_one_sim_run_per_testbench(tmp_path, monkeypatch):
    rel_path = _write_testbench(tmp_path)
    entry = _valid_entry(
        "entry-a",
        measured={
            "pdk": "sky130",
            "corner": "tt, 1.8V, 27C",
            "figures": [
                {
                    "name": "period_s",
                    "value": 0.5,
                    "unit": "s",
                    "testbench": rel_path,
                },
                {
                    "name": "freq_hz",
                    "value": 2.0,
                    "unit": "Hz",
                    "testbench": rel_path,
                },
            ],
        },
    )
    root = _make_kb(tmp_path, {"entry-a": entry})

    calls = []

    def _fake_run_sim(*args, **kwargs):
        calls.append((args, kwargs))
        return _fake_sim_report({"period": 0.5})

    monkeypatch.setattr(kb, "run_sim", _fake_run_sim)

    report = kb.validate_entries(root=root, recheck_measured=True)

    assert report["valid"] is True
    assert len(calls) == 1


# --------------------------------------------------------------------------- #
# CLI wiring
# --------------------------------------------------------------------------- #


@pytest.fixture()
def _cli_kb(tmp_path, monkeypatch):
    """Point the CLI's default kb root at an isolated tmp_path tree."""

    def _install(entries: dict[str, dict | str]) -> Path:
        root = _make_kb(tmp_path, entries)
        monkeypatch.setattr(kb, "DEFAULT_KB_ROOT", root)
        return root

    return _install


def test_cli_list_json(_cli_kb, capsys):
    _cli_kb({"entry-a": _valid_entry("entry-a")})

    exit_code = main(["kb", "list", "--format", "json"])

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["schema_version"] == 1
    assert payload["count"] == 1
    assert payload["entries"][0]["id"] == "entry-a"


def test_cli_list_text(_cli_kb, capsys):
    _cli_kb({"entry-a": _valid_entry("entry-a")})

    exit_code = main(["kb", "list"])

    assert exit_code == 0
    out = capsys.readouterr().out
    assert "count: 1" in out
    assert "entry-a" in out


def test_cli_show_json(_cli_kb, capsys):
    _cli_kb({"entry-a": _valid_entry("entry-a")})

    exit_code = main(["kb", "show", "entry-a", "--format", "json"])

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["entry"]["id"] == "entry-a"


def test_cli_show_text_includes_artifacts(_cli_kb, capsys):
    _cli_kb(
        {
            "entry-a": _valid_entry(
                "entry-a", artifacts={"netlist": "examples/entry-a/testbench.spice"}
            )
        }
    )

    exit_code = main(["kb", "show", "entry-a"])

    assert exit_code == 0
    out = capsys.readouterr().out
    assert "artifacts.netlist: examples/entry-a/testbench.spice" in out


def test_cli_show_missing_error_envelope(_cli_kb, capsys):
    _cli_kb({"entry-a": _valid_entry("entry-a")})

    exit_code = main(["kb", "show", "does-not-exist", "--format", "json"])

    assert exit_code == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    error = json.loads(captured.err)
    assert error["schema_version"] == 1
    assert error["error"]["command"] == "kb show"
    assert "not found" in error["error"]["message"]


def test_cli_search_json(_cli_kb, capsys):
    _cli_kb(
        {
            "bandgap": _valid_entry("bandgap", title="Curvature-Corrected Bandgap"),
            "inductor": _valid_entry("inductor", title="Spiral Inductor"),
        }
    )

    exit_code = main(["kb", "search", "spiral", "--format", "json"])

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["count"] == 1
    assert payload["entries"][0]["id"] == "inductor"


def test_cli_search_query_is_optional(_cli_kb, capsys):
    _cli_kb({"entry-a": _valid_entry("entry-a")})

    exit_code = main(["kb", "search", "--format", "json"])

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["query"] == ""
    assert payload["count"] == 1


def test_cli_search_where_and_pdk_json(_cli_kb, capsys):
    _cli_kb(
        {
            "high-gain": _measured_entry(
                "high-gain",
                "sky130",
                [{"name": "av_db", "value": 55, "unit": "dB", "testbench": "x.json"}],
            ),
            "low-gain": _measured_entry(
                "low-gain",
                "sky130",
                [{"name": "av_db", "value": 10, "unit": "dB", "testbench": "x.json"}],
            ),
        }
    )

    exit_code = main(
        [
            "kb",
            "search",
            "--where",
            "av_db>=40",
            "--pdk",
            "sky130",
            "--format",
            "json",
        ]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["where"] == ["av_db>=40"]
    assert payload["pdk"] == "sky130"
    assert payload["count"] == 1
    assert payload["entries"][0]["id"] == "high-gain"


def test_cli_search_malformed_where_error_envelope(_cli_kb, capsys):
    _cli_kb({"entry-a": _valid_entry("entry-a")})

    exit_code = main(
        ["kb", "search", "--where", "not-an-expression", "--format", "json"]
    )

    assert exit_code == 1
    error = json.loads(capsys.readouterr().err)
    assert error["error"]["command"] == "kb search"


def test_cli_search_where_text_output(_cli_kb, capsys):
    _cli_kb(
        {
            "entry-a": _measured_entry(
                "entry-a",
                "sky130",
                [{"name": "av_db", "value": 55, "unit": "dB", "testbench": "x.json"}],
            )
        }
    )

    exit_code = main(["kb", "search", "--where", "av_db>=40", "--pdk", "sky130"])

    assert exit_code == 0
    out = capsys.readouterr().out
    assert "where: av_db>=40" in out
    assert "pdk: sky130" in out
    assert "count: 1" in out


def test_cli_validate_exits_zero_when_all_valid(_cli_kb, capsys):
    _cli_kb({"entry-a": _valid_entry("entry-a")})

    exit_code = main(["kb", "validate", "--format", "json"])

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["valid"] is True


def test_cli_validate_exits_nonzero_with_structured_errors(_cli_kb, capsys):
    broken = _valid_entry("broken-entry")
    del broken["topology"]
    _cli_kb({"broken-entry": broken})

    exit_code = main(["kb", "validate", "--format", "json"])

    assert exit_code != 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["valid"] is False
    (result,) = payload["entries"]
    assert result["id"] == "broken-entry"
    assert result["valid"] is False
    assert result["errors"]


def test_cli_validate_text_lists_invalid_entries(_cli_kb, capsys):
    broken = _valid_entry("broken-entry")
    del broken["topology"]
    _cli_kb({"broken-entry": broken})

    exit_code = main(["kb", "validate"])

    assert exit_code != 0
    out = capsys.readouterr().out
    assert "valid: no" in out
    assert "broken-entry" in out


def test_cli_validate_recheck_measured_flag_reruns_testbench(
    _cli_kb, tmp_path, monkeypatch, capsys
):
    root = _cli_kb({"entry-a": _entry_with_one_figure("examples/entry-a/request.json")})
    (root.parent / "examples" / "entry-a").mkdir(parents=True)
    (root.parent / "examples" / "entry-a" / "request.json").write_text("{}")
    monkeypatch.setattr(
        kb, "run_sim", lambda *a, **k: _fake_sim_report({"av_db": 10.0})
    )

    exit_code = main(["kb", "validate", "--recheck-measured", "--format", "json"])

    assert exit_code != 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["valid"] is False
    (result,) = payload["entries"]
    assert any("drifted" in error for error in result["errors"])


def test_cli_validate_without_recheck_measured_flag_never_runs_sim(
    _cli_kb, tmp_path, monkeypatch, capsys
):
    root = _cli_kb({"entry-a": _entry_with_one_figure("examples/entry-a/request.json")})
    (root.parent / "examples" / "entry-a").mkdir(parents=True)
    (root.parent / "examples" / "entry-a" / "request.json").write_text("{}")

    def _boom(*_args, **_kwargs):
        raise AssertionError("run_sim must not run without --recheck-measured")

    monkeypatch.setattr(kb, "run_sim", _boom)

    exit_code = main(["kb", "validate", "--format", "json"])

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["valid"] is True


def test_cli_kb_no_subcommand_prints_help(capsys):
    exit_code = main(["kb"])

    assert exit_code == 2
    assert "kb" in capsys.readouterr().err


def test_cli_kb_missing_dir_error_envelope(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(kb, "DEFAULT_KB_ROOT", tmp_path / "no-such-kb")

    exit_code = main(["kb", "list", "--format", "json"])

    assert exit_code == 1
    error = json.loads(capsys.readouterr().err)
    assert error["error"]["command"] == "kb list"


def test_cli_validate_fails_with_missing_artifact_path(_cli_kb, tmp_path, capsys):
    _cli_kb(
        {
            "entry-a": _valid_entry(
                "entry-a",
                artifacts={"netlist": "examples/entry-a/does-not-exist.spice"},
            )
        }
    )

    exit_code = main(["kb", "validate", "--format", "json"])

    assert exit_code != 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["valid"] is False
    (result,) = payload["entries"]
    assert result["id"] == "entry-a"
    assert any("artifacts/netlist" in error for error in result["errors"])
