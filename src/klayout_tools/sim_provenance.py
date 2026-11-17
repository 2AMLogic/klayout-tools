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

Since issue #5593 a third sits beside the second when a worker-observed
stamp exists to compare it with:

- ``environment.pdk_revision_comparison`` -- the advisory result of
  comparing the asserted value with each worker's stamp (match by sha256 or
  ``SOURCES`` revision token; mismatch otherwise; ``not_comparable`` when no
  readable stamp). It never changes the run's outcome -- enforcement of a
  pin is #3462's, not this field's.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .pdk_store import sources_revision_token
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


def _revision_comparison_one(
    asserted: str, worker_environment: Any
) -> dict[str, Any] | None:
    """The comparison of ``asserted`` with one worker block -- ``None`` when
    the block does not exist at all (nothing to compare, no field). The
    observed stamp rides along so a divergence is attributable from the
    report alone."""
    if not isinstance(worker_environment, dict):
        return None
    not_comparable: dict[str, Any] = {
        "status": "not_comparable",
        "reason": "worker_stamp_unavailable",
    }
    if worker_environment.get("capture") != "ok":
        return not_comparable
    pdk = worker_environment.get("pdk")
    if not isinstance(pdk, dict):
        return not_comparable
    stamp: dict[str, Any] = {
        "variant": pdk.get("variant"),
        "stamp_scheme": pdk.get("stamp_scheme"),
        "stamp_sha256": pdk.get("stamp_sha256"),
    }
    token = sources_revision_token(pdk.get("stamp_text"))
    if token is not None:
        stamp["revision_token"] = token
    if pdk.get("stamp_scope"):
        stamp["stamp_scope"] = pdk.get("stamp_scope")
    sha = pdk.get("stamp_sha256")
    if not (isinstance(sha, str) and sha):
        return {**not_comparable, "worker_stamp": stamp}
    value = asserted.strip()
    if value.lower() == sha.lower() or (token is not None and value == token):
        return {"status": "match", "worker_stamp": stamp}
    warning = (
        "the declared options.pdk_revision matches neither the worker-observed "
        "stamp_sha256 nor its revision token; advisory only, it does not fail "
        "the run"
    )
    if pdk.get("stamp_scope") == "image":
        warning += (
            " (the observed stamp describes the runner image's tree, not the "
            "staged files this revision was asserted for)"
        )
    return {"status": "mismatch", "worker_stamp": stamp, "warning": warning}


def _fleet_revision_comparison(
    asserted: str, fleet: list[Any]
) -> dict[str, Any] | None:
    """The aggregate over ``fleet[]`` members (issue #5593): each member is
    compared independently and stays individually visible; the top-level
    ``status`` is the worst member result (``mismatch`` over
    ``not_comparable`` over ``match``). ``None`` when no member carries a
    worker block at all."""
    members = []
    for entry in fleet:
        if not isinstance(entry, dict):
            continue
        one = _revision_comparison_one(asserted, entry.get("worker_environment"))
        if one is not None:
            members.append({"job_id": entry.get("job_id"), **one})
    if not members:
        return None
    statuses = {member["status"] for member in members}
    status = (
        "mismatch"
        if "mismatch" in statuses
        else "not_comparable"
        if "not_comparable" in statuses
        else "match"
    )
    return {
        "status": status,
        **(
            {"reason": next(m["reason"] for m in members if "reason" in m)}
            if status == "not_comparable"
            else {}
        ),
        "fleet": members,
    }


def pdk_revision_comparison_environment(
    remote_environment: dict[str, Any] | None, request: dict[str, Any] | None
) -> dict[str, Any]:
    """``{"pdk_revision_comparison": ...}`` (issue #5593) when a declared
    ``options.pdk_revision`` -- echoed as ``pdk_revision_asserted`` by
    :func:`pdk_revision_environment` -- meets a worker-observed stamp on
    ``remote_environment``; ``{}`` otherwise.

    Advisory by construction: the field records a comparison result, never a
    failure (enforcement of a PDK pin is issue #3462's). Compared only when
    both sides are present: a run with no ``worker_environment`` block at all
    (local backends, the ``remote`` backend's missing capture) adds no field.
    The asserted value matches the worker's ``stamp_sha256`` (ignoring case)
    or the ``SOURCES`` revision token; anything else with a readable stamp is
    a ``mismatch`` carrying the observed stamp.
    """
    if not isinstance(remote_environment, dict):
        return {}
    asserted: str | None = None
    try:
        asserted = pdk_revision_asserted(request or {})
    except SimError:
        # A malformed value was already rejected where every run validates
        # it; reaching here unarmed means there is nothing to compare.
        return {}
    if asserted is None:
        return {}
    fleet = remote_environment.get("fleet")
    if isinstance(fleet, list) and fleet:
        block = _fleet_revision_comparison(asserted, fleet)
        if block is not None:
            return {"pdk_revision_comparison": block}
        return {}
    one = _revision_comparison_one(
        asserted, remote_environment.get("worker_environment")
    )
    if one is None:
        return {}
    return {"pdk_revision_comparison": one}
