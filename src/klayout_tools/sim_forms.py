"""Shared exclusive-measurement-form check for ``request.measurements[]``.

Each exclusive form module (``eye``, ``search``, ``snm``, ``tian``,
``transfer``, ``step_resolution``) declares its own keyword and body
validator; the "declare exactly one form" rule is identical, so it lives here.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any

from .sim_errors import SimError


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
