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

import atexit
import copy
import hashlib
import os
import shutil
import tempfile
from collections.abc import Callable
from typing import Any

from . import asap7_bsimcmg
from .sim_errors import SimError

#: Closed enum of ``adapt.kind`` values.
SUPPORTED_KINDS = ("asap7_bsimcmg",)

#: Mirrors ``sim._OFFHOST_BACKENDS`` (a test pins the equality).
OFFHOST_BACKENDS = ("remote", "batch")


class AdaptError(SimError):
    """A malformed or refused ``request.adapt`` (a ``SimError`` for the CLI)."""


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _validate(
    request: dict[str, Any], engine: str
) -> tuple[str, dict[str, Any] | None]:
    """Check ``request.adapt``; return ``(kind, model_cards or None)``."""
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
    if cards is not None and (request.get("models") or {}).get("lib") is not None:
        raise AdaptError(
            "request.adapt.model_cards and request.models.lib are both set; "
            "this is ambiguous -- give one of them"
        )
    return kind, cards


def _read(path: str, what: str) -> str:
    try:
        with open(path, encoding="utf-8") as handle:
            return handle.read()
    except OSError as exc:
        raise AdaptError(f"{what} not found: {path}") from exc


def _write(path: str, text: str) -> None:
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)


def _adapt_netlist(netlist_path: str, work_dir: str, env: dict[str, Any]) -> str:
    """Write the adapted netlist into ``work_dir``; return its path."""
    src = _read(netlist_path, "netlist")
    try:
        adapted = asap7_bsimcmg.adapt_extracted_netlist(src)
    except ValueError as exc:
        raise AdaptError(f"adapt netlist: {exc}") from exc
    adapted_path = os.path.join(work_dir, os.path.basename(netlist_path))
    _write(adapted_path, adapted)
    env["netlist_sha256_in"] = _sha(src)
    env["netlist_sha256_out"] = _sha(adapted)
    return adapted_path


def _adapt_cards(
    cards: dict[str, str],
    request_dir: str,
    work_dir: str,
    resolve: Callable[[str, str], str],
    env: dict[str, Any],
) -> str:
    """Write the adapted corner library into ``work_dir``; return its path."""
    sections: dict[str, str] = {}
    env["model_cards"] = []
    for corner, ref in cards.items():
        text = _read(resolve(ref, request_dir), "model card")
        try:
            sections[corner] = asap7_bsimcmg.adapt_model_cards(text)
        except ValueError as exc:
            raise AdaptError(f"adapt model card {corner!r}: {exc}") from exc
        env["model_cards"].append({"corner": corner, "sha256": _sha(text)})
    lib_path = os.path.join(work_dir, "models.lib")
    _write(lib_path, asap7_bsimcmg.corner_library(sections))
    return lib_path


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
    kind, cards = _validate(request, engine)
    out = copy.deepcopy(request)
    os.makedirs(work_dir, exist_ok=True)
    env: dict[str, Any] = {"kind": kind}
    out["netlist"] = _adapt_netlist(
        resolve(request["netlist"], request_dir), work_dir, env
    )
    if cards is not None:
        lib = _adapt_cards(cards, request_dir, work_dir, resolve, env)
        # The library is absolute and ours, so a PDK variant must not re-root it.
        out["models"] = {**(request.get("models") or {}), "lib": lib}
    return out, env


def prepare(
    request: dict[str, Any],
    request_dir: str,
    engine: str,
    backend: str,
    from_host_default: bool,
    resolve: Callable[[str, str], str],
) -> tuple[dict[str, Any], dict[str, Any], str]:
    """``klt sim``'s entry point: ``(request, {"adapt": report} or {}, backend)``.

    The adapted copies live in a private work dir (removed at exit), so an
    explicit off-host backend is refused rather than submitted unadapted, and
    an env-sourced one steps aside to ``local``."""
    if "adapt" not in request:
        return request, {}, backend
    if backend in OFFHOST_BACKENDS:
        if not from_host_default:
            raise AdaptError(
                f"request.adapt is not supported for backend {backend!r}: the "
                "adapted netlist and model library are local files that are "
                "not forwarded to remote/batch workers; use backend 'local' "
                "or 'local-parallel'"
            )
        backend = "local"
    work_dir = tempfile.mkdtemp(prefix="klt-sim-")
    atexit.register(shutil.rmtree, work_dir, ignore_errors=True)
    out, env = apply_adapt(
        request,
        request_dir,
        engine=engine,
        work_dir=work_dir,
        resolve=resolve,
    )
    return out, {"adapt": env}, backend
