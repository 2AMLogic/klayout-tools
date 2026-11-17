"""The provenance engine-mismatch warning's install remedy and emission
(issue #1490, remedy per issue #5601), split out of `_provenance.py` for
its size-ratchet ceiling -- the `signoff_yield_disclosure.py` precedent
(issue #5815): one pure function over its arguments, importing nothing
from the module it was split out of, re-imported there by name so
`_provenance._warn_klayout_version_mismatch` keeps resolving (`_devices`
and `sim` docstrings cite it there)."""

from __future__ import annotations

import sys


def _warn_klayout_version_mismatch(
    actual: str | None,
    expected: str | None,
    reinstall_spec: str | None,
) -> None:
    """The stderr warning printed once per report when
    :func:`klayout_tools._provenance._klayout_version_mismatch` is ``True``
    (issue #1490) -- a caller piping ``--format json`` to a file still sees
    this on the terminal, since JSON output goes to stdout only
    (``docs/json-contract.md``).

    ``reinstall_spec`` is the running build's
    :func:`klayout_tools.build_identity.reinstall_spec` value -- the same
    string already computed for ``provenance.klt_reinstall_spec``, passed
    in by :func:`klayout_tools._provenance.build_provenance` rather than
    re-derived here so the warning adds no probe of its own. A released
    install renders the version-pin install form; a clean dev build
    renders a real ``git+https://...@<commit>`` (issue #5601: the
    placeholder this warning previously printed was copy-pasteable by no
    one -- it showed a literal ``...`` where the sha should be). ``None``
    (dirty tree, or no recorded commit) never renders as a fabricated
    command: the line says the build is not reinstallable and points at
    ``klt version --format json``, which carries the same
    ``reinstall_spec`` field."""
    if reinstall_spec is None:
        remedy = (
            "this klayout-tools build is not reinstallable from a single "
            "spec (dirty working tree, or no recorded commit) -- see "
            "`klt version --format json`'s `reinstall_spec` field; the "
            f"engine constraint itself is `--with klayout=={expected}`"
        )
    else:
        remedy = f'`uv tool install "{reinstall_spec}" --with klayout=={expected}`'
    message = (
        f"klt: warning: resolved klayout engine version {actual!r} differs "
        f"from klayout=={expected}, the version this klayout-tools build/"
        "commit was tested against -- DRC/LVS report counts (deck content "
        "hash, rules_skipped, category_counts, ...) may differ from a "
        f"report generated with klayout=={expected}, even though the "
        "verdict itself is unaffected. To reproduce the exact engine: "
        f"{remedy} -- see docs/json-contract.md's "
        '"Shared `provenance` block".'
    )
    print(message, file=sys.stderr)
