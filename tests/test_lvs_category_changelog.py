"""Every ``CATEGORY_*`` value in ``lvs.py`` is recorded in ``CHANGELOG.md``.

``CHANGELOG.md``'s own header and ``docs/json-contract.md``'s pre-1.0 caveat
designate it the source of truth for which categories exist as of a given
date. This guard makes that promise mechanical: a new ``CATEGORY_*`` constant
in ``src/klayout_tools/lvs.py`` cannot ship without a ``CHANGELOG.md`` mention.

Scope is that one file, per issue #5864's acceptance criteria -- it is **not**
yet the full "every ``mismatches[].category`` value ``klt lvs`` can emit"
invariant. Four such values are defined in sibling modules
(``lvs_runset.py``, ``lvs_mos_options.py``, ``lvs_gate_level_convert.py``) and
are not checked here; issue #5927 tracks widening this guard to cover them.

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
_LVS_SOURCE = _REPO_ROOT / "src" / "klayout_tools" / "lvs.py"
_CHANGELOG = _REPO_ROOT / "CHANGELOG.md"

_CATEGORY_DEF_RE = re.compile(r'^(CATEGORY_[A-Z_]+)\s*=\s*"([^"]+)"', re.MULTILINE)


def test_every_category_value_appears_in_changelog():
    source = _LVS_SOURCE.read_text(encoding="utf-8")
    defs = _CATEGORY_DEF_RE.findall(source)
    assert defs, (
        f"no CATEGORY_* definitions found in {_LVS_SOURCE} — the file moved or "
        "the assignment format changed; update this guard rather than letting "
        "it pass vacuously"
    )
    changelog = _CHANGELOG.read_text(encoding="utf-8")
    missing = sorted({value for _, value in defs if value not in changelog})
    assert not missing, (
        "CATEGORY_* values emitted by klt lvs are missing from CHANGELOG.md — "
        "docs/json-contract.md's pre-1.0 caveat promises callers CHANGELOG.md "
        "is the source of truth for which mismatches[].category values exist "
        "as of a given date, so record each in its release entry: " + ", ".join(missing)
    )
