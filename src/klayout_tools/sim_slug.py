"""Filesystem-safe corner/signal slug helpers for ``klt sim`` artifacts.

Split out of ``sim.py`` (issue #4746) to keep that module under its per-file
size ceiling. Pure move: every name is re-exported from ``sim`` unchanged.
"""

from __future__ import annotations

import hashlib
import re


def _slugify_label(label: str) -> str:
    label = label.replace("/", "_")
    return label.replace(".", "p").replace("=", "").replace("-", "n").replace("@", "v_")


def _readable_slug(corner_id: str) -> str:
    return _slugify_label(corner_id)


#: ``<pvt>[/params:<pairs>][/options:<pairs>][/mc<n>]`` with at least one of
#: the two axis segments -- the shape of a parameter- or options-axis ID.
_PARAMS_ID_RE = re.compile(
    r"(?P<pvt>.*?)(?:/params:[^/]*/options:[^/]*|/params:[^/]*|/options:[^/]*)"
    r"(?:/mc\d+)?"
)

#: A readable slug longer than this many UTF-8 bytes is replaced by a bounded
#: form (readable prefix + hash of the full corner id) so the artifact
#: directory name stays well under the filesystem's ``NAME_MAX`` (255 bytes)
#: even with the ``__probe`` suffix or a ``__<signal>.svg`` plot filename.
_SLUG_MAX_BYTES = 100
_SLUG_PREFIX_BYTES = 80
_SLUG_HASH_CHARS = 12


def _bound_slug(readable: str) -> str:
    """Cut ``readable`` to ``_SLUG_PREFIX_BYTES`` UTF-8 bytes when it exceeds
    ``_SLUG_MAX_BYTES``; shorter slugs are returned unchanged."""
    encoded = readable.encode("utf-8")
    if len(encoded) <= _SLUG_MAX_BYTES:
        return readable
    return encoded[:_SLUG_PREFIX_BYTES].decode("utf-8", errors="ignore")


def _slugify_corner_id(corner_id: str) -> str:
    """Filesystem-safe, bounded transform of a corner's own ``corner_id``
    (e.g. ``ss/1.620V/125C`` -> ``ss_1p620V_125C``). Shared by
    :attr:`CornerPoint.slug` and the plot writer (which only has the report
    dict's ``corner_id``) so the two cannot drift.

    A parameter point (issue #3035) keeps its PVT slug (bounded like the
    readable form below) and appends the full
    SHA-256 of the whole ID (MC suffix included) -- parameter values never
    reach the filesystem, so close values cannot collide and no value can
    inject a path character.

    Otherwise short ids keep their readable form. When that exceeds
    ``_SLUG_MAX_BYTES`` bytes (many swept ``supply_v`` keys), it becomes a
    readable prefix plus a deterministic SHA-256 digest of the full
    ``corner_id`` (so ``mc<N>`` samples stay distinct); the full id is
    recoverable from the directory's ``corner.json``."""
    match = _PARAMS_ID_RE.fullmatch(corner_id)
    digest = hashlib.sha256(corner_id.encode("utf-8")).hexdigest()
    if match is not None:
        return f"{_bound_slug(_slugify_label(match.group('pvt')))}_params_{digest}"
    readable = _readable_slug(corner_id)
    prefix = _bound_slug(readable)
    if prefix == readable:
        return readable
    return f"{prefix}_{digest[:_SLUG_HASH_CHARS]}"


#: Any run of characters that is not alphanumeric/underscore, collapsed to a
#: single underscore -- turns a waveform variable name like ``v(out)`` into
#: a filesystem-safe ``v_out_`` for a deterministic plot filename.
_NON_FILENAME_CHARS_RE = re.compile(r"[^0-9A-Za-z_]+")


def _slugify_signal(signal_name: str) -> str:
    return _NON_FILENAME_CHARS_RE.sub("_", signal_name.lower()).strip("_") or "signal"
