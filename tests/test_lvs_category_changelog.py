"""Every ``CATEGORY_*`` value in ``src/klayout_tools/lvs*.py`` is recorded
in ``CHANGELOG.md`` or a pending ``changelog.d/`` fragment.

``CHANGELOG.md``'s own header and ``docs/json-contract.md``'s pre-1.0 caveat
designate it the source of truth for which categories exist as of a given
date. This guard makes that promise mechanical: a new ``CATEGORY_*`` constant
in any ``src/klayout_tools/lvs*.py`` module cannot ship without a recorded
entry.

The record may be a ``CHANGELOG.md`` entry **or** a ``changelog.d/*.md``
fragment. ``docs/json-contract.md`` tells contributors to record new
JSON-visible changes in ``changelog.d/`` "for inclusion in ``CHANGELOG.md`` at
release time", and ``scripts/release_prep.py`` folds every fragment into the
release section of ``CHANGELOG.md`` before a release is cut. So a category
recorded in a fragment is recorded under that contract, and cannot reach a
release without also landing in ``CHANGELOG.md``; only the file list a
CHANGELOG-only guard reads is too narrow (issue #6681). ``changelog.d/README.md``
is excluded, as in ``release_prep.py``: it is prose, never folded in.

The glob (issue #5927, widening issue #5864's single-file scope) covers the
full "every ``mismatches[].category`` value ``klt lvs`` can emit" invariant
for the ``lvs*`` modules: ``lvs.py`` plus the sibling modules categories
migrated into for file-size reasons (``lvs_runset.py``,
``lvs_mos_options.py``, ``lvs_gate_level_convert.py``) and any module added
later, so a category in a new ``lvs_*.py`` is picked up without editing this
guard. Engine-specific categories are **not** exempt --
``runset.incomplete`` and ``coverage.no_devices_recognized`` are published
``mismatches[].category`` values of ``klt lvs`` like any other.

Coverage is limited to module-level ``CATEGORY_*`` constants (one
``CATEGORY_X = "value"`` assignment per line, at column 0). A category literal
passed straight to ``_mismatch(...)`` is not seen by this guard, so a new
category must be declared as a ``CATEGORY_*`` constant to be checked (issue
#6162 promoted ``net.supply_fragmented`` and ``topology.known_limitation`` for
this reason).

Matching is plain substring containment, so a category string that appears in
``CHANGELOG.md`` or a fragment only inside a fenced code block (e.g. an example
report) would also satisfy this check. That residual false-positive risk is
accepted in exchange for keeping the guard trivial; the check is for *absence*
of a backfilled entry, and a real entry always contains the bare category
string outside any code block.
"""

import re
from collections.abc import Iterable
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
_LVS_SOURCES = sorted((_REPO_ROOT / "src" / "klayout_tools").glob("lvs*.py"))
_CORE_SOURCE = _REPO_ROOT / "src" / "klayout_tools" / "lvs.py"
_CHANGELOG = _REPO_ROOT / "CHANGELOG.md"
_FRAGMENT_DIR = _REPO_ROOT / "changelog.d"

_CATEGORY_DEF_RE = re.compile(r'^(CATEGORY_[A-Z_]+)\s*=\s*"([^"]+)"', re.MULTILINE)


def _recorded_text(changelog: Path, fragment_dir: Path) -> str:
    """``CHANGELOG.md`` plus every pending ``changelog.d/*.md`` fragment.

    Mirrors ``scripts/release_prep.py``'s fragment selection: ``*.md`` files
    only, and never ``README.md``.
    """
    parts = [changelog.read_text(encoding="utf-8")]
    for path in sorted(fragment_dir.glob("*.md")):
        if path.name != "README.md" and path.is_file():
            parts.append(path.read_text(encoding="utf-8"))
    return "\n".join(parts)


def _assert_recorded(
    values: Iterable[str], changelog: Path, fragment_dir: Path
) -> None:
    """Fail unless every value is in ``changelog`` or a ``fragment_dir`` fragment."""
    recorded = _recorded_text(changelog, fragment_dir)
    missing = sorted({value for value in values if value not in recorded})
    assert not missing, (
        "CATEGORY_* values emitted by klt lvs are missing from "
        "CHANGELOG.md or a changelog.d/ fragment — docs/json-contract.md's "
        "pre-1.0 caveat promises callers CHANGELOG.md (which release_prep.py "
        "folds the fragments into) is the source of truth for which "
        "mismatches[].category values exist as of a given date, so record "
        "each in its release entry or a changelog.d/<issue>-<slug>.md "
        "fragment: " + ", ".join(missing)
    )


def test_every_category_value_appears_in_changelog():
    assert _LVS_SOURCES, (
        "no src/klayout_tools/lvs*.py modules found — the package layout "
        "changed; update this guard rather than letting it pass vacuously"
    )
    assert _CORE_SOURCE in _LVS_SOURCES, (
        f"{_CORE_SOURCE} is missing from the lvs*.py glob — the categories' "
        "home module moved or was renamed; update this guard rather than "
        "letting it pass vacuously"
    )
    defs = [
        (source, value)
        for source in _LVS_SOURCES
        for _, value in _CATEGORY_DEF_RE.findall(source.read_text(encoding="utf-8"))
    ]
    assert defs, (
        "no CATEGORY_* definitions found in any src/klayout_tools/lvs*.py — "
        "the assignment format changed; update this guard rather than "
        "letting it pass vacuously"
    )
    assert _FRAGMENT_DIR.is_dir(), (
        f"{_FRAGMENT_DIR} is missing — the changelog fragment directory moved "
        "or was renamed; update this guard rather than letting it silently "
        "shrink the accepted text to CHANGELOG.md alone"
    )
    _assert_recorded((value for _, value in defs), _CHANGELOG, _FRAGMENT_DIR)


_VALUE = "hints.example_new_category"


def _tree(
    tmp_path: Path, *, changelog: str = "", fragments: dict[str, str] | None = None
):
    changelog_path = tmp_path / "CHANGELOG.md"
    changelog_path.write_text(changelog, encoding="utf-8")
    fragment_dir = tmp_path / "changelog.d"
    fragment_dir.mkdir()
    for name, text in (fragments or {}).items():
        (fragment_dir / name).write_text(text, encoding="utf-8")
    return changelog_path, fragment_dir


def test_value_recorded_only_in_changelog_is_accepted(tmp_path):
    changelog, fragments = _tree(tmp_path, changelog=f"- **New** ``{_VALUE}`` (#1).\n")
    _assert_recorded([_VALUE], changelog, fragments)


def test_value_recorded_only_in_fragment_is_accepted(tmp_path):
    changelog, fragments = _tree(
        tmp_path, fragments={"1-new.md": f"- **New** ``{_VALUE}`` (#1, 2026-11-18).\n"}
    )
    _assert_recorded([_VALUE], changelog, fragments)


def test_value_in_neither_fails_naming_both_places(tmp_path):
    changelog, fragments = _tree(
        tmp_path, changelog="- unrelated\n", fragments={"1-new.md": "- unrelated too\n"}
    )
    with pytest.raises(AssertionError) as excinfo:
        _assert_recorded([_VALUE, "hints.other"], changelog, fragments)
    message = str(excinfo.value)
    assert "CHANGELOG.md or a changelog.d/ fragment" in message
    assert "hints.example_new_category, hints.other" in message


def test_fragment_readme_mention_is_not_a_record(tmp_path):
    # release_prep.py skips changelog.d/README.md, so prose there is never
    # folded into CHANGELOG.md and must not satisfy the guard.
    changelog, fragments = _tree(
        tmp_path, fragments={"README.md": f"Example: ``{_VALUE}``\n"}
    )
    with pytest.raises(AssertionError, match=_VALUE):
        _assert_recorded([_VALUE], changelog, fragments)
