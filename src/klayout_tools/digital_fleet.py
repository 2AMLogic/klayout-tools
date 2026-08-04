"""``klayout_tools.digital_fleet`` -- digital-candidate extensions to Epic
#375's fleet scheduler (`remote_fleet.py`/`remote_launcher.py`/
`remote_transport.py`), per Epic #391 Phase 6 (issue #445) and the Phase 1
decision record `docs/design/digital-fleet-unit-abstraction-decision.md`
(issue #400).

This module never modifies `remote_fleet.py`/`remote_launcher.py`/
`remote_transport.py` -- everything here builds *beside* them, plugging
into `remote_fleet.FleetLauncher`'s/`run_fleet`'s already-generic
``ShardRunner`` contract (``(shard_index, launcher, public_ip) -> Any``)
and `remote_transport.JobDescription`'s already-generic push/run/pull
contract, exactly as both modules' own docstrings anticipate for "a future
`extract`/`lvs`/DRC remote backend" (here, a digital synth+P&R backend).
Confirmed against the merged implementation, not assumed: see the decision
record's "Grounding" section.

Per the decision record's Question 1, the unit of parallelism here is **one
candidate evaluation** -- one complete synthesis(+verification)+
place-and-route pipeline run at one design-space point (a
synthesis-strategy / floorplan / P&R-seed combination) -- never one
pipeline stage, and never "one corner" by analogy to `klt sim`'s own fleet
use. Pipeline stages within one candidate stay serial (synthesis before
place-and-route); parallelism is strictly *across* candidates.

What this module provides (the two new pieces the decision record's
Question 2 table names, plus enough plumbing to exercise both end to end
against the unmodified scheduler):

1. **Digital-specific instance sizing**
   (:func:`select_digital_instance_type`, :func:`digital_fleet_sizing`) --
   replaces the corner-shaped ``unit_count * threads_per_corner`` formula.
   A digital candidate wants ~1 whole host (``m ~= 1``), never several
   packed per host, so sizing is keyed off a fixed/configurable instance
   tier per PDK (or an explicit design-size-proxy override), never "how
   many units are on this shard." :func:`digital_fleet_sizing` derives the
   ``(shard_unit_counts, threads_per_corner)`` pair that makes
   :class:`~klayout_tools.remote_fleet.FleetLauncher`'s own **unmodified**
   ``remote_launcher.select_instance_type(max(shard_unit_counts),
   threads_per_corner)`` call resolve to exactly that tier -- reuse of the
   existing ladder/headroom/cost-table machinery, not a second
   implementation of it.

2. **A digital-specific ``JobDescription`` builder**
   (:func:`build_digital_job_description`) -- uploads a candidate's RTL
   sources (plus a functional-verification testbench, when given) and runs
   ``klt synthesize`` -> [``klt functional-verification``] ->
   ``klt place-and-route`` composed as one ``klt eval`` descriptor (issue
   #387), so the pipeline's gate/objective/metrics envelope is produced by
   the existing `eval.py` orchestration -- never reimplemented here.

3. **A concrete ``ShardRunner``** (:func:`run_digital_candidate`,
   :func:`make_digital_shard_runner`) that threads 1-2 together against
   `remote_transport`'s push/run/pull/cleanup functions, the same way
   `sim.py`'s ``_run_remote`` does for a single SPICE host -- proving the
   sizing function and the ``JobDescription`` builder genuinely compose
   with `remote_fleet.run_fleet`'s unmodified ``ShardRunner`` contract, not
   just in theory.

4. **A candidate-ranking merge step**
   (:func:`merge_candidate_results`, :func:`rank_candidates`) -- flattens
   every shard's ``ShardOutcome`` (as returned by item 3's ``ShardRunner``)
   into one :class:`CandidateResult` per candidate and ranks the scoreable
   ones by their declared ``objective`` (#387), by declared polarity,
   without silently dropping an errored/unscoreable candidate.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from . import remote_fleet, remote_transport
from .remote_launcher import (
    RemoteLauncher,
    RemoteLaunchError,
    instance_vcpu_count,
    select_instance_type,
)


class DigitalFleetError(Exception):
    """Raised for anything that prevents a digital candidate's sizing,
    ``JobDescription``, or ranking from being produced at all -- a bad
    candidate description, a remote-name collision between two uploaded
    sources, a shard/candidate count mismatch when merging results, or a
    transport failure while running one candidate. Mirrors
    ``remote_launcher.RemoteLaunchError``'s "fails loudly, never a silent
    default" posture, but is intentionally its own type -- this module
    never provisions anything itself (that stays
    :class:`~klayout_tools.remote_fleet.FleetLauncher`'s job), so there is
    no reason for a caller to catch it alongside a provisioning error.
    """


# --------------------------------------------------------------------------- #
# 1. Digital-specific instance sizing (decision record Question 2, row 1)
# --------------------------------------------------------------------------- #

#: A digital candidate's default instance tier, per supported PDK -- a
#: single whole-host allocation (``m ~= 1``, decision record Question 1),
#: not scaled by how many candidates a shard happens to run. Deliberately a
#: mid-ladder ``c7i`` size (16 vCPU): generous enough for OpenROAD's own
#: internal multi-threading (global placement, timing analysis, detailed
#: routing) on a small-to-medium sky130/gf180mcu block without guessing at
#: a design-size proxy that does not exist yet (no design has been
#: synthesized before this sizing call runs -- see
#: :func:`select_digital_instance_type`'s docstring). A documented
#: estimate, not a measurement -- expected to be recalibrated once a first
#: live digital fleet run exists (decision record Question 3, point 3).
DEFAULT_DIGITAL_INSTANCE_TIER_BY_PDK: dict[str, str] = {
    "sky130A": "c7i.4xlarge",
    "gf180mcu": "c7i.4xlarge",
}

#: Fallback tier for a PDK with no entry in
#: :data:`DEFAULT_DIGITAL_INSTANCE_TIER_BY_PDK` and no ``tier_overrides``
#: entry -- same value as every PDK above today, kept as an independent
#: constant so widening the per-PDK table later does not silently change
#: this fallback too.
DEFAULT_DIGITAL_INSTANCE_TIER = "c7i.4xlarge"

#: Design-size-proxy escalation ladder: ``(max_instance_count_inclusive,
#: instance_type)``, ascending. A caller with an actual size proxy in hand
#: (e.g. a previous synthesis run's own ``instance_count``, when sizing a
#: *second* candidate for the same design -- never available for a design's
#: first candidate, since that is exactly what synthesis has not run yet)
#: can bump the tier up the same ``c7i`` ladder ``remote_launcher`` already
#: knows about, instead of the fixed per-PDK default. Numbers are a
#: starting, documented estimate (mirroring the SPICE spike's own
#: "documented estimate, not a measurement" caveat) -- not yet calibrated
#: against a live digital fleet run.
_DESIGN_SIZE_TIER_LADDER: tuple[tuple[int, str], ...] = (
    (5_000, "c7i.2xlarge"),
    (20_000, "c7i.4xlarge"),
    (100_000, "c7i.8xlarge"),
    (400_000, "c7i.12xlarge"),
)

#: Tier used for a ``design_size_hint`` larger than every
#: :data:`_DESIGN_SIZE_TIER_LADDER` entry -- the largest ``c7i`` size this
#: module defaults to reaching for automatically; a design bigger than this
#: should pass an explicit ``tier_overrides`` entry instead of relying on
#: escalation.
_DESIGN_SIZE_TIER_CEILING = "c7i.16xlarge"


def select_digital_instance_type(
    *,
    pdk: str,
    design_size_hint: int | None = None,
    tier_overrides: Mapping[str, str] | None = None,
) -> str:
    """Return the ``c7i`` instance type one digital candidate should be
    sized for -- **not** the corner-shaped ``unit_count * threads_per_unit``
    formula ``remote_launcher.select_instance_type`` implements for SPICE.

    Two independent selection modes, per the decision record's "keyed off a
    design-size proxy, or a fixed/configurable instance tier per PDK":

    - ``design_size_hint`` given (a caller-supplied size proxy, e.g. a
      previous candidate's own ``instance_count`` from `klt synthesize`'s
      report -- there is no size proxy available before a design's *first*
      candidate has ever been synthesized) walks
      :data:`_DESIGN_SIZE_TIER_LADDER` and returns the smallest tier that
      fits, or :data:`_DESIGN_SIZE_TIER_CEILING` beyond the ladder's last
      entry.
    - ``design_size_hint`` omitted (the common case, including every
      design's first candidate) returns ``tier_overrides[pdk]`` when given,
      else :data:`DEFAULT_DIGITAL_INSTANCE_TIER_BY_PDK[pdk]`, else
      :data:`DEFAULT_DIGITAL_INSTANCE_TIER`.

    Raises :class:`DigitalFleetError` for a non-positive
    ``design_size_hint``.
    """
    if design_size_hint is not None:
        if design_size_hint < 1:
            raise DigitalFleetError("design_size_hint must be a positive integer")
        for ceiling, instance_type in _DESIGN_SIZE_TIER_LADDER:
            if design_size_hint <= ceiling:
                return instance_type
        return _DESIGN_SIZE_TIER_CEILING

    overrides = tier_overrides or {}
    if pdk in overrides:
        return overrides[pdk]
    return DEFAULT_DIGITAL_INSTANCE_TIER_BY_PDK.get(pdk, DEFAULT_DIGITAL_INSTANCE_TIER)


def _threads_per_corner_for_target(target_instance_type: str) -> int:
    """Reverse-derive a ``threads_per_corner`` value that makes
    ``remote_launcher.select_instance_type(1, threads_per_corner)`` --
    called **unmodified**, with its own default ~20% headroom -- resolve to
    exactly ``target_instance_type``.

    This is how :func:`digital_fleet_sizing` hands
    :class:`~klayout_tools.remote_fleet.FleetLauncher` a
    ``(shard_unit_counts, threads_per_corner)`` pair that reuses its
    existing, unmodified sizing call
    (``select_instance_type(max(shard_unit_counts), threads_per_corner)``)
    without ``remote_fleet.py`` itself knowing anything changed: pinning
    ``shard_unit_counts`` to ``1`` per shard (``m ~= 1``, decision record
    Question 1) and choosing ``threads_per_corner`` so the *existing*
    ladder-with-headroom lookup lands on the digital-specific tier this
    module already decided on.

    Searches downward from ``target_instance_type``'s own vCPU count
    (monotonic: fewer "threads" never selects a *larger* instance) rather
    than trusting one closed-form formula, so this stays correct even if
    ``remote_launcher``'s ladder or headroom constant changes shape later.
    At the very top of the ladder (``target_instance_type`` is the largest
    known ``c7i`` size), ``threads_per_corner == target_vcpu`` itself pushes
    the headroom-adjusted requirement *past* every known size --
    ``select_instance_type`` raises rather than falling back to a smaller
    one -- so that raise is treated as "not a match, keep searching
    downward" here, same as an outright wrong tier.

    Raises :class:`DigitalFleetError` if no such value exists (would only
    happen for a ``target_instance_type`` unreachable via any positive
    ``threads_per_corner``, e.g. an instance type outside the known ladder
    -- :func:`~klayout_tools.remote_launcher.instance_vcpu_count` already
    raises for that case).
    """
    target_vcpu = instance_vcpu_count(target_instance_type)
    for threads in range(target_vcpu, 0, -1):
        try:
            selected = select_instance_type(1, threads)
        except RemoteLaunchError:
            continue
        if selected == target_instance_type:
            return threads
    raise DigitalFleetError(
        f"could not derive a threads_per_corner value that makes "
        f"remote_launcher.select_instance_type(1, ...) resolve to "
        f"{target_instance_type!r}"
    )


def digital_fleet_sizing(
    *,
    hosts: int,
    pdk: str,
    design_size_hint: int | None = None,
    tier_overrides: Mapping[str, str] | None = None,
) -> tuple[list[int], int]:
    """Return ``(shard_unit_counts, threads_per_corner)`` ready to pass
    straight through to
    :class:`~klayout_tools.remote_fleet.FleetLauncher`/
    :func:`~klayout_tools.remote_fleet.run_fleet` **unchanged** as their own
    ``shard_unit_counts``/``threads_per_corner`` constructor arguments.

    ``shard_unit_counts`` is always ``[1] * hosts`` -- per the decision
    record, a digital candidate wants ~1 whole host regardless of how many
    candidates a shard ends up evaluating serially (``m ~= 1``; a shard
    running several candidates one after another via
    :func:`make_digital_shard_runner` never needs more than one candidate's
    own compute footprint *at a time*, unlike SPICE's several-corners-
    concurrently-per-host packing). ``threads_per_corner`` is derived by
    :func:`_threads_per_corner_for_target` from
    :func:`select_digital_instance_type`'s chosen tier, so
    ``FleetLauncher.__init__``'s own unmodified
    ``select_instance_type(max(shard_unit_counts), threads_per_corner)``
    call resolves to exactly that tier for every fleet member.

    Raises :class:`DigitalFleetError` for a non-positive ``hosts``.
    """
    if hosts < 1:
        raise DigitalFleetError("hosts must be a positive integer")
    instance_type = select_digital_instance_type(
        pdk=pdk, design_size_hint=design_size_hint, tier_overrides=tier_overrides
    )
    threads_per_corner = _threads_per_corner_for_target(instance_type)
    return [1] * hosts, threads_per_corner


# --------------------------------------------------------------------------- #
# 2. Digital candidate description + JobDescription builder
# --------------------------------------------------------------------------- #

#: `klt eval`'s own exit codes that mean "the candidate was evaluated to
#: completion and produced a report with a real `objective` value" -- 0
#: (every gate passed) and 3 (at least one gate failed, still scoreable).
#: Only the crash/usage exit codes (1/2) mean no report exists to parse --
#: see `docs/cli/eval.md`'s exit-code table and issue #387's "an optimizer
#: must never read exit 1 (crash) as exit 3 (a bad, but real, score)".
DIGITAL_CANDIDATE_SUCCESS_EXIT_CODES: tuple[int, ...] = (0, 3)

#: Job-relative directory `klt synthesize`/`klt place-and-route` write
#: debuggable artifacts under (``.klt/synthesize``/``.klt/place-and-route``
#: -- see each verb's own module docstring); ``.klt`` is their shared
#: parent, so one `pull_artifacts` call collects both stages' artifacts in
#: one pass.
DIGITAL_JOB_ARTIFACTS_RELATIVE_DIR = ".klt"

SYNTHESIZE_REQUEST_FILENAME = "synthesize_request.json"
PLACE_AND_ROUTE_REQUEST_FILENAME = "place_and_route_request.json"
FUNCTIONAL_VERIFICATION_REQUEST_FILENAME = "functional_verification_request.json"
EVAL_DESCRIPTOR_FILENAME = "eval_descriptor.json"

#: `klt eval` check name -> the request filename :func:`build_digital_job_description`
#: pushes for it -- used to build the ``args`` for both a gate/metric entry
#: and the descriptor's own ``objective``.
_REQUEST_FILENAME_BY_CHECK: dict[str, str] = {
    "synthesize": SYNTHESIZE_REQUEST_FILENAME,
    "place-and-route": PLACE_AND_ROUTE_REQUEST_FILENAME,
    "functional-verification": FUNCTIONAL_VERIFICATION_REQUEST_FILENAME,
}


@dataclass(frozen=True)
class DigitalVerification:
    """One `klt functional-verification` step, run between synthesis and
    place-and-route (Epic #391 Phase 3) -- optional, per the decision
    record's "one complete synthesis(+verification)+place-and-route
    pipeline run" unit definition (verification is bracketed, "+
    [verification]+", in both the epic and this issue).

    ``testbench_source`` is the local path to the cocotb testbench Python
    module named by ``testbench_module`` --
    `functional_verification.py`'s own ``_resolve_testbench`` convention is
    that the module's file lives next to the request, so this builder
    pushes it to ``<testbench_module>.py`` at the job root, alongside the
    generated request itself.

    ``sources`` defaults to the candidate's own ``rtl_sources`` when
    omitted (the common case: verify the same RTL synthesis will run).
    """

    hdl_toplevel: str
    testbench_module: str
    testbench_source: str
    sources: tuple[str, ...] | None = None
    testcase: str | list[str] | None = None
    simulator: str | None = None
    request_overrides: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class DigitalCandidate:
    """One design-space point to evaluate -- a synthesis-strategy /
    floorplan / P&R-seed combination -- the unit of parallelism the
    decision record's Question 1 names ("one candidate evaluation").

    ``candidate_id`` is an opaque bookkeeping label only (fleet reports,
    ranking output, error messages, and the per-candidate remote job
    subdirectory name) -- it is never interpreted as anything but a string
    key, and never reaches the remote host as more than a directory name.

    ``objective_check``/``objective_metric``/``objective_polarity`` declare
    the `klt eval` descriptor's own ``objective`` (#387) --
    :func:`build_digital_job_description` never hardcodes what a candidate
    is scored on, matching `eval.py`'s own "descriptor-driven, never
    hardcoded" design. ``objective_check`` must name a check this candidate
    actually runs (``"synthesize"``/``"place-and-route"``, or
    ``"functional-verification"`` only when ``verification`` is given).
    """

    candidate_id: str
    rtl_sources: tuple[str, ...]
    hdl_toplevel: str
    pdk: dict[str, Any]
    floorplan: dict[str, Any]
    seed: int
    io: dict[str, Any] | None = None
    synth_constraints: dict[str, Any] | None = None
    synth_engine: str | None = None
    pr_constraints: dict[str, Any] | None = None
    pr_engine: str | None = None
    target_stage: str = "route"
    verification: DigitalVerification | None = None
    objective_check: str = "place-and-route"
    objective_metric: str = "die_area_um2"
    objective_polarity: str = "minimize"
    objective_name: str | None = None
    extra_gates: tuple[dict[str, Any], ...] = ()
    extra_metrics: tuple[dict[str, Any], ...] = ()


def _push_unique_sources(
    sources: Sequence[str],
    inputs: list[remote_transport.JobInput],
    *,
    label: str,
) -> list[str]:
    """Push every entry of ``sources`` as a :class:`~remote_transport.JobInput`
    named by its own basename, and return the resulting job-relative
    filenames in the same order as ``sources`` (a request's own ``sources``
    array is built directly from this return value).

    The same local path appearing twice in ``sources`` is pushed once and
    referenced twice; two *different* local paths that happen to share a
    basename raise :class:`DigitalFleetError` naming both -- every source in
    one candidate's job directory must resolve to a distinct remote
    filename.
    """
    remote_name_by_path: dict[str, str] = {}
    local_path_by_remote_name: dict[str, str] = {}
    remote_names: list[str] = []
    for local_path in sources:
        existing = remote_name_by_path.get(local_path)
        if existing is not None:
            remote_names.append(existing)
            continue
        remote_name = os.path.basename(local_path)
        collision = local_path_by_remote_name.get(remote_name)
        if collision is not None:
            raise DigitalFleetError(
                f"two different {label} source paths both resolve to remote "
                f"filename {remote_name!r} ({collision!r} and {local_path!r}) "
                "-- rename one to avoid a remote collision"
            )
        local_path_by_remote_name[remote_name] = local_path
        remote_name_by_path[local_path] = remote_name
        inputs.append(
            remote_transport.JobInput(
                remote_name=remote_name,
                label=f"{label} {remote_name}",
                local_path=local_path,
            )
        )
        remote_names.append(remote_name)
    return remote_names


def _request_args_for_check(check: str) -> dict[str, str]:
    request_filename = _REQUEST_FILENAME_BY_CHECK.get(check)
    if request_filename is None:
        raise DigitalFleetError(
            f"objective_check/extra gate check must be one of "
            f"{sorted(_REQUEST_FILENAME_BY_CHECK)}, got {check!r}"
        )
    return {"request": request_filename}


def build_digital_job_description(
    candidate: DigitalCandidate, *, keep_artifacts: bool = False
) -> remote_transport.JobDescription:
    """Build one digital candidate's `klt eval` job as a generic
    :class:`remote_transport.JobDescription` (issue #278, Epic #253 Phase
    3) -- `sim.py`'s ``_build_remote_job_description`` role, for this
    epic's second adopter (decision record Question 2, "reusable for
    digital as-is": ``JobDescription``/push-run-pull, unchanged).

    Uploads the candidate's RTL sources (and, when ``candidate.verification``
    is given, its own sources plus the testbench module) and generates
    three-to-four request documents at the job root:
    :data:`SYNTHESIZE_REQUEST_FILENAME`,
    :data:`PLACE_AND_ROUTE_REQUEST_FILENAME` (its ``netlist`` field points
    at synthesis's own deterministic output path,
    ``.klt/synthesize/<hdl_toplevel>_synth.v`` -- computed here, never read
    back at runtime, so no ``${...}`` candidate-substitution step is needed
    to chain synthesis's output into place-and-route's input),
    :data:`FUNCTIONAL_VERIFICATION_REQUEST_FILENAME` (only when
    ``candidate.verification`` is given), and
    :data:`EVAL_DESCRIPTOR_FILENAME` (composing all of the above into one
    `klt eval` gate/objective/metrics descriptor, per #387).

    The remote command is a single ``klt eval ... --format json`` call --
    `eval.py`'s own per-check response cache means the objective's ``check``
    is never re-run just because it also appears as a gate or a metrics
    entry with the same ``args`` (see ``eval.run_eval``'s ``cache``).

    Raises :class:`DigitalFleetError` for an empty ``rtl_sources``, a
    remote-name collision between two different local source paths, or an
    ``objective_check``/``extra_gates`` check naming
    ``"functional-verification"`` without ``candidate.verification`` given.
    """
    if not candidate.rtl_sources:
        raise DigitalFleetError("candidate.rtl_sources must be a non-empty sequence")
    if (
        candidate.objective_check == "functional-verification"
        and candidate.verification is None
    ):
        raise DigitalFleetError(
            "objective_check='functional-verification' requires "
            "candidate.verification to be given"
        )

    inputs: list[remote_transport.JobInput] = []
    rtl_remote_names = _push_unique_sources(candidate.rtl_sources, inputs, label="rtl")

    synth_request: dict[str, Any] = {
        "sources": rtl_remote_names,
        "hdl_toplevel": candidate.hdl_toplevel,
        "pdk": candidate.pdk,
    }
    if candidate.synth_engine is not None:
        synth_request["engine"] = candidate.synth_engine
    if candidate.synth_constraints is not None:
        synth_request["constraints"] = candidate.synth_constraints
    inputs.append(
        remote_transport.JobInput(
            remote_name=SYNTHESIZE_REQUEST_FILENAME,
            label="synthesize request",
            content=json.dumps(synth_request),
        )
    )

    # synthesize.run_synthesize's own deterministic output path convention
    # (<request_dir>/.klt/synthesize/<hdl_toplevel>_synth.v) -- both request
    # files land at the job directory root, so this job-relative path is
    # correct for place-and-route's own request without any runtime
    # substitution step.
    synth_netlist_relpath = f".klt/synthesize/{candidate.hdl_toplevel}_synth.v"

    pr_request: dict[str, Any] = {
        "netlist": synth_netlist_relpath,
        "hdl_toplevel": candidate.hdl_toplevel,
        "pdk": candidate.pdk,
        "floorplan": candidate.floorplan,
        "seed": candidate.seed,
        "target_stage": candidate.target_stage,
    }
    if candidate.io is not None:
        pr_request["io"] = candidate.io
    if candidate.pr_constraints is not None:
        pr_request["constraints"] = candidate.pr_constraints
    if candidate.pr_engine is not None:
        pr_request["engine"] = candidate.pr_engine
    inputs.append(
        remote_transport.JobInput(
            remote_name=PLACE_AND_ROUTE_REQUEST_FILENAME,
            label="place-and-route request",
            content=json.dumps(pr_request),
        )
    )

    gates: list[dict[str, Any]] = [
        {
            "check": "place-and-route",
            "name": "place-and-route",
            "args": {"request": PLACE_AND_ROUTE_REQUEST_FILENAME},
            "threshold": {"metric": "stage_reached", "equals": candidate.target_stage},
        },
    ]
    metrics: list[dict[str, Any]] = [
        {
            "name": "synthesize",
            "check": "synthesize",
            "args": {"request": SYNTHESIZE_REQUEST_FILENAME},
        },
    ]

    if candidate.verification is not None:
        verification = candidate.verification
        verification_sources = verification.sources or candidate.rtl_sources
        verification_remote_names = _push_unique_sources(
            verification_sources, inputs, label="verification rtl"
        )
        inputs.append(
            remote_transport.JobInput(
                remote_name=f"{verification.testbench_module}.py",
                label="testbench",
                local_path=verification.testbench_source,
            )
        )
        functional_verification_request: dict[str, Any] = {
            "sources": verification_remote_names,
            "hdl_toplevel": verification.hdl_toplevel,
            "testbench": {"module": verification.testbench_module},
        }
        if verification.testcase is not None:
            functional_verification_request["testbench"]["testcase"] = (
                verification.testcase
            )
        if verification.simulator is not None:
            functional_verification_request["simulator"] = verification.simulator
        functional_verification_request.update(verification.request_overrides)
        inputs.append(
            remote_transport.JobInput(
                remote_name=FUNCTIONAL_VERIFICATION_REQUEST_FILENAME,
                label="functional-verification request",
                content=json.dumps(functional_verification_request),
            )
        )
        gates.insert(
            0,
            {
                "check": "functional-verification",
                "name": "functional-verification",
                "args": {"request": FUNCTIONAL_VERIFICATION_REQUEST_FILENAME},
            },
        )

    gates.extend(candidate.extra_gates)
    metrics.extend(candidate.extra_metrics)

    objective: dict[str, Any] = {
        "check": candidate.objective_check,
        "metric": candidate.objective_metric,
        "polarity": candidate.objective_polarity,
        "args": _request_args_for_check(candidate.objective_check),
    }
    if candidate.objective_name is not None:
        objective["name"] = candidate.objective_name

    descriptor = {"gates": gates, "objective": objective, "metrics": metrics}
    inputs.append(
        remote_transport.JobInput(
            remote_name=EVAL_DESCRIPTOR_FILENAME,
            label="eval descriptor",
            content=json.dumps(descriptor),
        )
    )

    return remote_transport.JobDescription(
        label=f"klt eval ({candidate.candidate_id})",
        inputs=tuple(inputs),
        command=f"klt eval {EVAL_DESCRIPTOR_FILENAME} --format json",
        success_exit_codes=DIGITAL_CANDIDATE_SUCCESS_EXIT_CODES,
        artifacts_relative_dir=(
            DIGITAL_JOB_ARTIFACTS_RELATIVE_DIR if keep_artifacts else None
        ),
    )


# --------------------------------------------------------------------------- #
# 3. A concrete ShardRunner: push/run/(pull)/cleanup one or more candidates
#    per shard, over the same transport `sim.py`'s single-host `_run_remote`
#    uses.
# --------------------------------------------------------------------------- #

#: Overall SSH-command timeout for one candidate's remote `klt eval`
#: invocation. Per the decision record's Question 3, a single synth+P&R
#: candidate's own compute time is realistically "minutes to hours
#: depending on design size" (``t >> T_o``, unlike SPICE) -- 4 hours is a
#: conservative first default, not a measured bound (no measured digital
#: ``t`` exists yet; see the decision record's point 3). Overridable per
#: call.
DEFAULT_DIGITAL_RUN_TIMEOUT_S = 4 * 3600.0

#: Same SSH-readiness budget `sim._run_remote` uses by default, reused here
#: for consistency rather than re-deriving an independent constant.
DEFAULT_DIGITAL_SSH_READY_TIMEOUT_S = 600.0
DEFAULT_DIGITAL_SSH_POLL_INTERVAL_S = 5.0


def run_digital_candidate(
    candidate: DigitalCandidate,
    launcher: RemoteLauncher,
    public_ip: str,
    *,
    ssh_user: str,
    ssh_key_path: str,
    keep_artifacts: bool = False,
    artifacts_dir: str | None = None,
    run_timeout_s: float = DEFAULT_DIGITAL_RUN_TIMEOUT_S,
    ssh_ready_timeout_s: float = DEFAULT_DIGITAL_SSH_READY_TIMEOUT_S,
    ssh_poll_interval_s: float = DEFAULT_DIGITAL_SSH_POLL_INTERVAL_S,
    wait_for_ssh: bool = True,
) -> dict[str, Any]:
    """Run one digital candidate's full synthesis-\\>[verification]-\\>P&R
    pipeline on an already-provisioned fleet member (``launcher``,
    ``public_ip`` -- exactly what
    :data:`~klayout_tools.remote_fleet.ShardRunner` hands its callable),
    via the same push/run/(pull)/cleanup transport `sim.py`'s single-host
    ``_run_remote`` uses. Returns the `klt eval` envelope (issue #387)
    parsed from the remote command's stdout.

    ``wait_for_ssh=False`` lets a caller already past SSH-readiness for
    this host (a second-or-later candidate on the same shard, run by
    :func:`make_digital_shard_runner`) skip the redundant wait.

    Raises :class:`DigitalFleetError` for a missing ``artifacts_dir`` when
    ``keep_artifacts=True``, or wrapping any
    :class:`~klayout_tools.remote_transport.RemoteTransportError` the
    transport raises (SSH/SCP failure, a non-success exit code, unparseable
    stdout). Never catches or retries internally -- see
    :func:`make_digital_shard_runner`'s docstring on why a raised exception
    here is deliberately left to propagate.
    """
    if keep_artifacts and artifacts_dir is None:
        raise DigitalFleetError("artifacts_dir is required when keep_artifacts=True")

    if wait_for_ssh:
        remote_transport.wait_for_ssh(
            public_ip,
            user=ssh_user,
            identity_file=ssh_key_path,
            timeout_s=ssh_ready_timeout_s,
            poll_interval_s=ssh_poll_interval_s,
        )

    job = build_digital_job_description(candidate, keep_artifacts=keep_artifacts)
    shard_job_dir = remote_transport.job_dir(ssh_user, launcher.job_id)
    remote_job_dir = f"{shard_job_dir}/{candidate.candidate_id}"

    try:
        remote_transport.push_job(
            host=public_ip,
            user=ssh_user,
            identity_file=ssh_key_path,
            remote_job_dir=remote_job_dir,
            job=job,
        )
        report = remote_transport.run_remote_job(
            host=public_ip,
            user=ssh_user,
            identity_file=ssh_key_path,
            remote_job_dir=remote_job_dir,
            job=job,
            timeout_s=run_timeout_s,
        )
        if keep_artifacts:
            assert artifacts_dir is not None  # validated above
            remote_transport.pull_artifacts(
                host=public_ip,
                user=ssh_user,
                identity_file=ssh_key_path,
                remote_job_dir=remote_job_dir,
                local_artifacts_dir=os.path.join(artifacts_dir, candidate.candidate_id),
                job=job,
            )
        remote_transport.cleanup_job(
            host=public_ip,
            user=ssh_user,
            identity_file=ssh_key_path,
            remote_job_dir=remote_job_dir,
        )
    except remote_transport.RemoteTransportError as exc:
        raise DigitalFleetError(
            f"candidate {candidate.candidate_id!r} failed: {exc}"
        ) from exc

    if not isinstance(report, dict):
        raise DigitalFleetError(
            f"candidate {candidate.candidate_id!r}: remote 'klt eval' did not "
            "return a JSON object"
        )
    return report


def make_digital_shard_runner(
    candidates_by_shard: Sequence[Sequence[DigitalCandidate]],
    *,
    ssh_user: str,
    ssh_key_path: str,
    keep_artifacts: bool = False,
    artifacts_dir: str | None = None,
    run_timeout_s: float = DEFAULT_DIGITAL_RUN_TIMEOUT_S,
    ssh_ready_timeout_s: float = DEFAULT_DIGITAL_SSH_READY_TIMEOUT_S,
    ssh_poll_interval_s: float = DEFAULT_DIGITAL_SSH_POLL_INTERVAL_S,
) -> remote_fleet.ShardRunner:
    """Build a :data:`~klayout_tools.remote_fleet.ShardRunner`
    (``(shard_index, launcher, public_ip) -> Any``) that runs every
    candidate in ``candidates_by_shard[shard_index]`` on that shard's own
    instance, serially -- the decision record's sizing note made concrete:
    "a caller wanting more candidates than available hosts can already
    express 'run several candidates sequentially on this shard' inside its
    own ``ShardRunner`` callable, with no ``FleetLauncher`` change
    required." Returns a ``list[dict]``, one `klt eval` envelope per
    candidate, in ``candidates_by_shard[shard_index]``'s own order --
    :func:`merge_candidate_results`'s expected shape.

    Any candidate's own transport failure propagates out of this callable
    unmodified, which is deliberate:
    :class:`~klayout_tools.remote_fleet.FleetLauncher`'s own
    one-retry-per-shard policy treats "this shard's run raised" as
    "relaunch and retry the *whole* shard" (Epic #375 decision 2) -- there
    is no independent per-candidate retry inside one shard. A shard that
    is still lost after its one retry is a fully errored shard, and
    :func:`merge_candidate_results` reports every candidate that shard was
    assigned as errored, mirroring `klt sim`'s own lost-shard convention
    (``sim._lost_shard_corner``) one level up.
    """

    def _run(
        shard_index: int, launcher: RemoteLauncher, public_ip: str
    ) -> list[dict[str, Any]]:
        candidates = candidates_by_shard[shard_index]
        reports: list[dict[str, Any]] = []
        for position, candidate in enumerate(candidates):
            reports.append(
                run_digital_candidate(
                    candidate,
                    launcher,
                    public_ip,
                    ssh_user=ssh_user,
                    ssh_key_path=ssh_key_path,
                    keep_artifacts=keep_artifacts,
                    artifacts_dir=artifacts_dir,
                    run_timeout_s=run_timeout_s,
                    ssh_ready_timeout_s=ssh_ready_timeout_s,
                    ssh_poll_interval_s=ssh_poll_interval_s,
                    wait_for_ssh=(position == 0),
                )
            )
        return reports

    return _run


# --------------------------------------------------------------------------- #
# 4. Candidate-ranking merge step (decision record Question 2, "Merge/report
#    assembly": new, but already designed to live beside the scheduler).
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class CandidateResult:
    """One candidate's outcome, flattened out of its shard's
    :class:`~klayout_tools.remote_fleet.ShardOutcome` by
    :func:`merge_candidate_results`.

    ``status`` is ``"ok"`` (the candidate's `klt eval` envelope was
    produced -- ``valid``/``objective``/``report`` are populated) or
    ``"error"`` (its shard was lost after the one automatic retry --
    ``error`` holds the shard's own diagnostic string, mirroring `klt sim`'s
    lost-shard convention one level up; every other field stays ``None``).
    """

    candidate_id: str
    shard_index: int
    status: str
    valid: bool | None = None
    objective: dict[str, Any] | None = None
    report: dict[str, Any] | None = None
    error: str | None = None


def merge_candidate_results(
    fleet_result: remote_fleet.FleetLaunchResult,
    candidates_by_shard: Sequence[Sequence[DigitalCandidate]],
) -> list[CandidateResult]:
    """Flatten ``fleet_result.shards`` (one
    :class:`~klayout_tools.remote_fleet.ShardOutcome` per shard) into one
    :class:`CandidateResult` per candidate, using
    ``candidates_by_shard[shard_index]`` to recover which candidates each
    shard was assigned (the same list :func:`make_digital_shard_runner` was
    built from). Every candidate is represented exactly once in the
    returned list, in ``candidates_by_shard``'s own flattened order --
    nothing is dropped, including a lost shard's candidates (reported
    ``status="error"``).

    Raises :class:`DigitalFleetError` if ``fleet_result``'s shard count
    does not match ``candidates_by_shard``'s, or if an ``"ok"`` shard's
    ``ShardOutcome.result`` is not a ``list`` of exactly
    ``len(candidates_by_shard[shard_index])`` entries -- the shape
    :func:`make_digital_shard_runner` always returns, so a mismatch means a
    caller supplied its own, differently-shaped ``ShardRunner``.
    """
    if len(fleet_result.shards) != len(candidates_by_shard):
        raise DigitalFleetError(
            f"fleet_result has {len(fleet_result.shards)} shard(s) but "
            f"candidates_by_shard names {len(candidates_by_shard)}"
        )

    results: list[CandidateResult] = []
    for shard_index, outcome in enumerate(fleet_result.shards):
        candidates = candidates_by_shard[shard_index]
        if outcome.status == "error":
            for candidate in candidates:
                results.append(
                    CandidateResult(
                        candidate_id=candidate.candidate_id,
                        shard_index=shard_index,
                        status="error",
                        error=outcome.error,
                    )
                )
            continue

        reports = outcome.result
        if not isinstance(reports, list) or len(reports) != len(candidates):
            got = (
                f"a list of {len(reports)}"
                if isinstance(reports, list)
                else type(reports).__name__
            )
            raise DigitalFleetError(
                f"shard {shard_index}: expected a list of "
                f"{len(candidates)} `klt eval` envelope(s) (one per "
                f"candidates_by_shard[{shard_index}] entry, see "
                f"make_digital_shard_runner), got {got}"
            )
        for candidate, report in zip(candidates, reports, strict=True):
            objective = report.get("objective") if isinstance(report, dict) else None
            valid = report.get("valid") if isinstance(report, dict) else None
            results.append(
                CandidateResult(
                    candidate_id=candidate.candidate_id,
                    shard_index=shard_index,
                    status="ok",
                    valid=valid,
                    objective=objective,
                    report=report,
                )
            )
    return results


def _is_scoreable(result: CandidateResult) -> bool:
    if result.status != "ok" or not isinstance(result.objective, dict):
        return False
    value = result.objective.get("value")
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def rank_candidates(results: Sequence[CandidateResult]) -> list[CandidateResult]:
    """Rank every scoreable candidate (``status="ok"``, a numeric
    ``objective.value``) best-first by #387's declared polarity
    (``"minimize"``/``"maximize"``), then append every unscoreable
    candidate (an errored shard, or an `klt eval` envelope with no usable
    ``objective``) unranked, in their original relative order -- never
    silently dropped, matching this issue's "or returns all candidates'
    metrics" acceptance criterion alongside ranking.

    Raises :class:`DigitalFleetError` if the scoreable candidates declare
    more than one distinct ``objective.polarity`` -- every candidate ranked
    together must have been evaluated against the same descriptor's
    objective (a single ``build_digital_job_description`` call per
    candidate already guarantees this in the common case; this is a guard
    against a caller mixing differently-configured candidates into one
    ranking call).
    """
    scoreable = [r for r in results if _is_scoreable(r)]
    unscoreable = [r for r in results if not _is_scoreable(r)]

    polarities = {r.objective["polarity"] for r in scoreable if r.objective is not None}
    if len(polarities) > 1:
        raise DigitalFleetError(
            "cannot rank candidates with differing objective polarities: "
            f"{sorted(polarities)} -- every candidate in one rank_candidates() "
            "call must share the same descriptor's objective"
        )
    polarity = next(iter(polarities), "minimize")
    reverse = polarity == "maximize"

    ranked = sorted(
        scoreable,
        key=lambda r: r.objective["value"],
        reverse=reverse,  # type: ignore[index]
    )
    return [*ranked, *unscoreable]
