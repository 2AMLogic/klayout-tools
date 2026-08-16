"""Tests for the ``docs/design-evidence-tiers.md`` parser
(:mod:`klayout_tools.design_evidence_tiers`, issue #722).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from klayout_tools import design_evidence_tiers
from klayout_tools.design_evidence_tiers import (
    CANONICAL_DOC_LABEL,
    DEFAULT_DOC_PATH,
    DOC_PATH_ENV_VAR,
    DesignEvidenceTiersError,
    default_doc_path,
    doc_source_label,
    parse_tier_doc,
)

# A minimal doc that reproduces the real doc's structural shape (ladder
# table + numbered T1 checklist with per-kind bullets) so tests can assert
# on parsing behaviour without depending on the real doc's exact prose --
# and so the "no code-change needed" test below can edit *this* text and
# see the parsed skeleton change.
_MINIMAL_DOC = """\
# Design-evidence tiers

## The ladder

| Tier | Claim | Demonstrated by |
|---|---|---|
| **T1 — sim-validated** | Designed and simulation-validated | Open-source evidence |
| **T2 — signoff-validated** | Validated on commercial tools | T1, plus commercial |
| **T3 — silicon-validated** | Fabricated and measured | T2, plus a tapeout |
| **T4 — production-validated** | Proven in silicon | An external project |

## T1 checklist — what "sim-validated" requires

### Block kind

Some prose ahead of the itemized list that must not be mistaken for an item.

1. **Design sources**
   - *Analog* — committed schematic sources.
   - *Digital* — committed RTL sources.
2. **DRC clean** — latest `klt drc` JSON report: `status: clean`.
   Continuation line about coverage gaps.
3. **Full corner verification**
   - *Analog* — PVT corner-matrix results.
   - *Digital* — multi-corner STA results.
   - Both require a ratified spec.

## Verification rules

- **Staleness is failure.** Some prose here should never be parsed as an item.
"""


REPO_ROOT = Path(__file__).resolve().parent.parent


def _write_doc(tmp_path, text: str) -> str:
    path = tmp_path / "design-evidence-tiers.md"
    path.write_text(text, encoding="utf-8")
    return str(path)


# --------------------------------------------------------------------------- #
# Real doc: mechanical parse matches the documented structure
# --------------------------------------------------------------------------- #


def test_real_doc_parses_without_error():
    doc = parse_tier_doc()

    assert [row["tier"] for row in doc["ladder"]] == ["T1", "T2", "T3", "T4"]
    assert [item["id"] for item in doc["t1_items"]] == list(range(1, 11))


def test_real_doc_ladder_rows_have_claim_and_demonstrated_by():
    doc = parse_tier_doc()

    t1 = next(row for row in doc["ladder"] if row["tier"] == "T1")
    assert t1["name"] == "sim-validated"
    assert "simulation-validated" in t1["claim"]
    assert "checklist below" in t1["demonstrated_by"]

    t2 = next(row for row in doc["ladder"] if row["tier"] == "T2")
    assert t2["claim"].startswith("Validated on commercial")
    assert t2["demonstrated_by"].startswith("T1, plus")


def test_real_doc_per_kind_items_have_both_columns():
    doc = parse_tier_doc()
    by_id = {item["id"]: item for item in doc["t1_items"]}

    for item_id in (1, 2, 5, 7):
        item = by_id[item_id]
        assert item["text"] is None
        assert set(item["columns"]) == {"analog", "digital"}
        assert item["columns"]["analog"]
        assert item["columns"]["digital"]


def test_real_doc_kind_independent_items_have_shared_text():
    doc = parse_tier_doc()
    by_id = {item["id"]: item for item in doc["t1_items"]}

    for item_id in (3, 4, 6, 8, 9, 10):
        item = by_id[item_id]
        assert item["columns"] == {}
        assert item["text"]


def test_real_doc_item_titles():
    doc = parse_tier_doc()
    titles = {item["id"]: item["title"] for item in doc["t1_items"]}

    assert titles[1] == "Design sources"
    assert titles[3] == "DRC clean"
    assert titles[4] == "LVS clean"
    assert titles[10] == "Repo hygiene"


def test_real_doc_item_5_has_a_kind_independent_note():
    doc = parse_tier_doc()
    item_5 = next(item for item in doc["t1_items"] if item["id"] == 5)

    assert len(item_5["notes"]) == 1
    assert "ratified" in item_5["notes"][0]


def test_default_doc_path_points_at_the_real_doc():
    assert DEFAULT_DOC_PATH.name == "design-evidence-tiers.md"
    assert DEFAULT_DOC_PATH.is_file()


# --------------------------------------------------------------------------- #
# Default-path resolution (issue #1050): a packaged (wheel) install has no
# sibling `docs/` directory, so the default must resolve against the
# *installed package* first and only fall back to the source checkout.
# --------------------------------------------------------------------------- #


def test_packaged_doc_path_lives_inside_the_installed_package():
    # The bundled copy must sit under the package directory itself -- that is
    # the only location that survives a wheel install (issue #1050).
    package_dir = Path(design_evidence_tiers.__file__).resolve().parent
    assert design_evidence_tiers._PACKAGED_DOC_PATH.parent.parent == package_dir, (
        "the bundled doc must resolve relative to the installed package"
    )


def test_wheel_build_bundles_the_doc_inside_the_package():
    # Mechanical check that packaging config and the resolution above cannot
    # drift: pyproject must force-include the canonical doc at exactly the
    # package-relative path `_PACKAGED_DOC_PATH` resolves to.
    #
    # `tomllib` is stdlib only from 3.11 (PEP 680) and this project supports
    # 3.10, so scope the import to this one test rather than the module: a
    # module-level import would make the whole file uncollectable on 3.10 and
    # take every other test here (including the #1050 regression tests) with
    # it. Deliberately not worth a `tomli` backport dependency -- the drift
    # guard running on 3.11+ is enough to catch a config/code mismatch.
    tomllib = pytest.importorskip("tomllib")

    pyproject = tomllib.loads(
        (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    )
    force_include = pyproject["tool"]["hatch"]["build"]["targets"]["wheel"][
        "force-include"
    ]

    package_dir = Path(design_evidence_tiers.__file__).resolve().parent
    expected = str(
        design_evidence_tiers._PACKAGED_DOC_PATH.relative_to(package_dir.parent)
    )
    assert force_include["docs/design-evidence-tiers.md"] == expected


def test_default_doc_path_prefers_the_packaged_copy(monkeypatch, tmp_path):
    packaged = tmp_path / "packaged.md"
    packaged.write_text(_MINIMAL_DOC, encoding="utf-8")
    monkeypatch.setattr(design_evidence_tiers, "_PACKAGED_DOC_PATH", packaged)
    monkeypatch.setattr(
        design_evidence_tiers, "_SOURCE_DOC_PATH", tmp_path / "checkout.md"
    )
    monkeypatch.delenv(DOC_PATH_ENV_VAR, raising=False)

    assert default_doc_path() == packaged


def test_default_path_works_without_a_sibling_docs_directory(monkeypatch, tmp_path):
    """Regression for issue #1050: a wheel install puts the package under
    site-packages with no `docs/` three directories up. The bundled copy must
    carry the parse on its own."""
    packaged = tmp_path / "site-packages" / "klayout_tools" / "data" / "tiers.md"
    packaged.parent.mkdir(parents=True)
    packaged.write_text(_MINIMAL_DOC, encoding="utf-8")
    monkeypatch.setattr(design_evidence_tiers, "_PACKAGED_DOC_PATH", packaged)
    # The source-checkout fallback does not exist in a packaged install.
    monkeypatch.setattr(
        design_evidence_tiers,
        "_SOURCE_DOC_PATH",
        tmp_path / "site-packages" / "docs" / "design-evidence-tiers.md",
    )
    monkeypatch.delenv(DOC_PATH_ENV_VAR, raising=False)

    doc = parse_tier_doc()

    assert [row["tier"] for row in doc["ladder"]] == ["T1", "T2", "T3", "T4"]


def test_default_doc_path_falls_back_to_the_source_checkout(monkeypatch, tmp_path):
    source = tmp_path / "checkout.md"
    source.write_text(_MINIMAL_DOC, encoding="utf-8")
    monkeypatch.setattr(
        design_evidence_tiers, "_PACKAGED_DOC_PATH", tmp_path / "not-bundled.md"
    )
    monkeypatch.setattr(design_evidence_tiers, "_SOURCE_DOC_PATH", source)
    monkeypatch.delenv(DOC_PATH_ENV_VAR, raising=False)

    assert default_doc_path() == source


def test_env_var_overrides_the_default_doc_path(monkeypatch, tmp_path):
    override = tmp_path / "vendored.md"
    override.write_text(_MINIMAL_DOC, encoding="utf-8")
    monkeypatch.setenv(DOC_PATH_ENV_VAR, str(override))

    assert default_doc_path() == override
    doc = parse_tier_doc()
    assert [item["id"] for item in doc["t1_items"]] == [1, 2, 3]


def test_both_override_forms_expand_a_leading_tilde(monkeypatch, tmp_path):
    """A quoted `--tiers-doc '~/vendored.md'` reaches the parser unexpanded
    (the shell never touched it), so it must read the same way
    `KLT_TIERS_DOC=~/vendored.md` does -- one expansion rule for both
    override paths."""
    home = tmp_path / "home"
    home.mkdir()
    (home / "vendored.md").write_text(_MINIMAL_DOC, encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))  # Windows' expanduser source

    monkeypatch.setenv(DOC_PATH_ENV_VAR, "~/vendored.md")
    assert default_doc_path() == home / "vendored.md"
    assert doc_source_label() == str(home / "vendored.md")

    monkeypatch.delenv(DOC_PATH_ENV_VAR, raising=False)
    doc = parse_tier_doc("~/vendored.md")
    assert [item["id"] for item in doc["t1_items"]] == [1, 2, 3]
    assert doc_source_label("~/vendored.md") == str(home / "vendored.md")


def test_doc_source_label_defers_to_default_doc_path(monkeypatch, tmp_path):
    """`doc_source_label()` must not carry its own copy of the precedence
    rule: with no override it names the canonical doc whichever default
    location resolved, and it reports an override only because
    `default_doc_path()` resolved to one."""
    packaged = tmp_path / "packaged.md"
    packaged.write_text(_MINIMAL_DOC, encoding="utf-8")
    source = tmp_path / "checkout.md"
    source.write_text(_MINIMAL_DOC, encoding="utf-8")
    monkeypatch.setattr(design_evidence_tiers, "_PACKAGED_DOC_PATH", packaged)
    monkeypatch.setattr(design_evidence_tiers, "_SOURCE_DOC_PATH", source)
    monkeypatch.delenv(DOC_PATH_ENV_VAR, raising=False)

    # Bundled copy present (wheel layout) -- still the canonical label.
    assert doc_source_label() == CANONICAL_DOC_LABEL

    # Source-checkout fallback -- same canonical label, different file.
    monkeypatch.setattr(
        design_evidence_tiers, "_PACKAGED_DOC_PATH", tmp_path / "not-bundled.md"
    )
    assert doc_source_label() == CANONICAL_DOC_LABEL

    # Only an override renames the source.
    monkeypatch.setenv(DOC_PATH_ENV_VAR, str(tmp_path / "vendored.md"))
    assert doc_source_label() == str(tmp_path / "vendored.md")


def test_explicit_path_beats_the_env_var(monkeypatch, tmp_path):
    override = tmp_path / "vendored.md"
    override.write_text(_MINIMAL_DOC.replace("Design sources", "Env doc"), "utf-8")
    monkeypatch.setenv(DOC_PATH_ENV_VAR, str(override))
    explicit = _write_doc(tmp_path, _MINIMAL_DOC)

    doc = parse_tier_doc(explicit)

    assert doc["t1_items"][0]["title"] == "Design sources"


def test_unreadable_default_doc_still_raises_the_structured_error(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(
        design_evidence_tiers, "_PACKAGED_DOC_PATH", tmp_path / "not-bundled.md"
    )
    monkeypatch.setattr(
        design_evidence_tiers, "_SOURCE_DOC_PATH", tmp_path / "not-a-checkout.md"
    )
    monkeypatch.delenv(DOC_PATH_ENV_VAR, raising=False)

    with pytest.raises(DesignEvidenceTiersError, match="could not read"):
        parse_tier_doc()


# --------------------------------------------------------------------------- #
# Minimal synthetic doc: verify the parser tracks structure, not prose
# --------------------------------------------------------------------------- #


def test_minimal_doc_parses_expected_shape(tmp_path):
    path = _write_doc(tmp_path, _MINIMAL_DOC)

    doc = parse_tier_doc(path)

    assert [row["tier"] for row in doc["ladder"]] == ["T1", "T2", "T3", "T4"]
    assert [item["id"] for item in doc["t1_items"]] == [1, 2, 3]
    assert doc["t1_items"][0]["title"] == "Design sources"
    assert doc["t1_items"][1]["title"] == "DRC clean"
    assert "coverage gaps" in doc["t1_items"][1]["text"]
    assert doc["t1_items"][2]["notes"] == ["Both require a ratified spec."]


def test_editing_the_doc_changes_the_parsed_skeleton_without_a_code_change(
    tmp_path,
):
    """The parser must be driven entirely by the doc's text -- renaming an
    item's title in the doc changes the parsed skeleton with zero code
    changes here, which is the whole point of parsing rather than
    hardcoding (issue #722's acceptance criteria)."""
    edited = _MINIMAL_DOC.replace(
        "1. **Design sources**", "1. **Renamed checklist item**"
    )
    path = _write_doc(tmp_path, edited)

    doc = parse_tier_doc(path)

    assert doc["t1_items"][0]["title"] == "Renamed checklist item"


def test_prose_before_first_item_is_not_parsed_as_an_item(tmp_path):
    path = _write_doc(tmp_path, _MINIMAL_DOC)

    doc = parse_tier_doc(path)

    # "### Block kind" and its prose paragraph precede item 1 -- neither
    # should show up as a spurious item.
    assert all(item["id"] in (1, 2, 3) for item in doc["t1_items"])


# --------------------------------------------------------------------------- #
# Error handling
# --------------------------------------------------------------------------- #


def test_missing_file_raises(tmp_path):
    with pytest.raises(DesignEvidenceTiersError, match="could not read"):
        parse_tier_doc(str(tmp_path / "nope.md"))


def test_missing_ladder_heading_raises(tmp_path):
    text = _MINIMAL_DOC.replace("## The ladder", "## Something else")
    path = _write_doc(tmp_path, text)

    with pytest.raises(DesignEvidenceTiersError, match="The ladder"):
        parse_tier_doc(path)


def test_missing_t1_checklist_heading_raises(tmp_path):
    text = _MINIMAL_DOC.replace(
        '## T1 checklist — what "sim-validated" requires', "## Something else"
    )
    path = _write_doc(tmp_path, text)

    with pytest.raises(DesignEvidenceTiersError, match="T1 checklist"):
        parse_tier_doc(path)


def test_empty_ladder_table_raises(tmp_path):
    text = """\
## The ladder

| Tier | Claim | Demonstrated by |
|---|---|---|

## T1 checklist — what "sim-validated" requires

1. **Design sources** — some text.

## Verification rules
"""
    path = _write_doc(tmp_path, text)

    with pytest.raises(DesignEvidenceTiersError, match="ladder"):
        parse_tier_doc(path)


def test_empty_t1_checklist_raises(tmp_path):
    text = """\
## The ladder

| Tier | Claim | Demonstrated by |
|---|---|---|
| **T1 — sim-validated** | claim | demonstrated |

## T1 checklist — what "sim-validated" requires

Just prose, no numbered items.

## Verification rules
"""
    path = _write_doc(tmp_path, text)

    with pytest.raises(DesignEvidenceTiersError, match="T1 checklist"):
        parse_tier_doc(path)
