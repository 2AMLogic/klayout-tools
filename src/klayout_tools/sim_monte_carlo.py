"""Monte Carlo *sampling*: request validation, seed derivation, expansion.

Moved verbatim out of ``sim.py`` (issue #8020, the follow-up #5428's
corner-matrix split left open) so that module stays under its per-file size
ratchet. Holds the sampling half of #344's Monte Carlo decomposition (phase
1, #348): the ``request.monte_carlo`` schema constants, validation of its
sampling fields (``n``/``seed``/``vary``/``seed_scope``/``seed_mode``) and
statistics fields (``quantiles``/``k_sigma``), the SHA-256 seed derivation
(:func:`_derive_mc_seed`), and :func:`_expand_monte_carlo` -- N
reproducibly-seeded :class:`~.sim_corner_point.CornerPoint` samples per
corner point. The statistics half that reduces those samples to a verdict
(phase 2, #349) is ``sim_monte_carlo_rollup``.

A strict leaf: nothing here imports ``sim.py``, which re-exports every name
its own code or an outside caller still reaches so
``klayout_tools.sim.<name>`` keeps working.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from . import sim_injected, sim_mismatch_scale
from ._jsonnum import is_int, is_number
from .sim_checkpoint import DEFAULT_MC_SEED_MODE, DEFAULT_MC_SEED_SCOPE
from .sim_corner_point import CornerPoint
from .sim_errors import SimError
from .sim_model_paths import resolve_lib_ref as _resolve_lib_ref
from .sim_population_limits import validate_k_sigma as _validate_k_sigma

#: Recognised values for ``request.monte_carlo.vary`` -- which axis (or
#: axes) of statistical variation the sample sequence is declared to
#: exercise. See :func:`_expand_monte_carlo` and this module's "Monte Carlo
#: sampling" section below for the seed-derivation contract this drives, and
#: docs/cli/sim.md's "Monte Carlo" section for the request/response shape.
SUPPORTED_MC_VARY = ("mismatch", "process", "both")

#: Recognised values for ``request.monte_carlo.seed_scope`` -- what, besides
#: ``seed`` and ``sample_index``, enters each sample's seed derivation (issue
#: #2899). ``per_corner`` (the default) keys on the point's position in this
#: request's expanded corner list and is bit-identical to the pre-#2899
#: behaviour; ``shared`` drops the corner entirely (sample *k* is the same
#: die at every corner); ``point`` keys on the point's own identity, so any
#: partition or reordering of a grid yields the same samples.
SUPPORTED_MC_SEED_SCOPES = ("per_corner", "shared", "point")

#: Recognised values for ``request.monte_carlo.seed_mode`` -- how each
#: sample's ``rndseed`` (the integer written as ``.options seed=``) is
#: produced (issue #4173). ``derived`` (the default) is the SHA-256
#: construction; ``sequential`` pins it to ``seed + sample_index`` so a
#: campaign with a documented ``base + draw_index`` rule can be re-expressed
#: draw-for-draw. ``process_seed``/``mismatch_seed`` stay derived either way.
SUPPORTED_MC_SEED_MODES = ("derived", "sequential")

#: Quantiles (percentiles, ``0``-``100``) reported per measurement for a
#: Monte Carlo sample set when ``request.monte_carlo.quantiles`` is omitted:
#: the median plus the symmetric 5%/95% tails, which is what both canary
#: repos' hand-rolled rollups report today. Percentiles rather than
#: ``0``-``1`` fractions so the response keys read as ``p5``/``p50``/``p95``
#: -- see :func:`_sample_statistics` and docs/cli/sim.md's "Monte Carlo
#: statistics" section.
DEFAULT_MC_QUANTILES = (5.0, 50.0, 95.0)


def _validate_monte_carlo_spec(mc_spec: dict[str, Any]) -> tuple[int, int, str]:
    """Validate ``request.monte_carlo`` and return its ``(n, seed, vary)``
    fields. Raises :class:`SimError` with an actionable message for a
    missing/malformed field -- mirroring the request-level validation style
    used elsewhere in :func:`run_sim` (e.g. ``request.analysis``,
    ``request.measurements[]``)."""
    if not isinstance(mc_spec, dict):
        raise SimError("request.monte_carlo must be an object")

    n = mc_spec.get("n")
    if not is_int(n) or n < 1:
        raise SimError("request.monte_carlo.n must be a positive integer")

    seed = mc_spec.get("seed")
    if not is_int(seed):
        raise SimError("request.monte_carlo.seed must be an integer")

    vary = mc_spec.get("vary")
    if vary not in SUPPORTED_MC_VARY:
        raise SimError(
            "request.monte_carlo.vary must be one of "
            f"{', '.join(SUPPORTED_MC_VARY)} (got {vary!r})"
        )

    _validate_mc_seed_scope(mc_spec)
    seed_mode = _validate_mc_seed_mode(mc_spec)
    if seed_mode == "sequential" and (seed < 0 or seed + n - 1 >= _MC_SEED_MODULUS):
        raise SimError(
            "request.monte_carlo.seed_mode 'sequential' needs "
            f"0 <= seed and seed + n - 1 < {_MC_SEED_MODULUS} "
            f"(got seed={seed}, n={n}); lower the seed or n, or use "
            "seed_mode 'derived'"
        )

    return n, seed, vary


def _validate_mc_seed_mode(mc_spec: dict[str, Any]) -> str:
    """Validate the optional ``request.monte_carlo.seed_mode`` (issue #4173)
    and return it, defaulting to ``"derived"`` when absent."""
    mode = mc_spec.get("seed_mode", DEFAULT_MC_SEED_MODE)
    if mode not in SUPPORTED_MC_SEED_MODES:
        raise SimError(
            "request.monte_carlo.seed_mode must be one of "
            f"{', '.join(SUPPORTED_MC_SEED_MODES)} (got {mode!r})"
        )
    return mode


def _validate_mc_seed_scope(mc_spec: dict[str, Any]) -> str:
    """Validate the optional ``request.monte_carlo.seed_scope`` (issue #2899)
    and return it, defaulting to ``"per_corner"`` when absent."""
    scope = mc_spec.get("seed_scope", DEFAULT_MC_SEED_SCOPE)
    if scope not in SUPPORTED_MC_SEED_SCOPES:
        raise SimError(
            "request.monte_carlo.seed_scope must be one of "
            f"{', '.join(SUPPORTED_MC_SEED_SCOPES)} (got {scope!r})"
        )
    return scope


def _mc_point_identity(point: CornerPoint) -> str:
    """A stable string naming ``point`` independent of its position in the
    request (``seed_scope: "point"``): process name, process sections,
    supply rails and temperature."""
    return json.dumps(
        {
            "process": point.process,
            "process_sections": point.process_sections,
            "supply_v": point.supply_v,
            "temperature_c": point.temperature_c,
        },
        sort_keys=True,
        default=str,
    )


def _resolve_mismatch_scale(
    mc_spec: dict[str, Any], pdk: Any, **kwargs: Any
) -> sim_mismatch_scale.MismatchScale | None:
    """``request.monte_carlo.mismatch_scale`` (issues #3942, #5486), resolved
    to the deck ``.param`` cards that apply it -- see
    :func:`klayout_tools.sim_mismatch_scale.resolve` for the two PDK shapes
    (a verified multiplicative switch, or sigma ``.param``s scanned out of
    the selected mismatch sections' libraries)."""
    return sim_mismatch_scale.resolve(
        mc_spec, pdk, resolve_lib_ref=_resolve_lib_ref, **kwargs
    )


def _validate_mc_statistics_spec(
    mc_spec: dict[str, Any],
) -> tuple[tuple[float, ...], float | None]:
    """Validate the *statistics* half of ``request.monte_carlo`` and return
    its ``(quantiles, k_sigma)``.

    Kept separate from :func:`_validate_monte_carlo_spec` (which validates
    the *sampling* half -- ``n``/``seed``/``vary``) because the two are
    consumed at different points: sampling drives corner fan-out before any
    run, statistics drive the rollup after every run. Both are still
    validated up front by :func:`run_sim`, so a malformed field is the same
    "the sweep never started" class of error as a bad ``vary``.

    ``quantiles`` are percentiles in ``[0, 100]``, defaulting to
    :data:`DEFAULT_MC_QUANTILES`; duplicates are dropped, declaration order
    is preserved. ``k_sigma`` is ``None`` when the request declares no
    run-wide sigma multiple (individual measurements may still declare their
    own).
    """
    raw_quantiles = mc_spec.get("quantiles")
    if raw_quantiles is None:
        quantiles: tuple[float, ...] = DEFAULT_MC_QUANTILES
    else:
        if not isinstance(raw_quantiles, (list, tuple)) or not raw_quantiles:
            raise SimError(
                "request.monte_carlo.quantiles must be a non-empty array of "
                "percentiles in [0, 100]"
            )
        ordered: list[float] = []
        for entry in raw_quantiles:
            if not is_number(entry):
                raise SimError(
                    "request.monte_carlo.quantiles entries must be numbers in "
                    f"[0, 100] (got {entry!r})"
                )
            if not 0 <= entry <= 100:
                raise SimError(
                    "request.monte_carlo.quantiles entries must be numbers in "
                    f"[0, 100] (got {entry!r})"
                )
            value = float(entry)
            if value not in ordered:
                ordered.append(value)
        quantiles = tuple(ordered)

    raw_k = mc_spec.get("k_sigma")
    k_sigma = (
        None
        if raw_k is None
        else _validate_k_sigma(raw_k, "request.monte_carlo.k_sigma")
    )
    return quantiles, k_sigma


#: Modulus applied to every derived Monte Carlo seed component -- the
#: largest signed 32-bit prime (``2**31 - 1``), safely inside the range
#: ngspice's own ``.options seed=<int>`` accepts. Not itself
#: security-sensitive (this is reproducible sampling, not cryptography); it
#: only needs to be a fixed, documented constant so the same inputs always
#: derive the same seed.
_MC_SEED_MODULUS = 2_147_483_647


def _derive_mc_seed(base_seed: int, *parts: object) -> int:
    """Deterministically derive a reproducible integer seed from
    ``base_seed`` and ``parts``.

    SHA-256-based, deliberately never Python's built-in ``hash()`` -- string
    hashing is salted per-process by default (``PYTHONHASHSEED``), which
    would silently break the "same seed -> same sampled sequence" contract
    the moment two runs happened to land in different interpreters. Two
    calls with identical arguments always return the same value, in any
    process, on any machine, forever -- this is the seed-reproducibility
    guarantee :func:`_expand_monte_carlo` and ``docs/cli/sim.md``'s "Monte
    Carlo" section build on. ``parts`` disambiguates *what* is being
    derived (axis name, corner index, sample index, ...) so unrelated
    derivations from the same ``base_seed`` never collide by construction.
    """
    payload = ":".join(str(part) for part in (base_seed, *parts)).encode("utf-8")
    digest = hashlib.sha256(payload).digest()
    return int.from_bytes(digest[:4], "big") % _MC_SEED_MODULUS


def _mc_sample_seed(
    mc_spec: dict[str, Any],
    seeding: tuple[int, str, str],
    varies: tuple[bool, bool],
    scope_key: object,
    sample_index: int,
    mismatch_scale: sim_mismatch_scale.MismatchScale | None,
) -> dict[str, Any]:
    """The ``mc_seed`` record of one Monte Carlo sample (see
    :func:`_expand_monte_carlo`'s seed contract)."""
    seed, seed_scope, seed_mode = seeding
    vary_process, vary_mismatch = varies
    process_seed = _derive_mc_seed(
        seed, "process", scope_key, sample_index if vary_process else "fixed"
    )
    mismatch_seed = _derive_mc_seed(
        seed, "mismatch", scope_key, sample_index if vary_mismatch else "fixed"
    )
    if seed_mode == "sequential":
        # Issue #4173: the caller pins the integer ngspice receives.
        rndseed = seed + sample_index
    else:
        rndseed = _derive_mc_seed(seed, "rndseed", process_seed, mismatch_seed)
    mc_seed: dict[str, Any] = {
        "process_seed": process_seed,
        "mismatch_seed": mismatch_seed,
        "rndseed": rndseed,
    }
    if "seed_scope" in mc_spec:
        # Echoed only when the request declared it, like
        # `quantiles`/`k_sigma`, so a request that omits the field
        # keeps its pre-#2899 response shape.
        mc_seed["seed_scope"] = seed_scope
    if "seed_mode" in mc_spec:
        # Echoed only when declared (issue #4173), like seed_scope.
        mc_seed["seed_mode"] = seed_mode
    if mismatch_scale is not None:
        # Rides the point (and so the fleet wire and the deck
        # writer); echoed only when declared (issue #3942/#5486).
        mc_seed.update(mismatch_scale.seed_fields())
    return mc_seed


def _expand_monte_carlo(
    corner_points: list[CornerPoint],
    mc_spec: dict[str, Any],
    injected: tuple[list[dict[str, Any]], float] | None = None,
    mismatch_scale: sim_mismatch_scale.MismatchScale | None = None,
) -> tuple[list[CornerPoint], dict[str, Any]]:
    """Expand each point in ``corner_points`` into ``mc_spec["n"]`` sample
    points, returning ``(sampled_points, monte_carlo_info)``.

    All backends consume this same list of sample points. Order is deterministic
    (outer: original corner order from :func:`expand_corners`; inner:
    ``sample_index`` ``0..n-1``), matching the odometer-style determinism
    :func:`expand_corners` already guarantees for the PVT axes.

    **Seed contract.** Each sample derives two independent seed components
    from ``mc_spec["seed"]`` via :func:`_derive_mc_seed` -- ``process_seed``
    and ``mismatch_seed`` -- and a combined ``rndseed`` fed to ngspice (see
    ``_write_corner_deck``'s ``.options seed=`` card). A component only
    varies across ``sample_index`` when ``mc_spec["vary"]`` actually asks
    for that axis (``"process"``/``"mismatch"``/``"both"``); otherwise every
    sample of that corner derives the same value for it. This is the
    **deterministic negative control**: request ``vary: "process"`` and
    every sample's reported ``mismatch_seed`` -- and anything downstream
    that consumes ``mc_mismatch_seed`` directly -- is identical across the
    N samples (sigma=0), proving the sampler isn't silently injecting
    variation nobody asked for. The control does **not** extend to a deck
    that draws mismatch from ngspice's own RNG (``agauss``/``gauss`` in
    ``.param`` expressions): ngspice receives only the combined
    ``.options seed=<rndseed>``, derived from both components, so it still
    changes per sample under ``vary: "process"``. For such decks, request
    ``monte_carlo.mismatch_scale: 0`` (issues #3942/#5486) to switch the
    deck's own mismatch off and get a sigma=0 control.

    Requesting the *same* ``mc_spec["seed"]`` again (same process, different
    process, doesn't matter) reproduces the exact same sequence of derived
    seeds -- see :func:`_derive_mc_seed`.

    ``injected`` (issue #2911) is ``(resolved, scale)`` from
    :func:`_resolve_injected`: each sample then also records its design-owned
    mismatch draws, a pure function of that sample's ``mismatch_seed`` (see
    :mod:`klayout_tools.sim_injected`).
    """
    n, seed, vary = _validate_monte_carlo_spec(mc_spec)
    seed_scope = _validate_mc_seed_scope(mc_spec)
    seed_mode = _validate_mc_seed_mode(mc_spec)
    vary_process = vary in ("process", "both")
    vary_mismatch = vary in ("mismatch", "both")

    sampled: list[CornerPoint] = []
    for corner_index, base in enumerate(corner_points):
        # What keys the derivation besides seed/axis/sample (issue #2899).
        # `per_corner` passes the bare positional index so every derived
        # integer stays bit-identical to the pre-#2899 contract.
        if seed_scope == "shared":
            scope_key: object = "shared"
        elif seed_scope == "point":
            scope_key = f"point:{_mc_point_identity(base)}"
        else:
            scope_key = corner_index
        for sample_index in range(n):
            mc_seed = _mc_sample_seed(
                mc_spec,
                (seed, seed_scope, seed_mode),
                (vary_process, vary_mismatch),
                scope_key,
                sample_index,
                mismatch_scale,
            )
            mismatch_seed = mc_seed["mismatch_seed"]
            sampled.append(
                CornerPoint(
                    base.process,
                    base.supply_v,
                    base.temperature_c,
                    process_sections=base.process_sections,
                    process_section_libs=base.process_section_libs,
                    params=base.params,
                    variant=base.variant,
                    nodeset=base.nodeset,
                    initial_conditions=base.initial_conditions,
                    analysis_args=base.analysis_args,
                    options=base.options,
                    sample_index=sample_index,
                    mc_seed=mc_seed,
                    supply_waveforms=base.supply_waveforms,
                    mc_injected=(
                        sim_injected.draw(injected[0], mismatch_seed, injected[1])
                        if injected
                        else None
                    ),
                )
            )
    info: dict[str, Any] = {"n": n, "seed": seed, "vary": vary}
    if injected:
        info["injected_scale"] = injected[1]
    if "seed_scope" in mc_spec:
        info["seed_scope"] = seed_scope
    if "seed_mode" in mc_spec:
        info["seed_mode"] = seed_mode
    if mismatch_scale is not None:
        info.update(mismatch_scale.seed_fields())
    return sampled, info
