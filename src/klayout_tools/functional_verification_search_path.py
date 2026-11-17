"""``testbench.search_path`` resolution and the ``env.PYTHONPATH``
rejection of the cocotb functional-verification flow.

Split out of ``functional_verification.py`` (the #1909 SDF-split
precedent, for headroom under the per-file size ratchet) when issue #5797
widened ``search_path`` from a single directory to an ordered list.
Names that stay in ``functional_verification.py`` are reached as
``_fv.<name>`` at call time, so monkeypatching them on
``klayout_tools.functional_verification`` still takes effect; every name
is re-exported from there.
"""

from __future__ import annotations

import os
from typing import Any


def resolve_testbench_search_dirs(
    search_path: Any, request_dir: str, module: str
) -> list[str]:
    """Validate ``testbench.search_path`` -- a string, or a non-empty array
    of non-empty strings -- resolve every entry
    absolute-or-relative-to-``request_dir`` (the same convention
    ``_resolve_sources()`` applies to each ``request.sources`` entry),
    collapse resolved duplicates to their first occurrence (order-stable),
    and require ``<module>.py`` to exist in at least one entry.

    Returns every resolved directory, absolute, in list order -- just
    ``[abspath(request_dir)]`` when ``search_path`` is omitted. ``module``
    is *found* in the first entry that contains it, but every entry belongs
    on the bench's import path (see ``functional_verification._run_test``),
    because the testbench module's own imports may reach helper modules
    living in later entries.
    """
    if search_path is not None:
        shape_error = _fv.FunctionalVerificationError(
            "request.testbench.search_path must be a non-empty string or a "
            "non-empty array of non-empty strings when given"
        )
        entries = [search_path] if isinstance(search_path, str) else search_path
        if (
            not isinstance(entries, list)
            or not entries
            or not all(isinstance(e, str) and e for e in entries)
        ):
            raise shape_error
        search_dirs = [
            os.path.abspath(e if os.path.isabs(e) else os.path.join(request_dir, e))
            for e in entries
        ]
    else:
        search_dirs = [os.path.abspath(request_dir)]
    # Collapse resolved duplicates to their first occurrence (order-stable).
    search_dirs = list(dict.fromkeys(search_dirs))

    if not any(os.path.isfile(os.path.join(d, f"{module}.py")) for d in search_dirs):
        location = (
            "request.testbench.search_path"
            if search_path is not None
            else "next to the request"
        )
        expected = ", ".join(
            f"'{os.path.join(d, f'{module}.py')}'" for d in search_dirs
        )
        raise _fv.FunctionalVerificationError(
            f"testbench module not found: expected {expected} "
            f"(request.testbench.module names a Python module resolved via {location})"
        )
    return search_dirs


def _reject_env_pythonpath(env: dict[str, str]) -> None:
    """Reject ``env.PYTHONPATH``: cocotb's ``Runner._set_env`` propagates the
    calling process's ``sys.path`` as ``PYTHONPATH`` (verified live), so a
    caller-supplied value would silently do nothing -- listing the
    directories in ``testbench.search_path`` is the supported way to put
    them on the bench's import path."""
    if "PYTHONPATH" in env:
        raise _fv.FunctionalVerificationError(
            "request.env cannot set PYTHONPATH -- cocotb overwrites it from the "
            "calling process's sys.path, so it would silently do nothing; list "
            "the directories in request.testbench.search_path instead"
        )


from . import functional_verification as _fv  # noqa: E402
