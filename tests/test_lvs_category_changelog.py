"""Every ``CATEGORY_*`` value in ``src/klayout_tools/lvs*.py`` is recorded
in ``CHANGELOG.md``.

``CHANGELOG.md``'s own header and ``docs/json-contract.md``'s pre-1.0 caveat
designate it the source of truth for which categories exist as of a given
date. This guard makes that promise mechanical: a new ``CATEGORY_*`` constant
in any ``src/klayout_tools/lvs*.py`` module cannot ship without a
``CHANGELOG.md`` mention.

The glob (issue #5927, widening issue #5864's single-file scope) covers the
full "every ``mismatches[].category`` value ``klt lvs`` can emit" invariant
for the ``lvs*`` modules: ``lvs.py`` plus the sibling modules categories
migrated into for file-size reasons (``lvs_runset.py``,
``lvs_mos_options.py``, ``lvs_gate_level_convert.py``) and any module added
later, so a category in a new ``lvs_*.py`` is picked up without editing this
guard. Engine-specific categories are **not** exempt --
``runset.incomplete`` and ``coverage.no_devices_recognized`` are published
``mismatches[].category`` values of ``klt lvs`` like any other.

Matching is plain substring containment, so a category string that appears in
``CHANGELOG.md`` only inside a fenced code block (e.g. an example report)
would also satisfy this check. That residual false-positive risk is accepted
in exchange for keeping the guard trivial; the check is for *absence* of a
backfilled entry, and a real CHANGELOG entry always contains the bare category
string outside any code block.
"""

import re
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_LVS_SOURCES = sorted((_REPO_ROOT / "src" / "klayout_tools").glob("lvs*.py"))
_CORE_SOURCE = _REPO_ROOT / "src" / "klayout_tools" / "lvs.py"
_CHANGELOG = _REPO_ROOT / "CHANGELOG.md"

_CATEGORY_DEF_RE = re.compile(r'^(CATEGORY_[A-Z_]+)\s*=\s*"([^"]+)"', re.MULTILINE)


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
    changelog = _CHANGELOG.read_text(encoding="utf-8")
    missing = sorted({value for _, value in defs if value not in changelog})
    assert not missing, (
        "CATEGORY_* values emitted by klt lvs are missing from CHANGELOG.md — "
        "docs/json-contract.md's pre-1.0 caveat promises callers CHANGELOG.md "
        "is the source of truth for which mismatches[].category values exist "
        "as of a given date, so record each in its release entry: " + ", ".join(missing)
    )
