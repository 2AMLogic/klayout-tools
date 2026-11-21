"""Shared request validation for primitive drawing and base-layout edits."""

from __future__ import annotations

from typing import Any

#: Prefix reserved for caller annotations. JSON has no comment syntax, so a
#: request file checked into a repository (the motivating case: a known-bad DRC
#: fixture whose dimensions must stay "wrong") has nowhere to record *why* it is
#: shaped the way it is. Any key beginning with this prefix -- ``_purpose``,
#: ``_expected_rules``, a per-shape ``_rule`` note -- is accepted and ignored
#: everywhere in the request, at every nesting level, and is guaranteed never to
#: be given meaning by a future version. Every *other* unrecognised key is an
#: application error (issue #950), so a typo in a real key (``rect_nm`` for
#: ``rect_um``) is caught instead of silently dropped. Same posture as
#: ``klt gen-compose``'s ``request.pdk`` (``gen_compose._ALLOWED_PDK_KEYS``).
ANNOTATION_KEY_PREFIX = "_"


class DrawError(Exception):
    """A ``klt draw`` request could not be satisfied.

    Raised for a malformed request (bad shape/label description, empty
    ``shapes``, an unwritable output path, ...). The CLI layer turns it into
    the documented error envelope; it is importable so callers embedding the
    library can ``except DrawError``.
    """


def _is_annotation_key(key: Any) -> bool:
    """True for a reserved caller-annotation key (see :data:`ANNOTATION_KEY_PREFIX`)."""
    return isinstance(key, str) and key.startswith(ANNOTATION_KEY_PREFIX)


def _reject_unknown_keys(
    mapping: dict[Any, Any], allowed: frozenset[str], context: str
) -> None:
    """Raise :class:`DrawError` if ``mapping`` carries an unrecognised key.

    Keys beginning with :data:`ANNOTATION_KEY_PREFIX` are the documented escape
    hatch and are always accepted. Everything else must be in ``allowed``.
    """
    unknown = [k for k in mapping if k not in allowed and not _is_annotation_key(k)]
    if not unknown:
        return
    raise DrawError(
        f"{context} has unknown key(s): {', '.join(sorted(str(k) for k in unknown))}"
        f" -- allowed: {', '.join(sorted(allowed))}"
        f" (keys beginning with '{ANNOTATION_KEY_PREFIX}' are reserved for "
        "caller annotations and ignored)"
    )
