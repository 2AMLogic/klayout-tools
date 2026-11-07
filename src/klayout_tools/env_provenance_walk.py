"""Field walker for the absolute-path lint in :mod:`klayout_tools.env_provenance`.

Split out so ``env_provenance.py`` stays under its per-file size ceiling.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any

# Fields under ``environment.remote.worker_environment`` (or, for a sharded run,
# ``environment.remote.fleet.<i>.worker_environment``) are observations of the
# *remote worker host* (its PDK root, simulator path, banner), not paths of the
# author's checkout, so a committer cannot make them non-reproducible (#5133).
# Anchored at the envelope root so a ``worker_environment`` key elsewhere is
# still linted.
_REMOTE_WORKER_ENV_FIELD_RE = re.compile(
    r"^environment\.remote\.(?:fleet\.\d+\.)?worker_environment(?:\.|$)"
)


def walk_string_fields(value: Any, path: str) -> Iterable[tuple[str, str]]:
    """Every string in a JSON-decoded ``value`` as ``(dotted_field, text)``.

    Mapping *keys* are yielded too (a dict keyed by an absolute path leaks
    just as thoroughly as one valued by it), and list elements are addressed
    by index -- the same dotted-path convention ``_report_verify
    .diff_verdict_fields`` reports drift under, so a field named by one tool
    is findable with the other.
    """
    if isinstance(value, Mapping):
        for key, item in value.items():
            child = f"{path}.{key}" if path else str(key)
            if isinstance(key, str):
                yield child, key
            yield from walk_string_fields(item, child)
        return
    if isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            child = f"{path}.{index}" if path else str(index)
            yield from walk_string_fields(item, child)
        return
    if isinstance(value, str):
        yield (path or "<root>"), value


# Fields under ``environment.remote.worker_environment`` (or, for a sharded run,
# ``environment.remote.fleet.<i>.worker_environment``) are observations of the
# *remote worker host* (its PDK root, simulator path, banner), not paths of the
# author's checkout, so a committer cannot make them non-reproducible (#5133).
# Anchored at the envelope root so a ``worker_environment`` key elsewhere is
# still linted.
_REMOTE_WORKER_ENV_FIELD_RE = re.compile(
    r"^environment\.remote\.(?:fleet\.\d+\.)?worker_environment(?:\.|$)"
)


def walk_lintable_string_fields(payload: Any) -> Iterable[tuple[str, str]]:
    """:func:`walk_string_fields` minus the remote-worker observations."""
    for field, text in walk_string_fields(payload, ""):
        if not _REMOTE_WORKER_ENV_FIELD_RE.match(field):
            yield field, text
