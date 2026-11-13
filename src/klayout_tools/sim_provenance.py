"""Report provenance for a staged off-host `klt sim` job (issue #5325).

`sim_staging` stages the files; this module shapes what the report *says*
about them, so the two concerns stay separable -- and so neither
`sim.py` nor `sim_staging.py` has to grow past the per-file size ratchet
(`scripts/README.md`).

Two fields live here:

- ``environment.staged_model_inputs[].transform`` -- what staging did to a
  file whose ``sha256`` therefore differs from its ``source_sha256``.
  Present exactly when ``rewritten`` is true, so a reader can tell an
  expected rewrite from a content change.
- ``environment.pdk_revision_asserted`` -- the caller's own claim about the
  PDK revision behind the staged models (``options.pdk_revision``). It is
  *caller-asserted*, never worker-observed, and is deliberately kept out of
  ``worker_environment.pdk``, which describes the runner's image tree.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .sim_errors import SimError

if TYPE_CHECKING:
    from .sim_staging import StagedModelAsset

#: ``staged_model_inputs[].transform`` for a file whose ``.include``/``.lib``
#: targets were rewritten to flat job-relative names.
STAGED_TRANSFORM_INCLUDE_REWRITE = "include-path-rewrite"


def staged_asset_json(asset: StagedModelAsset) -> dict[str, Any]:
    """The report document for one staged model file (issue #2668's shape,
    plus ``transform`` when the file was rewritten)."""
    doc: dict[str, Any] = {
        "name": asset.name,
        "field": asset.field,
        "kind": asset.kind,
        "sha256": asset.sha256,
        "rewritten": asset.rewritten,
        "source_sha256": asset.source_sha256 or asset.sha256,
    }
    if asset.rewritten:
        doc["transform"] = STAGED_TRANSFORM_INCLUDE_REWRITE
    return doc


def pdk_revision_asserted(request: dict[str, Any]) -> str | None:
    """Validate and return ``request.options.pdk_revision``: absent ->
    ``None``, anything but a non-empty string is a :class:`SimError`.

    Called for *every* run -- every backend, staging on or off -- so a
    malformed value is a named exit-1 error rather than a silently dropped
    one. Only a run that actually stages model inputs off-host echoes it
    (:func:`pdk_revision_environment`, and docs/cli/sim.md's option table).
    """
    options = request.get("options") or {}
    if not isinstance(options, dict) or "pdk_revision" not in options:
        return None
    value = options["pdk_revision"]
    if not isinstance(value, str) or not value.strip():
        raise SimError(
            f"request.options.pdk_revision must be a non-empty string (got {value!r})"
        )
    return value


def pdk_revision_environment(request: dict[str, Any]) -> dict[str, Any]:
    """The additive ``environment`` key a declared ``options.pdk_revision``
    contributes -- ``{}`` when the request declared none."""
    asserted = pdk_revision_asserted(request)
    if asserted is None:
        return {}
    return {"pdk_revision_asserted": {"value": asserted, "scope": "caller-asserted"}}
