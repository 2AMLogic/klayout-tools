"""Shared measurement-form registry and checks for ``request.measurements[]``.

Every exclusive form module (``transfer``, ``tian``, ``snm``, ``search``,
``eye``, ``step_resolution``, ``settled``) declares its own keyword and body
validator; the "declare exactly one form" rule is identical, so it lives here
-- and so does the list of forms it refuses, derived from the one registry
below (:data:`FORMS`) rather than hand-copied per module (issue #5695).
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from typing import Any

from .sim_errors import SimError

#: The measurement forms that are *not* mutually exclusive with one another --
#: each is still exclusive with every form in :data:`EXCLUSIVE_FORMS`, but
#: their own pairwise rules live in :mod:`klayout_tools.sim_validate`.
NON_EXCLUSIVE_FORMS = (
    "spice",
    "expr",
    "crossings",
    "cycle_mean",
    "energy",
    "stress",
    "noise",
)

#: The mutually exclusive forms, in the order they shipped -- which is also
#: the order :func:`other_forms` reports a conflict in, so it is observable in
#: the refusal message for an entry declaring three or more forms. A new
#: exclusive form module belongs at the end of this tuple; until it is listed,
#: :func:`other_forms` refuses its name, so the omission cannot ship quietly.
EXCLUSIVE_FORMS = (
    "transfer",
    "tian",
    "snm",
    "search",
    "eye",
    "step_resolution",
    "settled",
)

#: Every measurement form, in declaration order.
FORMS = (*NON_EXCLUSIVE_FORMS, *EXCLUSIVE_FORMS)

#: ``"v(node)"`` -- the only selector shape ``transfer`` and ``snm`` accept,
#: and the voltage half of ``tian``'s (which also accepts ``i(source)``).
_SELECTOR_RE = re.compile(r"^[vV]\(([^\s\x00-\x1f\x7f,()]+)\)$")


def other_forms(form: str) -> tuple[str, ...]:
    """Every measurement form except ``form``, in :data:`FORMS` order.

    Refuses a ``form`` that is not registered: an exclusive form module that
    forgets to list itself in :data:`EXCLUSIVE_FORMS` would otherwise be
    omitted from every *other* module's refusal list and silently accepted
    beside one of them."""
    if form not in FORMS:
        raise ValueError(
            f"unknown measurement form {form!r}: add it to "
            "sim_forms.EXCLUSIVE_FORMS (or NON_EXCLUSIVE_FORMS)"
        )
    return tuple(other for other in FORMS if other != form)


def normalize_selector(selector: Any) -> str | None:
    """``"v(node)"`` lower-cased, or ``None`` when not an accepted selector."""
    if not isinstance(selector, str):
        return None
    match = _SELECTOR_RE.match(selector)
    return None if match is None else f"v({match.group(1).lower()})"


def validate_exclusive_form(
    spec: dict[str, Any],
    form: str,
    other_forms: Iterable[str],
    validate_body: Callable[[Any, dict[str, Any]], None],
) -> bool:
    """For an entry declaring ``form``: refuse any of ``other_forms`` beside it
    and call ``validate_body(name, spec)``. Returns ``True`` when the entry
    declares ``form`` (so the caller has nothing more to validate)."""
    if spec.get(form) is None:
        return False
    name = spec.get("name")
    if name is None:
        raise SimError("each request.measurements[] entry requires 'name'")
    others = [other for other in other_forms if spec.get(other) is not None]
    if others:
        raise SimError(
            f"measurement '{name}' declares '{form}' together with "
            f"'{others[0]}': they are alternative forms of a measurement -- "
            "declare exactly one (use a separate measurements[] entry for "
            "the other)"
        )
    validate_body(name, spec)
    return True
