"""Validate the `kb/` knowledge base against its JSON Schema.

See `kb/README.md` for the schema summary, sourcing rules, and how to add
a new entry. Schema-conformance and id/filename-match checks against the
checked-in corpus are delegated to `klayout_tools.kb.validate_entries()` --
the same function `klt kb validate` calls -- so that verb is this repo's
single implementation of "is a kb entry valid" (see `tests/test_kb_cmd.py`
for the CLI/library's own dedicated, fixture-driven test coverage, including
an invalid-entry case). The tests below cover what that function does not:
schema well-formedness and content/sourcing rules specific to this corpus.
"""

import copy
import json
from pathlib import Path

import jsonschema
import pytest

from klayout_tools import kb

KB_ROOT = Path(__file__).resolve().parent.parent / "kb"
SCHEMA_PATH = KB_ROOT / "schema" / "entry.schema.json"
ENTRIES_DIR = KB_ROOT / "entries"


def _load_schema() -> dict:
    return json.loads(SCHEMA_PATH.read_text())


def _entry_paths() -> list[Path]:
    return sorted(ENTRIES_DIR.glob("*.json"))


def test_schema_file_is_valid_json():
    # json.loads raises if this isn't well-formed JSON.
    schema = json.loads(SCHEMA_PATH.read_text())
    assert isinstance(schema, dict)


def test_schema_is_well_formed_json_schema():
    schema = _load_schema()
    # Raises jsonschema.exceptions.SchemaError if the schema itself is
    # malformed (e.g. bad "type" values, invalid $ref).
    jsonschema.Draft202012Validator.check_schema(schema)


def test_at_least_one_seed_entry_exists():
    assert len(_entry_paths()) >= 2, "expected 2-3 seed entries under kb/entries/"


@pytest.mark.parametrize("path", _entry_paths(), ids=lambda p: p.stem)
def test_entry_is_valid_json(path: Path):
    data = json.loads(path.read_text())
    assert isinstance(data, dict)


def test_entries_validate_against_schema_and_id_matches_filename():
    """Schema conformance and id-matches-filename for every checked-in entry,
    via `klt kb validate`'s own implementation (`kb.validate_entries()`) --
    not a second, hand-rolled `jsonschema.validate()` loop -- so there is one
    place that decides what makes a kb entry valid."""
    report = kb.validate_entries(root=KB_ROOT)

    assert report["entry_count"] == len(_entry_paths())
    invalid = [entry for entry in report["entries"] if not entry["valid"]]
    assert invalid == [], invalid


@pytest.mark.parametrize("path", _entry_paths(), ids=lambda p: p.stem)
def test_entry_has_real_source_citation(path: Path):
    """Sourcing rule check (kb/README.md): every entry must cite a real,
    non-empty source. This does not verify the citation resolves (that is a
    manual step per the issue's test plan), only that one is present."""
    entry = json.loads(path.read_text())
    citation = entry["source"]["citation"]
    assert isinstance(citation, str)
    assert len(citation.strip()) > 20


@pytest.mark.parametrize("path", _entry_paths(), ids=lambda p: p.stem)
def test_seed_entry_populates_optional_fields(path: Path):
    """The issue's acceptance bar: seed entries should populate every field
    (not just the required ones) to actually prove the schema against real
    content."""
    entry = json.loads(path.read_text())
    assert entry.get("pdk_portability")
    assert entry["pdk_portability"].get("primary_pdk")
    assert entry["pdk_portability"].get("notes")
    assert entry.get("sizing_approach")
    assert entry.get("layout_idioms")
    assert entry.get("notes")
    assert entry["source"].get("url")
    assert entry["source"].get("license_or_openness")


def _valid_reference_entry() -> dict:
    """A minimal, schema-valid entry used as the base for the negative
    (deliberately-broken) test cases below."""
    return {
        "id": "example-entry",
        "title": "Example entry",
        "topology": "Example topology",
        "spec_class": "example spec class",
        "source": {"citation": 'Example, A., "An example paper," 2020.'},
    }


def test_reference_entry_is_valid():
    """Sanity check: the minimal fixture used by the negative tests below is
    itself schema-valid, so the negative tests are proven to fail for the
    *specific* reason under test, not because the fixture is broken some
    other way."""
    schema = _load_schema()
    jsonschema.validate(instance=_valid_reference_entry(), schema=schema)


def test_entry_missing_required_source_citation_fails_validation():
    """Deliberately-broken fixture (per the issue's test plan): dropping the
    required `source.citation` field must fail schema validation."""
    schema = _load_schema()
    broken = copy.deepcopy(_valid_reference_entry())
    del broken["source"]["citation"]

    with pytest.raises(jsonschema.exceptions.ValidationError):
        jsonschema.validate(instance=broken, schema=schema)


def test_entry_missing_required_top_level_field_fails_validation():
    schema = _load_schema()
    broken = copy.deepcopy(_valid_reference_entry())
    del broken["topology"]

    with pytest.raises(jsonschema.exceptions.ValidationError):
        jsonschema.validate(instance=broken, schema=schema)


def test_entry_without_optional_layout_idioms_still_validates():
    """Edge case: the schema must tolerate an entry where an optional field
    is entirely absent, without breaking validation."""
    schema = _load_schema()
    entry = _valid_reference_entry()
    assert "layout_idioms" not in entry
    jsonschema.validate(instance=entry, schema=schema)


def test_entry_with_null_optional_fields_still_validates():
    schema = _load_schema()
    entry = _valid_reference_entry()
    entry["pdk_portability"] = None
    entry["sizing_approach"] = None
    entry["layout_idioms"] = None
    entry["notes"] = None
    entry["source"]["url"] = None
    entry["source"]["license_or_openness"] = None
    jsonschema.validate(instance=entry, schema=schema)


# --------------------------------------------------------------------------- #
# artifacts field (schema shape only -- path-existence checks are
# validate_entries()'s job, covered in tests/test_kb_cmd.py)
# --------------------------------------------------------------------------- #


def test_entry_without_artifacts_still_validates():
    schema = _load_schema()
    entry = _valid_reference_entry()
    assert "artifacts" not in entry
    jsonschema.validate(instance=entry, schema=schema)


def test_entry_with_null_artifacts_still_validates():
    schema = _load_schema()
    entry = _valid_reference_entry()
    entry["artifacts"] = None
    jsonschema.validate(instance=entry, schema=schema)


@pytest.mark.parametrize(
    "artifacts",
    [
        {"netlist": "examples/kb/some-entry/testbench.spice"},
        {"layout": "examples/kb/some-entry/layout.gds"},
        {
            "netlist": "examples/kb/some-entry/testbench.spice",
            "layout": "examples/kb/some-entry/layout.gds",
        },
    ],
    ids=["netlist-only", "layout-only", "both"],
)
def test_entry_with_artifacts_link_validates(artifacts):
    schema = _load_schema()
    entry = _valid_reference_entry()
    entry["artifacts"] = artifacts
    jsonschema.validate(instance=entry, schema=schema)


def test_entry_with_empty_artifacts_object_fails_validation():
    """The schema requires at least one of netlist/layout when `artifacts`
    is given as an object at all -- an entirely empty object is a mistake,
    not a "no artifact yet" signal (that's what omitting/null is for)."""
    schema = _load_schema()
    entry = _valid_reference_entry()
    entry["artifacts"] = {}

    with pytest.raises(jsonschema.exceptions.ValidationError):
        jsonschema.validate(instance=entry, schema=schema)


def test_entry_with_unknown_artifacts_key_fails_validation():
    schema = _load_schema()
    entry = _valid_reference_entry()
    entry["artifacts"] = {"netlist": "examples/foo.spice", "bogus": "x"}

    with pytest.raises(jsonschema.exceptions.ValidationError):
        jsonschema.validate(instance=entry, schema=schema)


def test_at_least_two_seed_entries_have_a_real_artifacts_link():
    """Issue #126's end-to-end proof bar: at least two entries in the
    checked-in corpus link to a real artifact, not just a theoretical schema
    addition. Path existence itself is covered by
    `test_entries_validate_against_schema_and_id_matches_filename` above,
    which runs the full `validate_entries()` (the same function `klt kb
    validate` calls) against this corpus."""
    entries_with_artifacts = [
        path.stem
        for path in _entry_paths()
        if json.loads(path.read_text()).get("artifacts")
    ]
    assert len(entries_with_artifacts) >= 2, entries_with_artifacts
