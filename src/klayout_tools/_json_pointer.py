"""The one RFC 6901 JSON Pointer walk every ``klt`` consumer shares.

``klt signoff`` (issue #2342) needed it first; ``klt evidence claims`` and
``klt evidence render`` (issue #6860) now read their pointers through the
same traversal instead of two private permissive copies that accepted
``int()``-shaped indexes -- ``/readings/-1`` silently selected the last
Python list element, letting an invalid citation pass validation, and
``/readings/01`` resolved where RFC 6901 says no value is named. One
implementation, one contract, documented per command in
``docs/cli/evidence.md`` and ``docs/cli/signoff.md``.
"""

from __future__ import annotations

from typing import Any


def resolve_json_pointer(document: Any, pointer: str) -> tuple[Any, bool]:
    """Resolve ``pointer`` -- an RFC 6901 JSON Pointer -- against ``document``,
    returning ``(value, True)`` on success or ``(None, False)`` on any
    failure.

    A resolved JSON ``null`` is a value: it comes back as ``(None, True)``,
    distinct from a pointer that names nothing at all, which is
    ``(None, False)`` -- callers must not conflate the two (issue #6860).

    Implements RFC 6901 as written, and nothing beyond it:

    - The empty pointer ``""`` is "the whole document" and resolves to
      ``document`` itself. A single slash ``"/"`` is *not* the empty pointer:
      it names the empty-string key, and resolves only against an object
      that has one.
    - A non-empty pointer must start with ``"/"``; each subsequent
      ``"/"``-separated token names one step down.
    - Each token is unescaped ``~1`` -> ``"/"`` **then** ``~0`` -> ``"~"``,
      in that order (the order matters: the reverse would turn an escaped
      ``~1`` back into a separator). A ``~`` followed by anything other than
      ``0`` or ``1`` is malformed.
    - Against a JSON object, a token is a key, matched literally.
    - Against a JSON array, a token must be ASCII digits with no leading
      zero -- ``"0"`` or ``"1"``... ``"9"``, ``"10"``... -- and must be in
      range. ``"-"`` (RFC 6901's "past the last element"), a negative index
      and a leading zero all name no existing value, so each is a failure
      here, reported the same way as any other miss: no exception, no
      out-of-range or escaping conversion error.
    - Against any other value (a string, number, boolean, or ``null``) there
      is nothing to step into: failure.

    Deliberately pure and total: no exception escapes, because documents and
    manifests are caller-supplied data and a mistyped pointer must degrade
    *that one item* -- an ``unmet`` grade in :mod:`.signoff`, a
    ``claim_unresolved`` finding in :mod:`.evidence_claims`, a contextual
    :class:`.evidence_render.EvidenceRenderError` in :mod:`.evidence_render`
    -- never abort the report.
    """
    if pointer == "":
        return document, True
    if not pointer.startswith("/"):
        return None, False

    current = document
    for raw_token in pointer.split("/")[1:]:
        token, ok = unescape_json_pointer_token(raw_token)
        if not ok:
            return None, False
        if isinstance(current, dict):
            if token not in current:
                return None, False
            current = current[token]
        elif isinstance(current, list):
            if not _is_array_index(token):
                return None, False
            index = int(token)
            if index >= len(current):
                return None, False
            current = current[index]
        else:
            return None, False
    return current, True


def _is_array_index(token: str) -> bool:
    """``True`` only for RFC 6901 array indexes: ASCII digits, no leading
    zero (``str.isdigit`` alone is not enough -- it accepts non-ASCII digits
    such as ``"٣"`` that ``int()`` then reads as a number, and characters
    like ``"¹"`` that make ``int()`` raise)."""
    return (
        token.isascii()
        and token.isdigit()
        and (len(token) == 1 or not token.startswith("0"))
    )


def unescape_json_pointer_token(token: str) -> tuple[str, bool]:
    """Unescape one RFC 6901 reference token: ``~1`` -> ``"/"``, then ``~0``
    -> ``"~"``. Returns ``(unescaped, False)`` for a malformed escape -- a
    ``~`` that is last in the token or is followed by anything other than
    ``0``/``1`` -- which RFC 6901 leaves undefined and this module treats as
    an error rather than silently passing through (a pointer nobody can read
    as written must not be read as something else)."""
    out: list[str] = []
    index = 0
    while index < len(token):
        char = token[index]
        if char != "~":
            out.append(char)
            index += 1
            continue
        if index + 1 >= len(token):
            return "", False
        escaped = token[index + 1]
        if escaped == "0":
            out.append("~")
        elif escaped == "1":
            out.append("/")
        else:
            return "", False
        index += 2
    return "".join(out), True
