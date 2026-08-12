"""Drive a Monte Carlo yield campaign directly from `klt yield` -- issue
#906, Phase 2a of the statistical/yield epic #710.

Phase 1 (`yield_analysis.py`, issues #816/#817/#818) shipped `klt yield` as a
**consumer** of an already-run MC sample set: fit, estimate yield, report
CIs/Cpk, and enforce the negative-control/analytic-cross-check discipline.
This module closes the loop on the input side -- launching and managing the
campaign itself -- without touching Phase 1's pipeline at all.

A **campaign spec** is a `klt sim` request document (see ``docs/cli/sim.md``)
that additionally declares a mandatory ``monte_carlo`` block (a campaign
*is* its MC sampling -- a request without one is just a `klt sim` request;
run `klt sim` directly instead) and, optionally, this command's own
top-level run defaults (``confidence``/``target_ci_halfwidth``/
``min_samples``) -- the same run-level fields a `klt yield --limits` spec
file accepts (see ``yield_analysis._read_spec``), lifted to the campaign
spec's own top level since there is no separate limits file in this flow.
Every other field (``netlist``, ``analysis``, ``measurements[].limits``,
``corners``, ``backend``, ``remote``, ...) is exactly `klt sim`'s own
request shape -- device-mismatch/corner ranges are the *sampling* half
(``monte_carlo.vary``/``corners``), a measurement's spec limits are the
*yield* half (``measurements[].limits``, already a `klt sim` field this
module does not need to invent).

``run_campaign`` does three things, each delegating to an already-shipped,
already-tested implementation rather than reimplementing any of it:

1. **Seed management.** If the spec omits ``monte_carlo.seed``, one is
   derived deterministically from the spec's own sampling-relevant content
   (:func:`_default_campaign_seed`, reusing :mod:`sim`'s own
   :func:`sim._derive_mc_seed` -- the same SHA-256-based, never-``hash()``
   derivation :func:`sim._expand_monte_carlo` already uses, so this module
   adds no second hashing scheme). The exact same campaign spec file,
   re-run any number of times, therefore reproduces the exact same sample
   set -- and the resolved seed is always echoed in the response's
   ``campaign`` block, so a caller-supplied seed and a derived one are
   equally auditable.
2. **Dispatch.** The resolved request (spec plus the resolved seed) is
   handed straight to :func:`sim.run_sim` -- ``backend``/``hosts`` pass
   through unchanged (CLI flag beats the spec's own fields, matching `klt
   sim`'s own precedence rule). This reuses Epic #375's shard/merge engine
   for the corner x Monte-Carlo-sample grid exactly as `klt sim` itself
   does: `local`/`local-parallel` shard in-process, and (issue #906's `klt
   sim` wiring) `backend: "remote"` with `hosts > 1` shards across a real
   EC2 fleet via :mod:`remote_fleet`. This module adds no scheduling logic
   of its own.
3. **Collection.** :func:`sim.run_sim`'s own report -- already shaped
   exactly like any other `klt sim` Monte Carlo report -- is written to a
   sample-set file and handed to :func:`yield_analysis.run_yield`
   unmodified: the same reader (``yield_analysis._measurements_from_sim_report``)
   and the same fit/CI/Cpk/negative-control/analytic-cross-check pipeline
   (Phase 1) that already runs against a pre-run `klt sim` report.

Pure library: :func:`run_campaign` returns plain Python data and never
prints -- serialisation and human-readable formatting live in
``cli/yield_campaign_cmd.py``, matching every other `klt` verb.
"""

from __future__ import annotations

import copy
import json
import os
from typing import Any

from . import sim
from ._paths import _load_request_json, _resolve_relative, validate_request_shape
from .yield_analysis import YieldError, run_yield

#: Mirrors ``yield_analysis.SCHEMA_VERSION`` -- the response is a yield
#: report (Phase 1's own shape) plus one added ``campaign`` block, so this
#: command's schema version tracks yield's own rather than inventing a
#: second, independent one.
SCHEMA_VERSION = 1

#: A campaign spec is a `klt sim` request with a mandatory `monte_carlo`
#: block -- these are the fields `sim.load_request` itself already requires
#: (`netlist`/`analysis`), checked again here (before any seed resolution or
#: dispatch) so a malformed spec fails with this module's own, campaign-
#: flavoured message rather than a generic `klt sim` one.
_REQUIRED_SPEC_FIELDS = ("netlist", "analysis")

#: Top-level campaign-spec fields that are `klt yield`'s own run-level
#: defaults (mirrors a `klt yield --limits` spec file's
#: `confidence`/`target_ci_halfwidth`/`min_samples`, see
#: `yield_analysis._read_spec`) -- never valid `klt sim` request fields, so
#: they are always popped off before the spec is dispatched as a sim
#: request.
_YIELD_ONLY_FIELDS = ("confidence", "target_ci_halfwidth", "min_samples")


class CampaignError(Exception):
    """Raised when a campaign cannot be run: a bad/missing spec, a spec with
    no `monte_carlo` block, a `klt sim` dispatch failure, or a `klt yield`
    analysis failure against the resulting sample set.

    The CLI turns this into a clean stderr message + exit code 1, never a
    traceback -- see ``docs/json-contract.md``.
    """


def _load_spec(spec_path: str) -> dict[str, Any]:
    data = _load_request_json(spec_path, CampaignError)
    return validate_request_shape(
        data,
        f"campaign spec '{spec_path}'",
        error_cls=CampaignError,
        required_fields=_REQUIRED_SPEC_FIELDS,
    )


def _require_monte_carlo_block(spec: dict[str, Any]) -> dict[str, Any]:
    mc = spec.get("monte_carlo")
    if not isinstance(mc, dict):
        raise CampaignError(
            "campaign spec requires a 'monte_carlo' object ({'n': ..., "
            "'vary': ...}) -- a spec without Monte Carlo sampling is just a "
            "`klt sim` request; run `klt sim` directly instead. See "
            "docs/cli/yield.md's 'Campaign orchestration' section"
        )
    return mc


def _campaign_identity_json(spec: dict[str, Any]) -> str:
    """Canonical JSON of everything about ``spec`` that determines its
    sample set -- excludes ``monte_carlo.seed`` itself (the value being
    derived from this), execution-only fields (``backend``/``remote``/
    ``options``) that never change *what* is sampled, and `klt yield`'s own
    post-processing-only run defaults (``confidence``/
    ``target_ci_halfwidth``/``min_samples``). Two campaign specs that agree
    on everything else always derive the same default seed, independent of
    how they are dispatched.
    """
    identity = copy.deepcopy(spec)
    for key in ("backend", "remote", "options", *_YIELD_ONLY_FIELDS):
        identity.pop(key, None)
    mc = dict(identity.get("monte_carlo") or {})
    mc.pop("seed", None)
    identity["monte_carlo"] = mc
    return json.dumps(identity, sort_keys=True, default=str)


def _default_campaign_seed(spec: dict[str, Any]) -> int:
    """Derive a deterministic default `monte_carlo.seed` from ``spec``'s own
    sampling-relevant content, so a campaign spec that never declares its
    own seed still reproduces the exact same sample set on every re-run.

    Reuses :func:`sim._derive_mc_seed` (private, same convention
    :mod:`remote_fleet` already uses to reuse :mod:`remote_launcher`'s own
    private helpers rather than duplicating them) -- the same SHA-256-based
    derivation :func:`sim._expand_monte_carlo` itself uses for every
    per-sample seed, so this module introduces no second hashing scheme.
    """
    return sim._derive_mc_seed(0, "yield-campaign", _campaign_identity_json(spec))


def _absolutize_paths(request: dict[str, Any], spec_dir: str) -> dict[str, Any]:
    """Rewrite ``netlist`` (and, when it will be resolved as a literal path
    rather than through a PDK variant -- see ``sim._resolve_models_lib``)
    ``models.lib``, to absolute paths anchored at ``spec_dir`` -- the
    campaign spec's own directory.

    The derived `klt sim` request this module writes lives under ``out_dir``
    (a sibling of the spec by default, but caller-overridable), not next to
    the spec itself -- `klt sim` resolves a request's relative paths against
    *its own* file's directory, so a spec's relative ``netlist``/``models.lib``
    would silently break the moment it is written out anywhere else. Already-
    absolute paths, and env-var/``~`` paths, are passed through unchanged
    (:func:`~klayout_tools._paths._resolve_relative` is a no-op for those).
    """
    request = dict(request)
    netlist = request.get("netlist")
    if isinstance(netlist, str):
        request["netlist"] = _resolve_relative(netlist, spec_dir)

    models = request.get("models")
    if (
        isinstance(models, dict)
        and not models.get("pdk")
        and not models.get("pdk_root")
    ):
        lib = models.get("lib")
        if isinstance(lib, str):
            request["models"] = {**models, "lib": _resolve_relative(lib, spec_dir)}

    return request


def _resolve_request(
    spec: dict[str, Any], *, spec_dir: str, seed_override: int | None
) -> tuple[dict[str, Any], dict[str, Any], int, str]:
    """Build the `klt sim` request to dispatch: ``spec`` with its
    `monte_carlo.seed` resolved (CLI override > spec's own > derived
    default), its `netlist`/`models.lib` paths absolutized against
    ``spec_dir`` (:func:`_absolutize_paths`), and `klt yield`'s own
    run-level fields stripped off.

    Returns ``(request, yield_defaults, resolved_seed, seed_source)``.
    """
    mc = dict(_require_monte_carlo_block(spec))

    if seed_override is not None:
        resolved_seed = seed_override
        seed_source = "cli"
    elif mc.get("seed") is not None:
        resolved_seed = mc["seed"]
        seed_source = "spec"
    else:
        resolved_seed = _default_campaign_seed(spec)
        seed_source = "derived"

    request = _absolutize_paths(copy.deepcopy(spec), spec_dir)
    mc["seed"] = resolved_seed
    request["monte_carlo"] = mc

    yield_defaults = {key: request.pop(key, None) for key in _YIELD_ONLY_FIELDS}

    return request, yield_defaults, resolved_seed, seed_source


def run_campaign(
    spec_path: str,
    *,
    out_dir: str | None = None,
    backend: str | None = None,
    hosts: int | None = None,
    seed: int | None = None,
    confidence: float | None = None,
    target_ci_halfwidth: float | None = None,
    min_samples: int | None = None,
    measurements: list[str] | None = None,
) -> dict[str, Any]:
    """Launch and analyse an MC campaign declared by the spec at
    ``spec_path``.

    ``backend``/``hosts`` override the spec's own ``backend``/``remote.hosts``
    fields when given, the same precedence rule `klt sim`'s own
    ``--backend``/``--hosts`` flags use. ``seed`` overrides the spec's own
    ``monte_carlo.seed`` (or its derived default) when given.
    ``confidence``/``target_ci_halfwidth``/``min_samples``/``measurements``
    are `klt yield`'s own analysis knobs, resolved exactly as
    :func:`yield_analysis.run_yield` resolves them (CLI flag beats the
    spec's own top-level field, which beats the documented default).

    Returns this command's JSON payload: Phase 1's own yield-report shape
    (see ``docs/cli/yield.md``) plus one added ``campaign`` block recording
    the seed/dispatch provenance. Raises :class:`CampaignError` for anything
    that prevents the campaign from running end to end -- a bad spec, a
    `klt sim` dispatch failure, or a `klt yield` analysis failure against
    the resulting sample set.
    """
    spec = _load_spec(spec_path)
    spec_dir = os.path.dirname(os.path.abspath(spec_path))
    request, yield_defaults, resolved_seed, seed_source = _resolve_request(
        spec, spec_dir=spec_dir, seed_override=seed
    )
    mc = request["monte_carlo"]

    if out_dir is None:
        out_dir = os.path.join(spec_dir, ".klt", "yield-campaign")
    os.makedirs(out_dir, exist_ok=True)

    request_path = os.path.join(out_dir, "sim-request.json")
    with open(request_path, "w", encoding="utf-8") as handle:
        json.dump(request, handle, indent=2)
        handle.write("\n")

    try:
        sim_report = sim.run_sim(
            request_path,
            artifacts_dir=os.path.join(out_dir, "artifacts"),
            backend=backend,
            hosts=hosts,
        )
    except sim.SimError as exc:
        raise CampaignError(f"campaign dispatch failed: {exc}") from exc

    sample_set_path = os.path.join(out_dir, "sample-set.json")
    with open(sample_set_path, "w", encoding="utf-8") as handle:
        json.dump(sim_report, handle, indent=2)
        handle.write("\n")

    try:
        yield_report = run_yield(
            sample_set_path,
            confidence=(
                confidence if confidence is not None else yield_defaults["confidence"]
            ),
            target_ci_halfwidth=(
                target_ci_halfwidth
                if target_ci_halfwidth is not None
                else yield_defaults["target_ci_halfwidth"]
            ),
            min_samples=(
                min_samples
                if min_samples is not None
                else yield_defaults["min_samples"]
            ),
            measurements=measurements,
        )
    except YieldError as exc:
        raise CampaignError(str(exc)) from exc

    resolved_backend = (
        backend if backend is not None else request.get("backend", "local")
    )
    resolved_hosts = (
        hosts if hosts is not None else (request.get("remote") or {}).get("hosts", 1)
    )

    payload: dict[str, Any] = dict(yield_report)
    payload["campaign"] = {
        "spec": spec_path,
        "seed": resolved_seed,
        "seed_source": seed_source,
        "requested_samples": mc.get("n"),
        "vary": mc.get("vary"),
        "backend": resolved_backend,
        "hosts": resolved_hosts,
        "sim_status": sim_report.get("status"),
        "corner_count": sim_report.get("corner_count"),
        "sim_report_path": sample_set_path,
        "request_path": request_path,
    }
    return payload
