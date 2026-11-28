"""Validate and grade a batch worker's PDK revision stamp."""

from __future__ import annotations

import os
import re
from typing import Any

from .pdk_store import STORE_LAYOUTS, client_pdk_stamp, install_facts
from .sim_batch_contract_worker import _bounded_str
from .sim_errors import SimError, request_options

PDK_EXPECT_FIELD = "pdk_expect"
PDK_EXPECT_KEYS = ("stamp_sha256",)
_SHA256_HEX = re.compile(r"[0-9a-fA-F]{64}")


def _client_pdk_stamp(request: dict[str, Any] | None) -> dict[str, Any] | None:
    """The submitter's local view of the requested PDK variant's stamp (``SOURCES``,
    else bundle ``.pdk-store.json``, #5318); ``None`` when nothing comparable is
    readable. The root is ``models.pdk_root``, else ``$PDK_ROOT``, else the install
    the PDK search order resolves -- the one ``provenance.pdk`` names (#5474), so a
    host that pins neither still gets a comparison rather than
    ``client_stamp_unavailable`` (``models.pdk_root`` is refused for batch)."""
    models = (request or {}).get("models") or {}
    root = models.get("pdk_root") or os.environ.get("PDK_ROOT")
    return client_pdk_stamp(models.get("pdk"), root)


def pdk_expectation(request: dict[str, Any] | None) -> dict[str, Any] | None:
    """The validated ``models.pdk_expect`` pin (``{"stamp_sha256": <hex>}``),
    or ``None`` when the request states none. A malformed pin raises
    :class:`~klayout_tools.sim.SimError` -- never silently ignored, since an
    ignored pin would let a run pass that the caller meant to gate."""
    models = (request or {}).get("models")
    if not isinstance(models, dict) or PDK_EXPECT_FIELD not in models:
        return None
    pin = models[PDK_EXPECT_FIELD]
    if not isinstance(pin, dict):
        raise SimError(
            f"models.{PDK_EXPECT_FIELD} must be an object "
            '({"stamp_sha256": "<64 hex characters>"})'
        )
    unknown = sorted(set(pin) - set(PDK_EXPECT_KEYS))
    if unknown:
        raise SimError(
            f"models.{PDK_EXPECT_FIELD} has unknown key(s) {unknown} "
            f"(supported: {', '.join(PDK_EXPECT_KEYS)})"
        )
    value = pin.get("stamp_sha256")
    if not (isinstance(value, str) and _SHA256_HEX.fullmatch(value)):
        raise SimError(
            f"models.{PDK_EXPECT_FIELD}.stamp_sha256 must be a 64-character "
            "hex sha256 (the worker's pdk.stamp_sha256)"
        )
    return {"stamp_sha256": value.lower()}


def require_pdk_match_requested(request: dict[str, Any] | None) -> bool:
    """``options.require_pdk_match`` (issue #3462), validated: a non-boolean
    raises :class:`~klayout_tools.sim.SimError`."""
    options = (request or {}).get("options")
    value = (
        options.get("require_pdk_match", False) if isinstance(options, dict) else False
    )
    if not isinstance(value, bool):
        raise SimError("options.require_pdk_match must be a boolean")
    return value


def observed_stamp(value: Any, request: dict[str, Any] | None) -> Any:
    """A shortened or invalid digest cannot support a revision check."""
    if require_pdk_match_requested(request) or pdk_expectation(request) is not None:
        if not isinstance(value, str) or not _SHA256_HEX.fullmatch(value):
            return None
        return value.lower()
    return value


def bounded_store_evidence(capture: dict[str, Any]) -> dict[str, Any]:
    """The worker's own install evidence (issue #8007), re-validated and
    bounded: ``{"install": {sha256, version, via, synced} | None,
    "store_layout": manager_family | legacy_manager | flat | None}``. Each is
    ``None`` -- unknown -- when the capture carries no usable value: an older
    worker's capture has neither field, and an object with no valid fact is
    no evidence."""
    install = capture.get("install")
    facts = install_facts(install) if isinstance(install, dict) else None
    layout = capture.get("store_layout")
    return {
        "install": {key: _bounded_str(value) for key, value in facts.items()}
        if facts
        else None,
        "store_layout": layout
        if isinstance(layout, str) and layout in STORE_LAYOUTS
        else None,
    }


def decks_execute_declared_inputs(request: dict[str, Any] | None) -> bool:
    """Whether a deck-list request holds a unit whose ``.include``/``.lib``
    closure stages one of that unit's own declared ``inputs[]`` (issue
    #7981): the engine then executes client-supplied bytes, so the worker
    image's PDK stamp does not describe every model file the job ran. The
    unit-relative destinations are compared within a unit; a declared input
    no directive reads does not count. Never raises."""
    decks = (request or {}).get("decks")
    if not isinstance(decks, list):
        return False
    for unit in decks:
        if not isinstance(unit, dict):
            continue
        declared = _destinations(unit.get("inputs"))
        staged = _destinations(unit.get("includes"))
        if not declared.isdisjoint(staged):
            return True
    return False


def _destinations(refs: Any) -> set[str]:
    """The unit-relative ``name`` of every well-formed ``{staged, name}``
    reference in a worker-form unit list."""
    if not isinstance(refs, list):
        return set()
    return {
        ref["name"]
        for ref in refs
        if isinstance(ref, dict) and isinstance(ref.get("name"), str)
    }


def stamp_scope(captured: dict[str, Any], request: dict[str, Any] | None) -> str:
    """What the worker's ``pdk.stamp_*`` covers: ``image`` when the model
    bytes that ran were not the image tree's own -- ``models_staged`` (issue
    #4020) or a deck executing a declared input (issue #7981) -- and
    ``executed`` otherwise."""
    if captured.get("models_staged") is True or decks_execute_declared_inputs(request):
        return "image"
    return "executed"


def _compare_pdk_stamps(
    worker_pdk: dict[str, Any],
    client: dict[str, Any] | None,
    expected: dict[str, Any] | None = None,
    expected_variant: str | None = None,
) -> dict[str, Any]:
    """Compare worker and client PDK stamps -- only when both provide the
    same variant under the same stamp scheme. Anything else is
    ``not_comparable``, never a proven mismatch. A match is not model-closure
    equality (see docs/cli/sim.md)."""
    if expected is not None:
        pinned_client = {
            "variant": expected_variant or worker_pdk.get("variant"),
            "stamp_scheme": "sources-sha256",
            "stamp_sha256": expected["stamp_sha256"],
        }
        observed = {
            **worker_pdk,
            "stamp_sha256": (worker_pdk.get("stamp_sha256") or "").lower(),
        }
        comparison = _compare_observed_stamp(observed, pinned_client)
        if comparison["status"] == "mismatch":
            comparison["warning"] = (
                "the worker PDK stamp differs from models.pdk_expect.stamp_sha256"
            )
        return {**comparison, "expected": expected}
    return _compare_observed_stamp(worker_pdk, client)


def _compare_observed_stamp(
    worker_pdk: dict[str, Any], client: dict[str, Any] | None
) -> dict[str, Any]:
    worker_ok = (
        worker_pdk.get("variant")
        and worker_pdk.get("stamp_scheme")
        and worker_pdk.get("stamp_sha256")
    )
    # Issue #3946: say *why* a pair is not comparable (a closed vocabulary,
    # documented in docs/cli/sim.md) so a reader can tell the submitter's own
    # configuration from a runner-side gap without opening the raw capture.
    if not worker_ok:
        return {"status": "not_comparable", "reason": "worker_stamp_unavailable"}
    if client is None:
        return {"status": "not_comparable", "reason": "client_stamp_unavailable"}
    if client.get("variant") != worker_pdk.get("variant"):
        return {"status": "not_comparable", "reason": "variant_differs"}
    if client.get("stamp_scheme") != worker_pdk.get("stamp_scheme"):
        return {"status": "not_comparable", "reason": "scheme_differs"}
    if client.get("stamp_sha256") == worker_pdk.get("stamp_sha256"):
        return {"status": "match"}
    return {
        "status": "mismatch",
        "warning": (
            f"the worker's PDK revision stamp for {worker_pdk['variant']} differs "
            "from the one the submitter resolved locally; results may differ "
            "from a local run (a matching stamp would not prove identical models)"
        ),
    }


def apply_request_gate(
    request: dict[str, Any], override: bool | None
) -> dict[str, Any]:
    if override is not None:
        request = {
            **request,
            "options": {**request_options(request), "require_pdk_match": override},
        }
    pdk_expectation(request)
    if require_pdk_match_requested(request) and (
        "decks" in request or "commands" in request
    ):
        raise SimError("--require-pdk-match applies only to batch corner requests")
    return request


def mark_required(worker: dict[str, Any], request: dict[str, Any] | None) -> None:
    if not require_pdk_match_requested(request):
        return
    comparison = worker["pdk_stamp_comparison"]
    comparison["required"] = True
    expected = pdk_expectation(request)
    if expected:
        comparison["expected"] = expected
    if (worker.get("pdk") or {}).get("models_staged") or request_options(
        request or {}
    ).get("stage_model_inputs") is True:
        # The worker's image stamp does not identify the files it executed.
        comparison.pop("warning", None)
        comparison.update(status="not_comparable", reason="models_staged")


def failure_message(worker: dict[str, Any], client: dict[str, Any] | None) -> str:
    comparison = worker["pdk_stamp_comparison"]
    observed = (worker.get("pdk") or {}).get("stamp_sha256") or "absent"
    expected = (comparison.get("expected") or client or {}).get(
        "stamp_sha256"
    ) or "none available"
    reason = comparison.get("reason", "revision differs")
    return (
        f"options.require_pdk_match: worker stamp_sha256: {observed}, "
        f"expected stamp_sha256: {expected}, comparison: {comparison['status']} "
        f"({reason})"
    )


def allow_backend_fallback(
    request: dict[str, Any], backend: str, from_host: bool
) -> bool:
    """Keep a gated run on batch through later host-default yields."""
    if not require_pdk_match_requested(request):
        return from_host
    if backend != "batch":
        raise SimError(
            f"--require-pdk-match needs the 'batch' backend (got {backend!r})"
        )
    return False


def checkpoint_requirement(request: dict[str, Any]) -> dict[str, Any] | None:
    """Only reuse completed corners checked against the same PDK requirement."""
    if not require_pdk_match_requested(request):
        return None
    models = request.get("models") or {}
    pin = pdk_expectation(request)
    local = {} if pin else (_client_pdk_stamp(request) or {})
    return {
        "variant": models.get("pdk") or local.get("variant"),
        "scheme": "sources-sha256" if pin else local.get("stamp_scheme"),
        "stamp_sha256": (pin or local).get("stamp_sha256"),
        "staged": request_options(request).get("stage_model_inputs") is True,
    }
