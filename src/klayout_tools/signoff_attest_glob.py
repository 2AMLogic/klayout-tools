"""Inventory path-list member expansion for ``klt signoff --attest``
(issue #5614).

Split out of :mod:`klayout_tools.signoff_attest` (file-size ratchet), whose
:func:`~klayout_tools.signoff_attest._path_list` hands every raw member to
:func:`expand_entry`. One path-list entry (``sources`` for item 1,
``hygiene`` for item 10) is either a literal repo-relative path string -- the
only form before #5614 -- or, from inventory ``schema_version`` 2 on, a
``{"glob": "<pattern>"}`` object expanded at attest time to the
**git-tracked** files matching it.

Git-tracked-only is load-bearing, not a preference: a filesystem glob would
sweep up a developer's untracked local files, so a local ``--attest`` and
CI's ``--attest --check`` would expand to different member sets and the
envelope would stop being byte-deterministic. ``git ls-files`` failing is
therefore an error naming the glob entry -- never a fallback to the
filesystem.

The schema gate lives here too: a glob entry in a ``schema_version: 1``
inventory is refused with a message naming the required version, so an older
``klt`` never silently reinterprets an entry (a bare string must keep meaning
"literal path" forever, which is why the glob form is a mapping, not a
string).

Like :mod:`signoff_attest_item9`, this module does not import
:mod:`signoff_attest` at module level (which imports us): the error class and
the path validator are imported lazily.
"""

from __future__ import annotations

import fnmatch
import os
import subprocess
from collections.abc import Mapping
from typing import Any

from ._jsonnum import is_int

#: The inventory ``schema_version`` that introduced ``{"glob": ...}``
#: members. A schema-1 inventory is refused, not silently reinterpreted;
#: later versions accept the key too.
GLOB_SCHEMA_VERSION = 2

#: The only key a glob mapping entry may carry.
_GLOB_KEYS = frozenset({"glob"})

#: Upper bound (seconds) on the ``git ls-files`` call, matching
#: ``build_identity``'s probe budget.
_LS_FILES_TIMEOUT_S = 30


def _error(message: str) -> Exception:
    """An ``AttestationError`` (imported lazily: ``signoff_attest`` imports us)."""
    from klayout_tools.signoff_attest import AttestationError

    return AttestationError(message)


def _normalize(raw: Any, field: str, root: str) -> str:
    from klayout_tools.signoff_attest import _normalize_repo_path

    return _normalize_repo_path(raw, field, root)


def _claim(path: str, field: str, seen: dict[str, str]) -> str:
    """One member's acceptance: the duplicate rule every path obeys.

    Shared by literal and expanded members alike, so a glob naming an
    already-listed file raises the ordinary duplicate error instead of
    silently double-binding it."""
    if path in seen:
        raise _error(
            f"path '{path}' is listed more than once (in '{seen[path]}' and "
            f"'{field}') -- each inventoried file must appear exactly once"
        )
    seen[path] = field
    return path


def expand_entry(
    raw: Any,
    field: str,
    root: str,
    seen: dict[str, str],
    schema_version: Any,
) -> list[str]:
    """The normalised repo-relative paths one raw ``field`` entry stands for.

    A string entry is the literal path it always was. A mapping entry is a
    ``{"glob": ...}`` object (schema version 2 and up): the pattern is
    validated, expanded from the git index, and every member claimed through
    the same ``seen`` bookkeeping a literal path goes through. Anything else
    falls through to the literal validator, which refuses it with its usual
    message.
    """
    if isinstance(raw, Mapping):
        return _expand_glob(raw, field, root, seen, schema_version)
    return [_claim(_normalize(raw, field, root), field, seen)]


def _expand_glob(
    raw: Mapping[str, Any],
    field: str,
    root: str,
    seen: dict[str, str],
    schema_version: Any,
) -> list[str]:
    """The git-tracked files a ``{"glob": ...}`` entry binds.

    Zero matches are refused (a glob that attests nothing reintroduces the
    silent-coverage gap in a new shape), and each member walks the same
    normalisation a literal entry does -- so a symlink under a globbed
    directory is refused exactly as a literal symlink path would be.
    """
    if not is_int(schema_version) or schema_version < GLOB_SCHEMA_VERSION:
        raise _error(
            f"'{field}' glob entries need inventory schema_version "
            f"{GLOB_SCHEMA_VERSION} (this inventory declares "
            f'{schema_version!r}) -- set "schema_version": '
            f"{GLOB_SCHEMA_VERSION} or list the paths literally"
        )
    unknown = sorted(key for key in raw if key not in _GLOB_KEYS)
    if unknown or "glob" not in raw:
        raise _error(
            f"'{field}' entries accept a literal path string or an object "
            f"with exactly a 'glob' key (got {unknown or 'no glob key'})"
        )
    pattern = _validated_pattern(raw["glob"], field)
    matches = sorted(
        path
        for path in _tracked_files(root, field, pattern)
        if fnmatch.fnmatchcase(path, pattern)
    )
    if not matches:
        raise _error(
            f"'{field}' glob '{pattern}' matched no git-tracked file -- a "
            "glob that attests nothing is refused, like an empty path list"
        )
    return [_claim(_normalize(path, field, root), field, seen) for path in matches]


def _validated_pattern(raw: Any, field: str) -> str:
    """A glob entry's pattern, in canonical ``/``-separated repo-relative
    form.

    Refused: a non-string or empty value, an absolute path, and any ``..``
    escape -- the same repository-relative contract a literal entry has.
    Empty ``.`` and ``/`` segments are dropped, so ``records//*.md`` and
    ``./records/*.md`` mean ``records/*.md``."""
    if not isinstance(raw, str) or not raw.strip():
        raise _error(f"'{field}' glob patterns must be non-empty strings")
    if os.path.isabs(raw) or raw.startswith(("/", "\\")):
        raise _error(
            f"'{field}' glob pattern '{raw}' is absolute -- inventory paths "
            "are repository-relative"
        )
    parts = [part for part in raw.split("/") if part not in ("", ".")]
    if any(part == ".." for part in parts):
        raise _error(
            f"'{field}' glob pattern '{raw}' is outside the repository "
            "-- a glob may not escape it with '..'"
        )
    return "/".join(parts)


def _tracked_files(root: str, field: str, pattern: str) -> list[str]:
    """Every path the repository's git index tracks, repo-relative.

    ``git ls-files`` is the expansion source, never a filesystem walk: only
    tracked files can be members, or a local ``--attest`` and CI's
    re-attest would bind different sets and the envelope would stop being
    byte-deterministic. git being unusable is an error naming the entry --
    there is no fallback."""
    try:
        completed = subprocess.run(
            ["git", "-C", root, "ls-files", "-z"],
            capture_output=True,
            text=True,
            timeout=_LS_FILES_TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError):
        completed = None
    if completed is None or completed.returncode != 0:
        detail = "" if completed is None else f": {completed.stderr.strip()}"
        raise _error(
            f"'{field}' glob '{pattern}' could not list git-tracked files in "
            f"'{root}'{detail} -- a glob expands from the git index "
            "(git-tracked files only), never from the filesystem"
        )
    return [path for path in completed.stdout.split("\0") if path]
