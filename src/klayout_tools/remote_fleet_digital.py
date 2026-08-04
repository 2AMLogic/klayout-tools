"""Digital-candidate extension points for Epic #375's fleet scheduler --
Epic #391 ("adopt the digital engine class -- Yosys + OpenROAD -- RTL->GDS
as a first-class ``klt`` flow") Phase 6, issue #445.

Implements *only* the two pieces
``docs/design/digital-fleet-unit-abstraction-decision.md`` (Phase 1's
decision record, issue #400, already landed) identifies as necessary to
extend :mod:`klayout_tools.remote_fleet`'s K-host scheduler to
candidate-shaped digital work -- **``remote_fleet.py``/``remote_launcher.py``/
``remote_transport.py`` are reused verbatim, unmodified, by everything in
this module**, per that decision record's Question 2 conclusion ("extend
#375 in place, no rewrite"):

1. :func:`select_digital_instance_type` -- a digital-specific
   instance-sizing function, replacing the corner-shaped
   ``select_instance_type(unit_count, threads_per_corner)`` formula
   :mod:`klayout_tools.remote_launcher` uses for SPICE. A digital candidate
   wants ~1 whole host (the decision record's ``m ~= 1``, one candidate
   evaluation per host), not many lightweight units packed per host --
   keyed off a design-size proxy (e.g. an expected/measured instance count)
   or a fixed/configurable instance tier (globally or per PDK), never the
   ``corner_count x threads_per_corner`` shape. :func:`digital_threads_per_corner_for`
   is the small bridge that lets this function's chosen tier actually drive
   :class:`~klayout_tools.remote_fleet.FleetLauncher`'s own **unmodified**
   sizing call (``select_instance_type(1, threads_per_corner)``, run with
   ``shard_unit_counts=[1, 1, ...]`` -- one candidate per shard, the
   decision record's ``m ~= 1``) -- so no change to ``FleetLauncher``'s
   constructor is ever needed to honor a digital sizing choice.
2. :func:`build_digital_job_description` -- a digital-specific
   :class:`~klayout_tools.remote_transport.JobDescription` builder, living
   beside :mod:`klayout_tools.remote_fleet` exactly the way ``sim.py``'s own
   ``_build_remote_job_description`` does today (per
   ``docs/design/remote-job-description.md``'s "what a future adopter still
   has to bring" section). Unlike ``klt sim``'s remote job (which re-invokes
   ``klt sim --backend local-parallel`` on the pushed netlist), a digital
   candidate's remote command is **``klt eval``** -- the synthesis
   -> [functional-verification] -> place-and-route pipeline, and #387's
   scored ``valid``/``objective``/``metrics`` envelope, are already wired
   together by the already-landed digital ``klt eval`` descriptor support
   (Epic #391 Phase 5, issue #444) -- this module pushes the descriptor
   (plus the candidate's own RTL/constraints/per-stage request documents)
   and runs ``klt eval descriptor.json --candidate candidate.json --format
   json`` unchanged, rather than re-inventing a synth/P&R orchestrator of
   its own. :func:`make_digital_shard_runner` wires this job description
   into the :data:`~klayout_tools.remote_fleet.ShardRunner` callable
   contract (``(shard_index, launcher, public_ip) -> Any``) with **no**
   change to :class:`~klayout_tools.remote_fleet.FleetLauncher`.
   :func:`rank_digital_candidates` is the paired candidate-ranking merge
   step -- per the decision record, "merge/report assembly... was already
   designed to be an addition beside the scheduler, never a change within
   it" -- consuming each candidate's own #387-scored ``objective`` (already
   embedded per-candidate by ``klt eval``, so ranking is pure
   sort-and-annotate, not a second scoring implementation) to rank valid
   candidates best-first, or, absent a usable objective, still returning
   every candidate's raw ``metrics`` for inspection.

**Unit of parallelism, per the decision record**: one shard is one *host*
running one or more *candidate evaluations* in its own serial loop (never
one pipeline stage, never "one corner"). ``candidates_by_shard`` (shared
between :func:`make_digital_shard_runner` and :func:`rank_digital_candidates`)
is how a caller expresses "these candidate ids run on this shard" -- a
caller wanting more candidates than available hosts already expresses "run
several candidates sequentially on this shard" by putting more than one
:class:`DigitalCandidate` in one shard's own list, exactly as the decision
record's Question 2 table anticipates.
"""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from . import remote_transport
from .remote_fleet import ShardOutcome, ShardRunner
from .remote_launcher import (
    RemoteLauncher,
    RemoteLaunchError,
    instance_vcpu_count,
    select_instance_type,
)

SCHEMA_VERSION = 1


class DigitalFleetError(RemoteLaunchError):
    """Raised for anything that prevents a trustworthy digital-fleet sizing
    choice, job description, or ranking from being produced: an unknown
    instance tier/type, mismatched ``shard_outcomes``/``candidates_by_shard``
    lengths, or candidates whose descriptors disagree about the sweep's own
    objective polarity.

    Subclasses :class:`~klayout_tools.remote_launcher.RemoteLaunchError` for
    the same reason :class:`~klayout_tools.remote_fleet.FleetLaunchError`
    does: any existing caller that already catches that type keeps working
    unchanged.
    """


# --------------------------------------------------------------------------- #
# 1. Instance sizing -- replaces select_instance_type(unit_count,
#    threads_per_corner) for digital candidates
# --------------------------------------------------------------------------- #

#: Design-size-proxy buckets: ``(exclusive upper bound on the proxy, c7i
#: tier)``, ascending. The proxy is any caller-chosen non-negative integer
#: standing in for "how big is this design" -- the natural choice is a
#: synthesized instance count (``synthesize`` report's own
#: ``instance_count``, from a prior/estimated run of the same RTL), but any
#: monotonic size signal works. Unlike SPICE's ``corner_count x
#: threads_per_corner`` (many lightweight units packed per host), every
#: bucket here targets a tier that gives *one* candidate's OpenROAD run
#: (placement/routing/STA) most of a host's own cores to multi-thread
#: internally -- unrelated to how many candidates a shard runs, which is
#: never more than one *at a time* (the decision record's ``m ~= 1``).
_DESIGN_SIZE_TIERS: tuple[tuple[int, str], ...] = (
    (5_000, "c7i.4xlarge"),  # small blocks (<5k instances): 16 vCPU
    (50_000, "c7i.8xlarge"),  # mid-size blocks (<50k instances): 32 vCPU
    (250_000, "c7i.12xlarge"),  # large blocks (<250k instances): 48 vCPU
)

#: Tier used when ``design_size_proxy`` meets or exceeds every bucket above.
_DESIGN_SIZE_DEFAULT_TIER = "c7i.16xlarge"  # 64 vCPU

#: Per-PDK default instance tier, used only when the caller gives neither
#: ``design_size_proxy`` nor an explicit ``instance_tier`` -- the decision
#: record's "or a fixed/configurable instance tier per PDK" option. A repo
#: fork targeting a different PDK overrides this dict directly (module-level,
#: mutable by design -- mirrors how ``remote_launcher.SUPPORTED_PDKS`` is a
#: plain module constant, not a hardcoded literal deep in a function body).
DEFAULT_DIGITAL_INSTANCE_TIER_BY_PDK: dict[str, str] = {
    "sky130A": "c7i.8xlarge",
    "gf180mcu": "c7i.8xlarge",
}

#: Used when neither a proxy, an explicit tier, nor a recognized PDK is
#: given -- a reasonable single-host default, never a silent "pick the
#: smallest possible instance" (mirrors ``remote_launcher.estimate_cost``'s
#: "approximate, never zero" posture, applied to sizing instead of cost).
_FALLBACK_DIGITAL_INSTANCE_TIER = "c7i.8xlarge"


def select_digital_instance_type(
    design_size_proxy: int | None = None,
    *,
    instance_tier: str | None = None,
    pdk: str | None = None,
) -> str:
    """Choose a ``c7i`` instance type for **one digital candidate
    evaluation** (the decision record's unit of parallelism), replacing
    :func:`~klayout_tools.remote_launcher.select_instance_type`'s
    ``corner_count x threads_per_corner`` formula -- that formula answers
    "how many lightweight SPICE corners fit on one host," a question that
    does not apply here: a digital candidate wants ~1 whole host
    (``m ~= 1``) for OpenROAD's own internal multi-threading, not a packing
    density to optimize.

    Resolution order (first match wins):

    1. ``instance_tier`` -- an explicit, caller-chosen ``c7i`` type
       (validated against :func:`~klayout_tools.remote_launcher.instance_vcpu_count`'s
       known ladder; an unrecognized value raises :class:`DigitalFleetError`
       rather than silently launching an unsized host).
    2. ``design_size_proxy`` -- a non-negative integer size signal (e.g. a
       synthesized instance count from a prior run of the same RTL) bucketed
       into :data:`_DESIGN_SIZE_TIERS` -- a genuinely different formula from
       the SPICE sizing function, not that formula called with different
       arguments.
    3. ``pdk`` -- falls back to :data:`DEFAULT_DIGITAL_INSTANCE_TIER_BY_PDK`
       when the PDK is recognized.
    4. :data:`_FALLBACK_DIGITAL_INSTANCE_TIER` -- otherwise.

    Raises :class:`DigitalFleetError` for a negative ``design_size_proxy`` or
    an ``instance_tier`` outside :mod:`~klayout_tools.remote_launcher`'s
    known ``c7i`` ladder.
    """
    if instance_tier is not None:
        try:
            instance_vcpu_count(instance_tier)  # validates against the known ladder
        except RemoteLaunchError as exc:
            raise DigitalFleetError(str(exc)) from exc
        return instance_tier

    if design_size_proxy is not None:
        if design_size_proxy < 0:
            raise DigitalFleetError("design_size_proxy must be a non-negative integer")
        for bound, tier in _DESIGN_SIZE_TIERS:
            if design_size_proxy < bound:
                return tier
        return _DESIGN_SIZE_DEFAULT_TIER

    if pdk is not None and pdk in DEFAULT_DIGITAL_INSTANCE_TIER_BY_PDK:
        return DEFAULT_DIGITAL_INSTANCE_TIER_BY_PDK[pdk]

    return _FALLBACK_DIGITAL_INSTANCE_TIER


def digital_threads_per_corner_for(instance_type: str) -> int:
    """The ``threads_per_corner`` value to pass to
    :class:`~klayout_tools.remote_fleet.FleetLauncher`/
    :class:`~klayout_tools.remote_launcher.RemoteLauncher` -- paired with
    ``shard_unit_counts=[1, 1, ...]`` (one candidate per shard, the decision
    record's ``m ~= 1``) -- so that module's own **unmodified**
    ``select_instance_type(1, threads_per_corner)`` call resolves to
    ``instance_type``. This is the entire mechanism by which
    :func:`select_digital_instance_type`'s tier choice reaches a real fleet
    launch with *no change* to ``remote_fleet.py``/``remote_launcher.py``:
    sizing stays exactly ``select_instance_type``'s own arithmetic (ladder
    walk + headroom), digital's only contribution is choosing which inputs
    to feed it.

    Computed by construction (search over ``select_instance_type(1, t)``'s
    own output for decreasing ``t``, from ``instance_type``'s own vCPU count
    down to ``1``), not derived by hand -- correct for every current and
    future ``c7i`` ladder entry without re-deriving headroom arithmetic here.
    Raises :class:`DigitalFleetError` (unreachable for a known ladder entry)
    if no such value exists.
    """
    target_vcpu = instance_vcpu_count(instance_type)  # raises if unknown

    threads = target_vcpu
    while threads > 0:
        try:
            candidate = select_instance_type(1, threads)
        except RemoteLaunchError:
            threads -= 1
            continue
        if candidate == instance_type:
            return threads
        threads -= 1

    raise DigitalFleetError(
        f"could not derive a threads_per_corner value reproducing "
        f"{instance_type!r} through remote_launcher.select_instance_type "
        "(1 candidate, headroom-adjusted) -- unreachable for a known c7i "
        "ladder entry"
    )


# --------------------------------------------------------------------------- #
# 2a. JobDescription builder -- one klt eval invocation per candidate
# --------------------------------------------------------------------------- #

#: Job-relative filenames the descriptor/candidate documents are pushed to,
#: mirroring ``remote_transport.REMOTE_REQUEST_FILENAME``'s "convenience
#: constant, not a module-wide assumption" role.
DESCRIPTOR_FILENAME = "descriptor.json"
CANDIDATE_FILENAME = "candidate.json"

#: `klt eval`'s own "ran and produced a report" exit codes (see
#: ``docs/cli/eval.md``'s exit-code table): 0 (valid) and 3 (ran, invalid)
#: both mean a #387-scored envelope exists to parse; only 1 (crashed, no
#: report) is a transport-level failure here, mirroring `klt sim`'s
#: 0/3/4 vs. everything-else split in ``sim._REMOTE_SIM_SUCCESS_EXIT_CODES``.
EVAL_SUCCESS_EXIT_CODES: tuple[int, ...] = (0, 3)

#: Job-relative artifacts directory pulled back per candidate -- the whole
#: ``.klt/`` tree (synthesize's netlist+stats, place-and-route's DEF/GDS,
#: functional-verification's results, any drc/layout-metrics reports the
#: descriptor also runs), not just one stage's own subdirectory, since a
#: chosen "best" candidate's full evidence trail (including its GDS) is
#: exactly what a caller wants pulled back for the winning candidate.
DIGITAL_ARTIFACTS_RELATIVE_DIR = ".klt"


def build_digital_job_description(
    *,
    descriptor: dict[str, Any],
    candidate: dict[str, Any] | None,
    candidate_id: str,
    inputs: Sequence[remote_transport.JobInput] = (),
    artifacts_relative_dir: str | None = DIGITAL_ARTIFACTS_RELATIVE_DIR,
) -> remote_transport.JobDescription:
    """Build one digital candidate's push/run/collect job as a generic
    :class:`~klayout_tools.remote_transport.JobDescription` -- the digital
    analogue of ``sim._build_remote_job_description``, living beside
    :mod:`klayout_tools.remote_fleet` the same way that function lives
    beside ``sim.py``.

    Pushes the ``klt eval`` descriptor (and, when given, the candidate
    substitution document) as inline content, plus every caller-supplied
    ``inputs`` entry -- the RTL sources, constraint files, and per-stage
    (``synthesize``/``place-and-route``/``functional-verification``)
    request documents the descriptor's own ``gates``/``objective``/
    ``metrics`` ``args`` reference by job-relative filename (those checks'
    ``request`` args are file paths only, never inline JSON -- see
    ``docs/cli/eval.md``'s "Check `args`" table). This function does not
    introspect the descriptor to discover which files it needs; the caller
    (who built the descriptor and knows its own candidate sweep) supplies
    them explicitly, mirroring ``_build_remote_job_description``'s own
    "handed an explicit ``netlist_path``, never asked to scan ``request``"
    precedent.

    The remote command is ``klt eval <descriptor> [--candidate <candidate>]
    --format json`` -- unmodified reuse of the already-landed digital ``klt
    eval`` descriptor support (Epic #391 Phase 5, issue #444), which already
    composes ``synthesize`` -> ``[functional-verification]`` ->
    ``place-and-route`` (and any ``drc``/``layout-metrics`` gates over the
    produced GDS) into the one #387-scored envelope this module's merge step
    (:func:`rank_digital_candidates`) consumes.
    """
    job_inputs: list[remote_transport.JobInput] = [
        remote_transport.JobInput(
            remote_name=DESCRIPTOR_FILENAME,
            label="descriptor",
            content=json.dumps(descriptor),
        )
    ]

    command = f"klt eval {DESCRIPTOR_FILENAME}"
    if candidate:
        job_inputs.append(
            remote_transport.JobInput(
                remote_name=CANDIDATE_FILENAME,
                label="candidate",
                content=json.dumps(candidate),
            )
        )
        command += f" --candidate {CANDIDATE_FILENAME}"
    command += " --format json"

    job_inputs.extend(inputs)

    return remote_transport.JobDescription(
        label=f"klt eval ({candidate_id})",
        inputs=tuple(job_inputs),
        command=command,
        success_exit_codes=EVAL_SUCCESS_EXIT_CODES,
        artifacts_relative_dir=artifacts_relative_dir,
    )


def digital_synth_netlist_relative_path(hdl_toplevel: str) -> str:
    """The job-relative path ``synthesize.run_synthesize`` deterministically
    writes a candidate's mapped netlist to, given ``request.hdl_toplevel`` --
    computed ahead of time (never introspected after the fact) so a
    ``place-and-route`` request pushed alongside a ``synthesize`` request in
    the same job can already name the right ``netlist`` field before either
    stage has run on the remote host.

    Mirrors ``synthesize.run_synthesize``'s own convention exactly
    (``.klt/synthesize/<hdl_toplevel>_synth.v``, next to the request file --
    here, the job directory's own root, since
    :func:`build_digital_job_description` pushes the ``synthesize`` request
    document at the job directory root via the caller's own ``inputs``).
    Keep in sync with ``synthesize.py``'s own ``output_dir``/``netlist_path``
    construction if that convention ever changes.
    """
    return f".klt/synthesize/{hdl_toplevel}_synth.v"


def build_digital_candidate_descriptor(
    *,
    synthesize_request: str = "synth_request.json",
    place_and_route_request: str = "pr_request.json",
    functional_verification_request: str | None = None,
    objective_metric: str = "area_um2",
    objective_polarity: str = "minimize",
    objective_name: str | None = None,
) -> dict[str, Any]:
    """Build a standard ``klt eval`` descriptor for one digital candidate's
    synthesis -> [functional-verification] -> place-and-route pipeline (the
    decision record's "one candidate evaluation" unit), in the exact shape
    ``docs/cli/eval.md`` documents.

    ``gates`` run in ``run_eval``'s own declaration order, so listing
    ``synthesize`` (and, when given, ``functional-verification``) *ahead of*
    ``place-and-route`` as the descriptor's ``objective`` check is what
    sequences the pipeline correctly: ``place-and-route``'s own request
    references the netlist ``synthesize`` produces on disk (a filesystem
    side effect, not something ``klt eval``'s per-check cache passes between
    checks), so ``synthesize`` must already have run by the time
    ``place-and-route`` (the objective, evaluated after every gate) runs --
    see :func:`digital_synth_netlist_relative_path`.

    ``synthesize``/``place-and-route`` have no gate pass/fail semantics of
    their own (``eval.py``'s docstring: both always report
    ``"status": "ok"``, a failed stage instead raises before a gate entry is
    even recorded) -- each gets a trivial ``status == "ok"`` threshold
    purely to sequence execution and appear in the response's ``gates[]``,
    never to express a real pass/fail condition. ``functional-verification``
    already has built-in pass/fail semantics
    (``eval._status_functional_verification``), so its gate declares no
    threshold.

    ``synthesize_request``/``place_and_route_request``/
    ``functional_verification_request`` are job-relative filenames pushed as
    separate :class:`~klayout_tools.remote_transport.JobInput` entries
    alongside this descriptor (see :func:`build_digital_job_description`),
    matching each check's own file-path-only ``request`` arg convention.
    """
    gates: list[dict[str, Any]] = [
        {
            "check": "synthesize",
            "name": "synthesize",
            "args": {"request": synthesize_request},
            "threshold": {"metric": "status", "equals": "ok"},
        }
    ]
    if functional_verification_request:
        gates.append(
            {
                "check": "functional-verification",
                "name": "functional-verification",
                "args": {"request": functional_verification_request},
            }
        )

    pr_args = {"request": place_and_route_request}
    return {
        "gates": gates,
        "objective": {
            "check": "place-and-route",
            "metric": objective_metric,
            "polarity": objective_polarity,
            "name": objective_name or objective_metric,
            "args": pr_args,
        },
        "metrics": [
            {
                "name": "synthesize",
                "check": "synthesize",
                "args": {"request": synthesize_request},
            },
            {
                "name": "place-and-route",
                "check": "place-and-route",
                "args": pr_args,
            },
        ],
    }


# --------------------------------------------------------------------------- #
# 2b. ShardRunner factory -- the callable-contract extension point
# --------------------------------------------------------------------------- #

#: No calibrated digital ``T_o``/``t`` timing model exists yet (decision
#: record Question 3, point 3: "Phase 6 must derive its own T_o/t from a
#: first live digital fleet run... not import T_o ~= 140s or beta~=4-5 as a
#: default"). These generous, deliberately uncalibrated defaults exist only
#: so a genuinely wedged remote run doesn't hang forever -- override via
#: :func:`make_digital_shard_runner`'s own ``ssh_ready_timeout_s``/
#: ``run_timeout_s`` once a real measurement exists, exactly like `klt sim`'s
#: own ``remote.ssh_ready_timeout_s``/``remote.ssh_timeout_s`` knobs.
DEFAULT_DIGITAL_SSH_READY_TIMEOUT_S = 600.0
DEFAULT_DIGITAL_RUN_TIMEOUT_S = 4 * 60 * 60.0  # 4 hours per candidate


@dataclass(frozen=True)
class DigitalCandidate:
    """One digital candidate evaluation's identity and inputs -- the
    decision record's unit of parallelism.

    ``candidate_id`` is this sweep's own opaque label (used in
    :func:`rank_digital_candidates`'s output and in each candidate's
    dedicated remote job id / pulled-artifacts subdirectory -- never sent to
    the remote host as anything but a debugging label). ``values`` is the
    ``klt eval --candidate`` substitution document (``${name}`` placeholder
    values -- e.g. per-candidate RTL/constraint paths, a P&R seed, a
    synthesis-strategy flag); omit/empty when the descriptor's own ``args``
    need no substitution. ``inputs`` are this candidate's own pushed files
    (RTL sources, constraint files, and the ``synthesize``/
    ``place-and-route``/``functional-verification`` request documents the
    descriptor's checks reference by job-relative filename) -- generating
    these (e.g. sweeping {synthesis strategy} x {P&R seed} per the decision
    record's own worked examples) is the caller's job, not this module's.
    """

    candidate_id: str
    values: dict[str, Any] = field(default_factory=dict)
    inputs: tuple[remote_transport.JobInput, ...] = ()


def _run_one_candidate(
    *,
    candidate: DigitalCandidate,
    descriptor: dict[str, Any],
    launcher: RemoteLauncher,
    public_ip: str,
    ssh_key_path: str,
    ssh_user: str,
    run_timeout_s: float,
    keep_artifacts: bool,
    artifacts_dir: str | None,
) -> dict[str, Any]:
    """Push, run, and (optionally) pull artifacts for one candidate, on an
    already-provisioned/SSH-ready host. Never raises -- a transport failure
    for *this* candidate is reported as ``status: "error"`` so a sibling
    candidate on the same shard still gets its own attempt (per-candidate
    isolation within a shard, one level below
    :mod:`klayout_tools.remote_fleet`'s own per-shard isolation).
    """
    job_id = f"klt-digital-{candidate.candidate_id}-{uuid.uuid4().hex[:8]}"
    remote_job_dir = remote_transport.job_dir(ssh_user, job_id)
    job = build_digital_job_description(
        descriptor=descriptor,
        candidate=candidate.values,
        candidate_id=candidate.candidate_id,
        inputs=candidate.inputs,
    )

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
        if keep_artifacts and artifacts_dir:
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
        return {
            "candidate_id": candidate.candidate_id,
            "status": "error",
            "error": str(exc),
            "report": None,
            "instance_id": launcher.instance_id,
        }

    return {
        "candidate_id": candidate.candidate_id,
        "status": "ok",
        "error": None,
        "report": report,
        "instance_id": launcher.instance_id,
    }


def make_digital_shard_runner(
    *,
    descriptor: dict[str, Any],
    candidates_by_shard: Sequence[Sequence[DigitalCandidate]],
    ssh_key_path: str,
    ssh_user: str = remote_transport.DEFAULT_SSH_USER,
    keep_artifacts: bool = False,
    artifacts_dir: str | None = None,
    ssh_ready_timeout_s: float = DEFAULT_DIGITAL_SSH_READY_TIMEOUT_S,
    run_timeout_s: float = DEFAULT_DIGITAL_RUN_TIMEOUT_S,
) -> ShardRunner:
    """Build a :data:`~klayout_tools.remote_fleet.ShardRunner` -- ``(shard_index,
    launcher, public_ip) -> Any`` -- that runs every candidate in
    ``candidates_by_shard[shard_index]`` on that shard's already-provisioned
    host, **in its own serial loop** (per the decision record's "pipeline
    stages within one candidate are not independently parallelizable... a
    caller wanting more candidates than available hosts can already express
    'run several candidates sequentially on this shard' inside its own
    ShardRunner callable"). The returned callable is handed to
    :meth:`~klayout_tools.remote_fleet.FleetLauncher.run_shards`/
    :func:`~klayout_tools.remote_fleet.run_fleet` unchanged -- no
    :class:`~klayout_tools.remote_fleet.FleetLauncher` change required.

    Waits for SSH readiness once per shard (before that shard's first
    candidate -- :mod:`klayout_tools.remote_fleet` itself never does this,
    exactly like ``sim._run_remote``'s own explicit
    :func:`~klayout_tools.remote_transport.wait_for_ssh` call), then runs
    :func:`_run_one_candidate` per candidate; a shard with no assigned
    candidates returns ``[]`` without even connecting.

    Returns a list of ``{"candidate_id", "status", "error", "report",
    "instance_id"}`` dicts (this shard's own outcome per candidate) -- opaque to
    :class:`~klayout_tools.remote_fleet.FleetLauncher` itself, consumed by
    :func:`rank_digital_candidates`.
    """
    if keep_artifacts and not artifacts_dir:
        raise DigitalFleetError("artifacts_dir is required when keep_artifacts=True")

    def _runner(shard_index: int, launcher: RemoteLauncher, public_ip: str) -> Any:
        shard_candidates = candidates_by_shard[shard_index]
        if not shard_candidates:
            return []

        remote_transport.wait_for_ssh(
            public_ip,
            user=ssh_user,
            identity_file=ssh_key_path,
            timeout_s=ssh_ready_timeout_s,
        )

        return [
            _run_one_candidate(
                candidate=candidate,
                descriptor=descriptor,
                launcher=launcher,
                public_ip=public_ip,
                ssh_key_path=ssh_key_path,
                ssh_user=ssh_user,
                run_timeout_s=run_timeout_s,
                keep_artifacts=keep_artifacts,
                artifacts_dir=artifacts_dir,
            )
            for candidate in shard_candidates
        ]

    return _runner


# --------------------------------------------------------------------------- #
# 2c. Candidate-ranking merge step
# --------------------------------------------------------------------------- #


def rank_digital_candidates(
    shard_outcomes: Sequence[ShardOutcome],
    candidates_by_shard: Sequence[Sequence[DigitalCandidate]],
) -> dict[str, Any]:
    """Merge every shard's :func:`make_digital_shard_runner` result into one
    ranked report -- the decision record's "candidate-ranking merge step,"
    living beside :mod:`klayout_tools.remote_fleet` (never inside it, per
    that module's own docstring: "the actual corner/unit slicing... and the
    merged-report assembly" is always the caller's job).

    Ranks every **valid** candidate (``report["valid"] is True``) by its own
    already-#387-scored ``objective.value`` (best-first, honoring the shared
    descriptor's declared ``polarity`` -- every valid candidate in one sweep
    must share the same descriptor, so an inconsistent polarity across
    candidates raises :class:`DigitalFleetError`, a caller programming
    error, not a data condition to silently paper over). Invalid candidates
    (ran, ``valid: false``) and errored candidates (transport failure, a
    lost shard, or an underlying ``klt eval`` crash) are reported separately
    -- never silently dropped, and never mixed into the ranking. Every
    candidate that produced a report (valid or not) also contributes its own
    ``metrics`` entry, so ``metrics`` alone answers "or returns all
    candidates' metrics" even when nothing is rankable (e.g. every candidate
    invalid, or the descriptor declares no interesting objective for this
    sweep).

    A lost shard (``ShardOutcome.status == "error"``) attributes every
    candidate ``candidates_by_shard[shard.shard_index]`` assigned to it as
    ``errored`` with a ``"shard lost: <reason>"`` message -- mirrors
    ``sim._lost_shard_corner``'s one-level-down "the shard never returned,
    so nothing about any of its units is known beyond that" posture.

    Raises :class:`DigitalFleetError` if ``shard_outcomes`` and
    ``candidates_by_shard`` have different lengths (a caller error -- they
    must describe the same fleet).
    """
    if len(shard_outcomes) != len(candidates_by_shard):
        raise DigitalFleetError(
            "shard_outcomes and candidates_by_shard must have the same "
            f"length (got {len(shard_outcomes)} and {len(candidates_by_shard)})"
        )

    entries: list[dict[str, Any]] = []
    for outcome, shard_candidates in zip(
        shard_outcomes, candidates_by_shard, strict=True
    ):
        if outcome.status == "error":
            for candidate in shard_candidates:
                entries.append(
                    {
                        "candidate_id": candidate.candidate_id,
                        "shard_index": outcome.shard_index,
                        "status": "error",
                        "error": f"shard lost: {outcome.error}",
                        "report": None,
                    }
                )
            continue

        for result in outcome.result or []:
            entries.append(
                {
                    "candidate_id": result["candidate_id"],
                    "shard_index": outcome.shard_index,
                    "status": result["status"],
                    "error": result.get("error"),
                    "report": result.get("report"),
                }
            )

    polarity: str | None = None
    objective_name: str | None = None
    ranked: list[dict[str, Any]] = []
    invalid: list[dict[str, Any]] = []
    errored: list[dict[str, Any]] = []
    metrics: dict[str, Any] = {}

    for entry in entries:
        report = entry["report"]
        if entry["status"] != "ok" or report is None:
            errored.append(
                {
                    "candidate_id": entry["candidate_id"],
                    "shard_index": entry["shard_index"],
                    "error": entry["error"],
                }
            )
            continue

        metrics[entry["candidate_id"]] = report.get("metrics")

        if not report.get("valid", False):
            invalid.append(
                {
                    "candidate_id": entry["candidate_id"],
                    "shard_index": entry["shard_index"],
                    "gates": report.get("gates"),
                }
            )
            continue

        objective = report.get("objective") or {}
        value = objective.get("value")
        this_polarity = objective.get("polarity")
        if polarity is None:
            polarity = this_polarity
            objective_name = objective.get("name")
        elif this_polarity is not None and this_polarity != polarity:
            raise DigitalFleetError(
                "candidates report inconsistent objective polarity "
                f"({polarity!r} vs {this_polarity!r} for candidate "
                f"{entry['candidate_id']!r}) -- every candidate in one sweep "
                "must share the same descriptor's objective"
            )
        ranked.append(
            {
                "candidate_id": entry["candidate_id"],
                "shard_index": entry["shard_index"],
                "objective_value": value,
            }
        )

    ranked.sort(key=lambda e: e["objective_value"], reverse=(polarity == "maximize"))
    for rank, entry in enumerate(ranked, start=1):
        entry["rank"] = rank

    return {
        "schema_version": SCHEMA_VERSION,
        "objective_name": objective_name,
        "polarity": polarity,
        "ranked": ranked,
        "best": ranked[0] if ranked else None,
        "invalid": invalid,
        "errored": errored,
        "metrics": metrics,
    }
