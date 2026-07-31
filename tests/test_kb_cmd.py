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
