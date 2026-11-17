"""Payload redactor for :mod:`klayout_tools.env_provenance` (#5828).

The detectors in ``env_provenance`` say *where* a host path leaks; this module
rewrites a JSON payload the caller did not build (a ``klt`` response embedded
verbatim in a committed evidence record) so the named host roots are replaced
by caller-chosen tags. Split out so ``env_provenance.py`` stays under its
per-file size ceiling.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping
from typing import Any

__all__ = ["scrub_payload"]

# A root only matches on path-segment boundaries: ``/tmp/a`` must not rewrite
# ``/tmp/ab`` (next char continues the segment) nor ``/x/tmp/a`` (previous
# char continues a segment).
_BEFORE = r"(?<![\w.\-])"
_AFTER = r"(?![\w.\-])"


def _compile(subs: Iterable[tuple[str, str]]) -> list[tuple[re.Pattern[str], str]]:
    compiled: list[tuple[re.Pattern[str], str]] = []
    for root, tag in subs:
        norm = root.rstrip("/\\")
        if not norm:
            raise ValueError(
                f"scrub root must not be empty or a bare separator: {root!r}"
            )
        compiled.append((re.compile(_BEFORE + re.escape(norm) + _AFTER), tag))
    return compiled


def _literal_replacement(tag: str) -> Callable[[re.Match[str]], str]:
    """A ``re.sub`` replacement function emitting ``tag`` verbatim: a string
    replacement would treat backslashes in ``tag`` as escapes."""

    def replace(_match: re.Match[str]) -> str:
        return tag

    return replace


def _scrub_text(text: str, compiled: list[tuple[re.Pattern[str], str]]) -> str:
    for pattern, tag in compiled:
        text = pattern.sub(_literal_replacement(tag), text)
    return text


def _scrub(value: Any, compiled: list[tuple[re.Pattern[str], str]], path: str) -> Any:
    if isinstance(value, Mapping):
        out: dict[Any, Any] = {}
        for key, item in value.items():
            new_key = _scrub_text(key, compiled) if isinstance(key, str) else key
            if new_key in out:
                raise ValueError(
                    f"scrubbing key {key!r} at {path or '<root>'} collides with "
                    f"another key ({new_key!r}); refusing to drop a value silently"
                )
            child = f"{path}.{new_key}" if path else str(new_key)
            out[new_key] = _scrub(item, compiled, child)
        return out
    if isinstance(value, (list, tuple)):
        return [
            _scrub(item, compiled, f"{path}.{i}" if path else str(i))
            for i, item in enumerate(value)
        ]
    if isinstance(value, str):
        return _scrub_text(value, compiled)
    return value


def scrub_payload(obj: Any, subs: Iterable[tuple[str, str]]) -> Any:
    """Return a copy of JSON-decoded ``obj`` with host roots replaced by tags.

    ``subs`` is an ordered iterable of ``(host_root, tag)``; each is applied in
    order (so list the longest root first when roots nest) to every string
    value **and mapping key**, the same coverage as
    :func:`klayout_tools.env_provenance_walk.walk_string_fields`. Roots match
    on path boundaries and trailing separators are ignored: ``/tmp/a`` turns
    ``/tmp/a/x`` into ``<TAG>/x`` but leaves ``/tmp/ab/x`` alone.

    The input is not mutated; non-string scalars pass through; tuples become
    lists (JSON has no tuples). Two keys that collapse to the same string
    raise ``ValueError`` rather than silently dropping a value. Nothing is
    special-cased -- remote ``worker_environment`` observations are rewritten
    if (and only if) the caller names their roots.
    """
    return _scrub(obj, _compile(subs), "")
