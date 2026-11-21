"""Issue #6403: a ``generic``/``model-run`` envelope that declares
``provenance.input.content_hash`` with no usable ``provenance.input.path``
has that hash silently never re-hashed -- the citation renders
``input_verified: null`` exactly like one declaring no hash at all. The
additive ``input_verified_note: "declared_hash_unbound"`` disclosure makes
"declared, never checked" visible on the row; ``input_verified`` stays
``null`` and no verdict moves."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from klayout_tools.cli import main
from klayout_tools.signoff import build_tier_report, input_set_digest


def _hash_of(path) -> str:
    return "sha256:" + hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _generic_env(**input_block) -> dict:
    env = {
        "schema_version": 1,
        "kind": "generic",
        "status": "pass",
        "summary": "Q3 characterization sweep: all spec rows within limits",
        "source": "docs/characterization/2026-q3-report.md",
    }
    if input_block:
        env["provenance"] = {"input": input_block}
    return env


def _model_run_env(**input_block) -> dict:
    env = {
        "schema_version": 1,
        "kind": "model-run",
        "status": "pass",
        "method": "linear small-signal model, ideal supplies",
        "measurements": [
            {
                "name": "gain",
                "status": "pass",
                "limits": {"min": 1.0},
                "spec_row": "gain",
            }
        ],
    }
    if input_block:
        env["provenance"] = {"input": input_block}
    return env


def _cite(manifest: dict, item_id: int) -> dict:
    item = next(i for i in build_tier_report(manifest)["items"] if i["id"] == item_id)
    assert item["status"] == "met"
    cit = item["citation"]
    return cit[0] if isinstance(cit, list) else cit


def _generic_cite(tmp_path, env: dict) -> dict:
    path = tmp_path / "characterization.json"
    path.write_text(json.dumps(env))
    return _cite({"block": "b", "kind": "analog", "evidence": {"8": str(path)}}, 8)


def _model_run_cite(tmp_path, env: dict) -> dict:
    path = tmp_path / "model.json"
    path.write_text(json.dumps(env))
    manifest = {
        "block": "b",
        "kind": "analog",
        "evidence": {"5": [{"file": str(path), "rows": ["gain"]}]},
        "spec_rows": {"5": [{"id": "gain"}]},
    }
    return _cite(manifest, 5)


#: (envelope factory, citing helper, item id) per kind sharing the #2403
#: opt-in branch -- the note must behave identically on both.
_KINDS = [
    ("generic", _generic_env, _generic_cite),
    ("model-run", _model_run_env, _model_run_cite),
]


@pytest.mark.parametrize("kind,make,cite", _KINDS)
def test_note_on_declared_pathless_hash(tmp_path, kind, make, cite):
    """The reported gap: a declared hash with no path is never checked, yet
    read as a binding. The note says so on the met citation."""
    cit = cite(tmp_path, make(content_hash="sha256:" + "0" * 64))
    assert cit["input_verified_note"] == "declared_hash_unbound"
    assert cit["input_verified"] is None
    assert cit["content_hash"] == "sha256:" + "0" * 64


@pytest.mark.parametrize("kind,make,cite", _KINDS)
def test_no_note_when_path_present_and_matching(tmp_path, kind, make, cite):
    report = tmp_path / "artifact.txt"
    report.write_text("the attested record")
    cit = cite(tmp_path, make(content_hash=_hash_of(report), path="artifact.txt"))
    assert "input_verified_note" not in cit
    assert cit["input_verified"] is True


@pytest.mark.parametrize("kind,make,cite", _KINDS)
def test_no_note_when_path_present_and_changed(tmp_path, kind, make, cite):
    report = tmp_path / "artifact.txt"
    report.write_text("the attested record")
    recorded = _hash_of(report)
    report.write_text("regenerated underneath the report")
    cit = cite(tmp_path, make(content_hash=recorded, path="artifact.txt"))
    assert "input_verified_note" not in cit
    assert cit["input_verified"] is False


@pytest.mark.parametrize("kind,make,cite", _KINDS)
def test_no_note_when_path_present_but_unresolvable(tmp_path, kind, make, cite):
    """A declared path that resolves to nothing is grader-tried, not
    author-unbound: it keeps the plain ``null``, without the note."""
    cit = cite(tmp_path, make(content_hash="sha256:" + "1" * 64, path="nope.txt"))
    assert "input_verified_note" not in cit
    assert cit["input_verified"] is None


@pytest.mark.parametrize("kind,make,cite", _KINDS)
def test_no_note_when_no_hash_declared(tmp_path, kind, make, cite):
    cit = cite(tmp_path, make())
    assert "input_verified_note" not in cit
    assert cit["input_verified"] is None
    assert cit["content_hash"] is None


@pytest.mark.parametrize("kind,make,cite", _KINDS)
def test_no_note_for_empty_string_hash(tmp_path, kind, make, cite):
    cit = cite(tmp_path, make(content_hash=""))
    assert "input_verified_note" not in cit
    assert cit["input_verified"] is None
    assert cit["content_hash"] == ""


@pytest.mark.parametrize("kind,make,cite", _KINDS)
def test_note_for_null_path(tmp_path, kind, make, cite):
    """A present-but-``null`` ``path`` runs the opt-in branch and resolves
    to nothing, exactly like an absent one: the declared hash is equally
    never checked, so it counts as unbound."""
    cit = cite(tmp_path, make(content_hash="sha256:" + "2" * 64, path=None))
    assert cit["input_verified_note"] == "declared_hash_unbound"
    assert cit["input_verified"] is None


@pytest.mark.parametrize("kind,make,cite", _KINDS)
def test_note_for_external_scope_path(tmp_path, kind, make, cite):
    """``scope: "external"`` carries no resolvable path by design, so a
    hash declared beside it is unbound here too."""
    cit = cite(
        tmp_path,
        make(
            content_hash="sha256:" + "3" * 64,
            path={"path": "elsewhere/record.md", "scope": "external"},
        ),
    )
    assert cit["input_verified_note"] == "declared_hash_unbound"


def test_no_note_when_paths_set_declared(tmp_path):
    """A ``provenance.input.paths`` set binds through its members instead --
    verification answers there, so no unbound-hash note."""
    member = tmp_path / "member.txt"
    member.write_text("member bytes")
    digest = input_set_digest([("member.txt", _hash_of(member))])
    env = _generic_env(
        content_hash=digest,
        paths=[{"path": "member.txt", "content_hash": _hash_of(member)}],
    )
    cit = _generic_cite(tmp_path, env)
    assert "input_verified_note" not in cit
    assert cit["input_verified"] is True


def test_note_for_empty_paths_set(tmp_path):
    """An empty set has no member to verify, so nothing is re-hashed -- the
    declared hash binds nothing, exactly the reported gap: the note fires."""
    env = _generic_env(content_hash="sha256:" + "6" * 64, paths=[])
    cit = _generic_cite(tmp_path, env)
    assert cit["input_verified_note"] == "declared_hash_unbound"
    assert cit["input_verified"] is None


def test_note_for_malformed_paths_set(tmp_path):
    """A malformed set (not a list) never answers through its members and
    never falls back to ``input.path``: nothing is re-hashed, so the note
    fires rather than leaving the declared hash reading as a binding."""
    env = _generic_env(content_hash="sha256:" + "7" * 64, paths="not-a-list")
    cit = _generic_cite(tmp_path, env)
    assert cit["input_verified_note"] == "declared_hash_unbound"
    assert cit["input_verified"] is None


def test_note_for_model_run_paths_set(tmp_path):
    """A ``model-run`` envelope's set is never member-verified
    (``_fold_kind_inputs`` folds a set for ``generic`` only), so a declared
    hash beside a well-formed one is still never re-hashed: the note fires
    there too -- the blanket set exclusion reintroduced the reported gap."""
    member = tmp_path / "member.txt"
    member.write_text("member bytes")
    env = _model_run_env(
        content_hash=input_set_digest([("member.txt", _hash_of(member))]),
        paths=[{"path": "member.txt", "content_hash": _hash_of(member)}],
    )
    cit = _model_run_cite(tmp_path, env)
    assert cit["input_verified_note"] == "declared_hash_unbound"
    assert cit["input_verified"] is None


def test_no_note_when_paths_set_members_unresolvable(tmp_path):
    """A well-formed set whose members cannot be read here still answered
    through its members (``input_members`` rendered, each saying
    ``unverifiable``) -- grader-tried, like an unresolvable single ``path``,
    so no note even though ``input_verified`` stays ``null``."""
    recorded = "sha256:" + "9" * 64
    env = _generic_env(
        content_hash=input_set_digest([("gone.txt", recorded)]),
        paths=[{"path": "gone.txt", "content_hash": recorded}],
    )
    cit = _generic_cite(tmp_path, env)
    assert "input_verified_note" not in cit
    assert cit["input_verified"] is None


def test_no_note_for_native_kind(tmp_path):
    """A native kind's declared hash is checked against the field that kind
    names (``drc``'s ``file``) -- nothing is unbound, so no note."""
    layout = tmp_path / "design.gds"
    layout.write_bytes(b"GDS-A")
    env = {
        "schema_version": 1,
        "file": "design.gds",
        "deck": "sky130",
        "dbu_um": 0.001,
        "status": "clean",
        "violation_count": 0,
        "rule_counts": {},
        "violations": [],
        "coverage": {
            "deck_layers": ["65/20"],
            "layers_checked": ["65/20"],
            "layers_in_stream_without_rules": [],
            "rules_skipped": [],
        },
        "provenance": {"input": {"content_hash": _hash_of(layout), "role": "layout"}},
    }
    path = tmp_path / "drc.json"
    path.write_text(json.dumps(env))
    cit = _cite({"block": "b", "kind": "analog", "evidence": {"3": str(path)}}, 3)
    assert "input_verified_note" not in cit
    assert cit["input_verified"] is True


@pytest.mark.parametrize("kind,make,cite", _KINDS)
def test_note_never_moves_a_verdict(tmp_path, kind, make, cite):
    """Disclosure only: the same envelope with and without the pathless
    declaration grades ``met`` identically apart from the declared hash,
    its ``null`` verification, and the note itself."""
    with_hash = dict(cite(tmp_path, make(content_hash="sha256:" + "4" * 64)))
    without_hash = dict(cite(tmp_path, make()))
    for cit in (with_hash, without_hash):
        cit.pop("content_hash", None)
        cit.pop("input_verified_note", None)
    assert with_hash == without_hash


def test_text_report_names_the_note(tmp_path, capsys):
    """The rendered row a reviewer actually reads says "declared, never
    checked" -- not the generic not-re-hashed line alone."""
    path = tmp_path / "characterization.json"
    path.write_text(json.dumps(_generic_env(content_hash="sha256:" + "5" * 64)))
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps({"block": "b", "kind": "analog", "evidence": {"8": str(path)}})
    )

    main(["signoff", "--manifest", str(manifest_path), "--format", "text"])

    assert "input note: declared_hash_unbound" in capsys.readouterr().out


def test_text_report_omits_the_note_when_unbound_never_declared(tmp_path, capsys):
    """No declared hash, nothing to disclose: the note line never prints."""
    path = tmp_path / "characterization.json"
    path.write_text(json.dumps(_generic_env()))
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps({"block": "b", "kind": "analog", "evidence": {"8": str(path)}})
    )

    main(["signoff", "--manifest", str(manifest_path), "--format", "text"])

    assert "input note:" not in capsys.readouterr().out
