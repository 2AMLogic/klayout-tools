"""Digest-level canonical-YAML cases (issue #5862).

Sequences, unsupported node types and scalar canonicalization edge
cases, exercised directly through ``content_digest`` /
``canonical_dumps`` -- split out of ``test_signoff_attest_canonical_yaml``
to stay under the per-file size ratchet's 500-line new-file limit.
"""

from __future__ import annotations

import base64
import json
import sys
from pathlib import Path

import pytest
import yaml

from klayout_tools.signoff_attest import (
    AttestationError,
    build_attestation,
)
from klayout_tools.signoff_attest_canonical_yaml import (
    CanonicalYamlError,
    canonical_dumps,
    content_digest,
)

_OK = [sys.executable, "-c", "import sys; sys.exit(0)"]

_WORKFLOW = """\
name: ci
on:
  push:
    branches: [main]
jobs:
  signoff:
    runs-on: ubuntu-latest
    steps:
      - name: attest-check
        run: klt signoff --attest 10 --inventory inv.json --check evidence/item10.json
  lint:
    runs-on: ubuntu-latest
    steps:
      - run: make lint
"""


def _repo(tmp_path) -> Path:
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    (repo / "evidence").mkdir()
    (repo / ".github" / "workflows").mkdir(parents=True)
    (repo / ".github" / "workflows" / "ci.yml").write_text(_WORKFLOW)
    (repo / "README.md").write_text("# readme\n")
    return repo


def _inv(repo: Path, hygiene=None, item: int = 10, **fields) -> str:
    inventory: dict = {
        "schema_version": 1,
        "kind": "t1-attestation-inventory",
        "t1_item": item,
        **fields,
    }
    if item == 10:
        inventory["hygiene"] = hygiene
        inventory.setdefault("checks", [{"name": "ok", "argv": _OK}])
    path = repo / "inv.json"
    path.write_text(json.dumps(inventory))
    return str(path)


# --- sequences and unsupported node types at digest level --------------------- #


def test_key_order_inside_a_list_element_is_not_load_bearing():
    """Two spellings of the same document that differ only in the key
    order of a mapping *inside a list element* share a digest, whole-file
    and ``select``-scoped alike."""
    ordered = b"jobs:\n  build:\n    steps:\n      - name: x\n        run: make\n"
    swapped = b"jobs:\n  build:\n    steps:\n      - run: make\n        name: x\n"
    assert yaml.safe_load(ordered) == yaml.safe_load(swapped)
    for select in ("jobs.build", None):
        assert content_digest(ordered, select, "<where>") == content_digest(
            swapped, select, "<where>"
        )


def test_a_list_and_its_repr_shaped_string_hash_differently():
    """A string that spells a list's would-be ``repr`` fallback is a
    *string*: structural sequence canonicalisation keeps the two documents
    distinct (no digest collision)."""
    as_list = b"branches: [main]\n"
    as_string = b"branches: \"list:['main']\"\n"
    assert content_digest(as_list, None, "<where>") != content_digest(
        as_string, None, "<where>"
    )


def test_date_inside_a_list_canonicalises_to_its_iso_string():
    """A ``date`` inside a list gets its ISO string exactly like a mapping
    value would -- the recursion covers sequence elements."""
    document = yaml.safe_load("schedule:\n  - 2026-11-15\n  - plain\n")
    assert canonical_dumps(document) == '{"str:\'schedule\'":["2026-11-15","plain"]}'


def test_binary_node_digest_ignores_base64_spelling():
    """``!!binary`` canonicalises to the decoded bytes' hex, so two
    spellings of the same payload (line-wrapped base64) share a digest."""
    encoded = base64.b64encode(b"\x00\x01klt").decode("ascii")
    one_line = f"blob: !!binary {encoded}\n"
    wrapped = f"blob: !!binary {encoded[:3]}\n    {encoded[3:]}\n"
    assert yaml.safe_load(one_line) == yaml.safe_load(wrapped)
    assert content_digest(one_line.encode(), None, "<where>") == content_digest(
        wrapped.encode(), None, "<where>"
    )


def test_set_node_is_refused_at_digest_level():
    """The refusal names the file: a digest that cannot reproduce is an
    error, not an attestation."""
    with pytest.raises(
        CanonicalYamlError, match=r"ci\.yml.*unsupported node type 'set'"
    ):
        content_digest(b"matrix: !!set\n  ? a\n  ? b\n", None, "ci.yml")


def test_yaml_refused_on_items_1_and_9(tmp_path):
    """AC: ``yaml`` on a non-``hygiene`` path list (items 1/9) is refused
    -- elsewhere the file's bytes are the attested property."""
    repo = _repo(tmp_path)
    entry = {"path": "tb.spice", "attest": "yaml"}
    with pytest.raises(AttestationError, match="item 10 'hygiene'"):
        build_attestation(1, _inv(repo, item=1, sources=[entry], derived=[]))
    with pytest.raises(AttestationError, match="attest"):
        build_attestation(
            9, _inv(repo, item=9, testbenches=[entry], cold_start={"argv": _OK})
        )


# --- canonicalization edge cases -------------------------------------------- #


def test_on_boolean_key_is_hashed_without_raising():
    """AC: a workflow whose top-level keys include ``on:`` canonicalizes;
    the string ``"on"`` and the YAML 1.1 boolean ``on``/``true`` stay
    distinct type tags (``on:`` and ``true:`` parse to the *same* boolean
    key -- a parse-level YAML 1.1 identity, not a canonicalizer choice)."""
    unquoted = content_digest(b"on: [push]\njobs: {a: 1}\n", None, "<w>")
    quoted = content_digest(b'"on": [push]\njobs: {a: 1}\n', None, "<w>")
    boolean_true = content_digest(b"true: [push]\njobs: {a: 1}\n", None, "<w>")
    assert unquoted != quoted
    assert unquoted == boolean_true


def test_date_and_numeric_spellings_canonicalize():
    # a date canonicalises to its ISO string, so the date and its quoted
    # string spelling hash alike (non-JSON scalars become ISO strings)
    assert content_digest(b"d: 2026-11-15\n", None, "<w>") == content_digest(
        b'd: "2026-11-15"\n', None, "<w>"
    )
    assert content_digest(b"d: 2026-11-15\n", None, "<w>") != content_digest(
        b'd: "2026-11-16"\n', None, "<w>"
    )
    assert content_digest(b"d: 2026-11-15T00:00:00\n", None, "<w>") == content_digest(
        b"d: 2026-11-15t00:00:00\n", None, "<w>"
    )
    assert content_digest(b"x: 1.0\n", None, "<w>") == content_digest(
        b"x: 1.00\n", None, "<w>"
    )
    assert content_digest(b"x: 1.0\n", None, "<w>") != content_digest(
        b"x: 1\n", None, "<w>"
    )


def test_empty_file_hashes_as_null_and_select_refuses():
    assert content_digest(b"", None, "<w>").startswith("sha256:")
    with pytest.raises(ValueError, match="does not resolve"):
        content_digest(b"", "jobs.signoff", "<w>")


def test_duplicate_key_edits_can_be_invisible_to_the_hash():
    """PyYAML keeps the last duplicate key, so an edit to a shadowed
    duplicate cannot change the parse -- documented, not worked around."""
    assert content_digest(b"k: 1\nk: 2\n", None, "<w>") == content_digest(
        b"k: 9\nk: 2\n", None, "<w>"
    )


def test_int_and_bool_keys_are_type_tagged():
    assert content_digest(b"1: a\n", None, "<w>") != content_digest(
        b'"1": a\n', None, "<w>"
    )
