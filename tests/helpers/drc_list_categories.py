"""Shared fixture deck + helpers for the ``--list-rule-categories`` tests
(issue #5444), used by ``tests/test_drc_list_rule_categories.py`` and
``tests/test_drc_list_rule_categories_failures.py``.

The deck deliberately contains every construct the issue says a *hand*
derivation of a category list gets wrong, so the tests prove KLayout --
not klt -- resolves them:

* ``diff.1``        -- gated on a ``-rd`` deck var (``$feol``)
* ``M1.a``/``M2.a`` -- loop-generated names from a ``# %include``d file
* ``M1.dens``       -- declared only inside a branch taken on measured
  geometry, so an empty-layout listing must NOT see it
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import jsonschema
import klayout.db as kdb
import pytest

REPO_ROOT = Path(__file__).parent.parent.parent
SCHEMA_DIR = REPO_ROOT / "docs" / "schemas"

HAVE_KLAYOUT_BINARY = shutil.which("klayout") is not None
SKIP_NO_KLAYOUT_BINARY = pytest.mark.skipif(
    not HAVE_KLAYOUT_BINARY, reason="klayout binary is not installed on this machine"
)

MAIN_DECK = """
if $input
  source($input)
end
if $report
  report("fixture", $report)
end
if $feol == "true"
  input(3, 0).width(0.5.um).output("diff.1", "feol gated")
end
lay = input(1, 0)
if lay.area > 100.0
  lay.width(0.9.um).output("M1.dens", "geometry dependent")
end
# %include included.drc
"""

INCLUDED_DECK = """
[1, 2].each do |n|
  input(n, 0).width(0.5.um).output("M#{n}.a", "loop generated #{n}")
end
"""


def fixture_deck(tmp_path: Path) -> str:
    """Write the two-file fixture deck under ``tmp_path``; return its path."""
    (tmp_path / "included.drc").write_text(INCLUDED_DECK, encoding="utf-8")
    deck = tmp_path / "fixture.drc"
    deck.write_text(MAIN_DECK, encoding="utf-8")
    return str(deck)


def write_deck(tmp_path: Path, name: str, text: str) -> str:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return str(path)


def write_gds(path: Path, *, layers=((1, 0), (2, 0), (3, 0)), box=(0, 0, 2000, 2000)):
    """A layout wide enough to satisfy the fixture deck's 0.5um width rules
    but far too small for its 100um^2 ``area`` branch."""
    layout = kdb.Layout()
    layout.dbu = 0.001
    top = layout.create_cell("TOP")
    for layer in layers:
        top.shapes(layout.layer(*layer)).insert(kdb.Box(*box))
    layout.write(str(path))
    return str(path)


def drc_response_validator() -> jsonschema.Draft202012Validator:
    from referencing import Registry, Resource

    schema = json.loads((SCHEMA_DIR / "drc-response.schema.json").read_text())
    coverage = json.loads((SCHEMA_DIR / "coverage.schema.json").read_text())
    registry = Registry().with_resource(
        coverage["$id"], Resource.from_contents(coverage)
    )
    return jsonschema.Draft202012Validator(schema, registry=registry)
