"""Tightened re-runs of check-failing points: ``klt sim --regrade`` (#6778).

A sim acceptance check can fail on a point where the simulator returned a
poor transient solution rather than a property of the circuit (2AMLogic/
gf180-pll#1059: one of 504 VCO tuning curves dips ~11% at a single Vctrl
point under the batch image's ngspice-47; ngspice-42 on the same PDK gives a
monotonic value, and the pinned local ngspice-47 times out). The durable
remedy is procedural, not statistical: re-run *only* the affected units with
tightened solver settings, keep both observations, and let the tightened one
carry the grade.

Two halves live here:

- **The manifest declaration** ``options.tightened_rerun`` -- the bench
  declares its tightened transient settings *before* acquisition (a smaller
  ``tmax`` and/or tighter solver tolerances). ``sim.py`` validates the
  declaration on every run of the request (so a typo is caught before the
  first pass, not after a campaign) and echoes it as
  ``environment.tightened_rerun``. The declaration is inert in a normal
  run: nothing about the first pass changes.
- **The second pass** -- :func:`run_regrade`, reached from the CLI as
  ``klt sim REQUEST --regrade REPORT --check-id ID --corner CORNER_ID ...``.
  The checker (the chip repo's curve check, which lives outside KLT)
  supplies the source report, the failed check's identifier and the
  affected corner IDs; KLT owns selection, dispatch and the comparison
  record. Selection rides the internal ``request["_regrade"]`` field that
  :func:`prepare_regrade` applies after the request's own corner/Monte
  Carlo expansion -- client and worker apply the identical deterministic
  filter, so corner identities (and Monte Carlo seeds) are preserved by
  construction. Tightened solver tolerances merge into the derived
  request's ``options.ngspice_options`` (echo-enforced off-host by
  ``sim.check_ngspice_options_echo``); ``tmax`` rewrites the selected
  points' ``analysis_args`` (identity-neutral, echo-enforced by the
  ``analysis_overrides`` machinery, issue #4970).

The companion record (:func:`run_regrade`'s return, written beside the
re-run's artifacts as ``regrade.json``) states which units were re-run, with
which settings, and **both** values -- the first result is never silently
replaced. A completed tightened run can still FAIL; a timeout, a missing
result or an unusable solve is an ``unresolved`` grade with its reason, never
a PASS. No check definition is relaxed by any of this.
"""

from __future__ import annotations

from typing import Any

from ._jsonnum import is_finite_number
from ._spice_number import parse as _parse_spice_number
from .sim_errors import SimError

#: The ``options`` key a bench declares its tightened settings under.
TIGHTENED_RERUN_OPTION = "tightened_rerun"

#: The internal request field that carries one regrade's selection. Set only
#: by :func:`run_regrade`'s derived request document -- never by an external
#: author -- and shipped verbatim to an off-host worker, which applies the
#: same deterministic selection itself (see :func:`prepare_regrade`).
REGRADE_REQUEST_FIELD = "_regrade"

#: ``SimError.code`` when an off-host worker returned corners that do not
#: match the selection -- a runner built before this feature ran the whole
#: matrix instead of the selected units.
REGRADE_UNSUPPORTED_WORKER_CODE = "regrade_unsupported_worker"

_REGRADE_DIR_NAME = "regrade"
_REGRADE_REQUEST_FILENAME = "regrade-request.json"
_REGRADE_RECORD_FILENAME = "regrade.json"
_REGRADE_FIELD_KEYS = ("select_corner_ids", "tmax", "ngspice_options")


def _tmax_token(raw: Any, *, analysis: dict[str, Any] | None, steps: bool) -> str:
    """Normalise the declared ``tmax`` to the deck token :func:`tighten_tran_args`
    splices into the transient's arguments.

    A number is seconds (rendered compactly); a string must be a positive
    SPICE time literal (``"25p"``, ``"1e-9"``) and is kept verbatim so the
    deck reads the way the bench declared it. ``analysis`` (when given)
    gates the field onto a scalar ``tran`` analysis -- ``tmax`` is the
    transient's maximum internal timestep, so an ``ac``/``dc``/``op``/steps
    request cannot apply it.
    """
    if analysis is not None and (
        steps or str(analysis.get("kind") or "").lower() != "tran"
    ):
        raise SimError(
            f"options.{TIGHTENED_RERUN_OPTION}.tmax requires a scalar analysis of "
            'kind "tran" (not analysis_steps[]): tmax is the transient '
            "analysis's maximum internal timestep"
        )
    if isinstance(raw, bool) or isinstance(raw, (int, float)):
        if not is_finite_number(raw) or raw <= 0:
            raise SimError(
                f"options.{TIGHTENED_RERUN_OPTION}.tmax must be a positive number "
                f'of seconds or a SPICE time literal like "25p" (got {raw!r})'
            )
        return f"{float(raw):.6g}"
    if isinstance(raw, str):
        token = raw.strip()
        value = _parse_spice_number(token)
        if value is None or value <= 0:
            raise SimError(
                f"options.{TIGHTENED_RERUN_OPTION}.tmax must be a positive number "
                f'of seconds or a SPICE time literal like "25p" (got {raw!r})'
            )
        return token
    raise SimError(
        f"options.{TIGHTENED_RERUN_OPTION}.tmax must be a positive number of "
        f'seconds or a SPICE time literal like "25p" (got {raw!r})'
    )


def validate_tightened_rerun(
    value: Any,
    *,
    analysis: dict[str, Any] | None = None,
    engine: str | None = None,
    steps: bool = False,
) -> dict[str, Any] | None:
    """Validate ``options.tightened_rerun``; ``None`` when the request did
    not declare it (today's behaviour exactly).

    Keys: ``tmax`` plus any name from ``sim.NGSPICE_SOLVER_OPTIONS`` (the
    same allowlist ``options.ngspice_options`` uses, so a typo is a named
    error rather than a silently ignored ngspice option). At least one key
    must be declared. Solver options are refused for ``engine: "xyce"``
    (they render as an ngspice ``.options`` card), mirroring
    ``options.retry_on_convergence_error``.
    """
    from . import sim

    if value is None:
        return None
    if not isinstance(value, dict) or not value:
        raise SimError(
            f"options.{TIGHTENED_RERUN_OPTION} must be a non-empty object declaring "
            'the second pass\'s tightened settings, e.g. {"tmax": "25p", '
            '"reltol": 1e-4} (tmax plus any of: '
            + ", ".join(sim.NGSPICE_SOLVER_OPTIONS)
            + ")"
        )
    unknown = sorted(set(value) - ({"tmax"} | set(sim.NGSPICE_SOLVER_OPTIONS)))
    if unknown:
        raise SimError(
            f"options.{TIGHTENED_RERUN_OPTION} has unknown key(s): "
            f"{', '.join(unknown)} (known: tmax, "
            + ", ".join(sim.NGSPICE_SOLVER_OPTIONS)
            + ")"
        )
    tmax = (
        _tmax_token(value["tmax"], analysis=analysis, steps=steps)
        if "tmax" in value
        else None
    )
    solver = sim._validate_ngspice_options(
        {k: v for k, v in value.items() if k != "tmax"}
    )
    if solver and engine == "xyce":
        raise SimError(
            f"options.{TIGHTENED_RERUN_OPTION} solver options are not supported "
            "for engine 'xyce': they emit an ngspice `.options` card"
        )
    return {"tmax": tmax, "ngspice_options": solver}


def tighten_tran_args(args: str, tmax: str) -> str:
    """``args`` with ``tmax`` as the transient's maximum-timestep token.

    ngspice's transient command is positional -- ``tran tstep tstop tstart
    tmax`` with flags (``uic``) after -- so an existing 4th time token is
    *replaced* (the point's own ``tstart`` and every flag are preserved) and
    a 2/3-token window gains ``tmax`` (with ``tstart`` defaulting to 0, the
    ngspice default). Refuses an argument layout it cannot parse rather
    than emit a silently wrong command line.
    """
    tokens = str(args).split()
    positionals: list[str] = []
    flags: list[str] = []
    for token in tokens:
        if len(positionals) < 4 and _parse_spice_number(token) is not None:
            positionals.append(token)
        else:
            flags.append(token)
    if len(positionals) < 2 or any(
        _parse_spice_number(flag) is not None for flag in flags
    ):
        raise SimError(
            f"cannot apply the tightened tmax to analysis args {args!r}: expected "
            "'tran tstep tstop [tstart [tmax]] [uic]' with plain time literals"
        )
    if len(positionals) >= 3:
        # An explicit tstart (the 3rd positional) is preserved.
        out = [*positionals[:3], tmax]
    else:
        # 2-token window: tstart defaults to 0 and must be spelled out, or
        # ngspice would read the tightened tmax as tstart instead.
        out = [*positionals[:2], "0", tmax]
    return " ".join([*out, *flags])


def _validate_selection(ids: Any) -> list[str]:
    """Validate the CLI/caller's corner-id selection (unique, non-empty
    strings; possibly zero of them -- an empty selection submits zero jobs)."""
    if ids is None:
        return []
    if not isinstance(ids, (list, tuple)):
        raise SimError("the regrade selection must be a list of corner ids")
    seen: set[str] = set()
    duplicates: list[str] = []
    for item in ids:
        if not isinstance(item, str) or not item.strip():
            raise SimError(
                f"every selected corner id must be a non-empty string (got {item!r})"
            )
        if item in seen and item not in duplicates:
            duplicates.append(item)
        seen.add(item)
    if duplicates:
        raise SimError(
            "the regrade selection repeats corner id(s) "
            f"{', '.join(duplicates)}: each affected unit is dispatched once, "
            "so a duplicate is a checker bug, not a wider re-run"
        )
    return list(ids)


def validate_regrade_field(value: Any) -> dict[str, Any]:
    """Validate the internal ``request["_regrade"]`` document :func:`run_regrade`
    writes (and an off-host worker re-reads). Exact keys; ``tmax`` and
    ``ngspice_options`` optional."""
    if not isinstance(value, dict):
        raise SimError(f"{REGRADE_REQUEST_FIELD} must be an object")
    unknown = sorted(set(value) - set(_REGRADE_FIELD_KEYS))
    if unknown:
        raise SimError(
            f"{REGRADE_REQUEST_FIELD} has unknown key(s): {', '.join(unknown)} "
            f"(known: {', '.join(_REGRADE_FIELD_KEYS)})"
        )
    if "select_corner_ids" not in value:
        raise SimError(f"{REGRADE_REQUEST_FIELD}.select_corner_ids is required")
    ids = _validate_selection(value["select_corner_ids"])
    tmax = value.get("tmax")
    if tmax is not None and (not isinstance(tmax, str) or not tmax.strip()):
        raise SimError(f"{REGRADE_REQUEST_FIELD}.tmax must be a non-empty string")
    solver = value.get("ngspice_options")
    if solver is not None and not isinstance(solver, dict):
        raise SimError(f"{REGRADE_REQUEST_FIELD}.ngspice_options must be an object")
    return {
        "select_corner_ids": ids,
        "tmax": tmax,
        "ngspice_options": dict(solver or {}),
    }


def prepare_regrade(
    corner_points: list[Any],
    request: dict[str, Any],
    analysis: dict[str, Any],
    engine: str,
) -> list[Any]:
    """The ``sim.py`` seam: validate ``options.tightened_rerun`` (every run,
    so a malformed declaration is refused before the first pass dispatches
    anything) and, when the request carries :data:`REGRADE_REQUEST_FIELD`,
    filter ``corner_points`` down to the selected units and tighten them.

    Runs after the request's own corner/Monte Carlo expansion and before any
    dispatch, on the submitting client *and* on an off-host worker (both read
    the same request document, so both select the same units with the same
    seeds). Unknown ids are refused here -- before anything is simulated,
    uploaded or launched. ``tmax`` rewrites each selected point's
    ``analysis_args`` (never its identity); tightened solver options ride the
    derived request's merged ``options.ngspice_options`` instead, and a
    selected point whose swept ``corners.options`` axis already varies one of
    the tightened names is refused -- the sweep and the regrade would be
    racing to set the same solver option.
    """
    from . import sim_analysis_overrides as _sao
    from . import sim_steps

    # Validated on every run (not just a regrade's), so a malformed
    # declaration is refused before the first pass dispatches anything.
    validate_tightened_rerun(
        (request.get("options") or {}).get(TIGHTENED_RERUN_OPTION),
        analysis=analysis,
        engine=engine,
        steps=sim_steps.is_steps_analysis(analysis),
    )
    spec = request.get(REGRADE_REQUEST_FIELD)
    if spec is None:
        return corner_points
    spec = validate_regrade_field(spec)
    # Non-default Monte Carlo seeds and scales travel as resolved points.
    # The worker must receive the entire ordered selection before tightening.
    if (
        request.get("_explicit_points") is not None
        and [p.corner_id for p in corner_points] != spec["select_corner_ids"]
    ):
        raise SimError(
            "the explicit worker points do not match the regrade selection: "
            "their corner IDs must match select_corner_ids exactly and in order"
        )
    by_id: dict[str, Any] = {}
    for point in corner_points:
        by_id.setdefault(point.corner_id, point)
    missing = [i for i in spec["select_corner_ids"] if i not in by_id]
    if missing:
        raise SimError(
            "the regrade selection names corner id(s) absent from this request's "
            f"expansion: {', '.join(missing)} (the request expands to "
            f"{len(corner_points)} unit(s); the ids come from the source "
            "report's corners[].corner_id and must match this request)"
        )
    selected = [by_id[i] for i in spec["select_corner_ids"]]
    clash = sorted(
        {name for p in selected for name in (p.options or {})}
        & set(spec["ngspice_options"])
    )
    if clash:
        raise SimError(
            "the swept corners.options axis already sets "
            f"{', '.join(clash)} on a selected unit, so the regrade cannot "
            f"tighten it: drop the name from the sweep or from options."
            f"{TIGHTENED_RERUN_OPTION}"
        )
    if spec["tmax"] is not None:
        for point in selected:
            point.analysis_args = tighten_tran_args(
                _sao.effective_analysis_args(point, analysis), spec["tmax"]
            )
    return selected


def require_regrade_echo(
    request: dict[str, Any],
    points: list[Any],
    corners: list[dict[str, Any]],
    backend: str,
) -> None:
    """Refuse an off-host worker that did not apply the selection: a runner
    built before this feature ignores the internal field and runs the whole
    matrix. The returned corner ids must match the selected units exactly."""
    if request.get(REGRADE_REQUEST_FIELD) is None:
        return
    expected = [p.corner_id for p in points]
    actual = [c.get("corner_id") for c in corners if isinstance(c, dict)]
    if actual == expected:
        return
    if len(actual) != len(expected):
        problem = (
            f"it returned {len(actual)} corner report(s) for the {len(expected)} "
            "selected unit(s)"
        )
    else:
        index = next(i for i, cid in enumerate(actual) if cid != expected[i])
        problem = (
            f"at corners[{index}] the client expected {expected[index]!r} "
            f"and received {actual[index]!r}"
        )
    raise SimError(
        f"the {backend} worker did not apply the regrade selection -- {problem}. "
        "Check that the runner image matches this klt release. No result was reported",
        code=REGRADE_UNSUPPORTED_WORKER_CODE,
    )


def regrade_environment_entries(
    request: dict[str, Any],
    analysis: dict[str, Any],
    engine: str,
) -> dict[str, Any]:
    """The additive ``environment`` entries of a run whose request declared
    tightened-rerun settings (``environment.tightened_rerun``, the resolved
    declaration, present in *both* passes so the first report already states
    what a regrade would use) and/or carried a live selection
    (``environment.regrade``: the selected ids and the settings the re-run
    applied). Purely derived from the request document, so the re-run's own
    report echoes what the worker received."""
    from . import sim_steps

    entries: dict[str, Any] = {}
    declaration = validate_tightened_rerun(
        (request.get("options") or {}).get(TIGHTENED_RERUN_OPTION),
        analysis=analysis,
        engine=engine,
        steps=sim_steps.is_steps_analysis(analysis),
    )
    if declaration is not None:
        entries["tightened_rerun"] = declaration
    if request.get(REGRADE_REQUEST_FIELD) is not None:
        entries["regrade"] = validate_regrade_field(request[REGRADE_REQUEST_FIELD])
    return entries
