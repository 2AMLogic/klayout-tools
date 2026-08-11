"""Tests for the ``docs/design-evidence-tiers.md`` parser
(:mod:`klayout_tools.design_evidence_tiers`, issue #722).
"""

from __future__ import annotations

import pytest

from klayout_tools.design_evidence_tiers import (
    DEFAULT_DOC_PATH,
    DesignEvidenceTiersError,
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
