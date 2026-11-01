"""``models.param_file`` (issue #4759): a SPICE parameter file (e.g. gf180mcu's
``libs.tech/ngspice/design.ngspice``) ``.include``d ahead of the ``.lib``
cards. Split out of ``sim.py`` / ``sim_staging.py`` to keep the file-size
ratchet; ``sim.py`` re-exports :func:`_corner_lib_lines`.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator
from typing import Any

from ._provenance import sha256_file
from .sim_errors import SimError


def resolve_param_file(
    points: list[Any],
    models: dict[str, Any],
    request_dir: str,
    resolve_lib_ref: Callable[[str, dict[str, Any], str], str],
) -> str | None:
    """Resolve ``models.param_file`` with ``models.lib``'s own rule
    (``resolve_lib_ref``), existence-check it up front, and record the absolute
    path on every point. ``None`` when the request does not set the field
    (points and decks are then untouched)."""
    ref = models.get("param_file")
    if ref is None:
        return None
    if not isinstance(ref, str) or not ref:
        raise SimError("request.models.param_file must be a non-empty string")
    path = resolve_lib_ref(ref, models, request_dir)
    if not os.path.isfile(path):
        raise SimError(f"model parameter file not found: {path}")
    for point in points:
        point.param_file = path
    return path


def param_file_environment(
    param_file: str | None, relative_path: Callable[[str], str]
) -> dict[str, Any]:
    """Additive ``environment`` provenance; empty when the field is unset."""
    if param_file is None:
        return {}
    return {
        "models_param_file": relative_path(param_file),
        "models_param_file_sha256": sha256_file(param_file),
    }


def _corner_lib_lines(point: Any, models_lib: str | None) -> list[str]:
    """The ``.lib`` cards selecting this corner's model sections, in
    declaration order -- the one place every generated deck
    (``_write_corner_deck``, ``_write_xyce_deck``, and
    ``op_sanity._write_op_deck``) derives them from, so a sweep, its
    cross-validation oracle, and ``--op-lint`` can never disagree about which
    model cards a corner selects.

    Three shapes, in order: a multi-section bundle (one line per section,
    each against its own per-section library when it declared one --
    ``CornerPoint.section_lib``), a bare-string process corner (one line
    against ``models_lib``), or no process axis at all (no lines).

    Issue #4759: ``models.param_file`` defines the globals the model sections
    reference, so its ``.include`` precedes every ``.lib`` card.
    """
    lines = [f".include {point.param_file}"] if point.param_file else []
    if point.process_sections is not None:
        lines.extend(
            f".lib {point.section_lib(index, models_lib)} {section}"
            for index, section in enumerate(point.process_sections)
        )
    elif point.process is not None:
        lines.append(f".lib {models_lib} {point.process}")
    return lines


def declared_model_refs(models: dict[str, Any]) -> Iterator[tuple[str, str, Any]]:
    """``(field, label, ref)`` for ``models.lib`` then ``models.param_file``."""
    for key, label in (
        ("lib", "model library"),
        ("param_file", "model parameter file"),
    ):
        if models.get(key) is not None:
            yield f"models.{key}", label, models.get(key)


def rewrite_model_refs(models: Any, lib_names: dict[Any, str]) -> None:
    """Replace ``models.lib`` / ``models.param_file`` by their staged names."""
    for key in ("lib", "param_file") if isinstance(models, dict) else ():
        if models.get(key) in lib_names:
            models[key] = lib_names[models[key]]
