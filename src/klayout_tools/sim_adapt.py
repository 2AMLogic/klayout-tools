"""``request.adapt`` -- opt-in card adaptation for ``klt sim`` (issue #3873).

``{"adapt": {"kind": "asap7_bsimcmg", "model_cards": {"tt": "<.pm>", ...}}}``
makes ``klt sim`` simulate :func:`asap7_bsimcmg.adapt_extracted_netlist` of the
request's ``netlist`` and, when ``model_cards`` is given, a
:func:`asap7_bsimcmg.corner_library` of the adapted cards as ``models.lib``.
Absent ``adapt`` leaves the request untouched; nothing is sniffed.

The adapted files are written to a private work dir and the returned request
points at them, so everything downstream (hashing, staging, dispatch) sees an
ordinary request. Pure text-to-text; no toolchain needed.
"""

from __future__ import annotations

import copy
import hashlib
import os
from collections.abc import Callable
from typing import Any

from . import asap7_bsimcmg

#: Closed enum of ``adapt.kind`` values.
SUPPORTED_KINDS = ("asap7_bsimcmg",)


class AdaptError(ValueError):
    """A malformed or refused ``request.adapt``; the caller makes it a SimError."""


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def apply_adapt(
    request: dict[str, Any],
    request_dir: str,
    *,
    engine: str,
    work_dir: str,
    resolve: Callable[[str, str], str],
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """``resolve(path, base_dir)`` is the request-path rule shared with
    ``models.lib``. Return ``(request_to_run, environment_entry)``; the
    request unchanged and ``None`` when ``adapt`` is absent. Raises
    :class:`AdaptError`."""
    if "adapt" not in request:
        return request, None
    spec = request["adapt"]
    if not isinstance(spec, dict):
        raise AdaptError("request.adapt must be an object")
    kind = spec.get("kind")
    if kind not in SUPPORTED_KINDS:
        raise AdaptError(
            f"request.adapt.kind {kind!r} is not supported "
            f"(supported: {', '.join(SUPPORTED_KINDS)})"
        )
    if engine != "ngspice":
        raise AdaptError(
            f"request.adapt kind {kind!r} is OSDI/ngspice only; "
            f"engine {engine!r} is not supported"
        )
    cards = spec.get("model_cards")
    if cards is not None and (
        not isinstance(cards, dict)
        or not cards
        or not all(isinstance(k, str) and isinstance(v, str) for k, v in cards.items())
    ):
        raise AdaptError(
            "request.adapt.model_cards must be a non-empty object mapping "
            "corner name to a model-card file path"
        )
    models = request.get("models") or {}
    if cards is not None and models.get("lib") is not None:
        raise AdaptError(
            "request.adapt.model_cards and request.models.lib are both set; "
            "this is ambiguous -- give one of them"
        )

    out = copy.deepcopy(request)
    os.makedirs(work_dir, exist_ok=True)
    env: dict[str, Any] = {"kind": kind}

    netlist_path = resolve(request["netlist"], request_dir)
    try:
        with open(netlist_path, encoding="utf-8") as handle:
            src = handle.read()
    except OSError as exc:
        raise AdaptError(f"netlist not found: {netlist_path}") from exc
    try:
        adapted = asap7_bsimcmg.adapt_extracted_netlist(src)
    except ValueError as exc:
        raise AdaptError(f"adapt netlist: {exc}") from exc
    adapted_path = os.path.join(work_dir, os.path.basename(netlist_path))
    with open(adapted_path, "w", encoding="utf-8") as handle:
        handle.write(adapted)
    out["netlist"] = adapted_path
    env["netlist_sha256_in"] = _sha(src)
    env["netlist_sha256_out"] = _sha(adapted)

    if cards is not None:
        sections: dict[str, str] = {}
        env["model_cards"] = []
        for corner, ref in cards.items():
            path = resolve(ref, request_dir)
            try:
                with open(path, encoding="utf-8") as handle:
                    text = handle.read()
            except OSError as exc:
                raise AdaptError(f"model card not found: {path}") from exc
            try:
                sections[corner] = asap7_bsimcmg.adapt_model_cards(text)
            except ValueError as exc:
                raise AdaptError(f"adapt model card {corner!r}: {exc}") from exc
            env["model_cards"].append({"corner": corner, "sha256": _sha(text)})
        lib_text = asap7_bsimcmg.corner_library(sections)
        lib_path = os.path.join(work_dir, "models.lib")
        with open(lib_path, "w", encoding="utf-8") as handle:
            handle.write(lib_text)
        out["models"] = {**models, "lib": lib_path}
        # The library is absolute and ours, so a PDK variant must not re-root it.
    return out, env
