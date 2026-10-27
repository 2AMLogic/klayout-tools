"""Run a SPICE PVT corner matrix headlessly and report pass/fail as structured data.

Pure library: :func:`run_sim` returns plain Python data (a ``dict`` of
JSON-serialisable primitives) and never prints, mirroring ``drc.py`` /
``render.py``. Serialisation and human-readable formatting live in the CLI
command module (``cli/sim_cmd.py``).

This is the build carried by the accepted spike,
``docs/design/spice-corner-runner-spike.md`` -- read that document first; it
settles the engine survey, contract shape, and wrap/build decision. This
module implements it, with one deliberate deviation documented below.

Engine-neutral contract, ngspice v1 (per the spike): the JSON contract does
not name the engine in its shape -- ``request["engine"]`` is a data field,
not a code path. ``ngspice -b`` is invoked as a subprocess per corner (never
``libngspice``): process-corner selection needs a fresh ``.lib`` parse anyway, a
hung engine must be killable without taking down our own process, and process
fan-out is the whole parallelism story for a future ``max_parallel`` (see the
spike's "Invocation strategy").

Issue #2016 (pairing #5 of oracle-tracking #2007) adds a second, deliberately
independent engine: Sandia's Xyce, a from-scratch SPICE implementation rather
than a SPICE 3f5 derivative, used as a cross-validation oracle for the
``ngspice`` results. ``Xyce -b`` runs the same subprocess-per-corner way, on
its own deck shape (``_write_xyce_deck`` -- no ``.control`` block, no
``alter``; see that function for the syntax divergences that motivated it).
The ``xyce`` path is intentionally narrower than ``ngspice``'s: DC/OP/TRAN
analyses, process/temperature corners, ``local``/``local-parallel`` backends
only, no Monte Carlo, no supply-axis corners, no fail-fast probe -- every
unimplemented combination raises :class:`SimError` naming what *is*
supported. See docs/design/xyce-oracle.md for the methodology and measured
agreement, and tests/test_sim_xyce_oracle.py for the real-binary oracle.

Deviation from the spike: the spike's proposed response shape carries a
``"schema": "klt.sim.corners/1"`` top-level field. This module instead uses
the shared envelope's ``"schema_version": 1`` (integer, versioned per
command), per ``docs/json-contract.md`` -- the house convention that postdates
the spike and every other ``klt`` verb already conforms to. The request
document (user-authored input, never emitted by this tool) is free to use
whatever shape a caller likes; ``load_request`` does not require a ``schema``
field.

Netlist convention: the ``netlist`` a request references is a **circuit body**
-- device/subcircuit definitions and sources -- with no ``.control``/``.end``
cards of its own. ``klt sim`` generates those around an ``.include`` of the
file, appending the corner's ``.lib``/``.temp`` cards, the request's
``.meas`` cards, and a ``.control`` block that ``alter``s the supply sources
and runs the declared analysis. A netlist that already carries its own
``.end``/`.control`` is not supported in this version (see docs/cli/sim.md).

Because that convention gives this module sole ownership of the deck's only
``.control`` block, a measurement that is *not* expressible as a top-level
``.meas`` card -- an operating-point quantity (``.MEASURE`` has no ``op``
type, see ``_SUPPORTED_MEAS_TYPES``), or any reduction/combination of saved
vectors -- had no expression in a request at all. ``measurements[].expr``
(issue #2533) is the caller-facing seam into that block: one ngspice
expression bound to one response name, emitted as a ``let``/``print`` pair
after the analysis and harvested from the log by the same
``<name> = <value>`` pattern a ``.meas`` result uses. See
``_validate_measurement_forms`` for the contract and
``_corner_deck_control_lines`` for the emission ordering.

Failure classification is from the ngspice log text, never the process exit
code -- ngspice reliably exits ``0`` even when a ``.meas`` fails or the
matrix is singular (see the spike's "Failure signalling" survey row, verified
empirically against ngspice 46 while building this module). A
``singular_matrix``/``nonconvergence`` classification is only fatal when the
corner's own requested measurements did not actually come back (see
``_recovered_from_stepping``, issue #205) -- ngspice's internal gmin/source
stepping recovery narrates its own intermediate attempts failing even on a
run that ultimately succeeds, and that narration alone is not evidence of a
real failure.
"""

from __future__ import annotations

import contextlib
import hashlib
import itertools
import json
import math
import os
import re
import signal
import statistics
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator, Sequence
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass
from typing import IO, Any, NamedTuple

from . import _spice_number as spice_number
from . import (
    env_provenance,
    output_lock,
    sim_complex_vectors,
    sim_crossings,
    sim_cycle_mean,
    sim_decks,
    sim_energy,
    sim_eye,
    sim_injected,
    sim_mismatch_gate,
    sim_noise,
    sim_passes,
    sim_portable,
    sim_search,
    sim_snm_meas,
    sim_steps,
    sim_stimulus,
    sim_subckt_override,
    sim_supply_waveform,
    sim_tian,
    sim_transfer_meas,
    sim_twoport,
)

#: `remote_fleet`/`remote_transport`/`RemoteLauncher`/`RemoteLaunchError` are
#: not called directly from this module's own code any more (that code moved
#: to `sim_remote.py`, issue #1764) -- they are re-exported (self-aliased so
#: linters recognise the re-export as intentional) so
#: `klayout_tools.sim.<name>` keeps working for every external caller/test
#: that reached them before this split, including monkeypatching
#: `sim.remote_transport.*`/`sim.remote_fleet.*` (shared module singletons,
#: so a patch made through either module's own reference is visible from the
#: other) and `sim.RemoteLauncher` (looked up dynamically inside
#: `sim_remote._run_remote` for exactly this reason).
from . import remote_fleet as remote_fleet
from . import remote_transport as remote_transport
from . import sim_staging as sim_staging
from ._paths import (
    _load_request_json,
    _resolve_relative,
    resolve_tool_binary,
    validate_request_shape,
)
from ._provenance import INPUT_ROLE_NETLIST, build_provenance, sha256_file
from ._text import line_containing as _line_containing
from .coverage import (
    REASON_EMPTY_CORNER_MATRIX,
    REASON_UNRECOGNIZED_LIMIT_KEYS,
    build_check_coverage,
    coverage_rollup,
    rollup_status,
    work_id,
)
from .metrics import is_registered
from .pdk import PdkNotFoundError, find_pdk, list_corners
from .pdk import ambiguity_warning as pdk_ambiguity_warning
from .pdk_families import pdk_variant_family
from .remote_launcher import RemoteLauncher as RemoteLauncher
from .remote_launcher import RemoteLaunchError as RemoteLaunchError

#: ``batch`` backend entry points (issue #2080) -- the `klt sim` consumer of
#: 2am's EDA batch fleet. Same relationship to this module as
#: ``sim_remote``'s: `run_sim` dispatches into them and re-exports their
#: helpers (self-aliased) so ``klayout_tools.sim.<name>`` resolves them too.
from .sim_batch import _build_batch_job_spec as _build_batch_job_spec
from .sim_batch import (
    _remove_shard_manifest,
    _run_batch,
    _run_batch_fleet,
    _validate_batch_models_lib,
    attach_engine_build_comparison,
    attach_staged_model_comparison,
    dry_run_runner_check,
)
from .sim_batch import _resolve_batch_config as _resolve_batch_config
from .sim_batch_async import SUBMIT_ONLY_UNSUPPORTED_CODE
from .sim_batch_async import submit_only as _submit_batch_only
from .sim_decks import run_deck_request

#: Moved to `sim_validate.py` / `sim_errors.py` (issue #3689); re-exported self-aliased.
from .sim_errors import SimError as SimError
from .sim_plot import render_waveform_svg

#: The remaining names below are not called directly from this module's own
#: code -- they are re-exported (self-aliased so linters recognise the
#: re-export as intentional) so ``klayout_tools.sim.<name>`` keeps working
#: for every external caller/test that reached them before this split
#: (issue #1764).
from .sim_remote import ShardRunner as ShardRunner
from .sim_remote import _build_remote_job_description as _build_remote_job_description
from .sim_remote import _build_remote_request as _build_remote_request
from .sim_remote import _default_remote_run_timeout_s as _default_remote_run_timeout_s
from .sim_remote import _lost_shard_corner as _lost_shard_corner
from .sim_remote import _rewrite_remote_artifact_paths as _rewrite_remote_artifact_paths
from .sim_remote import (
    _run_remote,
    _run_remote_fleet,
    _run_sharded,
    _unrun_corner_report,
)
from .sim_remote import _run_remote_dispatch as _run_remote_dispatch
from .sim_remote import _shard_corner_points as _shard_corner_points
from .sim_validate import _EXPR_MEAS_NAME_RE as _EXPR_MEAS_NAME_RE
from .sim_validate import _MEAS_CARD_TYPE_RE as _MEAS_CARD_TYPE_RE
from .sim_validate import _MEAS_VALUE_REF_RE as _MEAS_VALUE_REF_RE
from .sim_validate import _SUPPORTED_MEAS_TYPES as _SUPPORTED_MEAS_TYPES
from .sim_validate import ON_UNREACHED_VALUES as ON_UNREACHED_VALUES
from .sim_validate import _expr_measurements as _expr_measurements
from .sim_validate import _measurement_name_key as _measurement_name_key
from .sim_validate import _require_measurement_form as _require_measurement_form
from .sim_validate import _validate_crossings_form as _validate_crossings_form
from .sim_validate import _validate_expr_body as _validate_expr_body
from .sim_validate import _validate_expr_names_unique as _validate_expr_names_unique
from .sim_validate import _validate_fail_on_diagnostic as _validate_fail_on_diagnostic
from .sim_validate import _validate_meas_card as _validate_meas_card
from .sim_validate import _validate_measurement_form as _validate_measurement_form
from .sim_validate import _validate_measurement_forms as _validate_measurement_forms
from .sim_validate import (
    _validate_no_dependent_meas_cards as _validate_no_dependent_meas_cards,
)
from .sim_validate import _validate_noise_measurement as _validate_noise_measurement
from .sim_validate import _validate_on_unreached as _validate_on_unreached
from .sim_validate import _validate_optional_flag as _validate_optional_flag
from .sim_validate import _validate_rawfile_form as _validate_rawfile_form

#: Bumped only on a non-additive (breaking) change to this command's own
#: JSON shape -- see docs/json-contract.md.
#:
#: Bumped 1 -> 2 (issue #1261): the top-level `netlist` field and
#: `environment.resume.checkpoint_path` changed from a raw (often absolute)
#: path string to the `{path, scope}` shape
#: `env_provenance.repo_relative_path` already defines -- a committed
#: evidence record wraps this response unmodified
#: (`docs/design/sim-evidence-discipline-spike.md`), so an absolute path
#: here used to leak the author's home directory/worktree layout into every
#: such record.
#:
#: Bumped 2 -> 3 (issue #1274): `environment.models_lib` -- missed by
#: #1261's field list -- changed from the resolved *absolute* model-library
#: path to the same `{path, scope}` shape. On a normal install the PDK lives
#: under the user's home directory, so that field leaked a home path into
#: every committed report. Nothing is lost: the library is still pinned by
#: identity via `environment.models_lib_sha256` and `provenance.deck`.
SCHEMA_VERSION = 3

#: Per-corner wall-clock budget applied when the request omits
#: ``options.timeout_s``.
DEFAULT_TIMEOUT_S = 120

#: ``run_sim()``'s own corner-sweep rollup field names -> their declared
#: METRICS2.1-style names in ``metrics.py``'s registry (issue #1849,
#: adopting the #247 registry beyond its ``klt layout-metrics``/``klt drc``
#: (#1847) precedent). Additive: the ``metrics`` block this backs is a
#: *parallel* object alongside the existing ``corner_count``/``passed``/
#: ``failed``/``errored`` fields, never a replacement for them -- see
#: ``docs/design/metric-namespace.md`` for the additive-vs-rename decision.
#: ``measurements[].name`` stays out of scope -- those names are
#: caller-supplied via the request spec, not ``klt``-declared.
_CORNER_COUNT_METRIC_NAME = "sim__corner__count"
_CORNER_PASSED_COUNT_METRIC_NAME = "sim__corner__passed_count"
_CORNER_FAILED_COUNT_METRIC_NAME = "sim__corner__failed_count"
_CORNER_ERRORED_COUNT_METRIC_NAME = "sim__corner__errored_count"
_CORNER_INCONCLUSIVE_COUNT_METRIC_NAME = "sim__corner__inconclusive_count"

assert is_registered(_CORNER_COUNT_METRIC_NAME)
assert is_registered(_CORNER_PASSED_COUNT_METRIC_NAME)
assert is_registered(_CORNER_FAILED_COUNT_METRIC_NAME)
assert is_registered(_CORNER_ERRORED_COUNT_METRIC_NAME)
assert is_registered(_CORNER_INCONCLUSIVE_COUNT_METRIC_NAME)

#: Every ``diagnostics[].code`` this command can emit -- the authoritative
#: vocabulary ``options.fail_on_diagnostic`` is validated against (issue
#: #2492), and the same table ``docs/cli/sim.md``'s "Failure classification"
#: section documents. Kept as an explicit literal (rather than derived) so
#: the accepted option vocabulary is readable in one place and stable
#: independent of which engine/backend module happens to define a pattern;
#: ``test_sim.py`` asserts it stays a superset of every code the classifier
#: tables and the unrun-report paths can actually produce, so it cannot
#: silently drift -- including a code emitted only through a named constant
#: or an ``_unrun_corner_report`` call, which it resolves rather than
#: restates (see :data:`_GRADING_MARKER_CODES`).
DIAGNOSTIC_CODES: frozenset[str] = frozenset(
    {
        # ngspice/Xyce log classifiers (`_DIAGNOSTIC_PATTERNS`,
        # `_XYCE_DIAGNOSTIC_PATTERNS`).
        "singular_matrix",
        "timestep_too_small",
        "nonconvergence",
        "missing_include",
        "netlist",
        "model_bin_range",
        "ngspice_fatal",
        "no_such_vector",
        # ngspice's 99-calls-per-file `par()` cap (issue #4084); a log
        # classifier (error) and a pre-flight deck count (warning).
        "par_call_cap",
        # Missing-capability prerequisites (issue #2814).
        "unsupported_model_level",
        "osdi_abi_mismatch",
        "osdi_load_failed",
        # The analysis never completed (engine abort trailer), issue #2846.
        "analysis_aborted",
        # Per-corner run outcomes synthesized by `_run_corner` itself.
        "timeout",
        "engine_killed",
        "engine_aborted",
        "measurement",
        # An opted-in (`on_unreached: "null"`) unreached `.meas` target
        # (issue #3108); `info` severity, so it never fails a corner itself.
        "measurement_not_reached",
        # An `optional` measurement with no value (issue #2925); info severity.
        "measurement_absent",
        "unknown",
        # Opt-in convergence retry record (`options.retry_on_convergence_error`,
        # issue #2973), attached by `_run_corner_with_retry` after the retry ran.
        "convergence_retry",
        # Waveform JSON conversion skipped over options.waveform_max_bytes
        # (issue #3211); always a warning.
        "waveform_too_large",
        # Step-sequence outcomes (`analysis_steps[]`, issue #2482), synthesized
        # by `sim_steps.grade_step_measurements`: a step whose analysis
        # produced no plot, and a result whose inputs produced no value.
        "step_failed",
        "derived_input_unavailable",
        # Multi-pass AC acquisition (`analysis.passes`, issue #3159): a pass
        # whose AC analysis left no decodable rawfile.
        "pass_failed",
        # Units that never ran (`_unrun_corner_report` and friends).
        "budget_exceeded",
        "orphaned",
        "lost_shard",
        "timeout_budget_unreachable",
        "batch_job_failed",
        "batch_job_timeout",
        "batch_poll_timeout",
        "batch_job_interrupted",
        # Report-level (`environment.job_diagnostic`, issue #2970).
        "all_units_at_timeout",
        # Report-level (top-level `diagnostics[]`, issue #2901): batch runner
        # predates request fields, under `runner_version_check: "warn"`.
        "batch_runner_request_feature_predated",
        # Report-level (top-level `diagnostics[]`, issue #4097): an off-host
        # `measurements[].expr` produced no value; the runner may predate it.
        "expr_runner_may_predate",
    }
)

#: The marker diagnostic :func:`_grade_corner` attaches to a corner it graded
#: ``inconclusive``, naming the ``options.fail_on_diagnostic`` code(s) that
#: disqualified it (issue #2492) -- so the report says *why* a corner that
#: produced perfectly good-looking numbers is not being reported as a pass.
INCONCLUSIVE_DIAGNOSTIC_CODE = "inconclusive"

#: The warning :func:`_run_corner` attaches when ``waveform.raw`` exceeds
#: ``options.waveform_max_bytes`` and the JSON conversion is skipped (#3211).
WAVEFORM_TOO_LARGE_CODE = "waveform_too_large"

#: The marker diagnostic :func:`_grade_measurement_value` attaches to a
#: measurement whose value fell outside its declared
#: ``measurements[].plausible_range``/``options.node_voltage_bounds``
#: (issue #2493) -- the *other* reason a corner can grade
#: :data:`_INCONCLUSIVE_STATUS`, and how a consumer tells the two apart.
IMPLAUSIBLE_DIAGNOSTIC_CODE = "implausible_solution"

#: The marker diagnostic :func:`_margin_measurement_result` attaches to a
#: ``transfer`` margin measurement (issue #3088) whose limit was met but whose
#: AC band cannot support a pass -- so the corner grades
#: :data:`_INCONCLUSIVE_STATUS` and the message lists the
#: ``MarginReport.ambiguity`` reasons.
MARGIN_AMBIGUOUS_DIAGNOSTIC_CODE = "margin_ambiguous"

#: The codes above: emitted by the **grading** step rather than by a log
#: classifier or an unrun-corner path, and therefore deliberately **not**
#: members of :data:`DIAGNOSTIC_CODES`. All are produced downstream of
#: ``options.fail_on_diagnostic``'s code match on an already-inconclusive
#: corner, so listing either there could not change any verdict; accepting
#: them would only invite the misreading that it could. ``test_sim.py``'s
#: drift guard derives this distinction rather than restating it.
_GRADING_MARKER_CODES: frozenset[str] = frozenset(
    {
        INCONCLUSIVE_DIAGNOSTIC_CODE,
        IMPLAUSIBLE_DIAGNOSTIC_CODE,
        MARGIN_AMBIGUOUS_DIAGNOSTIC_CODE,
    }
)

#: The corner/measurement/report status token issues #2492 and #2493 add
#: beside ``pass``/``fail``/``error``: the run produced numbers, but it will
#: not vouch for them. Ranked ``error > inconclusive > fail > pass`` at every
#: level (:func:`_grade_corner`, :func:`_aggregate_measurement_status`, and
#: ``run_sim``'s top-level rollup override).
#:
#: **Recorded decision -- one status, two reason codes.** The two increments
#: of split #2489 independently invented the same concept: #2492's
#: ``options.fail_on_diagnostic`` ("the solve narrated trouble I do not
#: trust") and #2493's plausibility bounds ("the solve converged somewhere
#: physically impossible"). Both assert *exactly* the same thing about the
#: report -- a number exists and must not be graded against ``limits`` as if
#: it were a result -- and both justified outranking ``fail`` with the same
#: argument, which is why neither could break the tie against the other.
#: Shipping them as two status tokens would have put two near-synonyms in
#: the JSON contract, forced a five-term sum invariant, two parallel counts,
#: two Monte Carlo exclusion fields and an arbitrary precedence between
#: them. They are therefore **one** status whose *reason* is carried by the
#: marker diagnostic's ``code`` (:data:`_GRADING_MARKER_CODES`) -- which
#: issue #2491's ``diagnostic_counts`` already rolls up per code, so the
#: per-reason breakdown a caller might want from two counts is still in the
#: report, derived rather than duplicated.
_INCONCLUSIVE_STATUS = "inconclusive"

#: Implemented engines. ``ngspice`` is the reference path (see the spike's
#: engine survey). ``xyce`` (issue #2016, pairing #5 of oracle-tracking
#: #2007) is the cross-validation oracle: Sandia's from-scratch SPICE
#: implementation, never a ngspice derivative, so agreement between the two
#: is real evidence rather than a shared-bug blind spot. The ``xyce`` path
#: is deliberately narrower than ``ngspice``'s -- DC/OP/TRAN analyses,
#: process/temperature corners, ``local``/``local-parallel`` backends only;
#: everything it does not yet implement raises a :class:`SimError` naming
#: what *is* supported rather than silently degrading. See
#: ``_run_corner``'s ``engine`` branch and docs/design/xyce-oracle.md.
SUPPORTED_ENGINES = ("ngspice", "xyce")

#: Recognised values for the optional ``request.netlist_source`` field --
#: caller-declared provenance of ``request.netlist``, distinguishing a
#: pre-layout schematic-level netlist (S6 in the stage graph) from a
#: post-layout netlist extracted from the laid-out design (S9). Mirrors the
#: caller-declares-intent pattern already used for ``engine``/``backend``
#: rather than trying to auto-detect provenance from the netlist file itself
#: (fragile -- a schematic and an extracted netlist can look syntactically
#: identical). See docs/cli/sim.md and docs/design/design-pipeline.md.
SUPPORTED_NETLIST_SOURCES = ("schematic", "extracted")

#: Execution backends behind ``run_sim``. ``local`` runs the corner matrix
#: sequentially in-process (the original behaviour). ``local-parallel`` fans
#: the same expanded corner list across a bounded local worker pool (Epic
#: #253 Phase 1) -- corners share nothing (each is a pure function of
#: netlist + models + corner), so this is purely a concurrency seam: same
#: request in, same report out, just faster on a multi-core box. ``remote``
#: (Epic #253 Phase 2, issue #265) provisions one right-sized EC2 instance
#: (``remote_launcher.RemoteLauncher``, #264) and runs the *same*
#: ``local-parallel`` worker-pool code on that box instead of the caller's
#: own cores, via the SSH/SCP push-then-pull transport in
#: :mod:`klayout_tools.remote_transport` -- see ``_run_remote`` and
#: ``docs/design/remote-sim-backend-spike.md`` decisions 2/5. Like
#: ``engine``, ``backend`` is a request *data* field, not part of the JSON
#: shape. Unknown names raise :class:`SimError` before any corner runs,
#: mirroring ``engine`` validation. ``batch`` (issue #2080) submits the
#: same ``local-parallel`` invocation to 2am's EDA batch fleet as an S3 job
#: contract instead of provisioning an instance itself -- see
#: :mod:`klayout_tools.sim_batch` and docs/cli/sim.md's "Batch backend".
SUPPORTED_BACKENDS = ("local", "local-parallel", "remote", "batch")

#: Backends whose corner reports this process does **not** construct: the
#: corners come back from an independently-run box (``remote``) or job
#: instance (``batch``) as a JSON report, so there is no local
#: :class:`CornerPoint`-keyed dispatch loop here to gate on a checkpoint, a
#: deadline, or a calibration probe of *this* host's hardware. Both
#: ``options.resume`` and the fail-fast probe are refused/skipped for these.
_OFFHOST_BACKENDS = ("remote", "batch")

#: Host-level default backend (2am#1004): consulted only when neither the
#: ``--backend`` flag nor the request's own ``backend`` field names one. A
#: fleet host that should not run SPICE grids itself (the AWS dispatch
#: workers) sets ``KLT_SIM_BACKEND=batch`` in its daemon environment so every
#: sweep's ``klt sim`` goes to the Spot batch fleet without editing ~20
#: repos' request files. It is a *preference*, not a requirement -- see
#: :func:`resolve_backend` and ``_yield_host_default_backend`` for the cases
#: where it steps aside to ``local``.
BACKEND_ENV = "KLT_SIM_BACKEND"


def resolve_backend(backend: str | None, request: dict[str, Any]) -> tuple[str, bool]:
    """``(backend, from_host_default)`` by the precedence ``--backend`` flag >
    ``request["backend"]`` > ``$KLT_SIM_BACKEND`` > ``"local"``.

    ``from_host_default`` is ``True`` only when the value came from the
    environment -- the one source that may later be overridden back to
    ``local`` by :func:`_yield_host_default_backend`. An explicit flag or
    request field is never second-guessed. The name is not validated here;
    the caller's :data:`SUPPORTED_BACKENDS` check names the bad value.
    """
    if backend is not None:
        return backend, False
    requested = request.get("backend")
    if requested is not None:
        return requested, False
    env_value = os.environ.get(BACKEND_ENV, "").strip()
    if env_value:
        return env_value, True
    return "local", False


def _backend_source(backend: str | None, request: dict[str, Any]) -> str:
    """Where :func:`resolve_backend`'s answer came from: ``"flag"``,
    ``"request"``, ``"env"`` (``$KLT_SIM_BACKEND``) or ``"default"``
    (issue #2977, reported as ``environment.backend.source``)."""
    if backend is not None:
        return "flag"
    if request.get("backend") is not None:
        return "request"
    if os.environ.get(BACKEND_ENV, "").strip():
        return "env"
    return "default"


def _yield_host_default_backend(
    backend: str, from_host_default: bool, *, reason_ok: bool
) -> str:
    """Step an env-sourced off-host default back to ``local`` when
    ``reason_ok`` is ``False`` -- i.e. the run cannot or should not leave
    this host (an engine the off-host toolchain does not ship, a resumed
    checkpoint, a single-unit probe that would spend minutes waiting on a
    Spot instance to answer one corner). A no-op for an explicit choice.
    """
    if from_host_default and backend in _OFFHOST_BACKENDS and not reason_ok:
        return "local"
    return backend


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
DEFAULT_MC_SEED_SCOPE = "per_corner"

#: Recognised values for ``request.monte_carlo.seed_mode`` -- how each
#: sample's ``rndseed`` (the integer written as ``.options seed=``) is
#: produced (issue #4173). ``derived`` (the default) is the SHA-256
#: construction; ``sequential`` pins it to ``seed + sample_index`` so a
#: campaign with a documented ``base + draw_index`` rule can be re-expressed
#: draw-for-draw. ``process_seed``/``mismatch_seed`` stay derived either way.
SUPPORTED_MC_SEED_MODES = ("derived", "sequential")
DEFAULT_MC_SEED_MODE = "derived"

#: Per-``(pdk_family, device_family)`` mismatch-activity table consulted by
#: :func:`_mismatch_family_report` (issue #355). A vendor model deck's global
#: mismatch switch/section does not necessarily enable *every* device
#: family's per-instance variation -- gf180mcu's poly-resistor subcircuits
#: carry the `(1 + mis_r * <switch>)` hook, but `mis_r`'s default is a
#: hardcoded ``0`` and the accompanying Pelgrom-style sigma formula
#: (``var_r = ... / sqrt(par*r_l*r_w)``, ``mis_r = agauss(0, var_r, 1)``)
#: ships **commented out** in the vendored deck, while MOS and BJT ship that
#: formula active under the same switch. A run that turns mismatch on gets
#: MOS/BJT variation and silently gets none for the resistor family, with
#: nothing in the request, deck, log, or (pre-#355) response distinguishing
#: that from "this family contributes negligibly". This table is the tool's
#: side of that gap: curated, documented per-family knowledge, cited to the
#: same vendored decks ``docs/cli/sim.md``'s "Known-good mismatch
#: mechanisms" section already documents. A ``(pdk_family, device_family)``
#: pair absent here -- including an unrecognised ``pdk_family`` -- is
#: reported with ``active: None`` ("not independently verified"), never
#: assumed active.
_MISMATCH_ACTIVITY: dict[tuple[str, str], dict[str, Any]] = {
    ("gf180mcu", "mosfet"): {
        "active": True,
        "note": (
            "MOS per-instance mismatch (the fets_mm include) is gated by "
            "the model-level sw_stat_mismatch switch and scales the "
            "per-device delvto/mulu0 terms -- active whenever that switch "
            "is set (see docs/cli/sim.md's 'Known-good mismatch "
            "mechanisms')."
        ),
    },
    ("gf180mcu", "bipolar"): {
        "active": True,
        "note": (
            "BJT per-instance mismatch (the bjt_mc include) is gated by "
            "the same sw_stat_mismatch switch as MOS -- active whenever "
            "that switch is set (see docs/cli/sim.md's 'Known-good "
            "mismatch mechanisms')."
        ),
    },
    ("gf180mcu", "resistor"): {
        "active": False,
        "note": (
            "The poly-resistor subcircuits' per-instance mismatch hook "
            "(mis_r, scaling body resistance via `(1 + mis_r * <switch>)`) "
            "defaults to a hardcoded 0, and the accompanying Pelgrom-style "
            "sigma formula ships commented out in the vendored deck -- "
            "resistor mismatch is structurally disabled regardless of "
            "sw_stat_mismatch. A run with mismatch enabled samples no "
            "resistor variation. See issue #355."
        ),
    },
    ("gf180mcu", "capacitor"): {
        "active": False,
        "note": (
            "The MIM-capacitor subcircuits (cap_mim_*) reference only the "
            "global sw_stat_global terms (one draw per run shared by every "
            "instance); sw_stat_mismatch is not referenced inside them, so "
            "no per-instance capacitor mismatch is sampled. See issue #2898."
        ),
    },
    ("sky130", "mosfet"): {
        "active": True,
        "note": (
            "MOS per-instance mismatch is active in any _mm-suffixed "
            "process-corner section (e.g. tt_mm) -- select that section "
            "via corners.process to sample it (see docs/cli/sim.md's "
            "'Known-good mismatch mechanisms')."
        ),
    },
    ("sky130", "bipolar"): {
        "active": True,
        "note": (
            "BJT per-instance mismatch is active in any _mm-suffixed "
            "process-corner section, validated by sky130-bandgap's "
            "pnp-mismatch simulation harness (see docs/cli/sim.md's "
            "'Known-good mismatch mechanisms')."
        ),
    },
    ("sky130", "resistor"): {
        "active": True,
        "note": (
            "Poly-resistor subcircuits (e.g. sky130_fd_pr__res_high_po_*) "
            "draw a per-instance AGAUSS term (res_match, rend) scaled by "
            "MC_MM_SWITCH, which any _mm-suffixed process-corner section "
            "(e.g. tt_mm) sets to 1 -- identical instances sample "
            "independent values (see docs/cli/sim.md's 'Known-good "
            "mismatch mechanisms'). Corner-level var_mult is a separate, "
            "shared term. See issue #3032."
        ),
    },
    # Section-dependent: the static entry is only the "section unknown"
    # fallback; `_sg13g2_mosfet_activity` refines it per selected section.
    ("sg13g2", "mosfet"): {
        "active": None,
        "note": (
            "MOS per-instance mismatch (sg13_lv_*/sg13_hv_* subcircuits) is "
            "active only in a *_mismatch process-corner section (e.g. "
            "mos_tt_mismatch); the selected section could not be determined "
            "(set corners.process). See issue #2961."
        ),
    },
}

#: SPICE element-type letter (first character of the instance name) ->
#: canonical device family, for a circuit body written with plain primitive
#: elements rather than PDK subcircuit calls. Used by
#: :func:`_classify_device_family`.
_ELEMENT_TYPE_FAMILY: dict[str, str] = {
    "R": "resistor",
    "C": "capacitor",
    "D": "diode",
    "Q": "bipolar",
    "M": "mosfet",
}

#: Case-insensitive substrings recognised in an ``X`` (subcircuit call)
#: line's own text -- covers a circuit body written against PDK subcircuit
#: calls (e.g. ``XM1 ... nfet_03v3 ...``), where the device family is
#: carried in the subcircuit name rather than the element-type letter.
#: First match wins (order matters only for a name that would otherwise
#: match more than one entry, which the PDK naming conventions this table
#: was built against do not produce).
_SUBCKT_FAMILY_KEYWORDS: tuple[tuple[str, str], ...] = (
    ("nfet", "mosfet"),
    ("pfet", "mosfet"),
    ("mosfet", "mosfet"),
    ("npn", "bipolar"),
    ("pnp", "bipolar"),
    ("bjt", "bipolar"),
    ("moscap", "capacitor"),
    ("mimcap", "capacitor"),
    ("cap_", "capacitor"),
    ("capacitor", "capacitor"),
    # sg13g2 MOS calls (``sg13_lv_nmos``/``sg13_hv_pmos``, issue #2961); after
    # the capacitor keywords so a ``..._nmoscap`` name still wins as capacitor.
    ("nmos", "mosfet"),
    ("pmos", "mosfet"),
    ("res_", "resistor"),
    ("resistor", "resistor"),
    ("diode", "diode"),
)


#: gf180mcu's own resistor subcircuit names (``ppolyf_u_1k``,
#: ``npolyf_s_1k``, ``nplus_u_*``, ``pplus_s_*``, ``rm1``..``rm3``/``tm*``
#: metal resistors) carry no ``res_`` keyword (issue #2898), so they are
#: matched against the X line's subcircuit-name token (the last token
#: before any ``name=value`` parameters) rather than the whole line.
_SUBCKT_NAME_FAMILY_RE: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(?:ppolyf|npolyf|nplus|pplus)_[a-z0-9_]+"), "resistor"),
    (re.compile(r"(?:rm|tm)\d+[a-z0-9_]*"), "resistor"),
)


def _subckt_name_token(line: str) -> str | None:
    """Subcircuit name of an ``X`` call line: the last token before the
    first ``name=value`` parameter (or the last token), lower-cased."""
    tokens = line.split()[1:]
    plain: list[str] = []
    for tok in tokens:
        if "=" in tok:
            break
        plain.append(tok)
    return plain[-1].lower() if len(plain) > 1 else None


def _classify_device_family(element_type: str, line: str) -> str | None:
    """Canonical device family for one netlist element line, or ``None``
    when ``element_type`` never names a PDK device (sources, controlled
    sources, transmission lines, ...). ``element_type`` is the instance
    name's first character, upper-cased; ``line`` is the full stripped
    element line (only consulted for ``X`` subcircuit calls, whose family
    lives in the referenced subcircuit name -- see
    :data:`_SUBCKT_FAMILY_KEYWORDS`). An ``X`` call matching no known
    keyword is classified ``"other"`` rather than dropped -- an
    unrecognised subcircuit may still be a mismatch-relevant device this
    table simply has no PDK-specific knowledge of yet."""
    if element_type in _ELEMENT_TYPE_FAMILY:
        return _ELEMENT_TYPE_FAMILY[element_type]
    if element_type == "X":
        name = _subckt_name_token(line)
        if name is not None:
            for pattern, family in _SUBCKT_NAME_FAMILY_RE:
                if pattern.fullmatch(name):
                    return family
        lowered = line.lower()
        for keyword, family in _SUBCKT_FAMILY_KEYWORDS:
            if keyword in lowered:
                return family
        return "other"
    return None


def _detect_device_families(netlist_text: str) -> list[str]:
    """Distinct device families ``netlist_text`` instantiates, in
    first-seen order. Comment (``*``/``;``), continuation (``+``), and
    dot-command (``.subckt``/``.control``/...) lines never name a device
    and are skipped, along with blank lines."""
    families: list[str] = []
    seen: set[str] = set()
    for raw_line in netlist_text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith(("*", ";", "+", ".")):
            continue
        element_type = line[0].upper()
        family = _classify_device_family(element_type, line)
        if family is not None and family not in seen:
            seen.add(family)
            families.append(family)
    return families


def _mismatch_scan_closure(
    request: dict[str, Any] | None, netlist_path: str, repo_root: str | None
) -> tuple[list[tuple[str, str]], list[dict[str, str]]]:
    """Files ``family_mismatch`` scans (issue #2928): the top-level netlist
    plus its resolved ``.include``/``.inc`` closure (same resolver as
    ``environment.netlist_closure``). Returns ``([(report_path, abs_path)],
    not_scanned)``; ``not_scanned`` lists directives left to the executing
    host (``env_var``/``pdk_root``) or the whole closure when it cannot be
    resolved -- the top-level file is still scanned in that case."""
    from . import env_provenance

    top = (
        env_provenance.repo_relative_path(netlist_path, repo_root=repo_root),
        netlist_path,
    )
    try:
        staged = sim_staging.stage_netlist(
            netlist_path,
            netlist_staged_name="netlist.cir",
            roots=sim_staging.host_resolved_roots(request or {}),
        )
    except sim_staging.IncludeStagingError as exc:
        return (
            [(_scan_label(top[0], netlist_path), netlist_path)],
            [{"target": "<include closure>", "reason": f"unresolved: {exc}"}],
        )
    files: list[tuple[str, str]] = []
    for item in staged.files:
        source = item.source_path or item.local_path
        # stage_netlist always sets one of the two (source_path whenever
        # content rewrote the file, local_path otherwise).
        assert source is not None
        info = env_provenance.repo_relative_path(source, repo_root=repo_root)
        files.append((_scan_label(info, source), source))
    not_scanned = [
        {"target": target, "reason": reason}
        for target, reason in staged.host_resolved_detail
    ]
    return files, not_scanned


def _scan_label(info: dict[str, Any], source: str = "") -> str:
    """Report label for a scanned file: repo-relative path, else
    ``<scope>/<basename>`` (never an absolute external path, like other
    report fields)."""
    return (
        info.get("path")
        or f"<{info.get('scope', 'external')}>/{os.path.basename(source)}"
    )


def _sg13g2_mosfet_activity(sections: list[str] | None) -> dict[str, Any] | None:
    """Section-dependent ``("sg13g2", "mosfet")`` activity (issue #2961), or
    ``None`` to keep the static table entry. IHP's ``cornerMOSlv/hv.lib``
    include the per-instance ``agauss()`` mismatch subcircuits only in
    ``mos_<corner>_mismatch`` sections; plain ``mos_<corner>`` and
    ``mos_tt_stat`` sections carry none."""
    if not sections:
        return None
    flags = {name.lower().endswith("_mismatch") for name in sections}
    if flags == {True}:
        return {
            "active": True,
            "note": (
                "MOS per-instance mismatch is active: every selected "
                "process section is a *_mismatch section "
                f"({', '.join(sorted(set(sections)))}), which includes the "
                "agauss() mismatch subcircuits (see docs/cli/sim.md's "
                "'Known-good mismatch mechanisms')."
            ),
        }
    if flags == {False}:
        return {
            "active": False,
            "note": (
                "No selected process section "
                f"({', '.join(sorted(set(sections)))}) is a *_mismatch "
                "section; plain mos_<corner> and mos_tt_stat sections carry "
                "no per-instance mismatch, so no MOS mismatch is sampled -- "
                "select e.g. mos_tt_mismatch via corners.process."
            ),
        }
    return None


def _mismatch_family_report(
    netlist_path: str,
    pdk_variant: str | None,
    *,
    request: dict[str, Any] | None = None,
    process_sections: list[str] | None = None,
    repo_root: str | None = None,
    scan: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Per-device-family mismatch-activity report for a ``monte_carlo.vary``
    request that includes ``"mismatch"`` (issue #355).

    For every distinct device family :func:`_detect_device_families` finds
    in the netlist, states whether that family's per-instance mismatch is
    structurally active in the selected PDK's model deck, per
    :data:`_MISMATCH_ACTIVITY` -- so a hard-zero-mismatch family (e.g.
    gf180mcu's poly resistor) is never left indistinguishable from a family
    that actually got sampled just because the request's global mismatch
    switch/section was engaged. A family with no curated table entry --
    including when ``pdk_variant`` is absent/unrecognised -- gets
    ``active: None`` ("not independently verified"), never a guessed
    ``True``/``False``.
    """
    # Issue #2928: scan the whole `.include` closure, not just the top-level
    # file, so a wrapper testbench reports the families its included flat
    # netlist actually instantiates. When ``scan`` is passed it is filled
    # with ``{"scanned": [...], "not_scanned": [...]}`` for the caller.
    files, not_scanned = _mismatch_scan_closure(request, netlist_path, repo_root)
    families: list[str] = []
    seen_families: set[str] = set()
    for _label, path in files:
        try:
            with open(path, encoding="utf-8") as handle:
                text = handle.read()
        except (OSError, UnicodeDecodeError):
            not_scanned.append({"target": _label, "reason": "unreadable"})
            continue
        for family in _detect_device_families(text):
            if family not in seen_families:
                seen_families.add(family)
                families.append(family)
    if scan is not None:
        scan["scanned"] = [label for label, _ in files]
        scan["not_scanned"] = not_scanned

    pdk_family = pdk_variant_family(pdk_variant) if pdk_variant else None

    report: list[dict[str, Any]] = []
    for family in families:
        entry = _MISMATCH_ACTIVITY.get((pdk_family, family)) if pdk_family else None
        if (pdk_family, family) == ("sg13g2", "mosfet"):
            entry = _sg13g2_mosfet_activity(process_sections) or entry
        if entry is not None:
            report.append(
                {"family": family, "active": entry["active"], "note": entry["note"]}
            )
        else:
            if pdk_family is None:
                note = (
                    f"PDK family could not be determined for this request "
                    f"(set models.pdk) -- mismatch activity for the "
                    f"'{family}' family could not be verified; treat any "
                    f"sampled spread for it as unconfirmed."
                )
            else:
                note = (
                    f"mismatch activity for the '{family}' family is not "
                    f"independently verified for PDK family '{pdk_family}'; "
                    f"treat any sampled spread for it as unconfirmed."
                )
            report.append({"family": family, "active": None, "note": note})
    return report


#: Quantiles (percentiles, ``0``-``100``) reported per measurement for a
#: Monte Carlo sample set when ``request.monte_carlo.quantiles`` is omitted:
#: the median plus the symmetric 5%/95% tails, which is what both canary
#: repos' hand-rolled rollups report today. Percentiles rather than
#: ``0``-``1`` fractions so the response keys read as ``p5``/``p50``/``p95``
#: -- see :func:`_sample_statistics` and docs/cli/sim.md's "Monte Carlo
#: statistics" section.
DEFAULT_MC_QUANTILES = (5.0, 50.0, 95.0)

#: Assumed ngspice worker-thread count used to derive the ``local-parallel``
#: backend's conservative default ``max_workers`` -- see :func:`_default_max_workers`.
#: Each ``ngspice -b`` process is itself internally multi-threaded (BLAS/matrix
#: solve), so a naive one-worker-per-corner pool oversubscribes a small box
#: immediately (see #253/#168's design note); this factor is a deliberately
#: conservative estimate, not a measured value.
_ASSUMED_THREADS_PER_NGSPICE = 8

#: Host-level upper bound on the ``local-parallel`` worker pool (issue #2286),
#: read from the environment rather than from any request document: the box a
#: sweep happens to land on is a property of the *host*, not of the design
#: being simulated, and the callers that oversubscribe a shared box (this
#: package's own test suite, agent-driven runs) are exactly the ones that never
#: pass ``--max-workers``. Set it once in the shared box's environment and
#: every entry point -- CLI, library call, ``pytest`` -- is bounded by it. See
#: :func:`_host_max_workers_cap` and docs/cli/sim.md's "Useful on a
#: workstation, harmful on a shared worker" section.
MAX_WORKERS_ENV = "KLT_SIM_MAX_WORKERS"

#: Clamp notices already printed for the sweep in progress, plus its guard --
#: see :func:`_worker_cap_notice`/:func:`_reset_worker_cap_notices`.
_WORKER_CAP_NOTICES_EMITTED: set[str] = set()
_WORKER_CAP_NOTICE_LOCK = threading.Lock()

#: Recognised ``.meas`` failure line, e.g.
#: `` .meas tran vout_high find v(out) when v(out)=5 failed!``
_MEAS_FAILED_RE = re.compile(
    r"^\s*\.meas\s+\S+\s+(\S+)\b.*\bfailed!\s*$", re.IGNORECASE
)

#: A completed ``.meas`` scalar line, e.g. ``vout_final          =  1.500e+00``
#: (optionally followed by arbitrary trailing ``word=value`` pairs -- e.g.
#: ``from=... to=...`` for AVG/etc, or ``targ=... trig=...`` for a
#: TRIG/TARG delay measurement like
#: ``tphl                =  4.02e-11 targ=  1.09e-09 trig=  1.05e-09`` --
#: which are not captured; only the measurement's own value is). ``print``
#: output has been seen to use the same ``name = value`` shape, so this
#: pattern is shared with the print fallback.
_MEAS_VALUE_RE = re.compile(
    r"^([A-Za-z_][\w.]*)\s*=\s*([+-]?[\d.]+(?:[eE][+-]?\d+)?)\s*(?:\S+=.*)?$"
)

#: Ordered diagnostic classifiers: (code, compiled pattern). Order matters --
#: singular-matrix and nonconvergence text can co-occur in one log (ngspice
#: tries gmin/source stepping as a nonconvergence *recovery* strategy after a
#: singular matrix) even when the corner's requested measurements ultimately
#: come back correctly -- see ``_run_corner``'s post-hoc recovery check,
#: which downgrades both codes' severity together in that case (issue #205).
_DIAGNOSTIC_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "singular_matrix",
        re.compile(r"^\s*Warning:\s*singular matrix", re.IGNORECASE | re.MULTILINE),
    ),
    (
        # ngspice's own transient abort, e.g.
        # `doAnalyses: TRAN:  Timestep too small; time = 1.0001e-09,
        # timestep = 5e-23: trouble with node "v1#branch"` (issue #2918).
        # Spelled without a space -- the generic `nonconvergence` entry's
        # `time step too small` alternative (Xyce/older wording) never matched
        # it, so such a run surfaced only as one `measurement` diagnostic per
        # `.meas`. Its own code because it is a *terminal* solver abort with a
        # time and a culprit node, never stepping-recovery narration: it is
        # deliberately absent from the `_recovered_from_stepping` downgrade.
        # Structured fields come from `_TIMESTEP_TOO_SMALL_RE`.
        "timestep_too_small",
        re.compile(r"Timestep too small", re.IGNORECASE),
    ),
    (
        "nonconvergence",
        re.compile(
            r"iteration limit reached"
            r"|gmin stepping failed"
            r"|source stepping failed"
            r"|no convergence"
            r"|time step too small"
            r"|doAnalyses:\s*.*iteration",
            re.IGNORECASE,
        ),
    ),
    (
        # ngspice's own text when a `.include`/`.inc` target does not
        # resolve, e.g. `Error: Could not find include file 07-reference.spice`
        # (verified against ngspice 46). Its own line is the whole diagnosis,
        # but everything that follows it -- every device the missing file
        # would have defined, then every `.meas` that found no vector -- reads
        # like a broken circuit rather than a missing file, so without its own
        # code the corner surfaces only as `measurement`/`unavailable_
        # measurement` rows indistinguishable from a real regression (issue
        # #2485). Classified ahead of the generic `netlist` code below: this
        # is the more specific reading of the same class of failure.
        "missing_include",
        re.compile(
            # The second form is ngspice 42's wording for the same failure
            # (`.include filename missing`, issue #2926).
            r"^\s*Error:\s*Could not find include file"
            r"|^\s*(?:Error:\s*)?\.inc(?:lude)?\s+filename\s+missing",
            re.IGNORECASE | re.MULTILINE,
        ),
    ),
    (
        # ngspice's own text when a `.meas`/`.print` reference names a node
        # or branch ngspice's current plot has no vector for, e.g.
        # `Error: no such vector as v(mid).` (verified against ngspice 46,
        # issue #2521). This fires for *both* of two causes ngspice's log
        # gives no way to tell apart: the name is a genuine typo/dead node,
        # or the vector exists but fell outside the saved set -- e.g. a
        # restrictive `.save` card the netlist body carries, from before
        # `_write_corner_deck`'s own `save all` (below) started restoring
        # the full set by default, or (under `options.save_mode: "netlist"`,
        # issue #2732) a node the body's own `.save` deliberately
        # excluded. Verified empirically: a vector genuinely absent from the
        # circuit and a vector merely unsaved both produce the identical
        # `no such vector as ...` line -- there is no ngspice-side signal
        # to distinguish them, so this code names the *symptom*
        # ("nothing came back for this name") rather than inventing an
        # unreliable heuristic about the cause. It is emitted *in addition
        # to* the per-measurement generic `measurement` diagnostic (see
        # `_run_corner`), not instead of it -- this is strictly more
        # specific information layered on top.
        "no_such_vector",
        re.compile(r"^Error:\s*no such vector as", re.IGNORECASE | re.MULTILINE),
    ),
    (
        # ngspice aborts the whole file at its 99th-plus `par()` call: `ERROR:
        # More than 99 function calls to par()` (verified against the ngspice
        # binary's own string table, issue #4084). Every `.meas` then reports
        # only the generic `produced no value`, so this names the cap and the
        # workaround. Ahead of the generic `netlist` entry, which would not
        # match it anyway; the per-measurement errors stay alongside.
        "par_call_cap",
        re.compile(
            r"^\s*(?:Error:\s*)?More than \d+ function calls to par\(\)",
            re.IGNORECASE | re.MULTILINE,
        ),
    ),
    (
        "netlist",
        re.compile(
            r"^Error:\s*.*\b(syntax|unknown|undefined|parse|subckt)\b",
            re.IGNORECASE | re.MULTILINE,
        ),
    ),
    (
        # ngspice's own text when a `.model ... level = N` card names a MOS
        # level this binary was not built with, e.g. ASAP7's BSIM-CMG
        # `level = 72` on a stock ngspice (`Only MOS device levels
        # 1-6,8-10,14,49,54-58,60,68,73 are supported in this binary`,
        # issue #2814). It is a missing *capability*, not a netlist or bin
        # problem, so it gets its own code and (see `_classify_diagnostics`)
        # suppresses the misleading `model_bin_range` follow-on error.
        "unsupported_model_level",
        re.compile(
            r"^.*Only MOS device levels .* supported in this binary",
            re.IGNORECASE | re.MULTILINE,
        ),
    ),
    (
        # ngspice rejecting a compiled `.osdi` whose OSDI ABI it cannot
        # load, e.g. `NGSPICE only supports OSDI v0.3 but "x.osdi" targets
        # v0.4!` (ngspice < 44 against an OpenVAF-Reloaded module).
        "osdi_abi_mismatch",
        re.compile(r"NGSPICE only supports OSDI v\d+\.\d+ but", re.IGNORECASE),
    ),
    (
        # `Error: Library <x>.osdi couldn't be loaded!` -- the module file is
        # unreadable or not a shared library this ngspice can dlopen (wrong
        # platform, or built against another OSDI runtime).
        "osdi_load_failed",
        re.compile(
            r"^Error:\s*Library\s+.*\.osdi\s+couldn't be loaded",
            re.IGNORECASE | re.MULTILINE,
        ),
    ),
    (
        # ngspice's own diagnostic when a BSIM model card's per-instance
        # width *bin range* is exceeded, e.g. gf180mcu's undocumented
        # ~100 um ceiling on `nfet_06v0`/`pfet_06v0` (issue #1214). The raw
        # message says nothing about width or even that a model was
        # matched by name at all -- on its own it reads as "wrong device
        # name" or "model library did not load", pointing away from the
        # actual cause. `_diagnose_modelname_error` inspects the netlist's
        # own device instances to name a likely culprit before falling
        # back to ngspice's raw line.
        "model_bin_range",
        re.compile(
            r"^Error:\s*could not find a valid modelname", re.IGNORECASE | re.MULTILINE
        ),
    ),
    (
        # The generic shapes of an ngspice deck abort (issue #3879): `Error
        # on line N ...` followed by `Simulation interrupted due to error!`,
        # or `fatal error in ngspice`. Last on purpose: every specific
        # pattern above names a cause, so this one is only the fallback cause
        # (`_classify_diagnostics` drops it when a specific fatal code was
        # already classified). The message is the first matching log line.
        "ngspice_fatal",
        re.compile(
            r"^\s*Error on line \d+"
            r"|Simulation interrupted due to error"
            r"|fatal error in ngspice",
            re.IGNORECASE | re.MULTILINE,
        ),
    ),
)

#: Codes that mean ngspice aborted while reading the deck (issue #3879). When
#: one of them is classified and the log shows no analysis banner, the run
#: never started: its per-measurement "produced no value" errors are
#: consequences and are collapsed (`_demote_abort_cascade`).
_NGSPICE_FATAL_CODES: frozenset[str] = frozenset(
    {"missing_include", "netlist", "model_bin_range", "ngspice_fatal"}
)

#: What ngspice prints once an analysis actually begins (or has produced
#: data); absent from a run that died in the deck parser.
_ANALYSIS_STARTED_RE = re.compile(
    r"Doing analysis at TEMP|No\. of Data Rows|Initial Transient Solution",
    re.IGNORECASE,
)

#: Appended to the cause diagnostic of a run that never started analysis.
_NO_ANALYSIS_NOTE = (
    "; no analysis started (the run's runtime_s is wall-clock before the "
    "failure, not simulation time)"
)


def _mark_analysis_not_started(
    diagnostics: list[dict[str, Any]], log_text: str
) -> bool:
    """Issue #3879: when ``diagnostics`` hold an ngspice deck-abort code and
    ``log_text`` shows no analysis ever began, tag every such diagnostic with
    the additive ``analysis_started: false`` field and a message note, and
    return ``True``. A failure mid-analysis returns ``False`` untouched."""
    fatal = [
        d
        for d in diagnostics
        if d.get("code") in _NGSPICE_FATAL_CODES and d.get("severity") == "error"
    ]
    if not fatal or _ANALYSIS_STARTED_RE.search(log_text):
        return False
    for diagnostic in fatal:
        diagnostic["analysis_started"] = False
        if _NO_ANALYSIS_NOTE not in diagnostic["message"]:
            diagnostic["message"] += _NO_ANALYSIS_NOTE
    return True


#: Prerequisite messages (issue #2814) for the capability diagnostics: they
#: name the missing capability instead of leaving the caller to decode
#: ngspice's raw line. ``{raw}`` is ngspice's own line, kept verbatim.
_PREREQUISITE_MESSAGES: dict[str, str] = {
    "unsupported_model_level": (
        "prerequisite missing: this ngspice build cannot run the model "
        "card's MOS level (BSIM-CMG `level = 72` needs a compiled OSDI "
        "module declared in options.osdi_preload; see docs/cli/sim.md "
        '"ASAP7 FinFET (BSIM-CMG) simulation") -- ngspice said: {raw}'
    ),
    "osdi_abi_mismatch": (
        "prerequisite missing: ngspice cannot load the OSDI module's ABI "
        "(an OpenVAF-Reloaded module needs ngspice >= 44; build it with "
        "scripts/fetch-asap7-sim-toolchain.sh for ASAP7 or "
        "scripts/fetch-sg13g2-sim-toolchain.sh for IHP SG13G2) -- "
        "ngspice said: {raw}"
    ),
    "osdi_load_failed": (
        "prerequisite missing: ngspice could not load the OSDI module "
        "(wrong platform or OSDI ABI; rebuild it with "
        "scripts/fetch-asap7-sim-toolchain.sh (ASAP7) or "
        "scripts/fetch-sg13g2-sim-toolchain.sh (SG13G2) on this host's "
        "ngspice) -- "
        "ngspice said: {raw}"
    ),
}


#: ``SimError.code`` for a request whose ``engine_version`` pin the resolved
#: engine does not satisfy (issue #3041): the *tool* is unavailable at the
#: pinned build. Distinct from :data:`UNSUPPORTED_ANALYSIS_CODE`.
ENGINE_VERSION_UNMET_CODE = "engine_version_unmet"

#: ``SimError.code`` for a request whose ``models.pdk_version`` pin the
#: installed PDK does not satisfy (issue #4046): the *PDK* drifted from the
#: pinned revision, or could not be identified.
PDK_VERSION_UNMET_CODE = "pdk_version_unmet"

#: ``SimError.code`` for an analysis kind `klt sim` has no implementation for
#: (issue #3041): the *capability* is absent, whatever engine build is pinned.
UNSUPPORTED_ANALYSIS_CODE = "unsupported_analysis"

#: Periodic small-signal / noise analysis kinds a request may name but `klt
#: sim` does not implement (no PSS/pnoise/pac; see also issue #2985).
UNSUPPORTED_ANALYSIS_KINDS: tuple[str, ...] = ("pss", "pnoise", "pac")


def reject_unsupported_analysis(kind: Any) -> None:
    """Raise :class:`SimError` (``code="unsupported_analysis"``) when ``kind``
    names a periodic analysis (issue #3041) -- before any corner runs, so
    callers can record "capability absent" apart from "tool unavailable"."""
    if isinstance(kind, str) and kind.lower() in UNSUPPORTED_ANALYSIS_KINDS:
        raise SimError(
            f"analysis.kind {kind!r} is not supported: `klt sim` implements no "
            "periodic small-signal/noise analysis "
            f"({', '.join(UNSUPPORTED_ANALYSIS_KINDS)}); "
            'see docs/cli/sim.md "Supported analyses"',
            code=UNSUPPORTED_ANALYSIS_CODE,
        )


def _version_components(text: str) -> tuple[int, ...] | None:
    """Leading dotted integer components of a version string (``"44.2"`` ->
    ``(44, 2)``, ``"7.10.0-opensource"`` -> ``(7, 10, 0)``), or ``None``."""
    match = re.match(r"\s*(\d+(?:\.\d+)*)", text)
    if match is None:
        return None
    return tuple(int(part) for part in match.group(1).split("."))


def _validate_engine_version_pin(request: dict[str, Any]) -> tuple[int, ...] | None:
    """The request's ``engine_version`` pin as ``(major,)`` or ``(major,
    minor)`` (issue #3041), ``None`` when unset."""
    pin = request.get("engine_version")
    if pin is None:
        return None
    if not isinstance(pin, str) or not re.fullmatch(r"\d+(?:\.\d+)?", pin.strip()):
        raise SimError(
            "request.engine_version must be a string of the form 'MAJOR' or "
            f"'MAJOR.MINOR' (got {pin!r})"
        )
    return _version_components(pin) or None


def _check_engine_version_pin(
    pin: tuple[int, ...], found: str | None, engine: str, *, where: str
) -> None:
    """Fail closed with ``engine_version_unmet`` unless ``found`` matches the
    pin on every component the pin states (``"44"`` pins the major only)."""
    required = ".".join(str(part) for part in pin)
    parsed = _version_components(found) if found else None
    if parsed is not None and parsed[: len(pin)] == pin:
        return
    if found is None:
        detail = f"{where} reported no {engine} version"
    elif parsed is None:
        detail = f"{where} reported an unparseable {engine} version {found!r}"
    else:
        detail = f"{where} found {engine} {found}"
    raise SimError(
        f"pinned simulator unavailable: request requires {engine} {required}; {detail}",
        code=ENGINE_VERSION_UNMET_CODE,
    )


def _check_local_engine_version_pin(
    pin: tuple[int, ...] | None, backend: str, engine: str, binary: str
) -> None:
    """Enforce the pin before any corner runs, where this process can see the
    engine (local backends); a no-op when unset or for off-host backends."""
    if pin is None or backend in _OFFHOST_BACKENDS:
        return
    _check_engine_version_pin(
        pin,
        _probe_engine_version(engine, binary),
        engine,
        where="the local engine probe",
    )


def _check_offhost_engine_version_pin(
    pin: tuple[int, ...] | None,
    backend: str,
    engine: str,
    engine_version: str | None,
) -> None:
    """Enforce the pin for off-host backends. No pre-submit probe exists for a
    remote/batch runner, so it is checked against the version its reports
    carried (after the run)."""
    if pin is None or backend not in _OFFHOST_BACKENDS:
        return
    _check_engine_version_pin(
        pin, engine_version, engine, where=f"the {backend} runner's report"
    )


def _validate_pdk_version_pin(models: Any) -> str | None:
    """The request's ``models.pdk_version`` pin (issue #4046), ``None`` when
    unset. Same value forms as ``klt pdk check --expect-version``."""
    if not isinstance(models, dict):
        return None
    pin = models.get("pdk_version")
    if pin is None:
        return None
    if not isinstance(pin, str) or not pin.strip():
        raise SimError(
            f"request.models.pdk_version must be a non-empty string (got {pin!r})"
        )
    return pin.strip()


def _check_pdk_version_pin(pin: str | None, models: Any, backend: str) -> None:
    """Enforce ``models.pdk_version`` before any corner runs (issue #4046).

    Local backends resolve the PDK the way ``provenance.pdk`` does and fail
    closed (``pdk_version_unmet``) on ``mismatch``/``unknown`` or when no PDK
    resolves. Off-host backends reject the field: the worker-side comparison
    is tracked in #3462, and silently passing would hide drift."""
    if pin is None:
        return
    if backend in _OFFHOST_BACKENDS:
        raise SimError(
            f"models.pdk_version is not supported for backend {backend!r}: "
            "the PDK is resolved on the runner, and the worker-side pin check "
            "is not implemented yet (see issue #3462) -- use backend 'local' "
            "or 'local-parallel', or drop the pin"
        )
    variant = models.get("pdk")
    root = models.get("pdk_root")
    if not (variant or root):
        raise SimError(
            f"models.pdk_version pins {pin!r} but no PDK is named: set "
            "models.pdk and/or models.pdk_root so the installed revision can "
            "be checked",
            code=PDK_VERSION_UNMET_CODE,
        )
    try:
        found = find_pdk(variant=variant, root=root, expect_version=pin)
    except PdkNotFoundError as exc:
        raise SimError(
            f"models.pdk_version pins {pin!r} but the PDK could not be resolved: {exc}",
            code=PDK_VERSION_UNMET_CODE,
        ) from exc
    expectation = found.get("version_expectation") or {}
    status = expectation.get("status")
    if status == "match":
        return
    installed = expectation.get("installed")
    if status == "mismatch":
        detail = f"installed PDK is {installed!r}"
    elif installed:
        detail = f"installed PDK {installed!r} could not be compared"
    else:
        detail = "installed PDK identity could not be determined"
    raise SimError(
        f"pinned PDK unavailable: request requires {pin!r}; {detail}",
        code=PDK_VERSION_UNMET_CODE,
    )


def _probe_engine_version(engine: str, binary: str) -> str | None:
    """Ask the local engine binary for its version without simulating."""
    argv = [binary, "--version"] if engine == "ngspice" else [binary, "-v"]
    try:
        proc = subprocess.run(
            argv, capture_output=True, text=True, timeout=30, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return None
    text = (proc.stdout or "") + (proc.stderr or "")
    if engine == "ngspice":
        return _extract_engine_version(text)
    match = re.search(r"Release\s+([\w.]+)", text)
    return match.group(1) if match else None


#: Issue #4438: first ngspice major that loads an OSDI ABI newer than v0.3
#: (OpenVAF-Reloaded modules); ngspice <= 43 hard-rejects anything but 0.3.
OSDI_V04_MIN_NGSPICE_MAJOR = 44


def _read_osdi_abi(path: str) -> tuple[int, int] | None:
    """The ``(major, minor)`` OSDI ABI a compiled ``.osdi`` declares through
    its exported ``OSDI_VERSION_MAJOR``/``OSDI_VERSION_MINOR`` symbols, or
    ``None`` when it cannot be read (not loadable on this platform, symbols
    absent) -- the caller then fails open."""
    import ctypes

    try:
        lib = ctypes.CDLL(path)
        major = ctypes.c_uint32.in_dll(lib, "OSDI_VERSION_MAJOR").value
        minor = ctypes.c_uint32.in_dll(lib, "OSDI_VERSION_MINOR").value
    except (OSError, ValueError, AttributeError):
        return None
    return major, minor


def _check_local_osdi_abi(
    osdi_preload: tuple[str, ...], backend: str, engine: str, binary: str
) -> None:
    """Refuse before any corner runs when a declared ``.osdi`` needs an ABI
    newer than v0.3 and the local ngspice is older than 44 (issue #4438).
    Fails open on every unknown (unreadable module, unparseable version); the
    per-corner ``osdi_abi_mismatch`` log classifier stays as the backstop."""
    if not osdi_preload or engine != "ngspice" or backend in _OFFHOST_BACKENDS:
        return
    found: str | None = None
    probed = False
    for path in osdi_preload:
        abi = _read_osdi_abi(path)
        if abi is None or abi <= (0, 3):
            continue
        if not probed:
            found = _probe_engine_version(engine, binary)
            probed = True
        parsed = _version_components(found) if found else None
        if parsed is None or parsed[0] >= OSDI_V04_MIN_NGSPICE_MAJOR:
            return
        raise SimError(
            f"prerequisite missing: {os.path.basename(path)} targets OSDI "
            f"v{abi[0]}.{abi[1]}, which needs ngspice >= "
            f"{OSDI_V04_MIN_NGSPICE_MAJOR}, but the local ngspice is {found}; "
            "run the corner with --backend batch (the fleet image carries a "
            "new enough ngspice) or build/use an ngspice >= "
            f"{OSDI_V04_MIN_NGSPICE_MAJOR}",
            code="osdi_abi_mismatch",
        )


def _worker_ngspice_binary(remote: Any) -> str | None:
    """The worker-observed ngspice path out of ``environment.remote`` (issue
    #3110): ``worker_environment.simulator.selected_path`` when its capture is
    ``"ok"``. A sharded run (``remote.fleet[]``) reports a value only when
    every member captured the same path -- never silently picks one. ``None``
    for a pre-#2834 runner, the SSH ``remote`` backend, or disagreement."""
    if not isinstance(remote, dict):
        return None
    members = remote.get("fleet")
    blocks = members if isinstance(members, list) and members else [remote]
    paths: set[str | None] = set()
    for block in blocks:
        worker = block.get("worker_environment") if isinstance(block, dict) else None
        simulator = worker.get("simulator") if isinstance(worker, dict) else None
        ok = isinstance(worker, dict) and worker.get("capture") == "ok"
        path = (
            simulator.get("selected_path")
            if ok and isinstance(simulator, dict)
            else None
        )
        paths.add(path if isinstance(path, str) and path else None)
    only = next(iter(paths)) if len(paths) == 1 else None
    return only


def apply_engine_host(
    environment: dict[str, Any], *, backend: str, client_ngspice_binary: str | None
) -> None:
    """Stamp ``environment.engine_host`` and keep ``ngspice_binary`` honest
    (issue #3110). Local runs: ``engine_host: "local"``, nothing else changes.
    Off-host runs: ``"worker"``; ``ngspice_binary`` becomes the worker's
    observed path (or ``null``) and the submitter's resolved path moves to
    ``client_ngspice_binary``."""
    if backend not in _OFFHOST_BACKENDS:
        environment["engine_host"] = "local"
        return
    environment["engine_host"] = "worker"
    environment["client_ngspice_binary"] = client_ngspice_binary
    environment["ngspice_binary"] = _worker_ngspice_binary(environment.get("remote"))


#: Environment-variable override for the ngspice binary (issue #2423), the
#: ``sim.py`` port of ``lvs_netgen.py``'s ``$KLT_NETGEN_BINARY`` (issue
#: #2373) -- checked after ``options.ngspice_binary`` and before the bare
#: ``ngspice`` name on ``PATH``. No known distro-name collision for ngspice
#: (unlike netgen's ``netgen``/``netgen-lvs`` split), so a single bare name.
_NGSPICE_BINARY_ENV_VAR = "KLT_NGSPICE_BINARY"
_NGSPICE_BINARY_NAMES: tuple[str, ...] = ("ngspice",)


def _resolve_ngspice_binary(options: dict[str, Any], request_dir: str) -> str:
    """Resolve the ``ngspice`` binary to invoke for this sweep (issue
    #2423), via ``options.ngspice_binary`` -> ``$KLT_NGSPICE_BINARY`` ->
    ``ngspice`` on ``PATH`` (:func:`klayout_tools._paths.resolve_tool_binary`).

    Only called for ``engine == "ngspice"`` -- the ``xyce`` engine's own
    binary (:data:`XYCE_BINARY`) is out of this issue's scope (see that
    name's own docstring: no known override need, unlike ngspice, where
    more than one build/version commonly coexists on a host)."""
    return resolve_tool_binary(
        "ngspice",
        options,
        option_key="ngspice_binary",
        option_label="options.ngspice_binary",
        env_var=_NGSPICE_BINARY_ENV_VAR,
        request_dir=request_dir,
        fallback_names=_NGSPICE_BINARY_NAMES,
        error_cls=SimError,
        install_hint="Install ngspice (https://ngspice.sourceforge.io/).",
    )


def load_request(request_path: str) -> dict[str, Any]:
    """Read and minimally validate a ``klt sim`` request JSON file.

    Raises :class:`SimError` if the file is missing/unreadable, not valid
    JSON, or missing a required top-level field (``netlist``, and one of
    ``analysis`` or -- issue #2482 -- ``analysis_steps``; which one, and
    that not both are declared, is validated in :func:`run_sim`).
    ``models`` is validated separately, in :func:`run_sim` -- it is only
    required when the request declares a ``corners.process`` axis (see
    ``_resolve_models_lib``). Does not require the request to carry a
    ``schema`` field (see this module's docstring).
    """
    request = _load_request_json(request_path, SimError)
    # Deck-list requests (issue #2804) carry `decks` instead of a corner
    # request's netlist/analysis; their shape is validated by the deck
    # subsystem (sim_decks.plan_deck_units), so the corner request's
    # required fields do not apply here.
    if "decks" in request:
        if not isinstance(request.get("decks"), list) or not request["decks"]:
            raise SimError("request.decks must be a non-empty list of deck objects")
        return request
    request = validate_request_shape(
        request,
        "request file",
        error_cls=SimError,
        required_fields=("netlist",),
    )
    if "analysis" not in request and "analysis_steps" not in request:
        raise SimError(
            "request is missing required field: analysis (or analysis_steps "
            "for an ordered multi-step sequence)"
        )
    return request


def _reject_linearize_in_steps(request: dict[str, Any]) -> None:
    """Issue #4014: ``linearize`` is a scalar-``tran``-only field."""
    holders = [("analysis", request.get("analysis"))]
    holders.extend(
        (f"analysis_steps[{i}]", step)
        for i, step in enumerate(request.get("analysis_steps") or [])
    )
    for where, holder in holders:
        if isinstance(holder, dict) and "linearize" in holder:
            raise SimError(
                f"{where}.linearize is not supported with analysis_steps[]: "
                "`linearize` is only available on a scalar `tran` analysis "
                '(analysis: {"kind": "tran", ..., "linearize": true})'
            )


def _validate_linearize(analysis: dict[str, Any]) -> None:
    """Issue #4014: ``analysis.linearize`` must be a boolean on a ``tran``."""
    if "linearize" not in analysis:
        return
    value = analysis["linearize"]
    if not isinstance(value, bool):
        raise SimError(
            f"analysis.linearize must be a boolean (got {value!r}); "
            "set true to resample the transient waveforms onto a uniform "
            "time grid before the expr measurements are evaluated"
        )
    if value and str(analysis.get("kind", "")).lower() != "tran":
        raise SimError(
            'analysis.linearize requires analysis.kind "tran" '
            f"(got {analysis.get('kind')!r}): `linearize` resamples a "
            "transient waveform and has no meaning for other analyses"
        )


def _validate_search_targets(
    measurements_spec: list[dict[str, Any]],
    netlist_path: str,
    corner_points: list[Any],
) -> None:
    """Issue #4121: pre-dispatch check of every ``search.source`` against the
    netlist (see :func:`klayout_tools.sim_search.validate_search_targets`)."""
    if not sim_search.search_measurements(measurements_spec):
        return
    try:
        with open(netlist_path, encoding="utf-8") as handle:
            waveforms = _top_level_waveform_sources(handle.read())
    except (OSError, UnicodeDecodeError):
        waveforms = {}
    sim_search.validate_search_targets(
        measurements_spec,
        waveforms,
        {key.strip().lower() for point in corner_points for key in point.supply_v},
    )


def _resolve_analysis_and_measurements(
    request: dict[str, Any],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """The request's ``(analysis, measurements_spec)``, validated.

    The scalar ``analysis`` form returns the request's own objects exactly as
    before issue #2482. ``analysis_steps[]`` (issue #2482) resolves to the
    internal step-sequence ``analysis`` object plus the flattened
    step-scoped and derived measurements -- see
    :func:`klayout_tools.sim_steps.resolve_analysis_steps`.
    """
    if request.get("analysis_steps") is not None:
        _reject_linearize_in_steps(request)
        return sim_steps.resolve_analysis_steps(request)
    analysis = request.get("analysis") or {}
    # Issue #3338: `kind: "twoport"` compiles to an `ac` analysis; resolved
    # (every structural error raised) before the `kind`/`args` check.
    analysis = sim_twoport.resolve_analysis(analysis)
    if "kind" not in analysis or "args" not in analysis:
        raise SimError("request.analysis requires 'kind' and 'args'")
    reject_unsupported_analysis(analysis["kind"])
    # Issue #3159: `analysis.passes` (AC only) is validated and normalised
    # here, before any engine launch; absent `passes` returns `analysis` as is.
    analysis = sim_passes.resolve_analysis(analysis)
    _validate_linearize(analysis)
    measurements_spec = request.get("measurements", [])
    # Issue #2533: `spice` (a verbatim top-level `.meas` card) and `expr` (an
    # expression evaluated after the analysis, inside the generated
    # `.control` block) are the two declarable forms, exactly one per entry
    # -- see `_validate_measurement_forms` for the full rule set.
    _validate_measurement_forms(measurements_spec)
    # Issue #3109: an `op` analysis has no dc/ac/tran/sp sweep for a `.meas`
    # card to search, so every `spice` card would end as "produced no value".
    if str(analysis["kind"]).lower() == "op":
        for spec in measurements_spec:
            if spec.get("spice") is None:
                continue
            # A `.meas op` card keeps its own, more specific error.
            _validate_meas_card(spec["name"], spec["spice"])
            raise SimError(
                f"measurement '{spec['name']}' is a `.meas` card but "
                'analysis.kind is "op": an operating-point analysis has no '
                "sweep for a `.meas` card to search, so it can never produce "
                "a value; declare the quantity as an expression instead "
                f'(measurements[].expr, e.g. {{"name": "{spec["name"]}", '
                '"expr": "v(out)"})'
            )
    _validate_no_dependent_meas_cards(measurements_spec)
    # Issue #2868: a crossings series is a transient-time quantity.
    sim_crossings.validate_crossings_context(measurements_spec, analysis)
    # Issue #3033: an energy window is a transient-time quantity too.
    sim_energy.validate_energy_context(measurements_spec, analysis)
    # Issue #2947: so is a cycle-aligned mean.
    sim_cycle_mean.validate_cycle_mean_context(measurements_spec, analysis)
    # Issue #2923: and a transfer metric is an AC-sweep quantity.
    sim_transfer_meas.validate_transfer_context(measurements_spec, analysis)
    # Issue #3160: a tian metric combines the named passes of `analysis.passes`.
    sim_tian.validate_tian_context(measurements_spec, analysis)
    # Issue #3895: and an snm margin is a DC-sweep quantity.
    sim_snm_meas.validate_snm_context(measurements_spec, analysis)
    # Issue #4292: an eye fold is a transient-time quantity too.
    sim_eye.validate_eye_context(measurements_spec, analysis)
    # Issue #4121: a search entry needs the scalar single-analysis deck.
    sim_search.validate_search_context(measurements_spec, analysis)
    # Issue #2938: a `noise` entry is lowered to an `expr` (plus helper
    # lines) so everything downstream treats it as one.
    sim_noise.validate_noise_context(measurements_spec, analysis)
    return analysis, sim_noise.lower_measurements(measurements_spec, analysis)


def _reject_param_braces(analysis: dict[str, Any], engine: str) -> None:
    """Reject a ``{...}`` brace reference in a scalar ``analysis.args``
    (issue #2846). ngspice emits the analysis as a ``.control`` command and
    does not expand ``.param`` braces in control mode, so ``tran 2e-06
    {stop_s}`` aborts the analysis. Xyce emits a ``.tran`` dot card where
    braces are valid, so this check is skipped for ``engine == "xyce"``."""
    args = analysis.get("args")
    if engine != "xyce" and isinstance(args, str) and re.search(r"[{}]", args):
        raise SimError(
            f"request.analysis.args {args!r} contains a '{{...}}' brace "
            "reference: ngspice runs the analysis as a .control command, "
            "which does not expand .param braces (the analysis aborts with "
            "'TSTOP is invalid'), and klt does not expand them. Write the "
            "numeric value literally in analysis.args (or use engine 'xyce', "
            "whose .tran card accepts braces)"
        )


def _report_path(path: str | None, *, repo_root: str | None) -> dict[str, Any]:
    """Normalise one input-path field for this module's own JSON response
    (issue #1261) -- a thin, module-local wrapper over
    :func:`~klayout_tools.env_provenance.repo_relative_path` rather than a
    second normalizer, so `klt sim`'s reports and `klt env-provenance`'s own
    emitter agree on exactly one `{path, scope}` shape. Always resolve
    ``path`` first (e.g. against the request file's own directory, per the
    "Netlist convention" -- see ``run_sim``'s docstring) before calling this;
    a bare relative string is resolved against the current process's cwd,
    which is not necessarily what a caller meant.
    """
    return env_provenance.repo_relative_path(path, repo_root=repo_root)


#: A literal numeric ``AT=<x>`` on a ``.meas`` card (issue #3520). The value
#: must be a bare SPICE number; ``at=t_flip`` / ``at=0.5*2.5u`` never match
#: (the lookahead rejects a trailing expression character).
_MEAS_AT_LITERAL_RE = re.compile(
    r"\bat\s*=\s*([+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?[a-zA-Z]*)(?![\w.*/+\-(])",
    re.IGNORECASE,
)


def _dc_sweep_bounds(analysis: dict[str, Any]) -> tuple[float, float] | None:
    """``(start, stop)`` of a ``dc`` analysis whose first sweep is all
    literal numbers (``"V1 0 1 0.1"``), else ``None``."""
    if str(analysis.get("kind", "")).lower() != "dc":
        return None
    tokens = str(analysis.get("args", "")).split()
    if len(tokens) < 4:
        return None
    start = _parse_plain_number(tokens[1])
    stop = _parse_plain_number(tokens[2])
    if start is None or stop is None:
        return None
    suffix_scale = _spice_scale(tokens[1]), _spice_scale(tokens[2])
    return start * suffix_scale[0], stop * suffix_scale[1]


def _spice_scale(raw: str) -> float:
    """The engineering-suffix multiplier of a numeric SPICE literal."""
    match = spice_number.NUMBER_RE.match(raw.strip())
    return spice_number.suffix_scale(match.group(2)) if match else 1.0


def _meas_at_lint(
    analysis: dict[str, Any], measurements_spec: list[dict[str, Any]]
) -> list[dict[str, str]]:
    """Warning-severity ``measurement`` diagnostics for a ``.meas`` card whose
    literal ``AT=<x>`` cannot resolve on a literal ``dc`` sweep (issue #3520):
    the sweep is degenerate (start == stop, one sample -- nothing to
    interpolate) or ``x`` lies outside ``[start, stop]``. Parameterized /
    expression ``AT=`` and non-literal or non-``dc`` sweeps are left alone.
    """
    bounds = _dc_sweep_bounds(analysis)
    if bounds is None:
        return []
    lo, hi = min(bounds), max(bounds)
    found: list[dict[str, str]] = []
    for spec in measurements_spec:
        match = _MEAS_AT_LITERAL_RE.search(spec.get("spice") or "")
        if match is None:
            continue
        at = _parse_plain_number(match.group(1))
        if at is None:
            continue
        at *= _spice_scale(match.group(1))
        if lo == hi:
            why = (
                f"the dc sweep is a single point ({lo:g}); AT= needs two "
                "samples to interpolate"
            )
        elif not lo <= at <= hi:
            why = f"AT={match.group(1)} lies outside the dc sweep [{lo:g}, {hi:g}]"
        else:
            continue
        found.append(
            {
                "severity": "warning",
                "code": "measurement",
                "message": (
                    f"measurement '{spec['name']}' will likely report "
                    f"'out of interval': {why}"
                ),
            }
        )
    return found


def _attach_meas_lint(
    corners: list[dict[str, Any]],
    analysis: dict[str, Any],
    measurements_spec: list[dict[str, Any]],
) -> None:
    """Add the :func:`_meas_at_lint` warnings (once per corner) to a run's
    corner reports, and echo them to stderr. Computed client-side from the
    request alone, so it is identical for local, batch and remote runs."""
    lint = _meas_at_lint(analysis, measurements_spec)
    for diag in lint:
        print(f"warning: {diag['message']}", file=sys.stderr)
    for corner in corners:
        corner["diagnostics"].extend(dict(d) for d in lint)


def _portable_artifacts_option(options: dict[str, Any]) -> bool:
    """Validate ``options.portable_artifacts`` (issue #3052): a boolean, default off."""
    value = options.get("portable_artifacts", False)
    if not isinstance(value, bool):
        raise SimError("request.options.portable_artifacts must be a boolean")
    return value


def _portable_roots_for_run(
    *,
    portable_artifacts: bool,
    keep_artifacts: bool,
    request_dir: str,
    artifacts_dir: str,
    models_lib: str | None,
    corner_points: list[Any],
    osdi_preload: Any,
) -> sim_portable.Roots | None:
    """Run-wide substitution roots for ``options.portable_artifacts``, or ``None``.

    Only active alongside ``keep_artifacts``: nothing is persisted otherwise.
    """
    if not (portable_artifacts and keep_artifacts):
        return None
    return sim_portable.build_roots(
        request_dir=request_dir,
        artifacts_dir=artifacts_dir,
        models_lib=models_lib,
        section_libs=[
            lib
            for point in corner_points
            if point.process_sections is not None
            for lib in (
                point.section_lib(i, models_lib)
                for i in range(len(point.process_sections))
            )
            if lib != models_lib
        ],
        osdi_preload=osdi_preload,
    )


def _portable_environment(
    roots: sim_portable.Roots | None,
    *,
    repo_root: str | None,
    models_lib_sha256: str | None,
) -> dict[str, Any]:
    """``environment.portable_artifacts`` (additive/optional, issue #3052).

    Only present when ``options.portable_artifacts`` rewrote the persisted
    deck/log. Names what each token stood for; the inputs themselves stay
    pinned by ``models_lib_sha256`` / ``netlist_closure``.
    """
    if roots is None:
        return {}
    return {
        "portable_artifacts": {
            "substitutions": _portable_substitutions(
                roots, repo_root=repo_root, models_lib_sha256=models_lib_sha256
            )
        }
    }


def _portable_substitutions(
    roots: sim_portable.Roots, *, repo_root: str | None, models_lib_sha256: str | None
) -> list[dict[str, Any]]:
    """``environment.portable_artifacts.substitutions``: one entry per token
    (issue #3052), with the root it stood for normalised like every other
    path this report echoes (so the report itself carries no host path),
    plus ``$CORNER_DIR``, the per-corner scratch directory."""
    out: list[dict[str, Any]] = [
        {
            "token": sim_portable.CORNER_DIR_TOKEN,
            "stands_for": "per-corner artifact directory",
        }
    ]
    seen: set[str] = set()
    for token, root in roots:
        if token in seen:
            continue
        seen.add(token)
        entry: dict[str, Any] = {
            "token": token,
            "stands_for": _report_path(root, repo_root=repo_root),
        }
        if token == "$MODELS_DIR" and models_lib_sha256 is not None:
            entry["models_lib_sha256"] = models_lib_sha256
        out.append(entry)
    return out


#: ``options.retry_on_convergence_error`` (issue #2973): the solver options a
#: retried unit runs with when the object form leaves a key out. ``gear``
#: integration and a looser ``reltol`` are the standard first remedies for
#: ngspice's "time step too small".
DEFAULT_CONVERGENCE_RETRY: dict[str, Any] = {
    "max_attempts": 1,
    "reltol": 0.003,
    "method": "gear",
}
_CONVERGENCE_RETRY_METHODS = ("trap", "gear")


def _validate_convergence_retry(value: Any) -> dict[str, Any] | None:
    """Validate and normalise ``options.retry_on_convergence_error`` (issue
    #2973).

    ``None``/absent/``false`` -> ``None``: no retry, today's behaviour
    exactly. ``true`` -> :data:`DEFAULT_CONVERGENCE_RETRY`. An object may set
    ``max_attempts`` (integer >= 1), ``reltol`` (number in ``(0, 1)``) and
    ``method`` (``"trap"``/``"gear"``); omitted keys take the default and an
    unknown key is an application error (``SimError``) rather than a silent
    no-op, like :func:`_validate_fail_on_diagnostic`.
    """
    if value is None or value is False:
        return None
    if value is True:
        return dict(DEFAULT_CONVERGENCE_RETRY)
    if not isinstance(value, dict):
        raise SimError(
            "options.retry_on_convergence_error must be a boolean or an object "
            'like {"max_attempts": 1, "reltol": 0.003, "method": "gear"}'
        )
    unknown = sorted(set(value) - set(DEFAULT_CONVERGENCE_RETRY))
    if unknown:
        raise SimError(
            "options.retry_on_convergence_error has unknown key(s): "
            f"{', '.join(unknown)} (known: {', '.join(DEFAULT_CONVERGENCE_RETRY)})"
        )
    resolved = {**DEFAULT_CONVERGENCE_RETRY, **value}
    attempts = resolved["max_attempts"]
    if isinstance(attempts, bool) or not isinstance(attempts, int) or attempts < 1:
        raise SimError(
            "options.retry_on_convergence_error.max_attempts must be an integer >= 1"
        )
    reltol = resolved["reltol"]
    if (
        isinstance(reltol, bool)
        or not isinstance(reltol, (int, float))
        or not 0 < reltol < 1
    ):
        raise SimError(
            "options.retry_on_convergence_error.reltol must be a number "
            "between 0 and 1 (exclusive)"
        )
    if resolved["method"] not in _CONVERGENCE_RETRY_METHODS:
        raise SimError(
            "options.retry_on_convergence_error.method must be one of: "
            + ", ".join(_CONVERGENCE_RETRY_METHODS)
        )
    return resolved


def _resolve_convergence_retry(
    options: dict[str, Any], engine: str
) -> dict[str, Any] | None:
    """Validate ``options.retry_on_convergence_error`` for ``engine`` (issue
    #2973); the retry emits an ngspice ``.options`` card, so Xyce is refused."""
    retry = _validate_convergence_retry(options.get("retry_on_convergence_error"))
    if retry is not None and engine == "xyce":
        raise SimError(
            "options.retry_on_convergence_error is not supported for engine "
            "'xyce': the retry emits an ngspice `.options` card"
        )
    return retry


def _convergence_retry_environment(
    retry: dict[str, Any] | None,
) -> dict[str, dict[str, Any]]:
    """The ``environment`` fragment recording a resolved retry policy (empty
    when the request did not opt in)."""
    return {} if retry is None else {"retry_on_convergence_error": retry}


def _convergence_retry_deck_options(retry: dict[str, Any]) -> str:
    """The ``.options`` card a retried unit's deck carries."""
    return f".options reltol={retry['reltol']} method={retry['method']}"


#: Error-severity diagnostic codes that can accompany a ``nonconvergence``
#: failure without making it something other than one (the measurements and
#: vectors a failed solve never produced). Any *other* error-severity code
#: -- ``singular_matrix``, ``netlist``, ``timeout``, ``engine_killed``, ... --
#: means a different failure class, which is never retried (issue #2973).
_CONVERGENCE_CONSEQUENCE_CODES = frozenset(
    {"nonconvergence", "measurement", "no_such_vector"}
)


def _is_retryable_convergence_failure(corner: dict[str, Any]) -> bool:
    if corner["status"] != "error":
        return False
    codes = {d["code"] for d in corner["diagnostics"] if d["severity"] == "error"}
    return "nonconvergence" in codes and codes <= _CONVERGENCE_CONSEQUENCE_CODES


def _validate_ngspice_init(value: Any) -> tuple[str, ...]:
    """Validate and normalise ``options.ngspice_init`` (issue #2520).

    Each entry becomes one line of a ``.spiceinit`` file ``ngspice`` reads
    from its own current working directory before touching the corner's
    deck -- the reproducible way to select a compatibility mode (most
    notably ``"set ngbehavior=hsa"`` for vendor decks written in HSPICE
    style) instead of depending on whichever ``$HOME/.spiceinit`` (or none)
    happens to exist on the host that ran ``klt sim``. See
    :func:`_write_spiceinit`, which turns the returned tuple into the file
    itself, and :func:`_run_corner`'s ``cwd=`` fix (the other half of issue
    #2520) that makes ``corner_dir`` -- not the caller's ambient cwd --
    ``ngspice``'s lookup directory in the first place.

    Returns the requested lines as a tuple, preserving the caller's order
    and any duplicates -- unlike :func:`_validate_fail_on_diagnostic`'s
    code-set dedupe, a repeated ``.spiceinit`` line is not obviously a
    mistake (e.g. two unrelated ``set`` lines that happen to collide is the
    caller's call, not this validator's). ``None``/absent (the house
    convention for an undeclared optional field) and ``[]`` both normalise
    to ``()`` -- "no ``.spiceinit`` is written", today's (pre-#2520)
    behaviour exactly.
    """
    if value is None:
        return ()
    if isinstance(value, str) or not isinstance(value, (list, tuple)):
        raise SimError(
            "options.ngspice_init must be an array of ngspice .spiceinit "
            'line strings (e.g. ["set ngbehavior=hsa"])'
        )
    lines: list[str] = []
    for entry in value:
        if not isinstance(entry, str) or not entry.strip():
            raise SimError(
                "options.ngspice_init entries must be non-empty ngspice "
                "init-line strings"
            )
        lines.append(entry)
    return tuple(lines)


#: ``options.save_mode`` (issue #2732): who owns the corner's saved
#: node/branch set. ``"all"`` (the default) keeps issue #2521's generated
#: ``save all`` -- emitted whenever the corner has measurements or a rawfile
#: to capture, superseding any restrictive ``.save`` card in the netlist
#: body. ``"netlist"`` suppresses that generated card entirely, so whatever
#: ``.save`` cards the included netlist carries (or ngspice's own default
#: when it carries none) decide what is resident -- the caller then owns the
#: vector coverage every measurement and waveform consumer needs. klt never
#: parses, rewrites or widens the netlist's ``.save`` selectors.
SAVE_MODES = ("all", "netlist")
DEFAULT_SAVE_MODE = "all"


def _validate_save_mode(options: dict[str, Any]) -> tuple[str, bool]:
    """Validate ``options.save_mode`` (issue #2732).

    Returns ``(mode, declared)``: the normalized mode (``"all"`` when the
    key is omitted) and whether the request declared the key at all -- the
    latter is what the Xyce support boundary refuses, so an omitted mode
    never changes an existing Xyce request. Unlike most optional fields an
    explicit ``null`` is refused rather than read as "unset": the field is a
    two-valued policy switch, and a ``null`` most likely means a templating
    slip the caller should see rather than a silent default.
    """
    if "save_mode" not in options:
        return DEFAULT_SAVE_MODE, False
    value = options["save_mode"]
    if not isinstance(value, str) or value not in SAVE_MODES:
        raise SimError(
            "options.save_mode must be one of "
            + ", ".join(f'"{mode}"' for mode in SAVE_MODES)
            + f' (got {json.dumps(value, default=repr)}): "all" (the default) restores '
            'the full saved set with a generated `save all`; "netlist" '
            "leaves the saved set to the netlist's own .save cards"
        )
    return value, True


#: Default for ``options.waveform_max_bytes`` (issue #3211): the largest
#: ``waveform.raw`` klt will convert to the ``waveform.raw.json`` artifact.
#: The conversion materializes the whole ASCII rawfile in memory, so an
#: unbounded transient can be OOM-killed with no diagnostic; above this size
#: the corner keeps its measurements and gets a ``waveform_too_large`` warning.
DEFAULT_WAVEFORM_MAX_BYTES = 1024**3


def _validate_waveform_max_bytes(options: dict[str, Any]) -> int | None:
    """Validate ``options.waveform_max_bytes`` (issue #3211).

    Returns the cap in bytes (:data:`DEFAULT_WAVEFORM_MAX_BYTES` when the key
    is omitted); an explicit ``null`` disables the guard and returns ``None``.
    """
    if "waveform_max_bytes" not in options:
        return DEFAULT_WAVEFORM_MAX_BYTES
    value = options["waveform_max_bytes"]
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise SimError(
            "options.waveform_max_bytes must be a positive integer byte count "
            f"or null to disable the guard (got {json.dumps(value, default=repr)})"
        )
    return value


def _validate_save_vectors(
    options: dict[str, Any], save_mode: str
) -> tuple[tuple[str, ...], bool]:
    """Validate ``options.save_vectors`` (issue #3211).

    Returns ``(vectors, declared)``: the de-duplicated, lower-cased vector
    names (ngspice folds vector names to lower case) and whether the request
    declared the key -- the latter is what the Xyce support boundary refuses.
    """
    if "save_vectors" not in options:
        return (), False
    value = options["save_vectors"]
    if not isinstance(value, list) or not value:
        raise SimError(
            "options.save_vectors must be a non-empty array of ngspice vector "
            'names (e.g. ["v(out)", "i(vdd)"])'
        )
    vectors: list[str] = []
    for entry in value:
        if not isinstance(entry, str) or not entry.strip() or len(entry.split()) != 1:
            raise SimError(
                "options.save_vectors entries must be non-empty vector-name "
                "strings without whitespace"
            )
        name = entry.strip().lower()
        if name not in vectors:
            vectors.append(name)
    if save_mode != DEFAULT_SAVE_MODE:
        raise SimError(
            "options.save_vectors and options.save_mode "
            f'"{save_mode}" are mutually exclusive: save_vectors replaces the '
            'generated `save all`, which save_mode "netlist" already suppresses'
        )
    return tuple(vectors), True


#: A ``v(...)``/``i(...)`` token (optionally with ngspice's derivative prefix,
#: e.g. ``vdb(out)``, ``vm(a,b)``) in a measurement's ``spice``/``expr``/
#: ``crossings.signal`` text -- the vectors ``options.save_vectors`` must keep
#: resident for that measurement to resolve.
_SAVE_TOKEN_RE = re.compile(r"\b([vi][a-z]{0,2})\(([^()]*)\)", re.IGNORECASE)


def _required_save_vectors(
    save_vectors: tuple[str, ...], measurements_spec: list[dict[str, Any]]
) -> list[str]:
    """``options.save_vectors`` plus every ``v(x)``/``i(x)`` vector the
    declared measurements reference, de-duplicated, in first-seen order.

    Only the parenthesized forms are detected; a measurement that names a
    vector bare (``out`` rather than ``v(out)``) must list it in
    ``save_vectors`` itself.
    """
    vectors = list(save_vectors)
    for spec in measurements_spec:
        crossings = spec.get("crossings")
        texts = [spec.get("spice"), spec.get("expr")]
        if isinstance(crossings, dict):
            texts.append(crossings.get("signal"))
        for text in texts:
            if not isinstance(text, str):
                continue
            for prefix, args in _SAVE_TOKEN_RE.findall(text):
                kind = prefix[0].lower()
                for arg in args.split(","):
                    arg = arg.strip().lower()
                    if arg:
                        vector = f"{kind}({arg})"
                        if vector not in vectors:
                            vectors.append(vector)
    return vectors


#: ``options.ngspice_options`` (issue #3111): the solver/convergence
#: ``.options`` names a request may set. ngspice silently ignores an unknown
#: ``.options`` name, so a free-form map would hide a typo as "no effect";
#: the allowlist turns it into a named error instead.
NGSPICE_SOLVER_OPTIONS = (
    "abstol",
    "gmin",
    "gminsteps",
    "itl1",
    "itl2",
    "itl4",
    "method",
    "noopiter",
    "pivtol",
    "reltol",
    "srcsteps",
    "vntol",
)

#: A string option value lands verbatim in a deck card, so it is limited to
#: a bare token (``method=gear``) -- no whitespace, quotes or newlines.
_NGSPICE_OPTION_TOKEN = re.compile(r"[A-Za-z0-9_.+-]+")


def _validate_ngspice_options(value: Any) -> dict[str, Any]:
    """Validate ``options.ngspice_options`` (issue #3111).

    Returns the declared map unchanged (``{}`` when omitted or ``null``).
    Keys must come from :data:`NGSPICE_SOLVER_OPTIONS`; values must be a
    finite number, a bool or a bare-token string.
    """
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise SimError(
            "options.ngspice_options must be an object mapping ngspice "
            f".options names to scalar values (got {json.dumps(value, default=repr)})"
        )
    for name, item in value.items():
        if name not in NGSPICE_SOLVER_OPTIONS:
            raise SimError(
                f"options.ngspice_options key {json.dumps(name)} is not a "
                "supported solver option; supported: "
                + ", ".join(NGSPICE_SOLVER_OPTIONS)
            )
        if isinstance(item, bool):
            continue
        if isinstance(item, (int, float)):
            if not math.isfinite(item):
                raise SimError(
                    f"options.ngspice_options.{name} must be finite (got {item!r})"
                )
            continue
        if isinstance(item, str) and _NGSPICE_OPTION_TOKEN.fullmatch(item):
            continue
        raise SimError(
            f"options.ngspice_options.{name} must be a number, a boolean or "
            f"a bare-token string (got {json.dumps(item, default=repr)})"
        )
    return dict(value)


def _ngspice_options_card(ngspice_options: dict[str, Any] | None) -> str | None:
    """Render the ``.options`` card for ``options.ngspice_options`` (issue
    #3111), or ``None`` when it would be empty. ``true`` renders as the bare
    flag (``noopiter``); ``false`` is the ngspice default for a flag and
    renders nothing (it is still echoed in ``environment.ngspice_options``).
    """
    terms = []
    for name, item in (ngspice_options or {}).items():
        if item is True:
            terms.append(name)
        elif item is not False:
            terms.append(f"{name}={item}")
    return ".options " + " ".join(terms) if terms else None


def check_ngspice_options_echo(request: dict[str, Any], report: dict[str, Any]) -> None:
    """Fail loudly when an off-host report did not honour
    ``options.ngspice_options`` (issue #3111).

    A runner that predates the field drops it silently; its report then
    omits ``environment.ngspice_options``. Comparing the echo with the
    declared map turns that into a named :class:`SimError`.
    """
    declared = (request.get("options") or {}).get("ngspice_options") or {}
    if not declared:
        return
    echoed = (report.get("environment") or {}).get("ngspice_options") or {}
    for name in declared:
        if echoed.get(name) != declared[name] or name not in echoed:
            raise SimError(
                f"off-host runner did not honour options.ngspice_options."
                f"{name}: declared {json.dumps(declared[name])}, runner "
                f"echoed {json.dumps(echoed.get(name))} (the runner is "
                "likely older than this client and dropped the option)"
            )
    if set(echoed) != set(declared):
        raise SimError(
            "off-host runner echoed options.ngspice_options "
            f"{json.dumps(echoed, sort_keys=True)}, which differs from the "
            f"declared {json.dumps(declared, sort_keys=True)}"
        )


def _own_persistent_roots(
    ownership: contextlib.ExitStack,
    artifacts_dir: str,
    plot_dir: str | None,
    *,
    persistent: bool,
    dry_run: bool = False,
) -> None:
    """Enter the output-directory lock for each persistent root (#2770).

    A dry run (#3064) writes nothing, so it owns nothing.

    Roots are taken outermost first (shortest canonical path), so a
    ``plot_dir`` that contains ``artifacts_dir`` (or vice versa) is owned
    once and the inner one re-enters for free instead of self-contending.
    """
    if dry_run:
        return
    roots = ([artifacts_dir] if persistent else []) + (
        [plot_dir] if plot_dir is not None else []
    )
    for root in sorted(map(output_lock.canonical_dir, roots), key=len):
        ownership.enter_context(output_lock.hold_output_dir(root))


@output_lock.owns_run
def run_sim(
    request_path: str,
    *,
    artifacts_dir: str | None = None,
    backend: str | None = None,
    max_workers: int | None = None,
    hosts: int | None = None,
    budget_s: float | None = None,
    resume: bool | None = None,
    plot_dir: str | None = None,
    fail_fast_probe: bool | None = None,
    fail_on_diagnostic: list[str] | tuple[str, ...] | None = None,
    max_units_per_job: int | None = None,
    max_concurrent_jobs: int | None = None,
    dry_run: bool = False,
    submit_only: bool = False,
    attach_job_id: str | None = None,
    capacity_wait_s: float | None = None,
    _ownership: contextlib.ExitStack | None = None,
) -> dict[str, Any]:
    """Run the request at ``request_path`` and return its report.

    ``run_sim`` dispatches over two request kinds (docs/cli/sim.md):

    - **Corner requests** (the default): a ``netlist`` plus a
      ``corners``/``analysis``/``measurements`` matrix -- the PVT corner
      sweep this verb has always run. The full parameter documentation
      below applies to these.
    - **Deck-list requests** (issue #2804): a ``decks`` list of
      caller-composed ngspice decks, transported -- never composed,
      parsed, or graded -- to the selected backend. Same parameters,
      minus the corner-only ones: ``--hosts``/``--budget-s``/``--plot``/
      ``--fail-fast-probe``/``--fail-on-diagnostic`` (and ``--resume`` on
      ``remote``) are refused by name (see
      :func:`klayout_tools.sim_decks.run_deck_request` and
      docs/cli/sim.md's "Pre-composed decks" section, which also
      documents the deck report's shape).

    The corner sweep itself lives in :func:`_run_sim_corners`, unchanged
    by the deck dispatch -- this wrapper only routes between the two
    request kinds, so the sweep's own complexity budget is untouched.

    ``artifacts_dir`` overrides where per-corner logs/rawfiles are written
    when ``options.keep_artifacts`` is true; it defaults to a ``.klt/sim/``
    directory next to the request file (the same "next to the input"
    convention as ``klt render``'s default output directory).

    ``backend`` selects the execution backend, overriding the request's own
    ``backend`` field when given (the ``--backend`` CLI flag path). When both
    are omitted the backend defaults to ``local``. ``local``,
    ``local-parallel``, ``remote``, and ``batch`` are implemented; any other
    name raises :class:`SimError` (see :data:`SUPPORTED_BACKENDS`).
    ``remote`` requires ``request.remote`` and ``request.models.pdk`` -- see
    ``docs/cli/sim.md``'s "Remote backend" section. ``batch`` (issue #2080)
    submits the same ``local-parallel`` invocation to 2am's EDA batch fleet
    as an S3 job contract and needs a resolvable
    ``request.batch.provision_script_path``/``$KLT_BATCH_PROVISION_SCRIPT``
    plus a job bucket -- see "Batch backend" in the same document.

    ``max_workers`` bounds the ``local-parallel`` backend's worker pool,
    overriding the request's own ``options.max_workers`` when given (the
    ``--max-workers`` CLI flag path). Ignored by ``local``. When both are
    omitted, defaults to a conservative estimate derived from
    ``os.cpu_count()`` (see :func:`_default_max_workers`) -- each ``ngspice``
    process is itself internally multi-threaded, so one worker per CPU
    oversubscribes a small box immediately. Must be a positive integer when
    given explicitly; a non-positive value raises :class:`SimError`.

    A host-level ``$KLT_SIM_MAX_WORKERS`` cap (:data:`MAX_WORKERS_ENV`, issue
    #2286), when set in the environment, is an upper bound on the pool
    regardless of how it was sized: the effective worker count is
    ``min(requested_or_default, cap)``. A request/flag above the cap is
    clamped, not refused, with a one-line stderr notice per sweep; unset means
    exactly the pre-#2286 behaviour. The cap itself must be a positive integer
    -- a malformed value raises :class:`SimError` here, up front, matching how
    an explicit ``options.max_workers`` is validated. Because it is read from
    the environment rather than from the request, it bounds library callers
    and this package's own test suite the same way it bounds the CLI. See
    docs/cli/sim.md's "Useful on a workstation, harmful on a shared worker"
    section.

    ``hosts`` shards the expanded unit list (corners x Monte Carlo samples)
    into that many contiguous slices and merges the per-shard reports back
    into one report in global unit order, overriding the request's own
    ``request.remote.hosts`` field when given (the ``--hosts`` CLI flag
    path, same precedence rule as ``backend``/``--backend`` -- Epic #375
    decision 4). Defaults to ``1`` when both are omitted, which is exactly
    today's single-host behaviour -- byte-identical, not just
    "equivalent" -- so every existing request/response is unaffected. Must
    be a positive integer when given explicitly, and (for backends
    ``remote``/``batch`` only) no greater than the number of units to
    dispatch -- an idle fleet member would still be billed.

    For ``local``/``local-parallel``, ``hosts > 1`` fans the already-expanded,
    already-seeded ``CornerPoint`` list across a thread pool in this same
    process (:func:`_run_sharded`, Epic #375 Phase 1A/#376) -- no new
    execution path, just a concurrency seam one level above the worker pool
    itself. For backend ``remote``, ``hosts > 1`` instead provisions ``hosts``
    real EC2 instances through :func:`_run_remote_fleet`, reusing Epic #375
    Phase 1B's fleet launch lifecycle (:mod:`klayout_tools.remote_fleet`,
    #377) unmodified for the K-instance launch, fleet-level cost gate, vCPU
    quota pre-check, one-shard retry, and guaranteed teardown-all -- this is
    that lifecycle's first real caller (issue #906). Each shard is pushed its
    own already-expanded, already-seeded slice of the corner/Monte-Carlo unit
    list (never re-derived on the remote box from ``corners``/``monte_carlo``
    ranges, which would risk two shards disagreeing about which points are
    whose, or reseeding a point differently than an unsharded run would have)
    -- see :func:`_corner_points_to_wire` and ``_build_remote_request``'s
    ``explicit_points`` parameter. For backend ``batch``, ``hosts > 1``
    submits one independent fleet *job* per shard (:func:`_run_batch_fleet`)
    -- the same per-shard slice contract, with the provisioning half owned
    by 2am's fleet rather than by this process.

    ``max_units_per_job`` (the ``--max-units-per-job`` CLI flag) overrides
    ``request.batch.max_units_per_job``: for backend ``batch`` with
    ``hosts == 1`` it opts the request into splitting its units into jobs of
    at most that many (issue #3446). Ignored by the other backends.

    ``budget_s`` bounds the **whole sweep's** wall-clock time, overriding the
    request's own ``options.wall_clock_budget_s`` when given (the
    ``--budget-s`` CLI flag path, same precedence rule as
    ``max_workers``/``hosts``). Unlike ``options.timeout_s`` (a *per-corner*
    bound), this is the bound the incident behind issue #473 was missing --
    a hand-rolled driver with only a per-sim timeout can still run for hours
    by simply marching through an N-corner matrix forever. When the budget
    is exceeded, the dispatch loop stops **launching new corners** (already
    in-flight ones still run to their own ``timeout_s``); every corner that
    never got to start is reported with ``status: "error"`` and a
    ``budget_exceeded`` diagnostic (see ``_unrun_corner_report``), and
    ``environment.budget`` summarises the outcome. ``None`` (the default)
    means unbounded, exactly today's behaviour -- opting a caller into a
    budget is deliberate, mirroring ``options.keep_artifacts``. Applies to
    the ``local``/``local-parallel`` backends (and any ``hosts > 1`` shard
    built on them); ``remote`` does not yet honor it (its own dispatch loop
    runs on the provisioned box, outside this process).

    ``resume`` enables checkpoint/resume: completed corner reports are
    persisted incrementally to a ``checkpoint.json`` file under
    ``artifacts_dir`` as the sweep runs, and -- when a *matching* checkpoint
    already exists (see ``_checkpoint_fingerprint``: same netlist/model
    library/analysis/measurements/corner list/timeout) -- corners already
    recorded there are skipped rather than recomputed. Overrides the
    request's own ``options.resume`` when given (the ``--resume`` CLI flag
    path). A checkpoint whose fingerprint no longer matches the request
    (netlist/matrix edited since it was written) is treated as absent, never
    silently reused. The checkpoint file is removed once a sweep finishes
    with nothing skipped -- a fully-completed sweep has nothing left to
    resume. ``None``/``False`` (the default) is exactly today's behaviour:
    no checkpoint is read or written.

    ``plot_dir`` (issue #1723, the ``--plot`` CLI flag) writes one
    self-contained, dependency-free waveform SVG per non-sweep signal
    captured in each corner's waveform artifact, to that directory --
    ``klt trajectory --plot``'s "no plotting library" pattern
    (:func:`klayout_tools.sim_plot.render_waveform_svg`), applied to `.tran`/
    `.ac`/`.dc` waveforms rather than an optimization trajectory. Given, this
    forces ``options.waveforms``/``options.keep_artifacts`` on for the
    duration of this run regardless of the request's own settings -- a
    waveform artifact cannot be plotted without first being captured and
    persisted to disk. File paths are deterministic
    (``<plot_dir>/<corner_slug>__<signal_slug>.svg``) and listed both in the
    top-level ``plots`` field and each corner's own ``artifacts.plots``; a
    measurement whose ``.meas`` card names a signal with a matching plot
    additionally gets that path attached to its own
    ``measurements[].plot`` entry, so the miss report itself names the SVG
    to look at (see docs/cli/sim.md's "Waveform plots" section).
    ``None`` (the default) writes nothing, exactly today's behaviour.

    ``fail_fast_probe`` (issue #1694, the two-pass probe model -- see
    docs/design/sim-corner-reached-s.md) opts into a dispatch-time
    calibration probe: before any real corner runs, a short, bounded
    ``tran`` slice of the grid's first corner is run to measure that
    corner/deck's own simulated-time-per-wall-clock-second rate, which is
    then used to estimate whether ``options.timeout_s`` can plausibly cover
    the *full* analysis window. When the estimate says no (see
    ``_run_fail_fast_probe``/``_PROBE_ABORT_MARGIN``), the entire grid is
    aborted before a single real corner is dispatched -- each reported
    ``status: "error"`` with a ``timeout_budget_unreachable`` diagnostic
    carrying an *estimated* ``reached_s``/``fraction`` derived from the
    measured rate (never a true per-corner recovery -- see
    docs/cli/sim.md's "Timeout-budget preflight" section for why that
    remains out of reach). Overrides the request's own
    ``options.fail_fast_probe`` when given. Only engaged for ``backend in
    ("local", "local-parallel")`` (including any ``hosts > 1`` shard built
    on them) and only when ``analysis.kind == "tran"``; ``None``/``False``
    (the default) runs no probe at all, exactly today's behaviour.

    ``fail_on_diagnostic`` (issue #2492) lists ``diagnostics[].code`` values
    the caller refuses to trust a result from, overriding the request's own
    ``options.fail_on_diagnostic`` when given (the
    ``--fail-on-diagnostic`` CLI flag path). A corner that emitted any
    listed code is graded ``status: "inconclusive"`` -- "the solve did not
    convincingly converge", a distinct claim from ``"fail"`` ("the circuit
    missed its spec") -- **regardless of whether that diagnostic was
    downgraded to ``severity: "warning"`` by the recovered-stepping rule**
    (:func:`_recovered_from_stepping`), which is the entire point: it lets a
    caller opt back into treating ngspice's own "recovered" convergence
    narration as untrustworthy. Precedence is
    ``error > inconclusive > fail > pass``; the corners are counted in the
    top-level ``inconclusive`` field and, unless some corner also errored,
    make the aggregate ``status`` ``"inconclusive"`` (exit ``4`` -- "the
    sweep is incomplete or untrustworthy"). This is the **same** status a
    measurement outside its declared ``plausible_range``/
    ``node_voltage_bounds`` produces (issue #2493) -- one status, two reason
    codes; see :data:`_INCONCLUSIVE_STATUS`. Unset/empty (the default) is
    today's behaviour exactly, for every field. An unrecognised code is an
    application error -- see :func:`_validate_fail_on_diagnostic`.

    ``metrics`` (issue #1849, adopting the declared metric namespace
    registry from #247 beyond its ``klt layout-metrics``/``klt drc`` (#1847)
    precedent) is a **parallel, additive** object re-keying
    ``corner_count``/``passed``/``failed``/``errored``/``inconclusive``
    under their declared
    METRICS2.1-style names from :mod:`klayout_tools.metrics`'s registry --
    ``sim__corner__count``/``sim__corner__passed_count``/
    ``sim__corner__failed_count``/``sim__corner__errored_count``/
    ``sim__corner__inconclusive_count``. It never
    replaces or changes those existing fields, which stay exactly as
    documented; ``metrics`` is always present, purely additive to the JSON
    contract (no ``schema_version`` bump).
    ``measurements[].name`` stays out of scope for this registry -- those
    names are caller-supplied via the request spec, not ``klt``-declared.

    ``coverage`` preserves the legacy counters alongside common checked-work
    v1 identities, skips, applicability and unknown execution. Known zero
    actual checks produces ``not_checked`` unless a real failure/error wins.
    See :func:`_build_coverage` and ``docs/coverage-contract.md``.

    Returns a dict matching the documented JSON schema (see
    ``docs/cli/sim.md``). Raises :class:`SimError` for anything that prevents
    the sweep from starting at all (bad request, unresolvable netlist/model
    library, unsupported engine, unknown backend) -- once the sweep starts,
    every corner is reported in the response, including ones that error.

    **Async submit (issue #3073).** ``submit_only=True`` uploads and launches
    one ``batch`` job, then returns its identity (``job_id``, ``bucket``,
    ``launched_at``, ...) *instead of the report*, without polling or
    collecting; read it later with :func:`sim_batch_async.read_job_status`.
    A deck-list request is one job too (issue #3958): ``submit_only`` and
    ``attach_job_id`` apply to it on ``batch`` with ``job_ceiling: null``.
    Anything it cannot represent as one job (another backend, ``hosts > 1``,
    ``resume``, ``plot_dir``) raises :class:`SimError`
    with code ``submit_only_unsupported`` before any S3 write.

    **Output-directory ownership (issue #2770).** A run that writes
    persistent artifacts -- ``options.keep_artifacts``, ``resume``, a
    ``plot_dir``, or an off-host (``remote``/``batch``) backend that collects
    results locally -- owns ``artifacts_dir`` (and ``plot_dir``) for its
    lifetime through :func:`klayout_tools.output_lock.hold_output_dir`; a
    competing cooperating run raises
    :class:`~klayout_tools.output_lock.OutputDirLockedError`. A run that
    only uses temporary directories takes no lock. A deck-list run always
    owns its outdir (#3168). Ownership of an ancestor
    (``run_pex``'s) is inherited, never re-acquired. ``_ownership`` is
    internal: the exit stack that releases the lock when the run ends.

    **Dry run (issue #3064).** ``dry_run=True`` runs every pre-dispatch
    check of a corner request (shape, Monte Carlo spec, measurement forms,
    model/OSDI resolution, staging closure, off-host PDK check) and returns
    :func:`_build_dry_run_report` -- the expanded unit list with its derived
    seeds -- without running the fail-fast probe, taking an output-directory
    lock, or invoking any backend (no ngspice, SSH, S3 or EC2).
    """
    request = load_request(request_path)
    # Issue #2892: the engine runs with ``cwd=<corner dir>``, so a relative
    # ``artifacts_dir`` baked into the command line would resolve against the
    # corner dir and ngspice would never find its deck. Absolutise once here.
    if artifacts_dir is not None:
        artifacts_dir = os.path.abspath(artifacts_dir)
    if capacity_wait_s is not None and sim_decks.DECKS_FIELD in request:
        raise SimError("--wait-for-capacity does not apply to deck-list requests")
    if sim_decks.DECKS_FIELD in request:
        if dry_run:
            raise SimError(
                "--dry-run does not apply to deck-list requests (`decks`); "
                "it validates corner/Monte Carlo requests only"
            )
        return _dispatch_deck_request(
            request,
            request_path=request_path,
            backend=backend,
            max_workers=max_workers,
            hosts=hosts,
            artifacts_dir=artifacts_dir,
            resume=resume,
            budget_s=budget_s,
            plot_dir=plot_dir,
            fail_fast_probe=fail_fast_probe,
            fail_on_diagnostic=fail_on_diagnostic,
            submit_only=submit_only,
            attach_job_id=attach_job_id,
            _ownership=_ownership,
        )
    return _run_sim_corners(
        request_path,
        artifacts_dir=artifacts_dir,
        backend=backend,
        max_workers=max_workers,
        hosts=hosts,
        budget_s=budget_s,
        resume=resume,
        plot_dir=plot_dir,
        fail_fast_probe=fail_fast_probe,
        fail_on_diagnostic=fail_on_diagnostic,
        max_units_per_job=max_units_per_job,
        max_concurrent_jobs=max_concurrent_jobs,
        dry_run=dry_run,
        submit_only=submit_only,
        attach_job_id=attach_job_id,
        capacity_wait_s=capacity_wait_s,
        _ownership=_ownership,
    )


def _refuse_corner_only_deck_flags(
    budget_s: float | None,
    plot_dir: str | None,
    fail_fast_probe: bool | None,
    fail_on_diagnostic: list[str] | tuple[str, ...] | None,
) -> None:
    """Refuse, by name, the corner-only flags a deck-list request has no
    machinery for (see :func:`_dispatch_deck_request`)."""
    if budget_s is not None:
        raise SimError(
            "--budget-s/options.wall_clock_budget_s is not supported for "
            "deck-list requests -- bound each unit with decks[].timeout_s "
            "instead"
        )
    if plot_dir is not None:
        raise SimError(
            "--plot writes waveform artifacts, which deck-list requests do not produce"
        )
    if fail_fast_probe:
        raise SimError(
            "--fail-fast-probe calibrates the corner engine, which "
            "deck-list requests do not run"
        )
    if fail_on_diagnostic:
        raise SimError(
            "--fail-on-diagnostic grades corner diagnostics, which "
            "deck-list requests do not produce"
        )


def _dispatch_deck_request(
    request: dict[str, Any],
    *,
    request_path: str,
    backend: str | None,
    max_workers: int | None,
    hosts: int | None,
    artifacts_dir: str | None,
    resume: bool | None,
    budget_s: float | None,
    plot_dir: str | None,
    fail_fast_probe: bool | None,
    fail_on_diagnostic: list[str] | tuple[str, ...] | None,
    submit_only: bool = False,
    attach_job_id: str | None = None,
    _ownership: contextlib.ExitStack | None = None,
) -> dict[str, Any]:
    """Route a deck-list request to :mod:`klayout_tools.sim_decks`,
    resolving the backend exactly as the corner path does (flag >
    request field > ``$KLT_SIM_BACKEND``, host defaults stepping
    aside) and refusing the corner-only flags by name: a deck request
    has no corner machinery for them to act on, so silently ignoring
    one would mislead the caller about what ran."""
    resolved_backend, from_host_default = resolve_backend(backend, request)
    if resolved_backend not in SUPPORTED_BACKENDS:
        source = f" (from ${BACKEND_ENV})" if from_host_default else ""
        raise SimError(
            f"unsupported backend '{resolved_backend}'{source} "
            f"(supported: {', '.join(SUPPORTED_BACKENDS)})"
        )
    # A deck request's engine is always ngspice (plan_deck_units
    # enforces it), so the host-default's ngspice-only condition holds. A
    # single deck is one unit: like a one-corner request it stays local
    # under a host default rather than waiting on a Spot instance.
    resolved_backend = _yield_host_default_backend(
        resolved_backend,
        from_host_default,
        reason_ok=len(request.get(sim_decks.DECKS_FIELD) or ()) > 1,
    )
    # Issue #3958: a deck list is one batch job with one job id, so
    # --submit-only / --attach apply -- under the corner path's own
    # refusals, all before the first S3 write.
    if submit_only:
        _refuse_unsupported_submit_only(
            resolved_backend, 1 if hosts is None else hosts, resume, plot_dir
        )
    _apply_attach(
        {}, attach_job_id, resolved_backend, 1 if hosts is None else hosts, submit_only
    )
    if hosts is not None and hosts != 1:
        raise SimError(
            "--hosts/remote.hosts sharding is not supported for "
            "deck-list requests -- one off-host job runs the whole deck "
            "list across its own worker pool (options.max_workers)"
        )
    if resume is None:
        resume = bool((request.get("options") or {}).get("resume", False))
    if resume and resolved_backend == "remote":
        raise SimError(
            "--resume/options.resume is not yet supported for deck-list "
            "requests on backend 'remote' -- use backend 'local', "
            "'local-parallel' or 'batch'"
        )
    _refuse_corner_only_deck_flags(
        budget_s, plot_dir, fail_fast_probe, fail_on_diagnostic
    )
    request_dir = os.path.dirname(os.path.abspath(request_path))
    # Issue #3168: a deck run always writes (and a remote/batch pull replaces)
    # unit trees under its outdir, so it owns that root unconditionally --
    # after the flag checks above, which must fail without locking anything.
    # A submit-only run collects nothing, so it owns no unit trees.
    if _ownership is not None and not submit_only:
        _own_persistent_roots(
            _ownership,
            sim_decks.resolve_deck_outdir(artifacts_dir, request_dir),
            None,
            persistent=True,
        )
    return run_deck_request(
        request,
        request_dir=request_dir,
        backend=resolved_backend,
        max_workers=max_workers,
        artifacts_dir=artifacts_dir,
        resume=bool(resume),
        submit_only=submit_only,
        attach_job_id=attach_job_id,
    )


#: Diagnostic codes of a ``batch`` corner that never ran: a lost Spot shard,
#: a job that failed or was stopped by the harness, or a poll that gave up
#: while the job may still be running (``sim_batch._unrun_corner_report``).
_UNRUN_BATCH_CODES = frozenset(
    {
        "lost_shard",
        "batch_job_failed",
        "batch_job_timeout",
        "batch_poll_timeout",
        "batch_job_interrupted",
    }
)


def _is_unrun_batch_corner(corner: dict[str, Any]) -> bool:
    """True when a corner report is a synthesized batch one (never ran)."""
    return any(diag["code"] in _UNRUN_BATCH_CODES for diag in corner["diagnostics"])


def _record_batch_checkpoint(
    checkpoint: Any,
    backend: str,
    corners_new: list[dict[str, Any]],
    engine_version: Any,
) -> None:
    """Record a ``batch`` run's returned corner reports into the checkpoint.

    Issue #3367: `_run_batch` collects a shard's reports in one
    ``report.json``, so there is nothing to record incrementally; record the
    returned set after dispatch. A corner that never ran (``lost_shard``,
    ``batch_job_failed``, ``batch_job_timeout``, ``batch_poll_timeout``,
    ``batch_job_interrupted``) is
    left out and stays outstanding for the next ``--resume``; a corner that
    ran and genuinely errored is recorded exactly as the local backends do.
    A no-op without a checkpoint or off ``batch``.
    """
    if checkpoint is None or backend != "batch":
        return
    for corner in corners_new:
        if not _is_unrun_batch_corner(corner):
            checkpoint.record(corner["corner_id"], corner, engine_version)


def _refuse_unsupported_submit_only(
    backend: str, hosts: int, resume: bool | None, plot_dir: str | None
) -> None:
    """``--submit-only`` is one batch job: refuse everything else by name,
    before the first S3 write (issue #3073). A sharded run would need one
    job id per shard and a merge on fetch -- not represented, so refused
    rather than silently submitting only part of the request."""
    reason = None
    if backend != "batch":
        reason = f"requires backend 'batch' (resolved backend: '{backend}')"
    elif hosts != 1:
        reason = (
            "submits one job; --hosts/remote.hosts > 1 would need one job per "
            "shard and is not supported"
        )
    elif resume:
        reason = "cannot be combined with --resume"
    elif plot_dir is not None:
        reason = "cannot be combined with --plot (nothing is collected)"
    if reason is not None:
        raise SimError(f"--submit-only {reason}", code=SUBMIT_ONLY_UNSUPPORTED_CODE)


def _with_forwarded_budget(
    request: dict[str, Any],
    options: dict[str, Any],
    backend: str,
    budget_s: float,
) -> dict[str, Any]:
    """Return ``request`` with ``budget_s`` written into a copy of its options.

    Issue #3167: `_build_remote_request` forwards ``request["options"]`` to the
    worker, so a CLI ``--budget-s`` must be written into a copy of it (never
    the caller's dict) or the off-host worker never sees it. On-host backends
    consume ``budget_s`` directly and get ``request`` back unchanged.
    """
    if backend not in _OFFHOST_BACKENDS:
        return request
    return {**request, "options": {**options, "wall_clock_budget_s": budget_s}}


def _with_max_units_override(
    request: dict[str, Any], max_units_per_job: int | None
) -> dict[str, Any]:
    """Fold the ``--max-units-per-job`` CLI override into ``request.batch``
    (issue #3446), leaving the loaded request untouched."""
    if max_units_per_job is None:
        return request
    return {
        **request,
        "batch": {
            **(request.get("batch") or {}),
            "max_units_per_job": max_units_per_job,
        },
    }


def _with_max_concurrent_override(
    request: dict[str, Any], max_concurrent_jobs: int | None
) -> dict[str, Any]:
    """Fold the ``--max-concurrent-jobs`` CLI override into ``request.batch``
    (issue #3914), leaving the loaded request untouched."""
    if max_concurrent_jobs is None:
        return request
    return {
        **request,
        "batch": {
            **(request.get("batch") or {}),
            "max_concurrent_jobs": max_concurrent_jobs,
        },
    }


def _with_capacity_wait_override(
    request: dict[str, Any], capacity_wait_s: float | None
) -> dict[str, Any]:
    """Fold the ``--wait-for-capacity`` CLI override into
    ``request.batch.capacity_wait_s`` (issue #3564), leaving the loaded request
    untouched. The flag wins over the request field, and the value is validated
    by the one resolver the field uses, so a bad value is refused before any
    S3 write."""
    if capacity_wait_s is None:
        return request
    from .sim_batch import _resolve_capacity_wait_s

    value = _resolve_capacity_wait_s({"capacity_wait_s": capacity_wait_s})
    return {
        **request,
        "batch": {**(request.get("batch") or {}), "capacity_wait_s": value},
    }


def _check_expected_netlist_identity(
    expect_identity: Any,
    netlist_identity: str | None,
    netlist_identity_error: str | None,
    netlist_ref: str,
) -> None:
    """Issue #3074: refuse to simulate when the request's
    ``expect_netlist_identity`` does not match the netlist's stamp."""
    if expect_identity is None:
        return
    if not isinstance(expect_identity, str) or not expect_identity:
        raise SimError("expect_netlist_identity must be a non-empty string")
    if netlist_identity is None:
        raise SimError(
            "expect_netlist_identity was given but the netlist identity "
            f"could not be computed: {netlist_identity_error}"
        )
    if expect_identity != netlist_identity:
        raise SimError(
            "netlist identity mismatch: request expects "
            f"{expect_identity} but '{netlist_ref}' has {netlist_identity} "
            "(the referenced record was produced from a different netlist)"
        )


def _stamp_netlist_identity(
    environment: dict[str, Any],
    netlist_identity: str | None,
    netlist_identity_error: str | None,
) -> None:
    """Issue #3074: record the netlist identity (or why it is unavailable)."""
    environment["netlist_identity"] = netlist_identity
    if netlist_identity_error is not None:
        environment["netlist_identity_error"] = netlist_identity_error


def _check_capacity_wait_applies(
    capacity_wait_s: Any, backend: str, attach_job_id: Any
) -> None:
    """Refuse ``--wait-for-capacity`` where it has nothing to wait for."""
    if capacity_wait_s is None:
        return
    if backend != "batch":
        raise SimError(
            f"--wait-for-capacity requires backend 'batch' (got '{backend}')"
        )
    if attach_job_id is not None:
        raise SimError(
            "--wait-for-capacity cannot be combined with --attach: attach "
            "launches nothing, so there is no capacity to wait for"
        )


def _apply_attach(
    engine_kwargs: dict[str, Any],
    attach_job_id: str | None,
    backend: str,
    hosts: int,
    submit_only: bool = False,
) -> None:
    """Issue #2978: route ``--attach <job_id>`` to the batch backend. Only the
    single-job batch path has a job id to re-attach to."""
    if attach_job_id is None:
        return
    if backend != "batch" or hosts != 1:
        raise SimError(
            "--attach requires backend 'batch' with a single job "
            "(hosts = 1); a sharded run has one job id per shard"
        )
    if submit_only:
        raise SimError(
            "--attach and --submit-only are mutually exclusive: attach "
            "resumes an already-submitted job, submit-only launches a new one"
        )
    engine_kwargs["attach_job_id"] = attach_job_id


@dataclass(frozen=True)
class _ResolvedSimRequest:
    """Everything :func:`_prepare_sim_request` validated and resolved, before any
    corner is dispatched. Field names match the locals of the dispatch tail in
    :func:`_run_sim_corners`, which unpacks them one-for-one."""

    analysis: Any
    artifacts_dir: Any
    backend: Any
    backend_from_host_default: Any
    backend_source: Any
    budget_s: Any
    capture_only: Any
    convergence_retry: Any
    corner_points: Any
    corner_section_libs: Any
    deadline: Any
    dispatch_netlist_path: Any
    engine: Any
    engine_version_pin: Any
    fail_fast_probe: Any
    fail_on_diagnostic_codes: Any
    hosts: Any
    initial_ppid: Any
    keep_artifacts: Any
    max_workers: Any
    measurements_spec: Any
    models_lib: Any
    monte_carlo_info: Any
    monte_carlo_stats: Any
    netlist_identity: Any
    netlist_identity_error: Any
    netlist_overrides_report: Any
    netlist_path: Any
    netlist_source: Any
    ngspice_binary: Any
    ngspice_init_lines: Any
    ngspice_options: Any
    osdi_preload: Any
    osdi_runner_environment: Any
    portable_artifacts: Any
    preflight_warning: Any
    provenance_pdk: Any
    repo_root: Any
    request: Any
    request_dir: Any
    requested_backend: Any
    resume: Any
    save_mode: Any
    save_vectors: Any
    complex_vectors: Any
    stage_model_inputs: Any
    stimulus_lines: Any
    stimulus_signals: Any
    stimulus_by_corner: Any
    sweep_started: Any
    threads_per_worker: Any
    timeout_s: Any
    want_waveforms: Any
    waveform_max_bytes: Any


def _prepare_sim_request(
    request_path: str,
    *,
    artifacts_dir: str | None,
    backend: str | None,
    max_workers: int | None,
    hosts: int | None,
    budget_s: float | None,
    resume: bool | None,
    plot_dir: str | None,
    fail_fast_probe: bool | None,
    fail_on_diagnostic: list[str] | tuple[str, ...] | None,
    max_units_per_job: int | None,
    max_concurrent_jobs: int | None,
    capacity_wait_s: float | None,
    dry_run: bool,
) -> _ResolvedSimRequest:
    """The up-front validate-and-resolve phase of :func:`_run_sim_corners`.

    Everything here runs before the first artifact write or backend dispatch;
    the order of the checks is the error precedence the JSON reports, so it
    must not be reordered."""
    request = _with_max_units_override(load_request(request_path), max_units_per_job)
    request = _with_max_concurrent_override(request, max_concurrent_jobs)
    request = _with_capacity_wait_override(request, capacity_wait_s)
    request_dir = os.path.dirname(os.path.abspath(request_path))
    # Issue #1261 (and #1274 for `environment.models_lib`): every input-path
    # field this run's own JSON response echoes (`netlist`,
    # `environment.resume.checkpoint_path`, `environment.models_lib`) is
    # normalised against this one repo root, resolved once from the request
    # file's own location -- the same "walk up from the input" default
    # `env_provenance.find_repo_root` uses for its own caller.
    repo_root = env_provenance.find_repo_root(request_dir)

    engine = request.get("engine", "ngspice")
    if engine not in SUPPORTED_ENGINES:
        raise SimError(
            f"unsupported engine '{engine}' (supported: {', '.join(SUPPORTED_ENGINES)})"
        )

    backend_source = _backend_source(backend, request)
    backend, backend_from_host_default = resolve_backend(backend, request)
    requested_backend = backend
    if backend not in SUPPORTED_BACKENDS:
        source = f" (from ${BACKEND_ENV})" if backend_from_host_default else ""
        raise SimError(
            f"unsupported backend '{backend}'{source} "
            f"(supported: {', '.join(SUPPORTED_BACKENDS)})"
        )
    # The off-host toolchain pins ngspice (see `_enforce_xyce_support_boundary`):
    # a host default must not turn a valid xyce request into a refusal.
    backend = _yield_host_default_backend(
        backend, backend_from_host_default, reason_ok=engine == "ngspice"
    )

    hosts = (
        hosts if hosts is not None else (request.get("remote") or {}).get("hosts", 1)
    )
    if not isinstance(hosts, int) or isinstance(hosts, bool) or hosts < 1:
        raise SimError("remote.hosts must be a positive integer")

    netlist_ref = request["netlist"]
    netlist_path = _resolve_relative(netlist_ref, request_dir)
    if not os.path.isfile(netlist_path):
        raise SimError(f"netlist not found: {netlist_path}")

    netlist_source = request.get("netlist_source")
    if netlist_source is not None and netlist_source not in SUPPORTED_NETLIST_SOURCES:
        raise SimError(
            f"unsupported netlist_source '{netlist_source}' "
            f"(supported: {', '.join(SUPPORTED_NETLIST_SOURCES)})"
        )

    # Issue #3074: normalised netlist identity, stamped on the report and
    # (when the caller hands the stamp of the record it compares against)
    # checked before anything is simulated.
    netlist_identity, netlist_identity_error = sim_staging.netlist_identity(
        request, netlist_path
    )
    _check_expected_netlist_identity(
        request.get("expect_netlist_identity"),
        netlist_identity,
        netlist_identity_error,
        netlist_ref,
    )

    corners_spec = request.get("corners") or {}
    models = request.get("models") or {}
    models_lib: str | None = None

    # Best-effort PDK resolution for the shared `provenance` block: `models`
    # may name a PDK variant (`models.pdk`) whose release stamps the run's
    # model library. Independent of whether `models_lib` itself ends up
    # resolved below; if the variant can't be found here, provenance.pdk is
    # null rather than fabricated (see `_provenance.build_provenance`).
    provenance_pdk: dict[str, Any] | None = None
    if models.get("pdk") or models.get("pdk_root"):
        try:
            provenance_pdk = find_pdk(
                variant=models.get("pdk"), root=models.get("pdk_root")
            )
        except PdkNotFoundError:
            provenance_pdk = None
        _warn_pdk_ambiguity(provenance_pdk)

    # Issue #2482: either the scalar `analysis` (unchanged) or an ordered
    # `analysis_steps[]` sequence, resolved -- every structural error raised
    # -- here, before any corner is dispatched.
    analysis, measurements_spec = _resolve_analysis_and_measurements(request)
    _reject_param_braces(analysis, engine)
    for spec in measurements_spec:
        if spec.get("spice") is not None:
            _validate_meas_card(spec["name"], spec["spice"])
        if spec.get("k_sigma") is not None:
            _validate_k_sigma(
                spec["k_sigma"], f"request.measurements[{spec['name']!r}].k_sigma"
            )
        _validate_spec_row(
            spec.get("spec_row"), f"request.measurements[{spec['name']!r}].spec_row"
        )

    options = request.get("options") or {}
    # Issue #2493: validates and resolves both plausibility declarations into
    # `measurements_spec` in place, before any corner is dispatched -- see
    # `_resolve_plausible_ranges`.
    _resolve_plausible_ranges(measurements_spec, options)
    # Issue #2423: resolve *which* ngspice binary to run before dispatching
    # any corner (`options.ngspice_binary` > `$KLT_NGSPICE_BINARY` >
    # `ngspice` on PATH), rather than hardcoding the name `ngspice` -- the
    # resolved path is both what every corner spawns and what
    # `environment.ngspice_binary` below records. `None` for the `xyce`
    # engine, which this issue adds no override for (see
    # `_resolve_ngspice_binary`'s own docstring) -- mirrors
    # `environment.netgen_binary`'s "always present, null for the other
    # engine" convention (`klt lvs`, issue #2373).
    ngspice_binary = (
        _resolve_ngspice_binary(options, request_dir) if engine == "ngspice" else None
    )
    # Issue #2513: OSDI (Verilog-A) model preload. Resolved and
    # existence-checked here, before any corner is dispatched, like
    # `models.lib` -- see `_resolve_osdi_preload`. Validated for every engine
    # so a typo'd path is always named; `engine: "xyce"` then refuses the
    # option outright in `_enforce_xyce_support_boundary` below rather than
    # silently dropping it.
    # Issue #3819: entries the executing host resolves for itself (a literal
    # `$PDK_ROOT/<rel>`, or a path under `models.pdk_root`) name an
    # image-baked `.osdi`; they are existence-checked here only when the run
    # stays on this host (see below).
    osdi_runner_resolved = sim_staging.runner_resolved_osdi(request, request_dir)
    osdi_preload = _resolve_osdi_preload(
        options, request_dir, skip_indices=frozenset(osdi_runner_resolved)
    )
    # Issue #2668: opt-in staging of the request's own model inputs for the
    # off-host backends. Validated for every backend (a typo'd value is
    # always named) but only *acts* off-host -- `local`/`local-parallel`
    # already read the files where they are. See `sim_staging.stage_sim_job`.
    stage_model_inputs = sim_staging.stage_model_inputs_requested(request)
    # An `.osdi` is a host-architecture shared library compiled against one
    # ngspice's OSDI ABI, so by default the off-host backends refuse the
    # option rather than stage it (see docs/cli/sim.md's "OSDI (Verilog-A)
    # model preload"). `options.stage_model_inputs` (issue #2668) is the
    # caller's explicit statement that the binaries are built for the
    # runner's platform and ngspice, so they are staged byte-for-byte
    # instead. Otherwise a host-default off-host backend steps aside to
    # `local`, like every other "this run cannot leave this host" case; an
    # explicit choice is never second-guessed, so it is refused by name.
    # Runner-resolved entries (issue #3819) need neither staging nor a local
    # file, so only the host-local ones count here.
    backend = _yield_host_default_backend(
        backend,
        backend_from_host_default,
        reason_ok=not osdi_preload or stage_model_inputs,
    )
    if osdi_preload and backend in _OFFHOST_BACKENDS and not stage_model_inputs:
        raise SimError(
            f"options.osdi_preload is not supported for backend {backend!r}: "
            "an `.osdi` file is a host-architecture shared library compiled "
            "against this host's ngspice, so it is neither staged to nor "
            "resolved on the off-host instance -- run OSDI-model sweeps with "
            "backend 'local' or 'local-parallel', or set "
            "options.stage_model_inputs: true to upload binaries built for "
            "the runner's platform and ngspice OSDI ABI, or name an "
            "image-baked `.osdi` as `$PDK_ROOT/<rel>` (or under "
            "models.pdk_root) so the runner resolves it"
        )
    osdi_preload, osdi_runner_environment = _finalize_osdi_preload(
        options, request_dir, backend, osdi_preload, osdi_runner_resolved
    )
    # Issue #3041: an `engine_version` pin is checked before any corner runs
    # where this process can see the engine (local backends); off-host
    # backends are checked against the report's `engine_version` afterwards.
    engine_version_pin = _validate_engine_version_pin(request)
    _check_local_engine_version_pin(
        engine_version_pin, backend, engine, ngspice_binary or XYCE_BINARY
    )
    # Issue #4438: an OSDI ABI the local ngspice cannot load is refused up
    # front rather than discovered inside ngspice, once per corner.
    _check_local_osdi_abi(osdi_preload, backend, engine, ngspice_binary or XYCE_BINARY)
    # Issue #4046: a `models.pdk_version` pin is checked the same way, before
    # any corner runs; off-host backends reject it rather than pass silently.
    _check_pdk_version_pin(_validate_pdk_version_pin(models), models, backend)
    timeout_s = options.get("timeout_s", DEFAULT_TIMEOUT_S)
    # Issue #3337: the complex-vector sidecar is the result, so declared
    # vectors force artifacts on (validated in full below).
    keep_artifacts = sim_complex_vectors.forces_artifacts(
        options, bool(options.get("keep_artifacts", False))
    )
    portable_artifacts = _portable_artifacts_option(options)
    want_waveforms = bool(options.get("waveforms", False))
    if plot_dir is not None:
        # A waveform SVG can only be rendered from a waveform artifact that
        # was actually captured and persisted to disk -- see run_sim's own
        # docstring on `plot_dir`. Forced on unconditionally (not just when
        # unset) so `--plot` behaves identically regardless of what the
        # request document itself declared.
        keep_artifacts = True
        want_waveforms = True
    # Issue #2482: a step sequence runs several analyses per corner, which the
    # one-analysis waveform artifact cannot represent -- refused by name.
    sim_steps.refuse_unsupported_options(
        analysis, want_waveforms=want_waveforms, plot_requested=plot_dir is not None
    )
    sim_passes.refuse_unsupported(
        analysis,
        measurements_spec,
        want_waveforms=want_waveforms,
        plot_requested=plot_dir is not None,
    )
    sim_passes.refuse_xyce(analysis, engine)
    sim_twoport.refuse_unsupported(
        analysis,
        measurements_spec,
        want_waveforms=want_waveforms,
        plot_requested=plot_dir is not None,
    )
    sim_twoport.refuse_xyce(analysis, engine)
    # The S-parameter `.s2p` sidecar is the result (issue #3338).
    keep_artifacts = sim_twoport.forces_artifacts(analysis, keep_artifacts)
    capture_only = _capture_only_requested(
        options, want_waveforms=want_waveforms, measurements_spec=measurements_spec
    )

    max_workers = max_workers if max_workers is not None else options.get("max_workers")
    if max_workers is not None:
        if (
            not isinstance(max_workers, int)
            or isinstance(max_workers, bool)
            or max_workers < 1
        ):
            raise SimError("options.max_workers must be a positive integer")
    threads_per_worker = _validate_threads_per_worker(options.get("threads_per_worker"))
    # Host worker cap (issue #2286): validated here, up front, so a malformed
    # `$KLT_SIM_MAX_WORKERS` fails the sweep with a clean application error
    # before any corner is dispatched -- rather than surfacing from inside a
    # `hosts > 1` shard thread once work is already in flight. The value
    # itself is re-read (never cached) where the pool is actually sized, in
    # `_resolve_max_workers`. Resetting the notice dedupe here scopes "say so
    # once" to this sweep rather than to the process.
    _host_max_workers_cap()
    _reset_worker_cap_notices()

    budget_s = budget_s if budget_s is not None else options.get("wall_clock_budget_s")
    if budget_s is not None:
        if (
            not isinstance(budget_s, (int, float))
            or isinstance(budget_s, bool)
            or budget_s <= 0
        ):
            raise SimError(
                "options.wall_clock_budget_s must be a positive number of seconds"
            )
        request = _with_forwarded_budget(request, options, backend, budget_s)

    resume = resume if resume is not None else bool(options.get("resume", False))
    fail_fast_probe = (
        fail_fast_probe
        if fail_fast_probe is not None
        else bool(options.get("fail_fast_probe", False))
    )
    # Issue #2492: validated up front, like every other option above, so a
    # typo'd code fails the sweep with a clean application error before any
    # corner is dispatched rather than silently grading nothing.
    fail_on_diagnostic_codes = _validate_fail_on_diagnostic(
        fail_on_diagnostic
        if fail_on_diagnostic is not None
        else options.get("fail_on_diagnostic")
    )
    # Issue #2520: no CLI-flag override (unlike `fail_on_diagnostic`) --
    # this is a per-request compatibility-mode declaration, not a runtime
    # dial an operator would reach for per invocation.
    ngspice_init_lines = _validate_ngspice_init(options.get("ngspice_init"))
    # Issue #2973: opt-in per-unit retry on a `nonconvergence` failure.
    # Validated up front like every option; an off-host worker re-reads it
    # from the forwarded request document, so only the local backends
    # receive the resolved value below.
    convergence_retry = _resolve_convergence_retry(options, engine)
    # Issue #2732: validated up front, before any corner, probe or off-host
    # job is launched. An off-host worker re-reads it from the forwarded
    # request document (see `_build_remote_request`), so only the local
    # backends receive the resolved value below.
    save_mode, save_mode_declared = _validate_save_mode(options)
    # Issue #3211: same up-front validation; off-host workers re-read both
    # from the forwarded request document.
    save_vectors, save_vectors_declared = _validate_save_vectors(options, save_mode)
    complex_vectors = sim_complex_vectors.validate(options, analysis, save_mode, engine)
    waveform_max_bytes = _validate_waveform_max_bytes(options)
    # Issue #3111: same up-front validation; off-host workers re-read it from
    # the forwarded request document, and the echo check below verifies it.
    ngspice_options = _validate_ngspice_options(options.get("ngspice_options"))
    # Issue #3036: the rule grammar is validated up front; the rules are
    # resolved onto points after expansion, before any dispatch.
    nodeset_rules = _validate_nodeset_rules(options.get("nodeset"))
    # Issue #3881: same grammar, applied as `.ic` + `uic` on a transient.
    ic_rules = _validate_nodeset_rules(
        options.get("initial_conditions"), label=_IC_LABEL
    )
    _require_plain_tran_for_ic(bool(ic_rules), analysis)

    # Timeout budget preflight (issue #1686): a coarse, pre-grid sanity
    # check -- never blocks the sweep, only surfaces an advisory string. See
    # `_preflight_timeout_warning`'s docstring for why this is deliberately
    # not a real per-engine throughput estimate. The dispatch-time
    # fail-fast guard this warning's own docstring used to say was "not
    # implemented" now exists (issue #1694's two-pass probe model, gated on
    # `fail_fast_probe`/`options.fail_fast_probe` -- see
    # `_run_fail_fast_probe`, run further below once `models_lib` and the
    # dispatched corner list are known).
    preflight_warning = _preflight_timeout_warning(
        analysis=analysis, timeout_s=timeout_s
    )

    if artifacts_dir is None:
        artifacts_dir = os.path.join(request_dir, ".klt", "sim")
    artifacts_dir = os.path.abspath(artifacts_dir)

    # Orphan safety (issue #473): capture the sweep's own parent PID once, up
    # front. `os.getppid()` changes the moment the launching process exits --
    # the kernel reparents this process to its new parent (init/systemd on
    # Linux) -- so a dispatch loop that re-checks this between corners
    # detects "my launcher is gone" without any external supervisor,
    # heartbeat file, or IPC. Always on (no opt-in): unlike the budget, there
    # is no legitimate reason to want a sweep that keeps running after the
    # process that asked for it has exited.
    initial_ppid = os.getppid()
    sweep_started = time.monotonic()
    deadline = sweep_started + budget_s if budget_s is not None else None

    corner_points = _expand_corners(corners_spec, request.get("exclude") or [])

    monte_carlo_spec = request.get("monte_carlo")
    _enforce_xyce_support_boundary(
        engine=engine,
        backend=backend,
        corners_spec=corners_spec,
        monte_carlo_declared=monte_carlo_spec is not None,
        fail_fast_probe=fail_fast_probe,
        osdi_preload_declared=bool(osdi_preload or osdi_runner_resolved),
        expr_measurements_declared=bool(_expr_measurements(measurements_spec)),
        save_mode_declared=save_mode_declared,
        save_vectors_declared=save_vectors_declared,
        ngspice_options_declared=bool(ngspice_options),
        nodeset_declared=bool(nodeset_rules),
        initial_conditions_declared=bool(ic_rules),
        analysis_steps_declared=sim_steps.is_steps_analysis(analysis),
        linearize_declared=analysis.get("linearize") is True,
        crossings_declared=bool(
            sim_crossings.crossings_measurements(measurements_spec)
        ),
        energy_declared=bool(sim_energy.energy_measurements(measurements_spec)),
        cycle_mean_declared=bool(
            sim_cycle_mean.cycle_mean_measurements(measurements_spec)
        ),
        transfer_declared=bool(
            sim_transfer_meas.transfer_measurements(measurements_spec)
        ),
        snm_declared=bool(sim_snm_meas.snm_measurements(measurements_spec)),
        search_declared=bool(sim_search.search_measurements(measurements_spec)),
        eye_declared=bool(sim_eye.eye_measurements(measurements_spec)),
    )
    monte_carlo_info: dict[str, Any] | None = None
    monte_carlo_stats: dict[str, Any] | None = None
    explicit_points_wire = request.get("_explicit_points")
    # Issue #3036: resolved once, after expansion/exclusions and before Monte
    # Carlo cloning (samples inherit their base map). A fleet shard (below)
    # receives the selected maps on the wire instead of re-resolving rules
    # against its subset of the points.
    _apply_nodeset_rules(
        corner_points, nodeset_rules if explicit_points_wire is None else []
    )
    _apply_nodeset_rules(
        corner_points,
        ic_rules if explicit_points_wire is None else [],
        label=_IC_LABEL,
        attr="initial_conditions",
    )
    if explicit_points_wire is not None:
        # Internal-only escape hatch: never set by an external caller or the
        # CLI, only by this same module's own `_run_remote_fleet` when it
        # builds one fleet shard's pushed request (issue #906). The
        # orchestrating process has already expanded `corners`/`monte_carlo`
        # and sliced out exactly this shard's points (with their seeds
        # already derived); re-deriving from `corners_spec`/`monte_carlo_spec`
        # on the remote box here would risk this shard disagreeing with a
        # sibling shard about which points are whose, or deriving different
        # seeds than an unsharded run of the same request would have. See
        # `_corner_points_to_wire`/`_corner_points_from_wire`.
        corner_points = _corner_points_from_wire(explicit_points_wire)
    elif monte_carlo_spec is not None:
        injected = _resolve_injected(monte_carlo_spec, netlist_path, request, repo_root)
        corner_points, monte_carlo_info = _expand_monte_carlo(
            corner_points,
            monte_carlo_spec,
            injected,
            mismatch_scale=_resolve_mismatch_scale(monte_carlo_spec, models.get("pdk")),
        )
        monte_carlo_info = _with_family_mismatch(
            monte_carlo_info,
            injected,
            netlist_path,
            models,
            request,
            repo_root,
            corner_points,
        )
        # Issue #2963: disclose transient-noise sources the seed contract
        # does not cover, whatever `vary` asks for.
        monte_carlo_info = _with_unseeded_noise(
            monte_carlo_info, netlist_path, request=request, repo_root=repo_root
        )
        quantiles, k_sigma = _validate_mc_statistics_spec(monte_carlo_spec)
        window_scope = _validate_mc_window_scope(monte_carlo_spec)
        monte_carlo_stats = {
            "quantiles": quantiles,
            "k_sigma": k_sigma,
            "window_scope": window_scope,
        }
        # Echo only what the request actually declared, so a sampling-only
        # request's `environment.monte_carlo` keeps its `{n, seed, vary}`
        # shape unchanged (plus `family_mismatch` above, which is gated on
        # `vary` rather than on this block -- see docs/cli/sim.md).
        if monte_carlo_spec.get("quantiles") is not None:
            monte_carlo_info["quantiles"] = list(quantiles)
        if k_sigma is not None:
            monte_carlo_info["k_sigma"] = k_sigma
        monte_carlo_info.update(_mc_window_scope_echo(monte_carlo_spec, window_scope))

    # Issue #2995: per-instance mismatch gating. Validated before any probe or
    # backend dispatch; the gated netlist copy is what local backends run.
    dispatch_netlist_path, monte_carlo_info = _maybe_gate_instances(
        netlist_path,
        monte_carlo_spec,
        monte_carlo_info,
        models.get("pdk"),
        backend=backend,
        explicit_points=explicit_points_wire is not None,
    )

    # Issue #3919: `.subckt` override of the include closure. Applied on top of
    # the (possibly gated) netlist; refused up front on an off-host backend.
    dispatch_netlist_path, netlist_overrides_report = _maybe_override_subckts(
        request,
        dispatch_netlist_path,
        request_dir=request_dir,
        # The rewritten closure cannot leave this host, so an env-sourced
        # off-host default yields to local (an explicit choice is refused).
        backend=_yield_host_default_backend(
            backend, backend_from_host_default, reason_ok=False
        ),
        explicit_points=explicit_points_wire is not None,
        repo_root=repo_root,
    )

    # Issue #2706: fail fast (before any probe or backend dispatch) when a
    # supply override targets a source with an explicit transient waveform.
    _validate_supply_override_targets(netlist_path, corner_points)
    # Issue #3035: the same for a `corners.params` name no `.param` declares.
    _validate_param_targets(netlist_path, corner_points)
    # Issue #3918: a supply-relative `crossings.level` needs its `frac_of`
    # key in every corner's supply map -- refused here, not mid-sweep.
    sim_crossings.validate_supply_relative_levels(
        measurements_spec, [point.supply_v for point in corner_points]
    )
    # Issue #2482: the same pre-dispatch check for a step's own `alter`
    # overrides (plus a clash with the corner's own supply axis).
    sim_steps.validate_alter_targets(analysis, netlist_path, corner_points)
    sim_passes.validate_alter_targets(analysis, netlist_path, corner_points)
    _validate_search_targets(measurements_spec, netlist_path, corner_points)
    # Issue #2977: the declarative timed stimulus, validated and expanded
    # before any corner is dispatched (an off-host worker re-expands it
    # from the forwarded request).
    stimulus_signals, stimulus_by_corner = _resolve_stimulus(
        request, analysis, engine, netlist_path, corner_points
    )
    stimulus_lines = (
        sim_stimulus.corner_deck_lines(
            {
                sim_stimulus.supply_key(point.supply_v): stimulus_by_corner[
                    point.corner_id
                ]
                for point in corner_points
            }
        )
        if stimulus_by_corner
        else sim_stimulus.deck_lines(stimulus_signals)
    )

    # Issue #2522: a `corners.process` bundle may name its own model library
    # per section. Resolved (and existence-checked) per host, from the refs
    # as declared -- a fleet shard resolves the same refs against its own box,
    # exactly as it resolves `models.lib` from the forwarded `models` block.
    # Issue #2668: a worker request whose model references were rewritten to
    # staged job-relative names (`sim_staging.STAGED_MODEL_INPUTS_FIELD`)
    # resolves them against its own request directory -- the job directory
    # -- never joined onto the image's PDK directory. `models.pdk` stays in
    # the request for image identity/provenance; only *resolution* ignores it.
    resolution_models = _model_resolution_models(request, models)
    corner_section_libs = _resolve_corner_section_libs(
        corner_points, resolution_models, request_dir
    )

    # Gated on the *actually dispatched* `corner_points`, not `corners_spec`,
    # so a fleet shard reconstructed from `_explicit_points` (which pops
    # `corners` entirely -- see `_build_remote_request`) still resolves a
    # model library when its own points carry a `process` value, even though
    # the pushed request's `corners_spec` is empty on the remote box. A bundle
    # whose every section names its own library needs no `models.lib` at all
    # (see `_needs_models_lib`).
    if any(_needs_models_lib(point) for point in corner_points):
        # Only the process axis needs a model library -- supply/temperature
        # are plain netlist/control-block mutations (see this module's
        # docstring and the spike's "Native PVT sweeping" survey row).
        models_lib = _resolve_models_lib(resolution_models, request_dir)

    # Resumability (issue #473): only active when the caller opts in. The
    # `local`/`local-parallel` backends (including any `hosts > 1` shard
    # built on them -- see `_run_sharded`) record each corner into the
    # checkpoint as it completes; `batch` (issue #3367) records its returned
    # reports after dispatch, keyed by `corner_id` (lost shards excluded --
    # see below). `remote`'s corners come back from an independently-run box
    # this process does not reconcile against a local `CornerPoint.corner_id`,
    # so it stays rejected, mirroring the existing `hosts > 1` + `remote`
    # restriction above rather than inventing a new error shape.
    backend = _yield_host_default_backend(
        backend, backend_from_host_default, reason_ok=not resume
    )
    if resume and backend == "remote":
        raise SimError(
            f"options.resume is not yet supported for backend {backend!r} -- "
            f"wiring the {backend} backend into the same checkpoint machinery "
            "as 'local'/'local-parallel'/'batch' is tracked follow-up work; "
            "use resume with backend 'local', 'local-parallel' or 'batch'"
        )
    return _ResolvedSimRequest(
        analysis=analysis,
        artifacts_dir=artifacts_dir,
        backend=backend,
        backend_from_host_default=backend_from_host_default,
        backend_source=backend_source,
        budget_s=budget_s,
        capture_only=capture_only,
        convergence_retry=convergence_retry,
        corner_points=corner_points,
        corner_section_libs=corner_section_libs,
        deadline=deadline,
        dispatch_netlist_path=dispatch_netlist_path,
        engine=engine,
        engine_version_pin=engine_version_pin,
        fail_fast_probe=fail_fast_probe,
        fail_on_diagnostic_codes=fail_on_diagnostic_codes,
        hosts=hosts,
        initial_ppid=initial_ppid,
        keep_artifacts=keep_artifacts,
        max_workers=max_workers,
        measurements_spec=measurements_spec,
        models_lib=models_lib,
        monte_carlo_info=monte_carlo_info,
        monte_carlo_stats=monte_carlo_stats,
        netlist_identity=netlist_identity,
        netlist_identity_error=netlist_identity_error,
        netlist_overrides_report=netlist_overrides_report,
        netlist_path=netlist_path,
        netlist_source=netlist_source,
        ngspice_binary=ngspice_binary,
        ngspice_init_lines=ngspice_init_lines,
        ngspice_options=ngspice_options,
        osdi_preload=osdi_preload,
        osdi_runner_environment=osdi_runner_environment,
        portable_artifacts=portable_artifacts,
        preflight_warning=preflight_warning,
        provenance_pdk=provenance_pdk,
        repo_root=repo_root,
        request=request,
        request_dir=request_dir,
        requested_backend=requested_backend,
        resume=resume,
        save_mode=save_mode,
        save_vectors=save_vectors,
        complex_vectors=complex_vectors,
        stage_model_inputs=stage_model_inputs,
        stimulus_lines=stimulus_lines,
        stimulus_signals=stimulus_signals,
        stimulus_by_corner=stimulus_by_corner,
        sweep_started=sweep_started,
        threads_per_worker=threads_per_worker,
        timeout_s=timeout_s,
        want_waveforms=want_waveforms,
        waveform_max_bytes=waveform_max_bytes,
    )


def _run_sim_corners(
    request_path: str,
    *,
    artifacts_dir: str | None = None,
    backend: str | None = None,
    max_workers: int | None = None,
    hosts: int | None = None,
    budget_s: float | None = None,
    resume: bool | None = None,
    plot_dir: str | None = None,
    fail_fast_probe: bool | None = None,
    fail_on_diagnostic: list[str] | tuple[str, ...] | None = None,
    max_units_per_job: int | None = None,
    max_concurrent_jobs: int | None = None,
    dry_run: bool = False,
    submit_only: bool = False,
    attach_job_id: str | None = None,
    capacity_wait_s: float | None = None,
    _ownership: contextlib.ExitStack | None = None,
) -> dict[str, Any]:
    """The corner-sweep body of :func:`run_sim` -- every parameter, the
    request/response contract, and the deck-list dispatch alternative
    are documented there. Kept as its own function so the deck dispatch
    in ``run_sim`` adds nothing to this sweep's own complexity budget."""
    resolved = _prepare_sim_request(
        request_path,
        artifacts_dir=artifacts_dir,
        backend=backend,
        max_workers=max_workers,
        hosts=hosts,
        budget_s=budget_s,
        resume=resume,
        plot_dir=plot_dir,
        fail_fast_probe=fail_fast_probe,
        fail_on_diagnostic=fail_on_diagnostic,
        max_units_per_job=max_units_per_job,
        max_concurrent_jobs=max_concurrent_jobs,
        capacity_wait_s=capacity_wait_s,
        dry_run=dry_run,
    )
    analysis = resolved.analysis
    artifacts_dir = resolved.artifacts_dir
    backend = resolved.backend
    backend_from_host_default = resolved.backend_from_host_default
    backend_source = resolved.backend_source
    budget_s = resolved.budget_s
    capture_only = resolved.capture_only
    convergence_retry = resolved.convergence_retry
    corner_points = resolved.corner_points
    corner_section_libs = resolved.corner_section_libs
    deadline = resolved.deadline
    dispatch_netlist_path = resolved.dispatch_netlist_path
    engine = resolved.engine
    engine_version_pin = resolved.engine_version_pin
    fail_fast_probe = resolved.fail_fast_probe
    fail_on_diagnostic_codes = resolved.fail_on_diagnostic_codes
    hosts = resolved.hosts
    initial_ppid = resolved.initial_ppid
    keep_artifacts = resolved.keep_artifacts
    max_workers = resolved.max_workers
    measurements_spec = resolved.measurements_spec
    models_lib = resolved.models_lib
    monte_carlo_info = resolved.monte_carlo_info
    monte_carlo_stats = resolved.monte_carlo_stats
    netlist_identity = resolved.netlist_identity
    netlist_identity_error = resolved.netlist_identity_error
    netlist_overrides_report = resolved.netlist_overrides_report
    netlist_path = resolved.netlist_path
    netlist_source = resolved.netlist_source
    ngspice_binary = resolved.ngspice_binary
    ngspice_init_lines = resolved.ngspice_init_lines
    ngspice_options = resolved.ngspice_options
    osdi_preload = resolved.osdi_preload
    osdi_runner_environment = resolved.osdi_runner_environment
    portable_artifacts = resolved.portable_artifacts
    preflight_warning = resolved.preflight_warning
    provenance_pdk = resolved.provenance_pdk
    repo_root = resolved.repo_root
    request = resolved.request
    request_dir = resolved.request_dir
    requested_backend = resolved.requested_backend
    resume = resolved.resume
    save_mode = resolved.save_mode
    save_vectors = resolved.save_vectors
    complex_vectors = resolved.complex_vectors
    stage_model_inputs = resolved.stage_model_inputs
    stimulus_lines = resolved.stimulus_lines
    stimulus_signals = resolved.stimulus_signals
    stimulus_by_corner = resolved.stimulus_by_corner
    sweep_started = resolved.sweep_started
    threads_per_worker = resolved.threads_per_worker
    timeout_s = resolved.timeout_s
    want_waveforms = resolved.want_waveforms
    waveform_max_bytes = resolved.waveform_max_bytes

    # `pre_completed` holds corner reports already recorded by a prior,
    # interrupted run of this *same* request (fingerprint-matched); those
    # units are never re-dispatched. `checkpoint` (when not None) is handed
    # to the backend so it can persist each newly-completed corner
    # incrementally -- if this run is itself interrupted, only the corners
    # actually completed here are lost from the next resume, not the whole
    # sweep.
    # Issue #2770: own every persistent artifact root before the first write
    # to it. Temporary-only runs (no keep_artifacts/resume/plots, local
    # backends) never touch `artifacts_dir`, so they take no lock.
    _own_persistent_roots(
        _ownership,
        artifacts_dir,
        plot_dir,
        persistent=bool(keep_artifacts or resume or backend in _OFFHOST_BACKENDS),
        dry_run=dry_run,
    )
    checkpoint_path = _checkpoint_path(artifacts_dir)
    fingerprint_inputs: dict[str, Any] = {
        "netlist_path": dispatch_netlist_path,
        "models_lib": models_lib,
        "analysis": analysis,
        "measurements_spec": measurements_spec,
        "corner_points": corner_points,
        "timeout_s": timeout_s,
        "engine": engine,
        "osdi_preload": osdi_preload,
        "corner_section_libs": corner_section_libs,
        "save_mode": save_mode,
        "save_vectors": save_vectors,
        "complex_vectors": complex_vectors,
        "ngspice_options": ngspice_options,
        "stimulus_lines": stimulus_lines,
    }
    pre_completed, loaded_engine_version, checkpoint = _open_resume_checkpoint(
        checkpoint_path, resume=bool(resume), **fingerprint_inputs
    )

    dispatch_points = (
        [point for point in corner_points if point.corner_id not in pre_completed]
        if resume
        else corner_points
    )
    _refuse_override_on_host_default(
        netlist_overrides_report,
        backend=backend,
        backend_from_host_default=backend_from_host_default,
        n_points=len(dispatch_points),
    )
    # A single-unit run under a host default stays here: one corner does not
    # repay a Spot instance's minutes of boot, and it is exactly the "probe
    # one corner locally" case 2am#1004's ruling keeps local.
    backend = _yield_host_default_backend(
        backend, backend_from_host_default, reason_ok=len(dispatch_points) > 1
    )
    # Issue #2977: which backend actually ran, and where the request for it
    # came from -- so a host-policy audit can spot a `local` run although
    # `$KLT_SIM_BACKEND=batch` asked otherwise.
    backend_provenance = {
        "resolved": backend,
        "source": backend_source,
        "requested": requested_backend,
    }
    # Issue #2924: off-host workers run the whole `klt sim` request and return
    # only the scalar results (the AC rawfile never leaves the worker), so the
    # `transfer` form is gated by the mandatory runner-feature check in
    # `sim_batch`/`sim_remote` rather than refused here.
    # Issues #3159/#3161: `analysis.passes` / `measurements[].tian` are gated
    # the same way (the `dual_injection` mandatory runner feature).

    # Two-pass fail-fast probe (issue #1694, opt-in via `fail_fast_probe`/
    # `options.fail_fast_probe`): run once per grid, before any real corner
    # is dispatched -- not per backend/shard/host, and not for `remote`/
    # `batch` (see `_run_remote`'s and `_run_batch`'s docstrings for why
    # probing this process's own host is not representative of a
    # provisioned remote box or a fleet job instance). `None` when the probe
    # did not run at all (opted out, wrong analysis kind, empty grid) or
    # came back inconclusive -- see `_run_fail_fast_probe`.
    probe_result: dict[str, Any] | None = None
    if (
        fail_fast_probe
        and not dry_run
        and backend not in _OFFHOST_BACKENDS
        and dispatch_points
    ):
        probe_result = _run_fail_fast_probe(
            corner_points=dispatch_points,
            netlist_path=dispatch_netlist_path,
            models_lib=models_lib,
            analysis=analysis,
            timeout_s=timeout_s,
            artifacts_dir=artifacts_dir,
            keep_artifacts=keep_artifacts,
            ngspice_binary=ngspice_binary,
            ngspice_init=ngspice_init_lines,
            osdi_preload=osdi_preload,
            save_mode=save_mode,
            ngspice_options=ngspice_options,
            stimulus_lines=stimulus_lines,
        )

    probe_abort: dict[str, Any] | None = None
    if probe_result is not None and probe_result["abort"]:
        probe_abort = {
            "code": "timeout_budget_unreachable",
            "message": (
                "the two-pass fail-fast probe (issue #1694) measured "
                f"{probe_result['measured_rate_s_per_s']:.4g}s of simulated "
                f"time per wall-clock second at corner "
                f"{probe_result['corner_id']!r} "
                + (
                    "(an upper bound -- the probe itself was killed at its "
                    "own bounded timeout before finishing its calibration "
                    "slice)"
                    if probe_result["rate_is_upper_bound"]
                    else "(from a completed calibration slice)"
                )
                + f", implying the full {probe_result['analysis_window_s']:.4g}s "
                f"tran window would need an estimated "
                f"{probe_result['estimated_wall_s']:.4g}s of wall-clock time "
                f"per corner -- more than {_PROBE_ABORT_MARGIN:g}x the "
                f"configured options.timeout_s ({timeout_s:g}s). Aborting "
                "the whole grid now rather than letting every corner burn "
                "its full timeout for a partial result -- see "
                "docs/design/sim-corner-reached-s.md."
            ),
            "reached_s": probe_result["estimated_reached_s"],
            "fraction": probe_result["estimated_fraction"],
        }

    # The engine seam (issue #2016) rides along to the local backends only:
    # `remote`/`batch` run whatever their provisioned box/job ships (the
    # ngspice toolchain -- and `run_sim` refuses `engine: "xyce"` for them
    # above anyway), so the kwarg would be dead weight there. `ngspice_binary`
    # (issue #2423) rides along the same way and for the same reason: a
    # `remote`/`batch` shard re-invokes `klt sim` on a *different* host,
    # which resolves its own `options.ngspice_binary`/`$KLT_NGSPICE_BINARY`
    # fresh against its own PATH from the forwarded request document --
    # this process's own resolution describes only this host.
    # `fail_on_diagnostic` (issue #2492) rides along on exactly the same
    # terms and for the same reason: an off-host shard re-invokes `klt sim`
    # on another host *from the forwarded request document*, so it reads
    # `options.fail_on_diagnostic` itself and grades its own corners
    # inconclusive there -- passing this process's resolved value too would
    # either be dead weight or, worse, double-grade. (A `--fail-on-diagnostic`
    # CLI override is therefore local-backend-only, exactly like
    # `--fail-fast-probe`; see docs/cli/sim.md.) `ngspice_init` (issue #2520)
    # rides along on the same terms for the same reason: an off-host shard
    # reads `options.ngspice_init` itself from the forwarded request
    # document and writes its own `.spiceinit` on its own box -- this
    # process's own resolved lines describe only this host's corner
    # directories.
    # `request_dir` (issue #2668) rides along to the off-host backends only:
    # it is where `options.stage_model_inputs` resolves model sources from.
    # Issue #3052: the run-wide substitution roots for `options.portable_artifacts`;
    # `None` (the default) leaves every persisted deck/log byte-identical.
    # Off-host shards read the option from the forwarded request themselves.
    portable_roots = _portable_roots_for_run(
        portable_artifacts=portable_artifacts,
        keep_artifacts=keep_artifacts,
        request_dir=request_dir,
        artifacts_dir=artifacts_dir,
        models_lib=models_lib,
        corner_points=corner_points,
        osdi_preload=osdi_preload,
    )
    effective_threads = _effective_threads_per_worker(
        threads_per_worker, backend, max_workers, len(dispatch_points)
    )
    local_engine_kwargs = (
        {
            "portable_roots": portable_roots,
            "threads_per_worker": effective_threads,
            "engine": engine,
            "ngspice_binary": ngspice_binary,
            "fail_on_diagnostic": fail_on_diagnostic_codes,
            "ngspice_init": ngspice_init_lines,
            "osdi_preload": osdi_preload,
            "save_mode": save_mode,
            "save_vectors": save_vectors,
            "complex_vectors": complex_vectors,
            "waveform_max_bytes": waveform_max_bytes,
            "ngspice_options": ngspice_options,
            "stimulus_lines": stimulus_lines,
            "convergence_retry": convergence_retry,
        }
        if backend not in _OFFHOST_BACKENDS
        else {"request_dir": request_dir}
    )
    _apply_attach(local_engine_kwargs, attach_job_id, backend, hosts, submit_only)
    _check_capacity_wait_applies(capacity_wait_s, backend, attach_job_id)

    # Issue #2668: stage the complete job closure once, here, before any
    # backend runs -- so a missing model input, unresolvable dependency or
    # exceeded cap is a named SimError before any S3 write, SSH push or
    # instance launch on every off-host path (single job, remote fleet,
    # batch shards alike). The result's asset list is also this report's
    # provenance for what the worker actually received.
    staged_model_environment = _preflight_staged_model_inputs(
        request,
        netlist_path,
        request_dir=request_dir,
        backend=backend,
        enabled=stage_model_inputs,
    )

    if dry_run:
        feature_diagnostic = (
            dry_run_runner_check(
                request, corner_count=len(dispatch_points), timeout_s=timeout_s
            )
            if backend == "batch"
            else None
        )
        return _build_dry_run_report(
            request,
            feature_diagnostic=feature_diagnostic,
            engine=engine,
            backend=backend,
            hosts=hosts,
            netlist_path=netlist_path,
            models_lib=models_lib,
            corner_points=corner_points,
            monte_carlo_info=monte_carlo_info,
            staged_model_environment=staged_model_environment,
        )

    if submit_only:
        _refuse_unsupported_submit_only(backend, hosts, resume, plot_dir)
        return _submit_batch_only(
            request=request,
            corner_points=dispatch_points,
            netlist_path=netlist_path,
            timeout_s=timeout_s,
            keep_artifacts=keep_artifacts,
            want_waveforms=want_waveforms,
            request_dir=request_dir,
            artifacts_dir=artifacts_dir,
        )

    if hosts == 1:
        # The exact pre-#376 call for the default case (no budget, no
        # resume, hosts=1, no fail-fast probe) -- `dispatch_points`/the four
        # new-since kwargs are identity no-ops then (`dispatch_points is
        # corner_points`, `deadline`/`checkpoint`/`probe_abort` all `None`),
        # so this path stays byte-identical for every existing
        # request/response.
        corners_new, backend_engine_version, remote_environment = _BACKENDS[backend](
            corner_points=dispatch_points,
            netlist_path=dispatch_netlist_path,
            models_lib=models_lib,
            analysis=analysis,
            measurements_spec=measurements_spec,
            timeout_s=timeout_s,
            keep_artifacts=keep_artifacts,
            want_waveforms=want_waveforms,
            artifacts_dir=artifacts_dir,
            max_workers=max_workers,
            request=request,
            deadline=deadline,
            initial_ppid=initial_ppid,
            checkpoint=checkpoint,
            probe_abort=probe_abort,
            **local_engine_kwargs,
            # Issue #3896: a `max_units_per_job` split reroute keeps its shard
            # map even without --resume (no checkpoint to take it from).
            **(
                {"shard_fingerprint": _checkpoint_fingerprint(**fingerprint_inputs)}
                if backend == "batch"
                else {}
            ),
        )
    elif backend in _OFFHOST_BACKENDS:
        # Real fleet dispatch (Epic #375 Phase 1B, #377, wired in here by
        # issue #906; `batch`'s own one-job-per-shard analog by issue
        # #2080) -- distinct from the generic `_run_sharded` seam below,
        # which just re-invokes the already-selected backend per shard
        # in-process. A `remote` shard instead needs its own provisioned
        # instance, guardrails, and teardown, which `_run_remote_fleet`
        # gets from `remote_fleet.run_fleet` unmodified; a `batch` shard
        # needs its own S3 job contract and launch. See this function's
        # docstring on `hosts`.
        fleet_dispatch = _run_remote_fleet if backend == "remote" else _run_batch_fleet
        # Issue #3300: a sharded batch run keeps a shard-to-job-id map keyed
        # by the resume fingerprint, so `--resume` re-attaches to shards that
        # already ran instead of paying for them again.
        shard_kwargs: dict[str, Any] = (
            {
                "shard_fingerprint": _checkpoint_fingerprint(**fingerprint_inputs),
                "resume": bool(resume),
            }
            if backend == "batch"
            else {}
        )
        corners_new, backend_engine_version, remote_environment = fleet_dispatch(
            corner_points=dispatch_points,
            netlist_path=netlist_path,
            timeout_s=timeout_s,
            keep_artifacts=keep_artifacts,
            want_waveforms=want_waveforms,
            artifacts_dir=artifacts_dir,
            request=request,
            hosts=hosts,
            measurements_spec=measurements_spec,
            request_dir=request_dir,
            **shard_kwargs,
        )
    else:

        def _shard_runner(
            shard_points: list[CornerPoint],
        ) -> tuple[list[dict[str, Any]], str | None, dict[str, Any] | None]:
            return _BACKENDS[backend](
                corner_points=shard_points,
                netlist_path=dispatch_netlist_path,
                models_lib=models_lib,
                analysis=analysis,
                measurements_spec=measurements_spec,
                timeout_s=timeout_s,
                keep_artifacts=keep_artifacts,
                want_waveforms=want_waveforms,
                artifacts_dir=artifacts_dir,
                max_workers=max_workers,
                request=request,
                deadline=deadline,
                initial_ppid=initial_ppid,
                checkpoint=checkpoint,
                probe_abort=probe_abort,
                **local_engine_kwargs,
            )

        corners_new, backend_engine_version, remote_environment = _run_sharded(
            shard_runner=_shard_runner,
            corner_points=dispatch_points,
            hosts=hosts,
            measurements_spec=measurements_spec,
        )

    _require_params_echo(dispatch_points, corners_new, backend=backend)
    _require_nodeset_echo(dispatch_points, corners_new, backend=backend)
    _require_nodeset_echo(
        dispatch_points,
        corners_new,
        backend=backend,
        label=_IC_LABEL,
        attr="initial_conditions",
        code=IC_UNSUPPORTED_WORKER_CODE,
    )
    _require_options_echo(dispatch_points, corners_new, backend=backend)
    _record_batch_checkpoint(checkpoint, backend, corners_new, backend_engine_version)

    if resume:
        # Reassemble the full corner list in the request's own order: a
        # corner is either freshly run just now (`corners_new`) or reused
        # verbatim from a matching checkpoint (`pre_completed`) -- every
        # `corner_points` entry is in exactly one of the two (see
        # `dispatch_points` above). Only done under `resume`: every backend
        # that reaches here is `local`/`local-parallel` (see the `remote`
        # guard above), which always return exactly one report per
        # dispatched point, keyed by that point's own `corner_id`. `batch`
        # does too: lost shards are synthesized per point (`_lost_shard_corner`).
        new_by_id = {corner["corner_id"]: corner for corner in corners_new}
        corners = [
            new_by_id[point.corner_id]
            if point.corner_id in new_by_id
            else pre_completed[point.corner_id]
            for point in corner_points
        ]
    else:
        # The exact pre-#473 assignment -- `corners_new` is already the
        # full, correctly-ordered report for every backend (including
        # `remote`, whose corners this process never reconciles against a
        # local `CornerPoint` list).
        corners = corners_new
    _attach_meas_lint(corners, analysis, measurements_spec)
    engine_version = (
        backend_engine_version
        if backend_engine_version is not None
        else loaded_engine_version
    )
    _check_offhost_engine_version_pin(
        engine_version_pin, backend, engine, engine_version
    )

    budget_exceeded = any(
        diag["code"] == "budget_exceeded"
        for corner in corners
        for diag in corner["diagnostics"]
    )
    orphaned = any(
        diag["code"] == "orphaned"
        for corner in corners
        for diag in corner["diagnostics"]
    )
    corners_skipped = sum(
        1
        for corner in corners
        if any(
            diag["code"] in ("budget_exceeded", "orphaned")
            for diag in corner["diagnostics"]
        )
    )
    # Issue #3367: a corner that never ran on `batch` (`lost_shard`,
    # `batch_job_failed`, `batch_job_timeout`, `batch_poll_timeout`,
    # `batch_job_interrupted`) is
    # outstanding too, so a checkpoint holding the rest must outlive this
    # run. Kept out of `corners_skipped`
    # (the reported `environment.budget.corners_skipped`) to leave that
    # field's meaning unchanged.
    corners_lost = sum(1 for corner in corners if _is_unrun_batch_corner(corner))

    # Issue #2491: a top-level rollup of every `corners[].diagnostics[]`
    # entry -- including one downgraded to `severity: "warning"` by
    # `_recovered_from_stepping` on an otherwise-`pass`ed corner (issue
    # #205). Pure visibility: counted across *all* corners regardless of
    # that corner's final `status`, so a caller reading only the top-level
    # report can see a fifth of a 100%-`pass` grid recovered from a
    # convergence warning along the way, without walking every corner's own
    # `diagnostics` list. Always present, even when empty (a zero-
    # diagnostic grid reports empty dicts and a zero count, never omits the
    # field) -- see docs/cli/sim.md's report schema.
    diagnostic_counts = _diagnostic_counts(corners)

    if backend == "batch" and corners_skipped == 0 and corners_lost == 0:
        # Issue #3300: nothing left to re-attach to.
        _remove_shard_manifest(artifacts_dir)

    if checkpoint is not None:
        if corners_skipped == 0 and corners_lost == 0:
            # Nothing left to resume -- remove the checkpoint rather than
            # leave a stale file a future --resume could misread (its
            # fingerprint guard makes that safe even if left behind, but a
            # completed sweep has no reason to leave one).
            checkpoint.clear()
            checkpoint_retained = False
        else:
            checkpoint_retained = True
    else:
        checkpoint_retained = False

    # Waveform plots (issue #1723): a post-processing step over the already-
    # assembled `corners`, independent of which backend produced them --
    # every backend (`local`/`local-parallel`/`remote`/a sharded run of
    # either) already leaves each corner's persisted waveform artifact at a
    # *local* path by the time it gets here (the `remote` backend rewrites
    # its pulled artifact paths to local ones -- see
    # `_rewrite_remote_artifact_paths`), so no backend-specific plotting code
    # is needed.
    plots_by_corner: dict[str, list[dict[str, str]]] = {}
    if plot_dir is not None:
        os.makedirs(plot_dir, exist_ok=True)
        for corner in corners:
            corner_plots = _write_corner_plots(
                corner=corner,
                plot_dir=plot_dir,
                analysis_kind=analysis.get("kind"),
                measurements_spec=measurements_spec,
            )
            if corner_plots:
                corner["artifacts"]["plots"] = corner_plots
                plots_by_corner[corner["corner_id"]] = corner_plots

    measurements_rollup = _rollup_measurements(
        measurements_spec, corners, monte_carlo_stats
    )
    if plot_dir is not None:
        # Additive: attach the plot for a measurement's own signal at its
        # worst-case corner, so the miss report itself names the SVG to look
        # at next -- see run_sim's `plot_dir` docstring and issue #1723's
        # "on a measurement miss, the text summary names the SVG path"
        # acceptance criterion. `None` when no worst-case corner exists (an
        # empty corner matrix) or that measurement's `.meas` card names no
        # recognisable signal/has no matching rendered plot.
        specs_by_name = {spec["name"]: spec for spec in measurements_spec}
        for entry in measurements_rollup:
            entry["plot"] = _measurement_plot_path(
                entry, specs_by_name.get(entry["name"]), plots_by_corner
            )

    coverage = _build_coverage(measurements_spec, corners, capture_only=capture_only)
    graded = _grade_corners(corners, measurements_rollup, coverage)
    passed = graded["passed"]
    failed = graded["failed"]
    errored = graded["errored"]
    inconclusive = graded["inconclusive"]
    status = graded["status"]

    engine_build = _engine_build_with_comparison(
        ngspice_binary,
        remote_environment,
        backend,
        staged_model_environment.get("staged_model_inputs"),
        request,
    )

    environment: dict[str, Any] = {
        "engine": engine,
        "engine_version": engine_version,
        # Issue #2423: which ngspice executable actually produced this
        # sweep's corners -- the absolute path `_resolve_ngspice_binary`
        # settled on, alongside `engine_version`, mirroring `klt lvs`'s
        # `environment.netgen_binary` (issue #2373). Always present-but-
        # nullable: `None` for `"engine": "xyce"`, which this issue adds no
        # override for (see `_resolve_ngspice_binary`'s own docstring).
        "ngspice_binary": ngspice_binary,
        # Issue #3095: build identity of that binary (``null`` for xyce).
        "engine_build": engine_build,
        # Issue #1274: normalised like every other path field this response
        # echoes -- the resolved library usually lives outside the repo (a
        # PDK under the user's home directory), which reports
        # `{"path": null, "scope": "external"}` rather than the absolute
        # path. `models_lib_sha256` right below (plus `provenance.deck`)
        # still pins exactly which library ran.
        "models_lib": _report_path(models_lib, repo_root=repo_root),
        "models_lib_sha256": sha256_file(models_lib),
        # Additive: `models_lib_sha256` is always hashed on the submitting
        # host, so for an off-host backend it is the *client's* resolution,
        # not necessarily what the runner loaded (see
        # `environment.remote.runner_model_inputs`).
        "models_lib_sha256_scope": (
            "client" if remote_environment is not None else "local"
        ),
        "netlist_sha256": sha256_file(netlist_path),
    }
    environment.update(_threads_environment(effective_threads))
    environment.update(_corner_coverage_environment(request, corner_points))
    # Issue #2799: `netlist_sha256` covers only the top-level file, so also
    # pin the resolved `.include`/`.inc` closure (same resolver the
    # off-host backends stage with). Additive; an unresolvable closure is
    # disclosed rather than failing the report.
    try:
        environment["netlist_closure"] = sim_staging.netlist_closure(
            request, netlist_path, repo_root=repo_root
        )
    except sim_staging.IncludeStagingError as exc:
        environment["netlist_closure_error"] = str(exc)
    # Additive/optional (issue #3111): the declared solver options, verbatim,
    # so the setting is part of the report's evidence.
    environment.update({"ngspice_options": ngspice_options} if ngspice_options else {})
    _stamp_netlist_identity(environment, netlist_identity, netlist_identity_error)
    if netlist_source is not None:
        # Additive/optional: only present when the request declares it, so
        # existing consumers of a request that omits netlist_source see an
        # unchanged environment block (see docs/cli/sim.md).
        environment["netlist_source"] = netlist_source
    # Additive/optional, one key each and only when the request declared that
    # kind of input: `osdi_preload` (issue #2513) and `corner_section_libs`
    # (issue #2522 -- `models_lib_sha256` alone no longer pins "which models
    # produced this result" once a corner reads sections from other files, and
    # may pin none of it: a bundle whose every section names its own library
    # needs no `models.lib`). See `_model_inputs_environment`.
    environment.update(
        _model_inputs_environment(
            osdi_preload=osdi_preload,
            corner_section_libs=corner_section_libs,
            repo_root=repo_root,
        )
    )
    # Additive/optional (issue #3819): `.osdi` entries the executing host
    # resolved itself -- named, never hashed; the worker's own report is
    # the source of what actually loaded.
    environment.update(_osdi_runner_environment_fields(osdi_runner_environment))
    # Additive/optional (issue #2668): `staged_model_inputs`, only present
    # when `options.stage_model_inputs` staged model inputs for an off-host
    # backend -- see `_preflight_staged_model_inputs`.
    environment.update(staged_model_environment)
    environment.update(
        _portable_environment(
            portable_roots,
            repo_root=repo_root,
            models_lib_sha256=environment["models_lib_sha256"],
        )
    )
    # Additive/optional (issue #2985): ngspice generates no device or
    # resistor noise in `.tran`, so a transient that relies on declared
    # `trnoise`/`trrandom` sources says so. Absent when it does not apply.
    environment.update(
        _transient_noise_environment(
            netlist_path,
            request=request,
            analysis=analysis,
            engine=engine,
            repo_root=repo_root,
        )
    )
    if remote_environment is not None:
        # Additive/optional: only present for the `remote` backend -- see
        # `_run_remote` and docs/cli/sim.md's "Remote backend" section.
        environment["remote"] = remote_environment
    apply_engine_host(
        environment, backend=backend, client_ngspice_binary=ngspice_binary
    )
    environment["backend"] = backend_provenance
    # Issue #2977: additive, only when declared; the schedule as applied (skew
    # and transition times resolved, PWL points included) -- see docs/cli/sim.md.
    environment.update(
        sim_stimulus.environment_entry(stimulus_signals, stimulus_by_corner)
    )
    # Additive/optional (issue #3919): only present when the request declares
    # `netlist_overrides` -- see docs/cli/sim.md.
    environment.update(_netlist_overrides_environment(netlist_overrides_report))
    if monte_carlo_info is not None:
        # Additive/optional: only present when the request declares
        # `monte_carlo` -- the seed contract's request-level echo, per
        # docs/cli/sim.md's "Monte Carlo" section. Per-sample seed values
        # live on each corner (`corners[].monte_carlo`), not here.
        environment["monte_carlo"] = monte_carlo_info
    # Additive/optional (issue #2973): the resolved retry policy, only when
    # `options.retry_on_convergence_error` opted in.
    environment.update(_convergence_retry_environment(convergence_retry))
    if budget_s is not None:
        # Additive/optional: only present when the request declares
        # `options.wall_clock_budget_s` (or `--budget-s` overrides it) --
        # see `run_sim`'s docstring and docs/cli/sim.md's "Wall-clock
        # budget" section. `exceeded`/`corners_skipped` are always reported
        # here, never left for a caller to infer from scanning
        # `corners[].diagnostics` itself.
        environment["budget"] = {
            "wall_clock_budget_s": budget_s,
            "elapsed_s": round(time.monotonic() - sweep_started, 3),
            "exceeded": budget_exceeded,
            "corners_skipped": corners_skipped,
        }
    if orphaned:
        # Additive/optional: only present when the always-on orphan check
        # (see `initial_ppid` above) actually fired -- reported for the
        # benefit of a later `--resume` invocation inspecting this report;
        # the process that would have read it live is, by definition, the
        # one that died.
        environment["orphaned"] = True
    if preflight_warning is not None:
        # Additive/optional: only present when the coarse, pre-grid budget
        # preflight had something to say (issue #1686, third acceptance
        # criterion) -- absent for the common case (an `options.timeout_s`
        # that looks plausible for the declared `tran` window), exactly
        # like `orphaned` above. Advisory only -- never blocks the sweep.
        environment["timeout_preflight_warning"] = preflight_warning
    if probe_result is not None:
        # Additive/optional: only present when `fail_fast_probe`/
        # `options.fail_fast_probe` opted in *and* the probe actually ran
        # and came back conclusive (issue #1694) -- reports the measured
        # rate/estimate regardless of whether it triggered an abort, so a
        # caller can see how close a passing sweep came to the margin, not
        # just learn about it after the fact. See
        # docs/design/sim-corner-reached-s.md and docs/cli/sim.md's
        # "Timeout-budget preflight" section.
        environment["fail_fast_probe"] = probe_result
    if resume:
        # Additive/optional: only present when `options.resume`/`--resume`
        # was requested -- see `run_sim`'s docstring.
        environment["resume"] = {
            "resumed_corners": len(pre_completed),
            "checkpoint_path": _report_path(checkpoint_path, repo_root=repo_root),
            "checkpoint_retained": checkpoint_retained,
        }

    _attach_timeout_budget_hint(environment, corners, timeout_s)

    # Issue #2901: lifted out of `environment.remote` to the top level.
    request_feature_diagnostic = (environment.get("remote") or {}).pop(
        "request_feature_diagnostic", None
    )

    report = {
        "schema_version": SCHEMA_VERSION,
        "netlist": _report_path(netlist_path, repo_root=repo_root),
        "status": status,
        "corner_count": len(corners),
        "passed": passed,
        "failed": failed,
        "errored": errored,
        # Issues #2492 + #2493: always present (default `0`), alongside its
        # three siblings above, and re-keyed into `metrics` below like them.
        "inconclusive": inconclusive,
        # Issue #2491: rollup of `corners[].diagnostics[]` across the whole
        # grid, counted regardless of each corner's final `status` -- see
        # the `diagnostic_counts` computation above.
        "diagnostic_counts": diagnostic_counts,
        "metrics": {
            _CORNER_COUNT_METRIC_NAME: len(corners),
            _CORNER_PASSED_COUNT_METRIC_NAME: passed,
            _CORNER_FAILED_COUNT_METRIC_NAME: failed,
            _CORNER_ERRORED_COUNT_METRIC_NAME: errored,
            _CORNER_INCONCLUSIVE_COUNT_METRIC_NAME: inconclusive,
        },
        "environment": environment,
        # Issue #1996/#2109: what this verdict was actually graded over --
        # always present, purely additive. `nothing_checked` is the
        # load-bearing field: an empty corner matrix, or a `limits` object
        # whose keys `klt sim` never applied, both leave `checked` empty, and
        # the common rollup rule above reports `status: "not_checked"`
        # (exit 4) rather than an unconditional pass for a run that checked
        # nothing. A nonempty `skipped` list beside real checked work (one
        # unrecognised `limits` key alongside a recognised one, say) instead
        # reports `status: "pass_partial"` -- real, exit-0 evidence that is
        # not the unconditional success. See `_build_coverage` and
        # `docs/coverage-contract.md`.
        "coverage": coverage,
        "provenance": build_provenance(
            deck_name=(os.path.basename(models_lib) if models_lib else None),
            deck_path=models_lib,
            pdk=provenance_pdk,
            input_path=netlist_path,
            input_role=INPUT_ROLE_NETLIST,
        ),
        "measurements": measurements_rollup,
        "corners": corners,
    }
    if request_feature_diagnostic is not None:
        # Additive/optional (issue #2901): only present when a batch runner
        # predates a request feature under `runner_version_check: "warn"`.
        report["diagnostics"] = [request_feature_diagnostic]
    _attach_expr_runner_hint(report, measurements_spec, corners, backend=backend)
    _attach_not_reached_count(report, measurements_spec, corners)
    if plot_dir is not None:
        # Additive/optional: only present when `--plot`/`plot_dir` was
        # requested -- the flat list of every SVG actually written, in
        # corner/signal order, so a caller (or a skill) never has to walk
        # `corners[].artifacts.plots` itself just to enumerate what exists.
        # See issue #1723's "file names are deterministic and listed in the
        # JSON" acceptance criterion.
        report["plots"] = [
            {
                "corner_id": corner["corner_id"],
                "signal": plot["signal"],
                "path": plot["path"],
            }
            for corner in corners
            for plot in plots_by_corner.get(corner["corner_id"], [])
        ]
    return report


# --------------------------------------------------------------------------- #
# Model/netlist path resolution
# --------------------------------------------------------------------------- #


def _diagnostic_counts(corners: list[dict[str, Any]]) -> dict[str, Any]:
    """Issue #2491 rollup of ``corners[].diagnostics[]`` across the grid.

    Shared by :func:`run_sim` and :mod:`klayout_tools.sim_merge`.
    """
    by_code: dict[str, int] = {}
    by_severity: dict[str, int] = {}
    corners_with_diagnostics = 0
    for corner in corners:
        corner_diagnostics = corner["diagnostics"]
        if corner_diagnostics:
            corners_with_diagnostics += 1
        for diag in corner_diagnostics:
            by_code[diag["code"]] = by_code.get(diag["code"], 0) + 1
            by_severity[diag["severity"]] = by_severity.get(diag["severity"], 0) + 1
    return {
        "by_code": by_code,
        "by_severity": by_severity,
        "corners_with_diagnostics": corners_with_diagnostics,
    }


def _grade_corners(
    corners: list[dict[str, Any]],
    measurements_rollup: list[dict[str, Any]],
    coverage: dict[str, Any],
) -> dict[str, Any]:
    """The per-corner status counts and the report-level ``status``.

    Shared by :func:`run_sim` and :mod:`klayout_tools.sim_merge`, so a merged
    report is graded by exactly the rule a single request is.
    """
    passed = sum(1 for c in corners if c["status"] == "pass")
    failed = sum(1 for c in corners if c["status"] == "fail")
    errored = sum(1 for c in corners if c["status"] == "error")
    # Issues #2492 + #2493: ONE count for the single "this corner produced
    # numbers, but they are not trustworthy enough to grade" status -- see
    # :data:`_INCONCLUSIVE_STATUS` for why the two increments' originally
    # separate `inconclusive`/`implausible_solution` statuses are one token
    # with two *reason codes* rather than two parallel statuses and two
    # parallel counts. Counted separately from `passed`/`failed`/`errored`
    # (a distrusted corner is none of the three), always present, and always
    # `0` for a request that declares neither `options.fail_on_diagnostic`
    # nor a plausibility bound -- so
    # `passed + failed + errored + inconclusive == corner_count` holds and a
    # caller scanning only the original three cannot silently miscount the
    # corners that moved to the new status. *Which* reason applied is read
    # off `diagnostic_counts` (issue #2491), which already counts the
    # `inconclusive`/`implausible_solution` marker diagnostics by code.
    inconclusive = sum(1 for c in corners if c["status"] == _INCONCLUSIVE_STATUS)
    # A `mean +/- k*sigma` Monte Carlo window outside the limits is a real
    # design failure even when every individual sample passed -- it is only
    # reachable via a declared sigma window, since every other rollup `fail`
    # is already inherited from a corner that failed above -- so it feeds
    # the same `failed` precedence input as a failed corner, rather than
    # being reported and ignored.
    measurement_rollup_failed = any(m["status"] == "fail" for m in measurements_rollup)
    # Issue #2109: the common rollup rule is the single decision table for
    # real failures/errors, zero/partial/unknown/malformed coverage and full
    # success -- see `docs/coverage-contract.md`'s "The common rollup rule"
    # and this module's own coverage-adapter row in its producer/
    # compatibility table. `failed`/`errored` are this run's own outcome,
    # decided before coverage is consulted at all; coverage is read from the
    # `coverage` block this function already built. `rollup_status` reports
    # `"pass_partial"` (the token #1997 already shipped for `klt power`) for
    # successful checks alongside a nonempty skip list -- e.g. an
    # unrecognised `limits` key beside a recognised one, or a corner with no
    # requested measurements -- rather than the unconditional `"pass"` a
    # nonempty skip list must never be reported as.
    rollup = coverage_rollup(
        {"coverage": coverage},
        failed=bool(failed) or measurement_rollup_failed,
        errored=bool(errored),
    )
    status = rollup_status(rollup, success="pass", failure="fail", errored="error")
    # Issues #2492 + #2493: `inconclusive` outranks every non-`"error"`
    # verdict token (`"fail"`, `"pass"`, `"pass_partial"`, `"not_checked"`).
    # The common rollup rule has no vocabulary for "at least one result
    # cannot be trusted enough to grade", so this is applied here as a
    # final, narrowly-scoped override rather than folded into
    # `coverage_rollup` itself -- `rollup`/`coverage` keep describing what
    # was *covered*, which is a separate question from whether the covered
    # results are trustworthy. `"error"` still wins: a corner that produced
    # no number at all is strictly less information than a number the run
    # declines to trust.
    #
    # This mints a top-level `status: "inconclusive"` rather than folding
    # into `"error"`. `docs/json-contract.md` frames status/exit codes as
    # *additive per verb* ("a command may ship a new code above 2 in a later
    # release ... the new code is a new verdict"), and `klt lvs` already
    # ships exactly this token at exactly this exit code
    # (`lvs.py`'s `STATUS_INCONCLUSIVE`, issue #1370) -- so `klt sim`
    # reporting `"inconclusive"`/exit `4` for "ran fine, cannot vouch for
    # the numbers" is the established house spelling, not a new invention,
    # and it keeps "the simulator broke" (`"error"`) distinguishable from
    # "I do not trust this solve" at the top level and not only in the
    # counts. Never fires unless the request opted in, so an unmodified
    # request's `status` is byte-identical to before either issue.
    if status != "error" and inconclusive:
        status = _INCONCLUSIVE_STATUS
    return {
        "passed": passed,
        "failed": failed,
        "errored": errored,
        "inconclusive": inconclusive,
        "status": status,
    }


#: Fraction of ``options.timeout_s`` at which a unit's runtime is reported as
#: a near-miss budget (issue #2894).
_TIMEOUT_HINT_FRACTION = 0.8


def _timeout_budget_hint(
    corners: list[dict[str, Any]], timeout_s: float
) -> dict[str, Any] | None:
    """Report-level hint that a failure is a per-unit time budget, not a
    netlist problem (issue #2894): present when any unit carries a
    ``timeout`` diagnostic or ran for >= 80% of ``options.timeout_s``."""
    runtimes = [
        float(c["runtime_s"])
        for c in corners
        if isinstance(c.get("runtime_s"), (int, float))
    ]
    timed_out = [
        c.get("corner_id")
        for c in corners
        if any(d.get("code") == "timeout" for d in c.get("diagnostics") or [])
    ]
    max_runtime = max(runtimes, default=0.0)
    if not timed_out and not (
        timeout_s > 0 and max_runtime >= _TIMEOUT_HINT_FRACTION * timeout_s
    ):
        return None
    suggested = math.ceil(max(max_runtime, timeout_s) * 2 / 60) * 60
    return {
        "max_unit_runtime_s": round(max_runtime, 3),
        "timeout_s": timeout_s,
        "suggested_timeout_s": suggested,
        "units_timed_out": len(timed_out),
        "message": (
            f"The longest unit ran {max_runtime:g}s against "
            f"options.timeout_s={timeout_s:g}s"
            + (f"; {len(timed_out)} unit(s) timed out" if timed_out else "")
            + ". This is a per-unit time budget, not a netlist problem: "
            f"raise options.timeout_s (for example to {suggested}s) and re-run."
        ),
    }


#: Code of the report-level diagnostic :func:`_job_timeout_diagnostic` emits
#: (issue #2970); listed in the vocabulary above, but it never appears on a
#: corner, so selecting it in ``options.fail_on_diagnostic`` has no effect.
ALL_UNITS_AT_TIMEOUT_CODE = "all_units_at_timeout"

#: Relative tolerance for "this unit's ``runtime_s`` equals the timeout".
_AT_TIMEOUT_TOLERANCE = 0.02


def _job_timeout_diagnostic(
    corners: list[dict[str, Any]], timeout_s: float
) -> dict[str, Any] | None:
    """One job-level diagnostic when *every* unit hit the time budget
    (issue #2970): each unit carries a ``timeout``/``batch_job_timeout``
    diagnostic, or ran for exactly ``options.timeout_s``. N identical
    per-unit timeouts plus M "produced no value" measurement errors read
    like N independent failures; this says they are one cause. Needs >= 2
    units (a lone timeout is already unambiguous)."""

    def _at_timeout(corner: dict[str, Any]) -> bool:
        codes = {d.get("code") for d in corner.get("diagnostics") or []}
        if codes & {"timeout", "batch_job_timeout"}:
            return True
        runtime = corner.get("runtime_s")
        return (
            isinstance(runtime, (int, float))
            and timeout_s > 0
            and abs(runtime - timeout_s) <= _AT_TIMEOUT_TOLERANCE * timeout_s
        )

    if len(corners) < 2 or not all(_at_timeout(c) for c in corners):
        return None
    return {
        "code": ALL_UNITS_AT_TIMEOUT_CODE,
        "severity": "error",
        "units": len(corners),
        "timeout_s": timeout_s,
        "message": (
            f"All {len(corners)} units hit the time budget "
            f"(options.timeout_s={timeout_s:g}s). The per-unit runtime_s is "
            "the timeout, not a measured cost, and the job's elapsed time is "
            "far below units x timeout. Concurrent workers oversubscribing "
            "the host's cores is a common cause: check "
            "environment.threads_per_worker, or set "
            "options.threads_per_worker=1 and split the request."
        ),
    }


def _attach_timeout_budget_hint(
    environment: dict[str, Any], corners: list[dict[str, Any]], timeout_s: float
) -> None:
    """Add ``environment["timeout_budget_hint"]`` when
    :func:`_timeout_budget_hint` reports one (issue #2894). Additive/optional:
    absent unless a unit timed out or came within 80% of ``options.timeout_s``.
    Kept out of ``_run_sim_corners`` to hold its C901 complexity ratchet."""
    hint = _timeout_budget_hint(corners, timeout_s)
    if hint is not None:
        environment["timeout_budget_hint"] = hint
    job_diagnostic = _job_timeout_diagnostic(corners, timeout_s)
    if job_diagnostic is not None:
        environment["job_diagnostic"] = job_diagnostic


def _warn_pdk_ambiguity(resolution: dict[str, Any] | None) -> None:
    """Print :func:`klayout_tools.pdk.ambiguity_warning` to stderr when the
    run's PDK root was resolved by *search* and more than one install on this
    host holds the requested variant (issue #2564).

    The run is still well-defined -- resolution is first-match-wins -- but a
    record can name a pinned ``open_pdks`` commit that a *different* build
    actually produced, and nothing else says so up front: the JSON's
    ``provenance.pdk.source`` is correct but purely after-the-fact, and a
    caller only finds it by inspecting provenance it had no reason to
    suspect. No-op for an unresolved PDK or an unambiguous one.
    """
    warning = pdk_ambiguity_warning(resolution) if resolution else None
    if warning is not None:
        print(warning, file=sys.stderr)


#: Top-level request keys ``klt sim`` reads (or documents). A dry run
#: reports anything else as a warning (issue #3064) -- never an error, so no
#: existing request starts failing.
_KNOWN_REQUEST_KEYS = frozenset(
    {
        "analysis",
        "analysis_steps",
        "backend",
        "batch",
        "confidence",
        "corners",
        "decks",
        "engine",
        "exclude",
        "measurements",
        "models",
        "monte_carlo",
        "netlist",
        "netlist_overrides",
        "netlist_source",
        "options",
        "remote",
        "schema",
        "schema_version",
        "target_ci_halfwidth",
        "_explicit_points",
        sim_staging.STAGED_MODEL_INPUTS_FIELD,
    }
)


def _build_dry_run_report(
    request: dict[str, Any],
    *,
    engine: str,
    backend: str,
    hosts: int,
    netlist_path: str,
    models_lib: str | None,
    corner_points: list[CornerPoint],
    monte_carlo_info: dict[str, Any] | None,
    staged_model_environment: dict[str, list[dict[str, Any]]],
    feature_diagnostic: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """The ``klt sim --dry-run`` response (issue #3064): the request as
    :func:`_run_sim_corners` expanded it, with nothing simulated or submitted.

    Built from the same ``corner_points`` / ``monte_carlo_info`` the real run
    dispatches, so the per-sample seeds are exactly the ones a real run uses.
    """
    if backend == "batch":
        # Issue #3917: a real batch submit refuses an unresolvable model
        # library before any S3 write; the dry-run must say the same.
        _validate_batch_models_lib(request)
    units: list[dict[str, Any]] = []
    for point in corner_points:
        unit: dict[str, Any] = {"corner_id": point.corner_id}
        unit.update(_nodeset_report_field(point))
        unit.update(_ic_report_field(point))
        if point.sample_index is not None:
            unit["sample_index"] = point.sample_index
        if point.mc_seed is not None:
            for key in ("rndseed", "process_seed", "mismatch_seed"):
                unit[key] = point.mc_seed[key]
        units.append(unit)
    warnings = [
        f"unknown top-level request key '{key}' (ignored by klt sim)"
        for key in sorted(set(request) - _KNOWN_REQUEST_KEYS)
    ]
    report: dict[str, Any] = {
        "dry_run": True,
        "status": "valid",
        "engine": engine,
        "backend": backend,
        "hosts": hosts,
        "netlist": netlist_path,
        "models_lib": models_lib,
        "unit_count": len(units),
        "units": units,
        "monte_carlo": monte_carlo_info,
        "staged_files": staged_model_environment.get("staged_model_inputs", []),
        "warnings": warnings,
    }
    if feature_diagnostic is not None:
        report["diagnostics"] = [feature_diagnostic]
    return report


def _preflight_staged_model_inputs(
    request: dict[str, Any],
    netlist_path: str,
    *,
    request_dir: str,
    backend: str,
    enabled: bool,
) -> dict[str, list[dict[str, Any]]]:
    """Stage the complete off-host job closure once, before any backend runs
    (issue #2668), so a missing model input, unresolvable dependency or
    exceeded cap is a named :class:`SimError` before any S3 write, SSH push
    or instance launch on every off-host path -- single job, remote fleet
    and batch shards alike.

    Returns the additive ``environment`` keys describing what the worker
    receives: ``{"staged_model_inputs": [...]}`` -- one ``{name, field,
    kind, sha256, rewritten}`` per staged model file, hashing the bytes the
    worker actually reads (a library whose ``.include``/``.lib`` targets were
    rewritten hashes differently from its source; an ``.osdi`` never does)
    -- or ``{}`` when the option is off or the backend runs locally.
    """
    if not enabled or backend not in _OFFHOST_BACKENDS:
        return {}
    staged_job = sim_staging.stage_sim_job(
        request,
        netlist_path,
        request_dir=request_dir,
        netlist_staged_name=remote_transport.REMOTE_NETLIST_FILENAME,
        reserved_names=(remote_transport.REMOTE_REQUEST_FILENAME,),
        backend=backend,
    )
    return {
        "staged_model_inputs": [asset.to_json() for asset in staged_job.model_assets]
    }


def _model_resolution_models(
    request: dict[str, Any], models: dict[str, Any]
) -> dict[str, Any]:
    """The ``models`` block model *resolution* should see (issue #2668).

    Normally ``models`` itself. A worker request built by
    :func:`sim_staging.stage_sim_job` carries the internal
    :data:`sim_staging.STAGED_MODEL_INPUTS_FIELD` marker: its ``models.lib``
    and per-section libraries are job-relative staged names, so they must
    resolve against the request (job) directory rather than be joined onto
    ``models.pdk``'s directory on the image -- which is what dropping
    ``pdk``/``pdk_root`` from the resolution view achieves, through the
    unchanged :func:`_resolve_lib_ref` rule.
    """
    marker = request.get(sim_staging.STAGED_MODEL_INPUTS_FIELD)
    if marker is None or marker is False:
        return models
    if marker is not True:
        raise SimError(
            f"request.{sim_staging.STAGED_MODEL_INPUTS_FIELD} is internal and "
            "must be a boolean when present"
        )
    return {
        key: value for key, value in models.items() if key not in ("pdk", "pdk_root")
    }


def _resolve_models_lib(models: dict[str, Any], request_dir: str) -> str:
    """Resolve ``request.models`` to an absolute model-library path.

    Two supported shapes:

    - ``{"pdk": "sky130A", "lib": "libs.tech/ngspice/sky130.lib.spice"}``
      (optionally ``"pdk_root"``) -- resolved through :func:`klayout_tools.pdk.find_pdk`
      (issue #45's discovery library), never a hand-rolled path. ``lib`` is
      joined against the resolved variant directory when relative.
    - ``{"lib": "$PDK_ROOT/sky130A/libs.tech/ngspice/sky130.lib.spice"}`` --
      the spike's literal shape, for callers that already resolved
      ``$PDK_ROOT`` themselves (e.g. via ``eval "$(klt pdk env)"``). Env vars
      and ``~`` are expanded; a relative path is joined against the request
      file's directory.

    Raises :class:`SimError` if neither shape resolves to a readable file.
    """
    lib = models.get("lib")
    if lib is None:
        raise SimError("request.models.lib is required")

    resolved = _resolve_lib_ref(lib, models, request_dir)
    if not os.path.isfile(resolved):
        raise SimError(f"model library not found: {resolved}")
    return resolved


def _resolve_lib_ref(lib: str, models: dict[str, Any], request_dir: str) -> str:
    """Apply ``models.lib``'s own resolution rule (see
    :func:`_resolve_models_lib`) to one library reference, without the
    existence check -- shared with issue #2522's per-section corner libraries
    so a ``corners.process`` bundle's ``{"lib": ...}`` entry resolves
    *identically* to the ``models.lib`` it stands in for: joined against the
    resolved PDK variant directory when the request names a PDK, otherwise
    ``$VAR``/``~``-expanded and joined against the request file's directory.
    """
    pdk_variant = models.get("pdk")
    pdk_root = models.get("pdk_root")
    if pdk_variant is not None or pdk_root is not None:
        try:
            resolution = find_pdk(variant=pdk_variant, root=pdk_root)
        except PdkNotFoundError as exc:
            raise SimError(str(exc)) from exc
        variant_dir = os.path.join(resolution["root"], resolution["variant"])
        return lib if os.path.isabs(lib) else os.path.join(variant_dir, lib)
    return _resolve_relative(lib, request_dir)


def _resolve_corner_section_libs(
    points: list[CornerPoint], models: dict[str, Any], request_dir: str
) -> tuple[str, ...]:
    """Resolve every per-section corner library declared by a
    ``corners.process`` bundle (issue #2522) and record the absolute paths on
    the points themselves, returning the distinct resolved libraries in
    first-appearance order (for ``environment.corner_section_libs`` and the
    resume checkpoint's fingerprint).

    Resolution is the same rule as ``models.lib``'s
    (:func:`_resolve_lib_ref`), and every ref is existence-checked **here**,
    before any corner is dispatched -- the same posture as
    :func:`_resolve_models_lib`'s ``model library not found`` and
    :func:`_resolve_osdi_preload`, rather than left to surface as a
    per-corner ngspice parse error deep inside the run.

    Fills each point's ``process_section_libs_resolved`` in place rather than
    rebuilding the point: a copy would have to enumerate every
    ``CornerPoint`` field, so a field added later (an already-derived Monte
    Carlo seed, say) would be silently dropped here. Points that declared no
    per-section library are left exactly as they were, so a pre-#2522 corner
    list is untouched and this returns ``()``.
    """
    resolved_by_ref: dict[str, str] = {}
    for point in points:
        declared = point.process_section_libs
        if declared is None:
            continue
        resolved: list[str | None] = []
        for ref in declared:
            if ref is None:
                resolved.append(None)
                continue
            if ref not in resolved_by_ref:
                path = _resolve_lib_ref(ref, models, request_dir)
                if not os.path.isfile(path):
                    raise SimError(f"corner section library not found: {path}")
                resolved_by_ref[ref] = path
            resolved.append(resolved_by_ref[ref])
        point.process_section_libs_resolved = resolved
    return tuple(resolved_by_ref.values())


def _needs_models_lib(point: CornerPoint) -> bool:
    """Whether this corner still needs the request's own ``models.lib``.

    ``False`` for a process-less point (nothing selects a model section), and
    also -- since issue #2522 -- for a bundle whose *every* section names its
    own library: a PDK that ships nothing but per-device-family corner files
    has no single all-device library to point ``models.lib`` at, so requiring
    one would force the caller to nominate an arbitrary family file just to
    satisfy the check.
    """
    if point.process is None:
        return False
    if point.process_sections is None:
        return True
    declared = point.process_section_libs
    if declared is None:
        return True
    return any(lib is None for lib in declared)


def _finalize_osdi_preload(
    options: dict[str, Any],
    request_dir: str,
    backend: str,
    osdi_preload: tuple[str, ...],
    osdi_runner_resolved: dict[int, str],
) -> tuple[tuple[str, ...], list[dict[str, str]]]:
    """Settle ``osdi_preload`` once the backend is known (issue #3819).

    Off-host, the runner-resolved entries are reported by name in
    ``environment.osdi_preload_runner_resolved`` and the already-resolved
    host-local ones stand. A run that stays on this host reads every entry
    here, so the full existence check and expansion apply, exactly as before.
    """
    if backend not in _OFFHOST_BACKENDS:
        return _resolve_osdi_preload(options, request_dir), []
    return osdi_preload, [
        {"entry": options["osdi_preload"][index], "reason": reason}
        for index, reason in sorted(osdi_runner_resolved.items())
    ]


def _osdi_runner_environment_fields(
    osdi_runner_environment: list[dict[str, str]],
) -> dict[str, Any]:
    """The ``environment`` fragment for runner-resolved ``.osdi`` entries."""
    if not osdi_runner_environment:
        return {}
    return {"osdi_preload_runner_resolved": osdi_runner_environment}


def _resolve_osdi_preload(
    options: dict[str, Any],
    request_dir: str,
    skip_indices: frozenset[int] = frozenset(),
) -> tuple[str, ...]:
    """Resolve ``options.osdi_preload`` (issue #2513) to absolute paths of
    compiled OSDI shared libraries the generated deck must ``pre_osdi``.

    ngspice can only instantiate a Verilog-A compact model through an OSDI
    shared library, loaded with the *control* command ``pre_osdi <file>``
    (``pre_``-prefixed commands run before the circuit is parsed). Several
    open PDKs ship their core devices that way -- IHP's ``sg13g2`` MOSFETs
    are PSP103 Verilog-A, compiled to ``libs.tech/ngspice/osdi/*.osdi`` by
    ``scripts/fetch-sg13g2-sim-toolchain.sh`` (see ``pdks/README.md``) --
    and without the preload every device fails with ``Unable to find
    definition of model ...``. :func:`_write_corner_deck` emits one
    ``pre_osdi`` line per entry, in declared order, at the top of the
    ``.control`` block it already generates, so a caller's netlist body
    never needs a ``.control`` block of its own (docs/cli/sim.md's "Netlist
    convention").

    Same resolution rule as ``models.lib``'s literal-path shape: env vars
    and ``~`` are expanded, a relative path is joined against the request
    file's own directory. Every entry is existence-checked **here**, before
    any corner is dispatched -- the same posture as
    :func:`_resolve_models_lib`'s ``model library not found`` -- rather than
    left to surface as a per-corner ngspice error deep inside the run.

    ``skip_indices`` (issue #3819) are entries the executing host resolves
    itself (see :func:`sim_staging.runner_resolved_osdi`): validated as
    paths but neither expanded nor existence-checked, and omitted from the
    result.

    Returns ``()`` when the option is absent or an empty list, which leaves
    every generated deck byte-identical to before this issue.
    """
    raw = options.get("osdi_preload")
    if raw is None:
        return ()
    if isinstance(raw, str) or not isinstance(raw, (list, tuple)):
        raise SimError(
            "request.options.osdi_preload must be an array of paths to "
            "compiled `.osdi` shared libraries"
        )
    resolved: list[str] = []
    for index, entry in enumerate(raw):
        if not isinstance(entry, str) or not entry.strip():
            raise SimError(
                f"request.options.osdi_preload[{index}] must be a non-empty path"
            )
        if index in skip_indices:
            continue
        path = _resolve_relative(entry, request_dir)
        if not os.path.isfile(path):
            raise SimError(f"osdi_preload file not found: {path}")
        resolved.append(path)
    return tuple(resolved)


def _model_inputs_environment(
    *,
    osdi_preload: tuple[str, ...],
    corner_section_libs: tuple[str, ...],
    repo_root: str | None,
) -> dict[str, list[dict[str, Any]]]:
    """The optional ``environment`` keys pinning the model input files a run
    read *besides* ``models.lib`` -- ``osdi_preload`` (issue #2513, in load
    order) and ``corner_section_libs`` (issue #2522, in first-appearance
    order). An ``.osdi`` binary and a per-device-family corner library are
    each as much a part of "which models produced this result" as
    ``models.lib`` is, and their hashes are what pin them when their paths
    are not reported.

    Each key is present **only** when the request declared that kind of
    input, so a request declaring neither leaves the ``environment`` block
    exactly as it was before either issue.
    """
    entries: dict[str, list[dict[str, Any]]] = {}
    for key, paths in (
        ("osdi_preload", osdi_preload),
        ("corner_section_libs", corner_section_libs),
    ):
        if paths:
            entries[key] = _model_file_entries(paths, repo_root=repo_root)
    return entries


def _model_file_entries(
    paths: tuple[str, ...], *, repo_root: str | None
) -> list[dict[str, Any]]:
    """One ``{name, path, scope, sha256}`` entry per model input file, in the
    order given -- the shared shape behind ``environment.osdi_preload``
    (issue #2513) and ``environment.corner_section_libs`` (issue #2522).
    ``path``/``scope`` are normalised exactly like
    ``environment.models_lib``, so a file inside a PDK under the user's home
    directory reports ``{"path": null, "scope": "external"}`` rather than an
    absolute host path, and ``sha256`` is what pins it when the path itself
    is not reported.
    """
    return [
        {
            "name": os.path.basename(path),
            **_report_path(path, repo_root=repo_root),
            "sha256": sha256_file(path),
        }
        for path in paths
    ]


# --------------------------------------------------------------------------- #
# Corner-matrix expansion
# --------------------------------------------------------------------------- #


class CornerPoint:
    """One expanded (process, supply, temperature) point, pre-run.

    ``sample_index``/``mc_seed`` are populated only when this point is one of
    a Monte Carlo sample sequence (see :func:`_expand_monte_carlo`) --
    ``None``/``None`` for an ordinary PVT corner point. Keeping them on
    :class:`CornerPoint` itself (rather than a parallel structure) is why
    sampling needs no new execution backend: every backend already consumes
    a plain ``list[CornerPoint]`` and calls :func:`_run_corner` per point,
    so a sampled point flows through exactly the same path as an
    unsampled one.

    ``params`` (issue #3035) is the normalized ``{name: scalar}`` row of the
    ``corners.params`` axis this point sits on -- ``{}`` for a request with
    no parameter axis, in which case :attr:`corner_id` and :attr:`slug` are
    exactly the legacy ones.

    ``nodeset`` (issue #3036) is the normalized ``{lowercase node: volts}``
    initial-guess map :func:`_apply_nodeset_rules` selected for this point --
    ``{}`` for a point no ``options.nodeset`` rule matched. It is a solver
    hint, not part of the point's identity: it never changes :attr:`corner_id`.

    ``initial_conditions`` (issue #3881) is the same shape for
    ``options.initial_conditions``: the ``{lowercase node: volts}`` transient
    start state, rendered as ``.ic`` cards plus ``uic`` on the analysis.
    Like ``nodeset`` it never changes :attr:`corner_id`.

    ``options`` (issue #3125) is the same for the ``corners.options`` axis: the
    normalized ``{solver option: number}`` row, ``{}`` when the request has
    no such axis (and then the IDs/slugs are untouched).

    ``process_sections`` is populated only when the request's
    ``corners.process[]`` entry for this point was the multi-section bundle
    object form (``{"name": str, "sections": [...]}``, see
    :func:`_parse_process_entry`) rather than a bare string -- ``None`` for
    an ordinary single-section (or process-less) corner. ``process`` always
    holds the corner's display name either way (the bundle's ``name``, or
    the bare string itself), so `corner_id`/`slug` labeling, the response's
    ``process`` field, and `_matches_exclude` need no bundle-awareness of
    their own.

    ``process_section_libs``/``process_section_libs_resolved`` (issue #2522)
    are the per-section model-library overrides of that bundle -- both
    ``None`` unless at least one ``sections[]`` entry used the
    ``{"lib": str, "section": str}`` object form, in which case both are
    lists **parallel to** ``process_sections`` whose entry is ``None``
    wherever that section takes the request's own ``models.lib``:

    - ``process_section_libs`` holds the refs exactly **as declared**
      (relative / ``$VAR`` / ``~``). This is what crosses the fleet wire
      (:func:`_corner_points_to_wire`): a remote shard resolves them against
      its *own* box, the same way it resolves ``models.lib`` itself, so
      this host's absolute paths must never be baked in here.
    - ``process_section_libs_resolved`` holds the absolute paths
      :func:`_resolve_corner_section_libs` settled on for *this* host. It is
      deliberately **not** serialised -- it is a local execution detail, not
      part of the point's identity.
    """

    __slots__ = (
        "process",
        "process_sections",
        "process_section_libs",
        "process_section_libs_resolved",
        "supply_v",
        "temperature_c",
        "params",
        "nodeset",
        "initial_conditions",
        "options",
        "sample_index",
        "mc_seed",
        "variant",
        "mc_injected",
        "supply_waveforms",
    )

    def __init__(
        self,
        process: str | None,
        supply_v: dict[str, float],
        temperature_c: float,
        *,
        process_sections: list[str] | None = None,
        process_section_libs: list[str | None] | None = None,
        process_section_libs_resolved: list[str | None] | None = None,
        params: dict[str, int | float] | None = None,
        nodeset: dict[str, float] | None = None,
        initial_conditions: dict[str, float] | None = None,
        options: dict[str, int | float] | None = None,
        sample_index: int | None = None,
        mc_seed: dict[str, Any] | None = None,
        variant: str | None = None,
        mc_injected: list[dict[str, Any]] | None = None,
        supply_waveforms: tuple[str, ...] = (),
    ) -> None:
        self.variant = variant
        self.process = process
        self.params: dict[str, int | float] = params or {}
        self.nodeset: dict[str, float] = nodeset or {}
        self.initial_conditions: dict[str, float] = initial_conditions or {}
        self.options: dict[str, int | float] = options or {}
        self.process_sections = process_sections
        self.process_section_libs = process_section_libs
        self.process_section_libs_resolved = process_section_libs_resolved
        self.supply_v = supply_v
        self.temperature_c = temperature_c
        self.sample_index = sample_index
        self.mc_seed = mc_seed
        # Issue #2911: this sample's recorded design-owned mismatch draws
        # (`sim_injected.draw`); ``None`` unless `monte_carlo.injected`.
        self.mc_injected = mc_injected
        # Issue #2902: `supply_v` keys whose PWL/PULSE source is rescaled in
        # the corner's netlist copy instead of `alter`ed.
        self.supply_waveforms = supply_waveforms

    def section_lib(self, index: int, models_lib: str | None) -> str | None:
        """The library path the ``.lib`` card for ``process_sections[index]``
        must name: this bundle's per-section override when it declared one
        for that section (issue #2522 -- the absolute path resolved by
        :func:`_resolve_corner_section_libs` when this run resolved it,
        otherwise the ref exactly as declared, so a direct caller that never
        resolved still names what it asked for rather than silently binding
        ``models.lib``), else the request's own ``models.lib``.
        """
        resolved = self.process_section_libs_resolved
        if resolved is not None and resolved[index] is not None:
            return resolved[index]
        declared = self.process_section_libs
        if declared is not None and declared[index] is not None:
            return declared[index]
        return models_lib

    @property
    def corner_id(self) -> str:
        """``<process>/<supply>V/<temp>C``, per the spike's response schema,
        with a ``/mc<sample_index>`` suffix appended for a Monte Carlo sample
        point (see :func:`_expand_monte_carlo`) so per-sample corner IDs and
        artifact paths never collide with each other or with the unsampled
        corner they were drawn from.

        ``process`` defaults to ``"default"`` when the request declares no
        process axis. Multiple supply rails (an extension beyond the spike's
        single-``vdd`` example) are joined ``key=value`` pairs, sorted by key
        for determinism.
        """
        process_label = self.process if self.process is not None else "default"
        if not self.supply_v:
            supply_label = "novdd"
        elif len(self.supply_v) == 1:
            (value,) = self.supply_v.values()
            supply_label = f"{value:.3f}V"
        else:
            supply_label = (
                "_".join(f"{k}={v:.3f}" for k, v in sorted(self.supply_v.items())) + "V"
            )
        temp_label = _format_number(self.temperature_c)
        base = f"{process_label}/{supply_label}/{temp_label}C"
        if self.variant is not None:
            base = f"{base}/@{self.variant}"
        if self.params:
            base = f"{base}/params:{_format_params_label(self.params)}"
        if self.options:
            base = f"{base}/options:{_format_params_label(self.options)}"
        if self.sample_index is None:
            return base
        return f"{base}/mc{self.sample_index}"

    @property
    def slug(self) -> str:
        """Filesystem-safe artifact-directory name for this corner, derived
        from :attr:`corner_id` (e.g. ``ss/1.620V/125C`` -> ``ss_1p620V_125C``).
        See :func:`_slugify_corner_id` for the parameter-axis and bounded forms."""
        return _slugify_corner_id(self.corner_id)

    @property
    def readable_slug(self) -> str:
        """The unbounded :attr:`slug` (before the long-name hash fallback)."""
        return _readable_slug(self.corner_id)


def _params_report_field(point: CornerPoint) -> dict[str, Any]:
    """The additive ``params`` field of a corner report: present only for a
    point on a ``corners.params`` axis, so every other report is unchanged."""
    return {"params": dict(point.params)} if point.params else {}


def _ic_report_field(point: CornerPoint) -> dict[str, Any]:
    """The additive ``initial_conditions`` field of a corner report (issue
    #3881): present only for a point an ``options.initial_conditions`` rule
    selected."""
    if not point.initial_conditions:
        return {}
    return {"initial_conditions": dict(point.initial_conditions)}


def _nodeset_report_field(point: CornerPoint) -> dict[str, Any]:
    """The additive ``nodeset`` field of a corner report (issue #3036):
    present only for a point an ``options.nodeset`` rule selected."""
    return {"nodeset": dict(point.nodeset)} if point.nodeset else {}


def _options_report_field(point: CornerPoint) -> dict[str, Any]:
    """The additive ``options`` field of a corner report (issue #3125):
    present only for a point on a ``corners.options`` axis."""
    return {"options": dict(point.options)} if point.options else {}


def _format_number(value: float) -> str:
    """Render an int-valued float without a trailing ``.0`` (e.g. temperatures)."""
    if float(value).is_integer():
        return str(int(value))
    return str(value)


_PARAM_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def _format_param_value(value: int | float) -> str:
    """Canonical text of one ``corners.params`` value (issue #3035): the
    shortest round-trip JSON decimal spelling, an integral value rendered as
    an integer, negative zero as ``0`` -- never a rounded or unit-suffixed
    form, so close-valued parameters keep distinct IDs."""
    return repr(_normalize_param_value(value))


def _normalize_param_value(value: int | float) -> int | float:
    """The scalar a parameter coordinate is carried as everywhere (report,
    wire, deck): an integer when the value is integral, else the float."""
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


def _format_params_label(params: dict[str, int | float]) -> str:
    """``name=value`` pairs, sorted by (lowercase) name, comma-joined."""
    return ",".join(
        f"{name}={_format_param_value(value)}" for name, value in sorted(params.items())
    )


def _validate_param_scalar(value: Any, where: str) -> int | float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
    ):
        raise SimError(f"{where} must be a finite number (booleans excluded)")
    return _normalize_param_value(value)


def _validate_param_name(name: Any, where: str) -> str:
    """Lowercased parameter name, or a :class:`SimError` for a malformed or
    reserved (``mc_*``) one."""
    if not isinstance(name, str) or not _PARAM_NAME_RE.fullmatch(name):
        raise SimError(
            f"{where} name {name!r} must match [A-Za-z_][A-Za-z0-9_]* "
            "(a bare SPICE parameter name)"
        )
    lowered = name.lower()
    if lowered.startswith("mc_"):
        raise SimError(
            f"{where} name {name!r} is reserved: mc_* parameters are "
            "generated by monte_carlo"
        )
    return lowered


def _normalize_param_row(
    raw: Any, where: str, supply_v: dict[str, Any] | None = None
) -> dict[str, int | float]:
    """One scalar ``{name: value}`` parameter row (a report/wire ``params``
    map), validated and normalized; ``supply_v`` names may not repeat."""
    if not isinstance(raw, dict):
        raise SimError(f"{where} must be an object mapping names to numbers")
    row: dict[str, int | float] = {}
    supply_names = {str(key).lower() for key in (supply_v or {})}
    for name, value in raw.items():
        lowered = _validate_param_name(name, where)
        if lowered in row:
            raise SimError(f"{where} declares {lowered!r} more than once")
        if lowered in supply_names:
            raise SimError(
                f"{where} name {name!r} is also a supply_v key: a name may "
                "not be in both corners.params and corners.supply_v"
            )
        row[lowered] = _validate_param_scalar(value, f"{where}.{name}")
    return dict(sorted(row.items()))


def _temperature_param_name(corners_spec: dict[str, Any]) -> str | None:
    """The validated, lowercased ``corners.temperature_param`` name (issue
    #3994), or ``None`` when the request does not bind one. It is refused if
    ``corners.params`` or ``corners.supply_v`` already uses the name."""
    raw = corners_spec.get("temperature_param")
    if raw is None:
        return None
    if "units" in corners_spec:
        raise SimError(
            "corners.temperature_param cannot be combined with corners.units: "
            "put the parameter in each unit's `params` instead"
        )
    name = _validate_param_name(raw, "corners.temperature_param")
    declared = {str(key).lower() for key in (corners_spec.get("params") or {})}
    if name in declared:
        raise SimError(
            f"corners.temperature_param name {name!r} is also a corners.params "
            "key: a name may be bound by only one of the two"
        )
    supply = {str(key).lower() for key in (corners_spec.get("supply_v") or {})}
    if name in supply:
        raise SimError(
            f"corners.temperature_param name {name!r} is also a "
            "corners.supply_v key: a name may be in only one of the two maps"
        )
    return name


def _bind_temperature_param(
    param_row: dict[str, int | float],
    temperature_param: str | None,
    temperature_c: float,
) -> dict[str, int | float]:
    """``param_row`` with the grid temperature bound to ``temperature_param``
    (issue #3994), name-sorted like every params row; ``param_row`` unchanged
    when the request binds no temperature parameter."""
    if not temperature_param:
        return param_row
    bound = {**param_row, temperature_param: _normalize_param_value(temperature_c)}
    return dict(sorted(bound.items()))


def _expand_param_axis(
    corners_spec: dict[str, Any],
) -> list[dict[str, int | float]]:
    """The zipped ``corners.params`` rows (issue #3035), or ``[{}]`` for a
    request with no parameter axis. Names zip by index; every array must be
    nonempty and the same length; duplicate normalized rows are rejected."""
    spec = corners_spec.get("params")
    if spec is None or spec == {}:
        return [{}]
    if not isinstance(spec, dict):
        raise SimError(
            "corners.params must be an object mapping parameter names to "
            "equal-length arrays of numbers"
        )
    columns: dict[str, list[int | float]] = {}
    for name, values in spec.items():
        lowered = _validate_param_name(name, "corners.params")
        if lowered in columns:
            raise SimError(
                f"corners.params declares {lowered!r} more than once "
                "(names are case-insensitive)"
            )
        if not isinstance(values, list) or not values:
            raise SimError(f"corners.params.{name} must be a nonempty array of numbers")
        columns[lowered] = [
            _validate_param_scalar(value, f"corners.params.{name}[{i}]")
            for i, value in enumerate(values)
        ]
    if len({len(values) for values in columns.values()}) != 1:
        raise SimError("corners.params arrays must all be the same length")
    supply_names = {str(key).lower() for key in (corners_spec.get("supply_v") or {})}
    clash = sorted(set(columns) & supply_names)
    if clash:
        raise SimError(
            f"corners.params name {clash[0]!r} is also a corners.supply_v key: "
            "a name may be in only one of the two maps (supply_v addresses a "
            "source with `alter`, params address a .param)"
        )
    count = len(next(iter(columns.values())))
    rows = [
        dict(sorted((name, values[i]) for name, values in columns.items()))
        for i in range(count)
    ]
    seen: set[str] = set()
    for row in rows:
        label = _format_params_label(row)
        if label in seen:
            raise SimError(f"corners.params declares the row {label!r} twice")
        seen.add(label)
    return rows


#: ``corners.options`` (issue #3125): the numeric solver ``.options`` a
#: corner axis may sweep. A subset of :data:`NGSPICE_SOLVER_OPTIONS` -- the
#: flag/token options (``method``, ``noopiter``) are not numbers -- plus
#: ``itl3``. ngspice silently ignores an unknown ``.options`` name, so the
#: allow-list turns a typo into a named error.
CORNER_OPTION_NAMES = (
    "abstol",
    "gmin",
    "gminsteps",
    "itl1",
    "itl2",
    "itl3",
    "itl4",
    "pivtol",
    "reltol",
    "srcsteps",
    "vntol",
)


def _validate_corner_option_name(name: Any, where: str) -> str:
    lowered = name.lower() if isinstance(name, str) else name
    if lowered not in CORNER_OPTION_NAMES:
        raise SimError(
            f"{where} name {name!r} is not a supported numeric solver "
            f"option; supported: {', '.join(CORNER_OPTION_NAMES)}"
        )
    return lowered


def _expand_options_axis(
    corners_spec: dict[str, Any],
) -> list[dict[str, int | float]]:
    """The zipped ``corners.options`` rows (issue #3125), or ``[{}]`` for a
    request with no options axis. Same shape and rules as
    :func:`_expand_param_axis`: names zip by index, every array nonempty and
    the same length, duplicate rows rejected."""
    spec = corners_spec.get("options")
    if spec is None or spec == {}:
        return [{}]
    if not isinstance(spec, dict):
        raise SimError(
            "corners.options must be an object mapping solver option names "
            "to equal-length arrays of numbers"
        )
    columns: dict[str, list[int | float]] = {}
    for name, values in spec.items():
        lowered = _validate_corner_option_name(name, "corners.options")
        if lowered in columns:
            raise SimError(
                f"corners.options declares {lowered!r} more than once "
                "(names are case-insensitive)"
            )
        if not isinstance(values, list) or not values:
            raise SimError(
                f"corners.options.{name} must be a nonempty array of numbers"
            )
        columns[lowered] = [
            _validate_param_scalar(value, f"corners.options.{name}[{i}]")
            for i, value in enumerate(values)
        ]
    if len({len(values) for values in columns.values()}) != 1:
        raise SimError("corners.options arrays must all be the same length")
    count = len(next(iter(columns.values())))
    rows = [
        dict(sorted((name, values[i]) for name, values in columns.items()))
        for i in range(count)
    ]
    seen: set[str] = set()
    for row in rows:
        label = _format_params_label(row)
        if label in seen:
            raise SimError(f"corners.options declares the row {label!r} twice")
        seen.add(label)
    return rows


def _normalize_option_row(raw: Any, where: str) -> dict[str, int | float]:
    """One scalar ``{option: number}`` row (a report/wire ``options`` map),
    validated and normalized."""
    if not isinstance(raw, dict):
        raise SimError(f"{where} must be an object mapping names to numbers")
    row: dict[str, int | float] = {}
    for name, value in raw.items():
        lowered = _validate_corner_option_name(name, where)
        if lowered in row:
            raise SimError(f"{where} declares {lowered!r} more than once")
        row[lowered] = _validate_param_scalar(value, f"{where}.{name}")
    return dict(sorted(row.items()))


def _parse_process_entry(
    entry: Any,
) -> tuple[str | None, list[str] | None, list[str | None] | None]:
    """Normalize one ``corners.process[]`` entry into ``(name, sections,
    section_libs)``.

    A bare string is today's single-``.lib``-section corner (e.g. sky130's
    ``"tt"``): returns ``(entry, None, None)`` -- ``process_sections=None``
    signals :func:`_write_corner_deck` to emit its historical single ``.lib``
    line, byte-for-byte unchanged from before this function existed.

    An object ``{"name": str, "sections": [...]}`` is a multi-section corner
    *bundle* -- e.g. gf180mcu's ``sm141064.ngspice``, which has no all-device
    corner sections and instead needs one ``.lib`` card per device family
    (MOS + ``bjt_*`` + ``diode_*`` + ``res_*`` + ``moscap_*`` + ``mimcap_*``)
    to fully select a named corner. :func:`_write_corner_deck` emits one
    ``.lib`` line per section, in declaration order (ordering matters -- the
    gf180 section set has interdependent global switch params).

    Each ``sections[]`` entry is either:

    - a **bare string** -- that section is read from the request's own
      ``models.lib``, exactly as before issue #2522; or
    - an object ``{"lib": str, "section": str}`` -- that section is read
      from ``lib`` instead, for a PDK that ships one corner library *per
      device family* rather than one file carrying every section. Returned
      as the third element, a list parallel to ``sections`` holding the
      declared ref (or ``None`` for a bare-string section).

    ``section_libs`` is ``None`` -- not a list of ``None``s -- when no entry
    used the object form, so an all-bare-string bundle produces exactly the
    ``CornerPoint`` (and fleet wire payload) it produced before #2522.
    """
    if isinstance(entry, str):
        return entry, None, None
    if isinstance(entry, dict):
        name = entry.get("name")
        sections = entry.get("sections")
        if not isinstance(name, str) or not name:
            raise SimError(
                "corners.process bundle entry requires a non-empty string 'name'"
            )
        if not isinstance(sections, list) or not sections:
            raise SimError(
                "corners.process bundle entry requires a non-empty list "
                '\'sections\' of section names or {"lib", "section"} objects'
            )
        parsed = [_parse_process_section(section) for section in sections]
        section_names = [section for section, _ in parsed]
        section_libs = [lib for _, lib in parsed]
        if all(lib is None for lib in section_libs):
            return name, section_names, None
        return name, section_names, section_libs
    raise SimError(
        "corners.process entries must be a string or an object "
        '{"name": str, "sections": list[str | {"lib": str, "section": str}]}'
    )


def _parse_process_section(section: Any) -> tuple[str, str | None]:
    """One ``corners.process[].sections[]`` entry -> ``(section name, library
    ref or None)``.

    A bare string names a section of the request's own ``models.lib``
    (``None`` library ref -- today's behaviour); an object
    ``{"lib": str, "section": str}`` names the model library that section is
    read from instead (issue #2522). The ref is returned exactly as declared;
    :func:`_resolve_corner_section_libs` is what resolves and
    existence-checks it.
    """
    if isinstance(section, str) and section:
        return section, None
    if not isinstance(section, dict):
        raise SimError(
            "corners.process bundle 'sections' entries must be a "
            'non-empty string or an object {"lib": str, "section": str}'
        )
    lib = section.get("lib")
    name = section.get("section")
    if not isinstance(lib, str) or not lib:
        raise SimError(
            "corners.process bundle per-section entry requires a non-empty "
            "string 'lib' (the model library this section is read from)"
        )
    if not isinstance(name, str) or not name:
        raise SimError(
            "corners.process bundle per-section entry requires a non-empty "
            "string 'section'"
        )
    return name, lib


def _corner_lib_lines(point: CornerPoint, models_lib: str | None) -> list[str]:
    """The ``.lib`` cards selecting this corner's model sections, in
    declaration order -- the one place every generated deck
    (:func:`_write_corner_deck`, :func:`_write_xyce_deck`, and
    ``op_sanity._write_op_deck``) derives them from, so a sweep, its
    cross-validation oracle, and ``--op-lint`` can never disagree about which
    model cards a corner selects.

    Three shapes, in order: a multi-section bundle (one line per section,
    each against its own per-section library when it declared one --
    :meth:`CornerPoint.section_lib`), a bare-string process corner (one line
    against ``models_lib``), or no process axis at all (no lines).
    """
    if point.process_sections is not None:
        return [
            f".lib {point.section_lib(index, models_lib)} {section}"
            for index, section in enumerate(point.process_sections)
        ]
    if point.process is not None:
        return [f".lib {models_lib} {point.process}"]
    return []


# --------------------------------------------------------------------------- #
# CornerPoint <-> JSON wire format (issue #906's remote-fleet shard dispatch)
# --------------------------------------------------------------------------- #


def _corner_points_to_wire(points: list[CornerPoint]) -> list[dict[str, Any]]:
    """Serialise a ``list[CornerPoint]`` verbatim, including any already-
    derived Monte Carlo seed, to the internal ``request["_explicit_points"]``
    wire shape a remote fleet shard's pushed request carries -- see
    ``_build_remote_request``'s ``explicit_points`` parameter and this
    module's ``_corner_points_from_wire`` counterpart.

    ``process_section_libs`` (issue #2522) carries each per-section model
    library **as the request declared it**, never
    ``process_section_libs_resolved``'s absolute paths: the shard resolves
    them against its own box, exactly as it resolves ``models.lib`` from the
    forwarded ``models`` block (see ``_build_remote_request``'s decision 4).
    ``None`` for every pre-#2522 corner list, so those shards' payloads are
    unchanged apart from one added null field.
    """
    wire = []
    for point in points:
        entry: dict[str, Any] = {
            "process": point.process,
            "process_sections": point.process_sections,
            "process_section_libs": point.process_section_libs,
            "supply_v": point.supply_v,
            "temperature_c": point.temperature_c,
            "sample_index": point.sample_index,
            "mc_seed": point.mc_seed,
            "variant": point.variant,
            "mc_injected": point.mc_injected,
            "supply_waveforms": list(point.supply_waveforms),
        }
        if point.params:
            # Issue #3035: only a parameter point carries the field, so every
            # request without a `corners.params` axis serialises exactly as
            # before.
            entry["params"] = point.params
        if point.nodeset:
            # Issue #3036: the already-selected map, never the rules -- a
            # shard holds a subset of the points and could not re-resolve them.
            entry["nodeset"] = point.nodeset
        if point.initial_conditions:
            # Issue #3881: likewise the selected map, never the rules.
            entry["initial_conditions"] = point.initial_conditions
        if point.options:
            # Issue #3125: likewise additive, only for an options point.
            entry["options"] = point.options
        wire.append(entry)
    return wire


def _corner_points_from_wire(data: Any) -> list[CornerPoint]:
    """Inverse of :func:`_corner_points_to_wire` -- reconstructs the exact
    ``CornerPoint`` list a fleet shard was handed, seeds included, so a
    remote box's own ``run_sim`` never re-derives (and could never
    disagree about) which points are its own.
    """
    if not isinstance(data, list):
        raise SimError("request._explicit_points must be an array")
    points = []
    for entry in data:
        if not isinstance(entry, dict):
            raise SimError("request._explicit_points entries must be objects")
        sections = entry.get("process_sections")
        section_libs = entry.get("process_section_libs")
        if section_libs is not None and (
            not isinstance(sections, list)
            or not isinstance(section_libs, list)
            or len(section_libs) != len(sections)
        ):
            # Issue #2522: the two lists are parallel by construction
            # (`_parse_process_entry`), so a payload where they disagree is a
            # corrupted shard dispatch -- refuse it rather than emit a deck
            # binding some section to the wrong library.
            raise SimError(
                "request._explicit_points[].process_section_libs must be the "
                "same length as process_sections"
            )
        supply_v = entry.get("supply_v") or {}
        raw_params = entry.get("params")
        # Issue #3035: validated here again, at the receiving worker, with
        # the same rules the caller applied (a wire payload is not trusted).
        params = (
            _normalize_param_row(
                raw_params, "request._explicit_points[].params", supply_v
            )
            if raw_params is not None
            else {}
        )
        # Issue #3036: re-validated at the receiving worker as well.
        raw_nodeset = entry.get("nodeset")
        nodeset = (
            _normalize_nodeset_values(raw_nodeset, "request._explicit_points[].nodeset")
            if raw_nodeset is not None
            else {}
        )
        raw_ic = entry.get("initial_conditions")
        initial_conditions = (
            _normalize_nodeset_values(
                raw_ic, "request._explicit_points[].initial_conditions"
            )
            if raw_ic is not None
            else {}
        )
        raw_options = entry.get("options")
        options = (
            _normalize_option_row(raw_options, "request._explicit_points[].options")
            if raw_options is not None
            else {}
        )
        points.append(
            CornerPoint(
                entry.get("process"),
                supply_v,
                entry.get("temperature_c"),
                process_sections=sections,
                process_section_libs=section_libs,
                params=params,
                nodeset=nodeset,
                initial_conditions=initial_conditions,
                options=options,
                sample_index=entry.get("sample_index"),
                mc_seed=entry.get("mc_seed"),
                variant=entry.get("variant"),
                mc_injected=entry.get("mc_injected"),
                supply_waveforms=tuple(entry.get("supply_waveforms") or ()),
            )
        )
    return points


def _parse_supply_waveforms(
    corners_spec: dict[str, Any], supply_spec: dict[str, list[float]]
) -> tuple[str, ...]:
    """Validate ``corners.supply_waveforms`` (issue #2902): an array of
    ``corners.supply_v`` keys whose PWL/PULSE source is scaled per corner."""
    raw = corners_spec.get("supply_waveforms")
    if raw is None:
        return ()
    if (
        not isinstance(raw, list)
        or not all(isinstance(n, str) and n.strip() for n in raw)
        or len(set(raw)) != len(raw)
    ):
        raise SimError(
            "corners.supply_waveforms must be an array of unique "
            "corners.supply_v source names"
        )
    stray = [n for n in raw if n not in supply_spec]
    if stray:
        raise SimError(
            f"corners.supply_waveforms names {stray!r}, which "
            "corners.supply_v does not sweep: every entry must also be a "
            "corners.supply_v key"
        )
    return tuple(raw)


def _expand_corners(
    corners_spec: dict[str, Any], exclude_spec: list[dict[str, Any]]
) -> list[CornerPoint]:
    """Expand the declared axes into the deterministic, odometer-style corner list.

    Axis order (outer to inner, per the spike): process, then the supply
    sweep, then temperature, then ``params`` (#3035), then ``options``
    (#3125). A missing axis collapses to a single default
    point (``process=None``, ``supply_v={}``, ``temperature_c=27``) rather
    than an error -- a request that only cares about one axis need not
    populate the others.

    Each ``corners.process[]`` entry is either a bare string (one ``.lib``
    section, unchanged) or a ``{"name": str, "sections": [...]}`` bundle
    object (multiple ``.lib`` sections under one named corner, each
    optionally naming its own model library) -- see
    :func:`_parse_process_entry`.

    ``corners.supply_v``'s keys sweep together by index (same-length arrays;
    rails move as a set), matching the spike's documented semantics.

    ``corners.units`` (issue #2993) is the alternative, non-crossing form: an
    explicit list of points executed exactly as given -- see
    :func:`_expand_corner_units`.
    """
    if "units" in corners_spec:
        return _expand_corner_units(corners_spec, exclude_spec)
    raw_processes: list[Any] = corners_spec.get("process") or [None]
    processes: list[tuple[str | None, list[str] | None, list[str | None] | None]] = [
        (None, None, None) if raw is None else _parse_process_entry(raw)
        for raw in raw_processes
    ]

    supply_spec: dict[str, list[float]] = corners_spec.get("supply_v") or {}
    if supply_spec:
        lengths = {len(values) for values in supply_spec.values()}
        if len(lengths) != 1:
            raise SimError("corners.supply_v arrays must all be the same length")
        (count,) = lengths
        supply_points = [
            {key: values[i] for key, values in supply_spec.items()}
            for i in range(count)
        ]
    else:
        supply_points = [{}]

    supply_waveforms = _parse_supply_waveforms(corners_spec, supply_spec)
    temperatures: list[float] = corners_spec.get("temperature_c") or [27]

    variants = _parse_variants(corners_spec.get("variants"))

    # Issue #3035: the innermost axis. Its rows are validated before the
    # exclusion rules so a selector can be checked against the declared names.
    param_rows = _expand_param_axis(corners_spec)
    temperature_param = _temperature_param_name(corners_spec)
    declared_params = set(param_rows[0]).union(filter(None, [temperature_param]))
    _validate_exclude_params(exclude_spec, declared_params)
    # Issue #3125: the solver-option axis, innermost of all.
    option_rows = _expand_options_axis(corners_spec)
    _validate_exclude_options(exclude_spec, set(option_rows[0]))

    points: list[CornerPoint] = []
    for process, process_sections, process_section_libs in processes:
        for supply_v in supply_points:
            for variant_name, variant_sources in variants:
                point_supply = {**supply_v, **variant_sources}
                for temperature_c in temperatures:
                    for param_row, option_row in itertools.product(
                        param_rows, option_rows
                    ):
                        param_row = _bind_temperature_param(
                            param_row, temperature_param, temperature_c
                        )
                        point = CornerPoint(
                            process,
                            point_supply,
                            temperature_c,
                            process_sections=process_sections,
                            process_section_libs=process_section_libs,
                            params=param_row,
                            options=option_row,
                            variant=variant_name,
                            supply_waveforms=supply_waveforms,
                        )
                        if not _is_excluded(point, exclude_spec):
                            points.append(point)
    return points


def _validate_exclude_params(
    exclude_spec: list[dict[str, Any]], declared: set[str]
) -> None:
    """Reject an ``exclude[].params`` selector naming a parameter the request
    does not sweep (issue #3035) -- it could never match, so it would
    silently exclude nothing."""
    for entry in exclude_spec:
        if not isinstance(entry, dict) or "params" not in entry:
            continue
        selector = entry["params"]
        if not isinstance(selector, dict):
            raise SimError("exclude[].params must be an object of name: number")
        for name, value in selector.items():
            lowered = name.lower() if isinstance(name, str) else name
            if lowered not in declared:
                raise SimError(
                    f"exclude[].params names {name!r}, which is not a "
                    "corners.params parameter of this request"
                    + (
                        f" (declared: {', '.join(sorted(declared))})"
                        if declared
                        else ""
                    )
                )
            _validate_param_scalar(value, f"exclude[].params.{name}")


def _validate_exclude_options(
    exclude_spec: list[dict[str, Any]], declared: set[str]
) -> None:
    """Reject an ``exclude[].options`` selector naming an option the request
    does not sweep (issue #3125) -- it could never match."""
    for entry in exclude_spec:
        if not isinstance(entry, dict) or "options" not in entry:
            continue
        selector = entry["options"]
        if not isinstance(selector, dict):
            raise SimError("exclude[].options must be an object of name: number")
        for name, value in selector.items():
            lowered = name.lower() if isinstance(name, str) else name
            if lowered not in declared:
                raise SimError(
                    f"exclude[].options names {name!r}, which is not a "
                    "corners.options option of this request"
                    + (
                        f" (declared: {', '.join(sorted(declared))})"
                        if declared
                        else ""
                    )
                )
            _validate_param_scalar(value, f"exclude[].options.{name}")


_CORNER_UNIT_KEYS = ("process", "temperature_c", "supply_v")
_CORNER_UNIT_CROSSED_KEYS = ("process", "supply_v", "temperature_c", "variants")


def _is_plain_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _expand_corner_units(
    corners_spec: dict[str, Any], exclude_spec: list[dict[str, Any]]
) -> list[CornerPoint]:
    """Expand ``corners.units`` -- an explicit, non-crossed point list.

    Each record is ``{process?, temperature_c?, supply_v?: {key: number}}``
    and becomes exactly one :class:`CornerPoint`, in record order. Omitted
    axes collapse to the same defaults as the crossed form (``process=None``,
    ``supply_v={}``, ``temperature_c=27``) and ``process`` accepts the same
    string-or-bundle forms (:func:`_parse_process_entry`), so a unit that
    names the same coordinates as a crossed point yields an identical
    ``corner_id`` and checkpoint/``--resume`` reuse works across both forms.

    Mutually exclusive with the crossed axes and with ``exclude`` (a unit
    list is already the exact set wanted). Duplicates are judged on
    ``corner_id``, so two records that differ only below its 3-decimal supply
    formatting are refused rather than silently merged.
    """
    for key in _CORNER_UNIT_CROSSED_KEYS:
        if key in corners_spec:
            raise SimError(
                f"corners.units cannot be combined with corners.{key}: units "
                "are executed as given, not crossed"
            )
    if exclude_spec:
        raise SimError(
            "corners.units cannot be combined with exclude: list only the units to run"
        )
    units = corners_spec["units"]
    if not isinstance(units, list) or not units:
        raise SimError("corners.units must be a non-empty array of objects")

    points: list[CornerPoint] = []
    seen: dict[str, int] = {}
    for index, record in enumerate(units):
        point = _parse_corner_unit(record, f"corners.units[{index}]")
        corner_id = point.corner_id
        if corner_id in seen:
            raise SimError(
                f"corners.units[{index}] duplicates corners.units[{seen[corner_id]}] "
                f"(both resolve to corner_id '{corner_id}')"
            )
        seen[corner_id] = index
        points.append(point)
    return points


def _parse_corner_unit(record: Any, where: str) -> CornerPoint:
    """Validate one ``corners.units`` record and build its :class:`CornerPoint`."""
    if not isinstance(record, dict):
        raise SimError(f"{where} must be an object")
    unknown = sorted(set(record) - set(_CORNER_UNIT_KEYS))
    if unknown:
        raise SimError(
            f"{where} has unknown key(s) {', '.join(unknown)} "
            f"(supported: {', '.join(_CORNER_UNIT_KEYS)})"
        )
    raw_process = record.get("process")
    process, sections, section_libs = (
        (None, None, None) if raw_process is None else _parse_process_entry(raw_process)
    )
    temperature_c = record.get("temperature_c", 27)
    if not _is_plain_number(temperature_c):
        raise SimError(f"{where}.temperature_c must be a number")
    return CornerPoint(
        process,
        _parse_unit_supply_v(record.get("supply_v") or {}, where),
        temperature_c,
        process_sections=sections,
        process_section_libs=section_libs,
    )


def _parse_unit_supply_v(supply_v: Any, where: str) -> dict[str, Any]:
    """Require a ``corners.units`` ``supply_v`` to be rail -> scalar number."""
    if not isinstance(supply_v, dict):
        raise SimError(f"{where}.supply_v must be an object of scalar numbers")
    for rail, value in supply_v.items():
        if not _is_plain_number(value):
            raise SimError(
                f"{where}.supply_v.{rail} must be a scalar number "
                "(arrays are only valid in the crossed corners.supply_v form)"
            )
    return dict(supply_v)


_VARIANT_NAME_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]*$")
_EXPR_TOKEN_RE = re.compile(
    r"\s*(?:(?P<num>\d+\.?\d*(?:[eE][+-]?\d+)?|\.\d+(?:[eE][+-]?\d+)?)"
    r"|(?P<name>[A-Za-z_][A-Za-z0-9_.]*)|(?P<op>[-+*/()]))"
)


def _parse_variants(raw: Any) -> list[tuple[str | None, dict[str, float]]]:
    """Normalize ``corners.variants`` (issue #3252) into ``(name, sources)``
    pairs with every source value resolved to a number.

    ``None`` (axis absent) yields the single nameless, source-less entry so
    :func:`_expand_corners` is byte-identical to its pre-variant behaviour.
    Each variant's ``sources`` map a voltage-source name to a number or to an
    arithmetic expression of the *other* sources in the same variant; the
    resolved numbers are applied with ``alter`` exactly like ``supply_v`` and
    override a same-named ``supply_v`` key.
    """
    if raw is None:
        return [(None, {})]
    if not isinstance(raw, list) or not raw:
        raise SimError("corners.variants must be a non-empty array")
    seen: set[str] = set()
    seen_slugs: dict[str, str] = {}
    out: list[tuple[str | None, dict[str, float]]] = []
    for index, entry in enumerate(raw):
        where = f"corners.variants[{index}]"
        if not isinstance(entry, dict):
            raise SimError(f"{where} must be an object")
        name = entry.get("name")
        if not isinstance(name, str) or not _VARIANT_NAME_RE.match(name):
            raise SimError(
                f"{where}.name must be a non-empty string of letters, digits, "
                "'_', '.' or '-'"
            )
        if name in seen:
            raise SimError(f"corners.variants has duplicate name {name!r}")
        seen.add(name)
        # '.' and '-' are both legal in a name but slug to 'p' / 'n', so two
        # distinct names can share an artifact directory -- refuse that.
        slug = _slugify_label(name)
        if slug in seen_slugs:
            raise SimError(
                f"corners.variants names {seen_slugs[slug]!r} and {name!r} "
                f"both map to artifact slug {slug!r}; rename one"
            )
        seen_slugs[slug] = name
        sources = entry.get("sources")
        if not isinstance(sources, dict) or not sources:
            raise SimError(f"{where}.sources must be a non-empty object")
        out.append((name, _resolve_variant_sources(name, sources)))
    return out


def _resolve_variant_sources(variant: str, sources: dict[str, Any]) -> dict[str, float]:
    resolved: dict[str, float] = {}
    visiting: list[str] = []
    lowered = {key.strip().lower(): key for key in sources}

    def resolve(key: str) -> float:
        if key in resolved:
            return resolved[key]
        if key in visiting:
            cycle = " -> ".join([*visiting[visiting.index(key) :], key])
            raise SimError(
                f"corners.variants[{variant!r}].sources has a cyclic "
                f"expression: {cycle}"
            )
        value = sources[key]
        if isinstance(value, bool) or not isinstance(value, (int, float, str)):
            raise SimError(
                f"corners.variants[{variant!r}].sources[{key!r}] must be a "
                "number or an arithmetic expression string"
            )
        if isinstance(value, str):
            visiting.append(key)
            try:
                result = _eval_source_expr(variant, key, value, lowered, resolve)
            finally:
                visiting.pop()
        else:
            result = float(value)
        resolved[key] = result
        return result

    for key in sources:
        resolve(key)
    return {key: resolved[key] for key in sources}


def _tokenize_source_expr(where: str, text: str) -> list[tuple[str, str]]:
    """Split ``text`` into ``(kind, lexeme)`` tokens for the source-expression
    grammar, raising :class:`SimError` on any unparseable character."""
    tokens: list[tuple[str, str]] = []
    pos = 0
    while pos < len(text):
        if not text[pos:].strip():
            break
        match = _EXPR_TOKEN_RE.match(text, pos)
        if match is None:
            raise SimError(f"{where}: cannot parse expression {text!r}")
        kind = match.lastgroup or ""
        tokens.append((kind, match.group(kind)))
        pos = match.end()
    return tokens


class _SourceExprParser:
    """Recursive-descent evaluator for ``+ - * /``, parentheses, numbers, and
    other source names of the same variant (no ``eval``)."""

    def __init__(
        self,
        where: str,
        text: str,
        tokens: list[tuple[str, str]],
        lowered: dict[str, str],
        resolve: Any,
    ) -> None:
        self.where = where
        self.text = text
        self.tokens = tokens
        self.lowered = lowered
        self.resolve = resolve
        self.cursor = 0

    def peek(self) -> tuple[str, str] | None:
        return self.tokens[self.cursor] if self.cursor < len(self.tokens) else None

    def take(self) -> tuple[str, str]:
        token = self.peek()
        if token is None:
            raise SimError(f"{self.where}: unexpected end of expression {self.text!r}")
        self.cursor += 1
        return token

    def _peek_op(self, ops: str) -> str | None:
        token = self.peek()
        if token is not None and token[0] == "op" and token[1] in ops:
            return token[1]
        return None

    def parse(self) -> float:
        result = self.expr()
        leftover = self.peek()
        if leftover is not None:
            raise SimError(
                f"{self.where}: unexpected {leftover[1]!r} in expression {self.text!r}"
            )
        return result

    def expr(self) -> float:
        value = self.term()
        while (op := self._peek_op("+-")) is not None:
            self.take()
            rhs = self.term()
            value = value + rhs if op == "+" else value - rhs
        return value

    def term(self) -> float:
        value = self.factor()
        while (op := self._peek_op("*/")) is not None:
            self.take()
            rhs = self.factor()
            value = self._apply_muldiv(op, value, rhs)
        return value

    def _apply_muldiv(self, op: str, lhs: float, rhs: float) -> float:
        if op == "*":
            return lhs * rhs
        if rhs == 0:
            raise SimError(f"{self.where}: division by zero in {self.text!r}")
        return lhs / rhs

    def _name(self, tok: str) -> float:
        target = self.lowered.get(tok.lower())
        if target is None:
            raise SimError(
                f"{self.where}: unknown source name {tok!r} in expression "
                f"{self.text!r} (expressions may only reference other sources "
                "of the same variant)"
            )
        return float(self.resolve(target))

    def _group(self) -> float:
        value = self.expr()
        if self.take()[1] != ")":
            raise SimError(f"{self.where}: unbalanced parentheses in {self.text!r}")
        return value

    def factor(self) -> float:
        kind, tok = self.take()
        if kind == "num":
            return float(tok)
        if kind == "name":
            return self._name(tok)
        if tok == "-":
            return -self.factor()
        if tok == "+":
            return self.factor()
        if tok == "(":
            return self._group()
        raise SimError(f"{self.where}: unexpected {tok!r} in expression {self.text!r}")


def _eval_source_expr(
    variant: str,
    key: str,
    text: str,
    lowered: dict[str, str],
    resolve: Any,
) -> float:
    """Evaluate ``text`` (``+ - * /``, parentheses, numbers, and other source
    names of the same variant) without ``eval``."""
    where = f"corners.variants[{variant!r}].sources[{key!r}]"
    tokens = _tokenize_source_expr(where, text)
    return _SourceExprParser(where, text, tokens, lowered, resolve).parse()


def _is_excluded(point: CornerPoint, exclude_spec: list[dict[str, Any]]) -> bool:
    """A point is excluded if it matches every key of any one exclude entry."""
    for entry in exclude_spec:
        if _matches_exclude(point, entry):
            return True
    return False


def _mapping_matches(actual: dict[str, Any], selector: Any, fold: Any) -> bool:
    """Whether every ``selector`` key (name folded by ``fold``) equals the
    point's value for it -- the subset match of ``exclude[].supply_v`` and
    ``exclude[].params``."""
    if not isinstance(selector, dict):
        return False
    return all(actual.get(fold(name)) == value for name, value in selector.items())


def _fold_lower(name: Any) -> str:
    return str(name).lower()


# exclude-entry key -> predicate(point, selector value); an unknown key never matches.
_EXCLUDE_KEY_MATCHERS: dict[str, Any] = {
    "process": lambda point, value: point.process == value,
    "temperature_c": lambda point, value: point.temperature_c == value,
    "variant": lambda point, value: point.variant == value,
    "supply_v": lambda point, value: _mapping_matches(point.supply_v, value, str),
    "params": lambda point, value: _mapping_matches(point.params, value, _fold_lower),
    "options": lambda point, value: _mapping_matches(point.options, value, _fold_lower),
}


def _matches_exclude(point: CornerPoint, entry: dict[str, Any]) -> bool:
    for key, value in entry.items():
        matcher = _EXCLUDE_KEY_MATCHERS.get(key)
        if matcher is None or not matcher(point, value):
            return False
    return True


# --------------------------------------------------------------------------- #
# Initial-guess hints: `options.nodeset` (issue #3036)
# --------------------------------------------------------------------------- #

#: A node name a ``.nodeset`` card may carry: a bare SPICE identifier (with
#: ``.``/``:`` for hierarchy) or an all-digit node. Anything else -- spaces,
#: newlines, parentheses, operators -- could break out of the one generated
#: card, so it is refused rather than escaped.
_NODESET_NODE_RE = re.compile(r"(?:[A-Za-z_][A-Za-z0-9_.:]*|[0-9]+)")
_NODESET_RULE_KEYS = ("match", "values")
_NODESET_MATCH_KEYS = ("process", "temperature_c", "supply_v", "params")


def _normalize_nodeset_values(raw: Any, where: str) -> dict[str, float]:
    """A nonempty ``{node: volts}`` map, validated: names match
    :data:`_NODESET_NODE_RE`, are lowercased, and may not repeat
    case-insensitively; values are finite numbers (booleans and nulls are
    refused). Used for the request rules and re-run at the receiving worker
    on the wire map."""
    if not isinstance(raw, dict) or not raw:
        raise SimError(f"{where} must be a nonempty object mapping nodes to volts")
    values: dict[str, float] = {}
    for node, volts in raw.items():
        if not isinstance(node, str) or not _NODESET_NODE_RE.fullmatch(node):
            raise SimError(
                f"{where} node {node!r} is not a supported node name "
                "(expected [A-Za-z_][A-Za-z0-9_.:]* or digits only)"
            )
        lowered = node.lower()
        if lowered in values:
            raise SimError(
                f"{where} names node {lowered!r} more than once "
                "(node names are case-insensitive)"
            )
        if not _is_plain_number(volts) or not math.isfinite(volts):
            raise SimError(f"{where}.{node} must be a finite number of volts")
        values[lowered] = float(volts)
    return dict(sorted(values.items()))


def _nodeset_match_process(value: Any, where: str) -> str:
    if not isinstance(value, str):
        raise SimError(f"{where} must be a string")
    return value


def _nodeset_match_number(value: Any, where: str) -> Any:
    if not _is_plain_number(value) or not math.isfinite(value):
        raise SimError(f"{where} must be a finite number")
    return value


def _nodeset_match_supply(supply: Any, where: str) -> dict[str, Any]:
    if not isinstance(supply, dict):
        raise SimError(f"{where} must be an object of rail: number")
    return {
        rail: _nodeset_match_number(v, f"{where}.{rail}") for rail, v in supply.items()
    }


def _nodeset_match_params(params: Any, where: str) -> dict[str, int | float]:
    if not isinstance(params, dict):
        raise SimError(f"{where} must be an object of name: number")
    row: dict[str, int | float] = {}
    for name, value in params.items():
        lowered = _validate_param_name(name, where)
        if lowered in row:
            raise SimError(
                f"{where} names {lowered!r} more than once (names are case-insensitive)"
            )
        row[lowered] = _validate_param_scalar(value, f"{where}.{name}")
    return row


_NODESET_MATCH_VALIDATORS = {
    "process": _nodeset_match_process,
    "temperature_c": _nodeset_match_number,
    "supply_v": _nodeset_match_supply,
    "params": _nodeset_match_params,
}


def _validate_nodeset_match(raw: Any, where: str) -> dict[str, Any]:
    """One rule's ``match`` selector, normalized to the shape
    :func:`_matches_exclude` consumes (``params`` names lowercased)."""
    if not isinstance(raw, dict):
        raise SimError(f"{where} must be an object")
    unknown = sorted(str(key) for key in set(raw) - set(_NODESET_MATCH_KEYS))
    if unknown:
        raise SimError(
            f"{where} has unknown key(s) {', '.join(unknown)} "
            f"(supported: {', '.join(_NODESET_MATCH_KEYS)})"
        )
    return {
        key: _NODESET_MATCH_VALIDATORS[key](value, f"{where}.{key}")
        for key, value in raw.items()
    }


def _validate_nodeset_rules(
    value: Any, *, label: str = "options.nodeset"
) -> list[dict[str, Any]]:
    """Validate ``options.nodeset`` (issue #3036) into normalized rules
    ``[{"match": {...}, "values": {node: volts}}]``; ``[]`` when omitted or
    empty, so a legacy request is untouched."""
    if value is None:
        return []
    if not isinstance(value, list):
        raise SimError(
            f"{label} must be an array of {{match, values}} rules "
            f"(got {json.dumps(value, default=repr)})"
        )
    rules: list[dict[str, Any]] = []
    for index, rule in enumerate(value):
        where = f"{label}[{index}]"
        if not isinstance(rule, dict) or set(rule) != set(_NODESET_RULE_KEYS):
            raise SimError(
                f"{where} must be an object with exactly the keys "
                f"{', '.join(_NODESET_RULE_KEYS)}"
            )
        rules.append(
            {
                "match": _validate_nodeset_match(rule["match"], f"{where}.match"),
                "values": _normalize_nodeset_values(rule["values"], f"{where}.values"),
            }
        )
    return rules


def _check_nodeset_selector_names(
    points: list[CornerPoint],
    rules: list[dict[str, Any]],
    label: str = "options.nodeset",
) -> None:
    """A selector naming a parameter or supply rail no point carries could
    never match, so it is refused by name rather than as an unused rule."""
    declared_params = {name for point in points for name in point.params}
    declared_rails = {str(rail) for point in points for rail in point.supply_v}
    for index, rule in enumerate(rules):
        match = rule["match"]
        for name in match.get("params", {}):
            if name not in declared_params:
                raise SimError(
                    f"{label}[{index}].match.params names {name!r}, "
                    "which is not a corners.params parameter of this request"
                )
        for rail in match.get("supply_v", {}):
            if str(rail) not in declared_rails:
                raise SimError(
                    f"{label}[{index}].match.supply_v names rail "
                    f"{rail!r}, which no corner of this request sets"
                )


def _apply_nodeset_rules(
    points: list[CornerPoint],
    rules: list[dict[str, Any]],
    *,
    label: str = "options.nodeset",
    attr: str = "nodeset",
) -> None:
    """Resolve ``options.nodeset`` rules onto ``points`` in place (issue
    #3036). Runs once, after corner expansion and exclusions and before Monte
    Carlo cloning, so a sample inherits its base point's map.

    Zero matching rules at a point mean no hint there; exactly one selects its
    whole map; two is an error whatever their node maps are (no implicit merge
    or priority). A rule that matches no retained point is an error, and so is
    a selector naming a parameter or supply rail no point carries.
    """
    if not rules:
        return
    _check_nodeset_selector_names(points, rules, label)
    used = [False] * len(rules)
    for point in points:
        hits = [
            i for i, rule in enumerate(rules) if _matches_exclude(point, rule["match"])
        ]
        if len(hits) > 1:
            raise SimError(
                f"{label} rules {hits[0]} and {hits[1]} both match "
                f"corner {point.corner_id!r}; overlapping rules are never "
                "merged -- make the selectors disjoint"
            )
        if hits:
            used[hits[0]] = True
            setattr(point, attr, dict(rules[hits[0]]["values"]))
    if not all(used):
        raise SimError(
            f"{label}[{used.index(False)}] matches no retained corner "
            "of this request (every declared rule must apply somewhere)"
        )


def _nodeset_cards(point: CornerPoint) -> list[str]:
    """One file-scope ``.nodeset`` card per selected node. ngspice has no
    ``nodeset`` control command (it answers "no such command"); the card
    form, placed after the netlist ``.include``, wins over a body-level hint
    for the same node and survives ``alterparam`` + ``reset``."""
    return [
        f".nodeset v({node})={volts!r}" for node, volts in sorted(point.nodeset.items())
    ]


#: ngspice's warning for a ``.nodeset`` whose node the circuit does not have:
#: it keeps running and exits 0, so without this the corner would grade on an
#: unhinted solve.
_NODESET_MISSING_RE = re.compile(
    r"Nodeset on non-existent node - (?P<node>\S+?), ignored", re.IGNORECASE
)


def _nodeset_failures(point: CornerPoint, log_text: str) -> list[dict[str, Any]]:
    """An error diagnostic for each requested hint node the engine reported as
    non-existent (issue #3036). Warnings about a netlist body's own hints on
    other nodes are none of the request's business and are ignored."""
    if not point.nodeset:
        return []
    missing = sorted(
        {
            match.group("node").lower()
            for match in _NODESET_MISSING_RE.finditer(log_text)
        }
        & set(point.nodeset)
    )
    return [
        {
            "severity": "error",
            "code": "netlist",
            "message": (
                f"options.nodeset could not be applied: the engine reports "
                f"node {node!r} does not exist, so this corner would have "
                "solved without the requested initial guess"
            ),
        }
        for node in missing
    ]


NODESET_UNSUPPORTED_WORKER_CODE = "nodeset_unsupported_worker"


def _require_nodeset_echo(
    points: list[CornerPoint],
    corners: list[dict[str, Any]],
    *,
    backend: str = "remote",
    label: str = "options.nodeset",
    attr: str = "nodeset",
    code: str = NODESET_UNSUPPORTED_WORKER_CODE,
) -> None:
    """Fail the request when an off-host worker did not honor
    ``options.nodeset`` (issue #3036) -- or, with ``label``/``attr``/``code``
    of ``options.initial_conditions``, that field (issue #3881).

    A worker built before the field ignores the hints and would report a
    passing, unhinted solve; one that supports it echoes each point's
    ``nodeset`` in its corner report, which is compared with what was
    dispatched. Independent of any version check, so it also catches a
    same-release runner image built before the feature landed.
    """
    if backend not in _OFFHOST_BACKENDS or not any(getattr(p, attr) for p in points):
        return
    problem: str | None = None
    if len(corners) != len(points):
        problem = (
            f"it returned {len(corners)} corner report(s) for "
            f"{len(points)} dispatched point(s)"
        )
    else:
        for point, corner in zip(points, corners, strict=True):
            expected = getattr(point, attr)
            if (corner.get(attr) or {}) != expected:
                problem = (
                    f"corner {point.corner_id!r} came back with {attr} "
                    f"{corner.get(attr)!r}, expected {expected!r}"
                )
                break
    if problem is not None:
        raise SimError(
            f"the remote/batch worker did not honor {label} -- "
            f"{problem}. A worker built before the field ignores it and "
            "would solve without the requested initial values; update the "
            "runner image to this klt release, or use a local backend. No "
            "result was reported",
            code=code,
        )


# --------------------------------------------------------------------------- #
# Transient start states: `options.initial_conditions` (issue #3881)
# --------------------------------------------------------------------------- #
#
# Same rule grammar and per-point resolution as `options.nodeset`, but the
# map is a transient *start state*: `.ic v(node)=volts` cards plus `uic` on
# the `tran` command (a `.nodeset` is only a DC-solve guess and is dropped
# once `uic` skips the operating point).

_IC_LABEL = "options.initial_conditions"
IC_UNSUPPORTED_WORKER_CODE = "initial_conditions_unsupported_worker"


def _require_plain_tran_for_ic(declared: bool, analysis: dict[str, Any]) -> None:
    """``uic`` is a ``tran`` flag, so a start state needs a plain transient
    analysis; step/pass/two-port sequences and other kinds are refused up
    front rather than silently run without it."""
    if not declared:
        return
    if (
        sim_steps.is_steps_analysis(analysis)
        or str(analysis.get("kind", "")).lower() != "tran"
    ):
        raise SimError(
            f"{_IC_LABEL} requires a single analysis of kind 'tran' "
            "(`.ic` is applied with the `uic` flag of the transient command)"
        )


def _ic_cards(point: CornerPoint) -> list[str]:
    """One file-scope ``.ic`` card per node of the point's start state."""
    return [
        f".ic v({node})={volts!r}"
        for node, volts in sorted(
            (getattr(point, "initial_conditions", None) or {}).items()
        )
    ]


def _ic_analysis_args(point: CornerPoint, args: str) -> str:
    """``args`` with ``uic`` appended for a point carrying a start state,
    unless the request already spells it; untouched otherwise."""
    if not getattr(point, "initial_conditions", None):
        return args
    if "uic" in str(args).lower().split():
        return args
    return f"{args} uic".strip()


# --------------------------------------------------------------------------- #
# Monte Carlo sampling
# --------------------------------------------------------------------------- #
#
# Two halves of #344's decomposition live in this module. *Sampling* (phase
# 1, #348) is here: request schema, seed handling, fan-out mechanics --
# N reproducibly-seeded samples per corner point. *Statistics* (phase 2,
# #349) reduce those samples to a verdict and live with the rest of the
# aggregation code, next to `_rollup_measurements`: see
# :func:`_sample_statistics` (mean/sigma/quantiles) and
# :func:`_evaluate_sigma_window` (the optional `mean +/- k*sigma` inside
# `measurements[].limits` check), both reached from
# :func:`_monte_carlo_rollup`.
#
# ``request.monte_carlo`` is additive and orthogonal to `corners.*`: the PVT
# axes still select *which* process/supply/temperature points are simulated
# (including, per docs/cli/sim.md's "Corner axes" section, an
# already-supported mismatch-enabled `.lib` section like sky130's `tt_mm` --
# no schema change needed there); `monte_carlo` instead asks for each of
# those points to be re-run N times with a fresh, reproducible random seed,
# standing in for the per-instance device variation ``AGAUSS``/``GAUSS``
# calls in a mismatch-aware model library draw on. See
# :func:`_expand_monte_carlo` for the seed-derivation contract and
# docs/cli/sim.md's "Monte Carlo" section for the full request/response
# shape.


def _maybe_gate_instances(
    netlist_path: str,
    mc_spec: dict[str, Any] | None,
    monte_carlo_info: dict[str, Any] | None,
    pdk: str | None,
    *,
    backend: str,
    explicit_points: bool,
) -> tuple[str, dict[str, Any] | None]:
    """Apply ``request.monte_carlo.instances`` when present (issue #2995):
    returns ``(netlist_to_dispatch, monte_carlo_info)``, with the gating
    report attached as ``instance_mismatch``; unchanged when absent."""
    if mc_spec is None or "instances" not in mc_spec:
        return netlist_path, monte_carlo_info
    gated_path, gating = _apply_mismatch_instance_gating(
        netlist_path,
        mc_spec,
        monte_carlo_info,
        pdk,
        backend=backend,
        explicit_points=explicit_points,
    )
    if monte_carlo_info is not None:
        monte_carlo_info = {**monte_carlo_info, "instance_mismatch": gating}
    return gated_path, monte_carlo_info


def _apply_mismatch_instance_gating(
    netlist_path: str,
    mc_spec: dict[str, Any],
    monte_carlo_info: dict[str, Any] | None,
    pdk: str | None,
    *,
    backend: str,
    explicit_points: bool,
) -> tuple[str, dict[str, Any]]:
    """Resolve ``request.monte_carlo.instances`` (issue #2995): validate it,
    and write a gated copy of the netlist in which every instance not
    selected has the PDK's mismatch switch shadowed to ``0``. Returns
    ``(gated_netlist_path, report)``; raises :class:`SimError` for anything
    unsupported, before any corner is dispatched."""
    import atexit

    if backend in _OFFHOST_BACKENDS or explicit_points:
        raise SimError(
            "request.monte_carlo.instances is only supported on local "
            f"backends (got backend {backend!r}); the per-instance netlist "
            "rewrite is not forwarded to remote/batch workers"
        )
    vary = (monte_carlo_info or {}).get("vary")
    if vary not in ("mismatch", "both"):
        raise SimError(
            "request.monte_carlo.instances requires monte_carlo.vary "
            f"'mismatch' or 'both' (got {vary!r})"
        )
    family = pdk_variant_family(pdk) if pdk else None
    switch = sim_mismatch_gate.MISMATCH_SWITCH.get(family or "")
    if switch is None:
        known_pdks = sorted(sim_mismatch_gate.MISMATCH_SWITCH)
        raise SimError(
            "request.monte_carlo.instances needs models.pdk naming a PDK with "
            f"a known mismatch switch ({', '.join(known_pdks)}); got {pdk!r}"
        )
    try:
        include, exclude = sim_mismatch_gate.validate_instances_spec(
            mc_spec["instances"]
        )
        with open(netlist_path, encoding="utf-8", errors="surrogateescape") as fh:
            text = fh.read()
        gated, rows = sim_mismatch_gate.gate_netlist(text, include, exclude, switch)
    except (sim_mismatch_gate.GateError, OSError) as exc:
        raise SimError(str(exc)) from exc
    base = os.path.dirname(os.path.abspath(netlist_path))
    gated = "\n".join(
        _absolutize_relative_include(line, base) for line in gated.split("\n")
    )
    work_dir = _tmp_work_dir()
    atexit.register(_cleanup_dir, work_dir)
    gated_path = os.path.join(work_dir, "netlist.mismatch-gated.cir")
    with open(gated_path, "w", encoding="utf-8", errors="surrogateescape") as fh:
        fh.write(gated)
    report = {
        "switch": switch,
        "include": include,
        "exclude": exclude,
        "instances": rows,
        # All excluded: the run must equal the mismatch-off run (negative control).
        "negative_control": not any(r["randomised"] for r in rows),
    }
    return gated_path, report


def _netlist_overrides_environment(report: Any) -> dict[str, Any]:
    return {} if report is None else {"netlist_overrides": report}


def _refuse_override_on_host_default(
    report: Any,
    *,
    backend: str,
    backend_from_host_default: bool,
    n_points: int,
) -> None:
    """Issue #3919: a multi-corner request with ``netlist_overrides`` cannot
    follow a host-default off-host backend (the rewritten closure stays here)."""
    if (
        report is not None
        and backend_from_host_default
        and backend in _OFFHOST_BACKENDS
        and n_points > 1
    ):
        raise SimError(
            "request.netlist_overrides cannot run on the host-default "
            f"{backend!r} backend (the rewritten include closure is not "
            "forwarded to remote/batch workers) and this request has "
            f"{n_points} corners; run a single corner, or pass "
            "--backend local explicitly"
        )


def _maybe_override_subckts(
    request: dict[str, Any],
    netlist_path: str,
    *,
    request_dir: str,
    backend: str,
    explicit_points: bool,
    repo_root: str | None,
) -> tuple[str, dict[str, Any] | None]:
    """Apply ``request.netlist_overrides`` (issue #3919): returns
    ``(netlist_to_dispatch, environment_entry)``; unchanged and ``None`` when
    absent. Raises :class:`SimError` before any corner is dispatched."""
    import atexit

    if "netlist_overrides" not in request:
        return netlist_path, None
    if backend in _OFFHOST_BACKENDS or explicit_points:
        raise SimError(
            "request.netlist_overrides is only supported on local backends "
            f"(got backend {backend!r}); the rewritten include closure is not "
            "forwarded to remote/batch workers"
        )
    try:
        overrides = sim_subckt_override.parse_spec(
            request["netlist_overrides"], request_dir
        )
        work_dir = _tmp_work_dir()
        atexit.register(_cleanup_dir, work_dir)
        new_path, entries = sim_subckt_override.apply_overrides(
            netlist_path,
            overrides,
            work_dir=work_dir,
            roots=sim_staging.host_resolved_roots(request),
            repo_root=repo_root,
        )
    except sim_subckt_override.OverrideError as exc:
        raise SimError(str(exc)) from exc
    return new_path, {"subckt": entries}


def _validate_monte_carlo_spec(mc_spec: dict[str, Any]) -> tuple[int, int, str]:
    """Validate ``request.monte_carlo`` and return its ``(n, seed, vary)``
    fields. Raises :class:`SimError` with an actionable message for a
    missing/malformed field -- mirroring the request-level validation style
    used elsewhere in :func:`run_sim` (e.g. ``request.analysis``,
    ``request.measurements[]``)."""
    if not isinstance(mc_spec, dict):
        raise SimError("request.monte_carlo must be an object")

    n = mc_spec.get("n")
    if not isinstance(n, int) or isinstance(n, bool) or n < 1:
        raise SimError("request.monte_carlo.n must be a positive integer")

    seed = mc_spec.get("seed")
    if not isinstance(seed, int) or isinstance(seed, bool):
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


def _validate_k_sigma(value: Any, field: str) -> float:
    """Validate a sigma-multiple (``k``) field and return it as a float.

    Shared by ``request.monte_carlo.k_sigma`` (the run-wide default) and
    ``request.measurements[].k_sigma`` (the per-measurement override) so both
    reject the same shapes with the same message style. Zero is allowed --
    ``k=0`` degenerates the limit window to the mean itself, a legitimate
    "is the *typical* part inside the window?" question.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SimError(f"{field} must be a non-negative number (got {value!r})")
    if value < 0:
        raise SimError(f"{field} must be a non-negative number (got {value!r})")
    return float(value)


def _resolve_mismatch_scale(
    mc_spec: dict[str, Any], pdk: Any
) -> tuple[float, str] | None:
    """Validate the optional ``request.monte_carlo.mismatch_scale`` (issue
    #3942) and return ``(scale, param)`` -- the factor and the PDK family's
    mismatch ``.param`` it sets -- or ``None`` when the request omits it.

    The scale is applied by writing the family's multiplicative mismatch
    switch (:data:`sim_mismatch_gate.MISMATCH_SWITCH`), so only a family
    whose switch is verified to scale the ``AGAUSS``/``delvto`` terms
    linearly is supported; any other family (or an undeterminable PDK) is
    refused rather than approximated."""
    if "mismatch_scale" not in mc_spec:
        return None
    value = mc_spec["mismatch_scale"]
    field = "request.monte_carlo.mismatch_scale"
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SimError(f"{field} must be a finite non-negative number (got {value!r})")
    if not math.isfinite(value) or value < 0:
        raise SimError(f"{field} must be a finite non-negative number (got {value!r})")
    family = pdk_variant_family(pdk) if pdk else None
    param = sim_mismatch_gate.MISMATCH_SWITCH.get(family or "")
    if param is None:
        known = ", ".join(sorted(sim_mismatch_gate.MISMATCH_SWITCH))
        raise SimError(
            f"{field} is only supported for PDK families with a verified "
            f"multiplicative mismatch switch ({known}); models.pdk "
            f"{pdk!r} resolves to family {family!r}"
        )
    return float(value), param


def _validate_spec_row(value: Any, field: str) -> str | None:
    """Validate ``request.measurements[].spec_row`` (issue #3778): the id of
    the spec row this measurement grades, a non-empty string (``None`` when
    undeclared). Echoed onto the
    report's per-measurement rollup so `klt signoff` can derive which spec
    rows a citation covers instead of trusting the manifest."""
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise SimError(f"{field} must be a non-empty string (got {value!r})")
    return value


def _validate_plausible_range(value: Any, field: str) -> dict[str, float]:
    """Validate a plausibility-range field (``min``/``max``, either optional
    but at least one required) and return it as a normalised
    ``{"min": float, ...}``/``{"max": float, ...}`` dict.

    Shared by ``request.options.node_voltage_bounds`` (the run-wide,
    voltage-scoped default) and ``request.measurements[].plausible_range``
    (the per-measurement, unscoped declaration) -- issue #2493 -- so both
    reject the same malformed shapes with the same message style, mirroring
    :func:`_validate_k_sigma`'s shared-validator pattern for ``k_sigma``.

    Unlike ``limits`` (which ``_evaluate_limits`` reads defensively via
    ``.get`` and never rejects), a plausibility bound is validated eagerly
    here: it exists specifically to *reclassify* an otherwise-graded value,
    so a malformed declaration should fail the whole request up front
    rather than silently never firing.
    """
    if not isinstance(value, dict):
        raise SimError(
            f"{field} must be an object with 'min' and/or 'max' (got {value!r})"
        )
    result: dict[str, float] = {}
    for key in ("min", "max"):
        if key not in value:
            continue
        entry = value[key]
        if isinstance(entry, bool) or not isinstance(entry, (int, float)):
            raise SimError(f"{field}.{key} must be a number (got {entry!r})")
        result[key] = float(entry)
    if not result:
        raise SimError(f"{field} must declare at least one of 'min'/'max'")
    if "min" in result and "max" in result and result["min"] > result["max"]:
        raise SimError(
            f"{field}.min must be <= {field}.max "
            f"(got min={result['min']!r}, max={result['max']!r})"
        )
    return result


def _resolve_plausible_ranges(
    measurements_spec: list[dict[str, Any]], options: dict[str, Any]
) -> None:
    """Validate and resolve issue #2493's two plausibility declarations into
    each measurement spec, **in place**, before any corner is dispatched.

    Two declarations, one resolved field:

    - ``request.measurements[].plausible_range`` -- per-measurement and
      **unscoped**: it applies to whatever measurement it is attached to
      regardless of ``unit``, because an explicit per-measurement
      declaration is always the caller's deliberate act.
    - ``request.options.node_voltage_bounds`` -- a run-wide default that is
      **voltage-scoped**: it only auto-applies to a measurement declaring
      ``unit: "V"``. There is no measurement "kind" tag in the request
      schema to gate on otherwise, so ``unit`` is the caller's only
      declared signal that a measurement reads a node voltage; one with any
      other ``unit`` (``dB``, ``A``, ``s``, ...) or none never inherits it,
      though it can still opt in explicitly. A measurement's own
      ``plausible_range`` always wins over the inherited default.

    Both are normalised in place (bools rejected, bounds coerced to float,
    ``min <= max`` enforced) so :func:`_run_corner`'s plausibility pre-check
    never has to re-validate -- the same "validate once during request
    parsing" discipline ``k_sigma`` follows. A malformed declaration raises
    :class:`SimError` here, before the first corner runs, rather than
    silently never firing.

    Resolving the default into each spec *here* rather than at each
    measurement's call site is also what makes ``options.resume`` correct
    for free: :func:`_checkpoint_fingerprint` hashes ``measurements_spec``,
    so changing a declared bound between runs invalidates the checkpoint
    instead of silently reusing corner verdicts graded under the old one.

    See ``docs/cli/sim.md``'s "Plausibility bounds" section.
    """
    for spec in measurements_spec:
        declared = spec.get("plausible_range")
        if declared is not None:
            spec["plausible_range"] = _validate_plausible_range(
                declared, f"request.measurements[{spec['name']!r}].plausible_range"
            )

    node_voltage_bounds = options.get("node_voltage_bounds")
    if node_voltage_bounds is None:
        return
    node_voltage_bounds = _validate_plausible_range(
        node_voltage_bounds, "request.options.node_voltage_bounds"
    )
    for spec in measurements_spec:
        if spec.get("plausible_range") is None and spec.get("unit") == "V":
            spec["plausible_range"] = node_voltage_bounds


def _within_plausible_range(value: float, plausible_range: dict[str, float]) -> bool:
    """Whether ``value`` sits inside a declared plausibility bound.

    The numeric-range pre-check for ``options.node_voltage_bounds``/
    ``measurements[].plausible_range`` (issue #2493) -- run by
    :func:`_grade_measurement_value`, *before* the ordinary ``limits``
    comparison (:func:`_evaluate_limits`). Deliberately not a
    log-text classification: an implausible-but-numerically-valid solve
    (a compact model evaluated far outside its fitted range) produces no
    distinguishing ngspice log line for ``_classify_diagnostics`` to catch
    -- this is a purely numeric check the tool performs on the measured
    value itself.
    """
    min_bound = plausible_range.get("min")
    max_bound = plausible_range.get("max")
    if min_bound is not None and value < min_bound:
        return False
    if max_bound is not None and value > max_bound:
        return False
    return True


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
            if isinstance(entry, bool) or not isinstance(entry, (int, float)):
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


def _corner_process_sections(corner_points: list[Any]) -> list[str]:
    """Every process section the run's corner points exercise, in order."""
    return [
        name
        for point in corner_points
        for name in (
            point.process_sections
            if point.process_sections is not None
            else ([point.process] if point.process else [])
        )
    ]


def _corner_coverage_environment(
    request: dict[str, Any], corner_points: list[Any]
) -> dict[str, Any]:
    """Additive ``environment.corner_coverage`` (issue #3794): per requested
    non-typical process section, the device flavours the PDK deck leaves at
    their typical model (e.g. gf180mcu's 5/6V devices), so a multi-corner
    sweep never reads as skewing devices it does not. Empty (key absent) when
    there is no resolvable PDK, no per-flavour corner data, or nothing
    unmoved."""
    models = request.get("models") or {}
    if not (models.get("pdk") or models.get("pdk_root")):
        return {}
    process_sections = list(dict.fromkeys(_corner_process_sections(corner_points)))
    if not process_sections:
        return {}
    try:
        report = list_corners(variant=models.get("pdk"), root=models.get("pdk_root"))
    except PdkNotFoundError:
        return {}
    by_corner = {entry["corner"]: entry for entry in report["corners"]}
    entries: list[dict[str, Any]] = []
    for name in process_sections:
        corner = by_corner.get(name)
        if corner is None:
            continue
        devices = [
            device
            for section in corner["sections"]
            for device in section.get("devices", [])
        ]
        if not devices:
            continue
        unmoved = [d["flavour"] for d in devices if d["skew"] == "typical"]
        if name != "typical" and unmoved:
            entries.append({"process": name, "unmoved_devices": unmoved})
    if not entries:
        return {}
    return {"corner_coverage": {"pdk": report["pdk"], "sections": entries}}


def _with_family_mismatch(
    monte_carlo_info: dict[str, Any],
    injected: tuple[list[dict[str, Any]], float] | None,
    netlist_path: str,
    models: dict[str, Any],
    request: dict[str, Any],
    repo_root: str | None,
    corner_points: list[Any],
) -> dict[str, Any]:
    """``monte_carlo_info`` plus the additive ``family_mismatch`` report: the
    per-device-family activity report (#355) when ``vary`` includes mismatch
    -- never claiming a hard-zero-mismatch family (e.g. gf180mcu's poly
    resistor) was sampled just because the global mismatch switch/section was
    engaged -- plus any design-owned ``injected`` families (#2911)."""
    mismatch_varies = monte_carlo_info["vary"] in ("mismatch", "both")
    if not (mismatch_varies or injected):
        return monte_carlo_info
    family_scan: dict[str, Any] = {}
    family_report: list[dict[str, Any]] = []
    if mismatch_varies:
        family_report = _mismatch_family_report(
            netlist_path,
            models.get("pdk"),
            request=request,
            process_sections=_corner_process_sections(corner_points),
            repo_root=repo_root,
            scan=family_scan,
        )
    if injected:
        family_report = family_report + _injected_family_report(
            injected, mismatch_varies=mismatch_varies, pdk_entries=family_report
        )
    result = {**monte_carlo_info, "family_mismatch": family_report}
    # Issue #2928: additive disclosure of what was scanned, only when the
    # closure is more than the lone top-level file or some part of it could
    # not be scanned (simple requests unchanged).
    if family_scan and (len(family_scan["scanned"]) > 1 or family_scan["not_scanned"]):
        result["family_mismatch_scan"] = family_scan
    return result


def _resolve_injected(
    mc_spec: dict[str, Any],
    netlist_path: str,
    request: dict[str, Any],
    repo_root: str | None,
) -> tuple[list[dict[str, Any]], float] | None:
    """Validate ``monte_carlo.injected`` and resolve it against the netlist's
    ``.include`` closure (issue #2911); ``None`` when not declared. Raises
    :class:`SimError` before any corner runs."""
    try:
        specs, scale = sim_injected.validate(mc_spec)
        if not specs:
            return None
        files, _ = _mismatch_scan_closure(request, netlist_path, repo_root)
        texts = []
        for _label, path in files:
            try:
                with open(path, encoding="utf-8") as handle:
                    texts.append(handle.read())
            except (OSError, UnicodeDecodeError):
                continue
        return sim_injected.resolve(specs, texts), scale
    except sim_injected.InjectedError as exc:
        raise SimError(str(exc)) from exc


def _injected_family_report(
    injected: tuple[list[dict[str, Any]], float],
    *,
    mismatch_varies: bool,
    pdk_entries: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """``family_mismatch`` entries for design-owned mismatch (issue #2911):
    one per (family, parameter, sigma, distribution), ``source: "injected"``.
    ``active`` is true only when the injection can actually vary: scale > 0 and
    ``vary`` includes mismatch (otherwise every sample draws the same value)."""
    resolved, scale = injected
    active = scale > 0 and mismatch_varies
    pdk_active = {e["family"] for e in pdk_entries if e.get("active")}
    groups: dict[tuple[Any, ...], dict[str, Any]] = {}
    for item in resolved:
        families = _detect_device_families(item["card"])
        family = families[0] if families else "other"
        key = (family, item["parameter"], item["relative_sigma"], item["distribution"])
        if key not in groups:
            note = (
                f"design-owned mismatch injected by klt on '{item['parameter']}' "
                f"({item['distribution']}, relative sigma "
                f"{item['relative_sigma']:g} x injected_scale {scale:g})"
            )
            if not mismatch_varies:
                note += (
                    "; monte_carlo.vary does not include mismatch, so draws are pinned"
                )
            if family in pdk_active:
                note += (
                    f"; the PDK's own '{family}' mismatch is also active, so the "
                    f"two compose rather than replace each other"
                )
            groups[key] = {
                "family": family,
                "active": active,
                "source": "injected",
                "parameter": item["parameter"],
                "relative_sigma": item["relative_sigma"],
                "distribution": item["distribution"],
                "instances": [],
                "note": note,
            }
        groups[key]["instances"].append(item["instance"])
    return list(groups.values())


def _mc_sample_seed(
    mc_spec: dict[str, Any],
    seeding: tuple[int, str, str],
    varies: tuple[bool, bool],
    scope_key: object,
    sample_index: int,
    mismatch_scale: tuple[float, str] | None,
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
        # writer); echoed only when declared (issue #3942).
        mc_seed["mismatch_scale"], mc_seed["mismatch_param"] = mismatch_scale
    return mc_seed


def _expand_monte_carlo(
    corner_points: list[CornerPoint],
    mc_spec: dict[str, Any],
    injected: tuple[list[dict[str, Any]], float] | None = None,
    mismatch_scale: tuple[float, str] | None = None,
) -> tuple[list[CornerPoint], dict[str, Any]]:
    """Expand each point in ``corner_points`` into ``mc_spec["n"]`` sample
    points, returning ``(sampled_points, monte_carlo_info)``.

    Fan-out only -- reuses the same ``list[CornerPoint]`` shape every
    execution backend already consumes (see :class:`CornerPoint`'s
    docstring), so no new backend is needed. Sample order is deterministic
    (outer: original corner order from :func:`_expand_corners`; inner:
    ``sample_index`` ``0..n-1``), matching the odometer-style determinism
    :func:`_expand_corners` already guarantees for the PVT axes.

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
    ``monte_carlo.mismatch_scale: 0`` (issue #3942; sky130/gf180mcu) to
    switch the deck's own mismatch off and get a sigma=0 control.

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
                    options=base.options,
                    sample_index=sample_index,
                    mc_seed=mc_seed,
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
        info["mismatch_scale"], info["mismatch_param"] = mismatch_scale
    return sampled, info


# --------------------------------------------------------------------------- #
# Checkpoint/resume (issue #473)
# --------------------------------------------------------------------------- #


def _checkpoint_path(artifacts_dir: str) -> str:
    """Where a resumable sweep's checkpoint lives: a single ``checkpoint.json``
    directly under ``artifacts_dir`` -- the same directory ``keep_artifacts``
    already writes per-corner logs/rawfiles into (see ``run_sim``'s
    docstring), so a resumable run's on-disk footprint stays in one place
    regardless of ``keep_artifacts``.
    """
    return os.path.join(artifacts_dir, "checkpoint.json")


def _open_resume_checkpoint(
    checkpoint_path: str,
    *,
    resume: bool,
    **fingerprint_inputs: Any,
) -> tuple[dict[str, dict[str, Any]], str | None, _Checkpoint | None]:
    """The ``resume`` state of :func:`_run_sim_corners`: the corner reports a
    prior run of this same request already recorded, the engine version that
    run saw, and the checkpoint this run keeps extending. Without ``resume``
    there is nothing to load and no checkpoint is kept.
    """
    if not resume:
        return {}, None, None
    fingerprint = _checkpoint_fingerprint(**fingerprint_inputs)
    pre_completed, loaded_engine_version = _load_checkpoint(
        checkpoint_path, fingerprint
    )
    checkpoint = _Checkpoint(checkpoint_path, fingerprint)
    checkpoint.completed = dict(pre_completed)
    checkpoint.engine_version = loaded_engine_version
    return pre_completed, loaded_engine_version, checkpoint


def _key_in_initial_conditions(
    payload: dict[str, Any], corner_points: Sequence[CornerPoint]
) -> None:
    """Issue #3881: a start state changes the transient result, so a changed
    map must not reuse a stored one. Keyed in only when present, leaving every
    legacy fingerprint untouched."""
    ics = {
        point.corner_id: point.initial_conditions
        for point in corner_points
        if point.initial_conditions
    }
    if ics:
        payload["initial_conditions"] = ics


def _key_in_mc_mismatch_scale(
    payload: dict[str, Any], corner_points: Sequence[CornerPoint]
) -> None:
    """Issue #3942: corner IDs carry neither seeds nor the sigma scale, so a
    resume under another ``monte_carlo.mismatch_scale`` would reuse samples
    drawn at the old scale. Keyed in only when declared."""
    mc_scales = sorted(
        {
            (point.mc_seed["mismatch_param"], point.mc_seed["mismatch_scale"])
            for point in corner_points
            if point.mc_seed and "mismatch_scale" in point.mc_seed
        }
    )
    if mc_scales:
        payload["mc_mismatch_scale"] = mc_scales


def _key_in_mc_seed_options(
    payload: dict[str, Any], corner_points: Sequence[CornerPoint]
) -> None:
    """Issues #2899/#4173: corner IDs carry no seeds, so a resume under another
    ``monte_carlo.seed_scope`` / ``seed_mode`` would reuse samples drawn from
    other seeds. Each is keyed in off its default only, so an existing
    checkpoint stays valid for a request that omits the field."""
    for field, default, key in (
        ("seed_scope", DEFAULT_MC_SEED_SCOPE, "mc_seed_scope"),
        ("seed_mode", DEFAULT_MC_SEED_MODE, "mc_seed_mode"),
    ):
        values = {
            point.mc_seed[field]
            for point in corner_points
            if point.mc_seed and field in point.mc_seed
        } - {default}
        if values:
            payload[key] = sorted(values)


def _checkpoint_fingerprint(
    *,
    netlist_path: str,
    models_lib: str | None,
    analysis: dict[str, Any],
    measurements_spec: list[dict[str, Any]],
    corner_points: list[CornerPoint],
    timeout_s: float,
    engine: str,
    osdi_preload: tuple[str, ...] = (),
    corner_section_libs: tuple[str, ...] = (),
    save_mode: str = DEFAULT_SAVE_MODE,
    save_vectors: tuple[str, ...] = (),
    complex_vectors: tuple[str, ...] = (),
    ngspice_options: dict[str, Any] | None = None,
    stimulus_lines: tuple[str, ...] = (),
) -> str:
    """A SHA-256 fingerprint of everything that determines what a sweep's
    corners actually run -- the basis for deciding whether an on-disk
    checkpoint still applies to *this* request.

    Content-hashes the netlist/model library (not just their paths -- an
    edited-in-place file at the same path must invalidate the checkpoint,
    per this issue's own acceptance criteria) alongside the analysis,
    measurement cards, per-corner timeout, engine, and the exact list of
    expanded corner IDs (covers the whole corner matrix, including any
    Monte Carlo expansion, since two requests that expand to a different
    corner list are not resumable against each other's progress). A
    fingerprint mismatch means "resume against a request file that has
    changed since the checkpoint was written" -- the checkpoint is treated
    as if it did not exist, never silently reused against a since-modified
    matrix (see ``run_sim``'s docstring).
    """
    payload: dict[str, Any] = {
        "netlist_sha256": sha256_file(netlist_path),
        "models_lib_sha256": sha256_file(models_lib),
        "analysis": analysis,
        "measurements_spec": measurements_spec,
        "timeout_s": timeout_s,
        "engine": engine,
        "corner_ids": [point.corner_id for point in corner_points],
    }
    if osdi_preload:
        # Issue #2513: a rebuilt `.osdi` changes every device it defines, so
        # it invalidates a checkpoint exactly like an edited model library.
        # Only keyed in when declared, so a request without the option keeps
        # its pre-#2513 fingerprint (and its existing checkpoint) unchanged.
        payload["osdi_preload_sha256"] = [sha256_file(p) for p in osdi_preload]
    if save_mode != DEFAULT_SAVE_MODE:
        # Issue #2732: the saved set decides which measurements resolve, so
        # a checkpoint written under one mode never applies to the other.
        # Only keyed in off the default, so an existing checkpoint stays
        # valid for a request that omits the option.
        payload["save_mode"] = save_mode
    if save_vectors:
        # Issue #3211: the saved vector set decides which measurements resolve.
        payload["save_vectors"] = list(save_vectors)
    if complex_vectors:
        # Issue #3337: the declared vectors decide what the corner returns.
        payload["complex_vectors"] = list(complex_vectors)
    if ngspice_options:
        # Issue #3111: solver options change which solves converge, so a
        # checkpoint written under other options never applies. Only keyed
        # in when declared, leaving existing fingerprints untouched.
        payload["ngspice_options"] = ngspice_options
    _key_in_mc_seed_options(payload, corner_points)
    _key_in_mc_mismatch_scale(payload, corner_points)
    if stimulus_lines:
        # Issue #2977: the declared stimulus decides every corner's drive, so
        # an edited schedule must invalidate the checkpoint. Only keyed in
        # when declared, so existing checkpoints stay valid.
        payload["stimulus"] = list(stimulus_lines)
    if any(point.nodeset for point in corner_points):
        # Issue #3036: an initial guess can change which solution a
        # multi-equilibrium circuit lands on, so a changed hint must not reuse
        # a stored result. The corner IDs above do not carry it. Only keyed
        # in when a hint exists, leaving every legacy fingerprint untouched.
        payload["nodeset"] = {
            point.corner_id: point.nodeset for point in corner_points if point.nodeset
        }
    _key_in_initial_conditions(payload, corner_points)
    if corner_section_libs:
        # Issue #2522: a corner's per-section libraries determine its device
        # models exactly as `models.lib` does, so editing one must invalidate
        # the checkpoint too. Only keyed in when the request declares them, so
        # a bundle without per-section libraries keeps its pre-#2522
        # fingerprint (and its existing checkpoint) unchanged.
        payload["corner_section_libs_sha256"] = [
            sha256_file(p) for p in corner_section_libs
        ]
    blob = json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def _load_checkpoint(
    path: str, fingerprint: str
) -> tuple[dict[str, dict[str, Any]], str | None]:
    """Read a checkpoint file, returning ``({corner_id: corner_report},
    engine_version)`` -- both empty/``None`` when the file is missing,
    unreadable, not valid JSON, or its own fingerprint does not match
    ``fingerprint`` (see :func:`_checkpoint_fingerprint`). Never raises: a
    corrupt or stale checkpoint degrades to "nothing completed yet", the
    same outcome as no checkpoint at all, rather than aborting the sweep.
    """
    if not os.path.isfile(path):
        return {}, None
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return {}, None
    if not isinstance(data, dict) or data.get("fingerprint") != fingerprint:
        return {}, None
    completed = data.get("completed")
    if not isinstance(completed, dict):
        return {}, None
    return completed, data.get("engine_version")


class _Checkpoint:
    """Incrementally persists completed corner reports to a JSON file so an
    interrupted sweep (wall-clock budget hit, orphaned parent, plain crash)
    can resume without recomputing corners that already finished.

    Only constructed when the request enables ``options.resume``/``--resume``
    (see ``run_sim``) -- writing/reading a checkpoint is an explicit opt-in,
    mirroring ``options.keep_artifacts``, so a default sweep leaves no extra
    file behind. Every :meth:`record` call flushes the *entire* completed set
    to disk immediately (via a temp-file-plus-``os.replace`` atomic write,
    never a partially-written file a concurrent resume could read) -- a
    corner report is small, and a sweep is corner-count-bounded, so this
    stays cheap relative to the ``ngspice`` run it follows. Thread-safe: both
    ``local-parallel``'s bounded dispatch window and ``_run_sharded``'s
    per-shard threads call :meth:`record` concurrently.
    """

    @property
    def fingerprint(self) -> str:
        return self._fingerprint

    def __init__(self, path: str, fingerprint: str) -> None:
        self._path = path
        self._fingerprint = fingerprint
        self._lock = threading.Lock()
        self.completed: dict[str, dict[str, Any]] = {}
        self.engine_version: str | None = None

    def record(
        self, corner_id: str, report: dict[str, Any], engine_version: str | None
    ) -> None:
        with self._lock:
            self.completed[corner_id] = report
            if engine_version is not None:
                self.engine_version = engine_version
            self._flush_locked()

    def clear(self) -> None:
        """Remove the checkpoint file once a sweep finishes with nothing
        skipped -- a fully-completed sweep has nothing left to resume, and a
        stale file left behind is a pointless leftover (its fingerprint
        guard already makes it harmless to a future *different* request,
        but there is no reason to keep it around either)."""
        with self._lock:
            self.completed = {}
            try:
                os.remove(self._path)
            except OSError:
                pass

    def _flush_locked(self) -> None:
        os.makedirs(os.path.dirname(self._path), exist_ok=True)
        tmp_path = f"{self._path}.tmp"
        payload = {
            "fingerprint": self._fingerprint,
            "engine_version": self.engine_version,
            "completed": self.completed,
        }
        with open(tmp_path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle)
        os.replace(tmp_path, self._path)


# --------------------------------------------------------------------------- #
# Execution backends
# --------------------------------------------------------------------------- #


def _refuse_xyce_control_block_measurements(
    *,
    expr_declared: bool,
    crossings_declared: bool,
    cycle_mean_declared: bool,
    energy_declared: bool,
    transfer_declared: bool = False,
    snm_declared: bool = False,
    search_declared: bool = False,
    eye_declared: bool = False,
) -> None:
    """The ``engine: "xyce"`` refusals for measurement forms that exist only
    as commands in klt's generated ngspice ``.control`` block (``expr``,
    issue #2533; ``crossings``, issue #2868; ``energy``, issue #3033). Split out of
    :func:`_enforce_xyce_support_boundary` to keep that gate under the
    complexity limit."""
    if expr_declared:
        # Issue #2533: an `expr` measurement is emitted as ngspice
        # `let`/`print` commands inside the generated `.control` block, and
        # the Xyce deck has no control block to carry them (see
        # `_write_xyce_deck`'s docstring). Refused by name rather than
        # silently dropped: a corner that ran without its `expr` lines would
        # report every such measurement as "produced no value", which reads
        # as a circuit regression rather than an unsupported request field.
        raise SimError(
            "measurements[].expr is not supported for engine 'xyce': it is "
            "evaluated by ngspice `let`/`print` commands inside a `.control` "
            "block, and the Xyce deck has none (its analysis is a plain "
            "top-level dot card); use engine 'ngspice', or express the "
            "quantity as a `.meas` card in measurements[].spice"
        )
    if crossings_declared:
        # Issue #2868: the series is dumped by ngspice `let`/`write`
        # commands in the generated `.control` block -- same boundary as
        # `expr`, refused by name for the same reason.
        raise SimError(
            "measurements[].crossings is not supported for engine 'xyce': "
            "the signal is dumped by ngspice `let`/`write` commands inside a "
            "`.control` block, and the Xyce deck has none; use engine "
            "'ngspice'"
        )
    if cycle_mean_declared:
        # Issue #2947: same `.control` let/write boundary as `crossings`.
        raise SimError(
            "measurements[].cycle_mean is not supported for engine 'xyce': "
            "the signals are dumped by ngspice `let`/`write` commands inside "
            "a `.control` block, and the Xyce deck has none; use engine "
            "'ngspice'"
        )
    if energy_declared:
        # Issue #3033: the source current/voltage are dumped by ngspice
        # `let`/`write` commands in the generated `.control` block -- same
        # boundary as `crossings`.
        raise SimError(
            "measurements[].energy is not supported for engine 'xyce': the "
            "source current and voltage are dumped by ngspice `let`/`write` "
            "commands inside a `.control` block, and the Xyce deck has none; "
            "use engine 'ngspice'"
        )
    if transfer_declared:
        # Issue #2923: same `.control` let/write boundary as `cycle_mean`.
        raise SimError(
            "measurements[].transfer is not supported for engine 'xyce': the "
            "AC vectors are dumped by ngspice `let`/`write` commands inside a "
            "`.control` block, and the Xyce deck has none; use engine "
            "'ngspice'"
        )
    if snm_declared:
        # Issue #3895: same `.control` let/write boundary as `transfer`.
        raise SimError(
            "measurements[].snm is not supported for engine 'xyce': the sweep "
            "vectors are dumped by ngspice `let`/`write` commands inside a "
            "`.control` block, and the Xyce deck has none; use engine "
            "'ngspice'"
        )
    if search_declared:
        # Issue #4121: the bisection is an ngspice `.control` `while` loop.
        raise SimError(
            "measurements[].search is not supported for engine 'xyce': the "
            "bisection runs as an ngspice `.control` `while` loop, and the "
            "Xyce deck has no control block; use engine 'ngspice'"
        )
    if eye_declared:
        # Issue #4292: same `.control` let/write boundary as `crossings`.
        raise SimError(
            "measurements[].eye is not supported for engine 'xyce': the "
            "signal is dumped by ngspice `let`/`write` commands inside a "
            "`.control` block, and the Xyce deck has none; use engine "
            "'ngspice'"
        )


def _refuse_xyce_params_axis(corners_spec: dict[str, Any]) -> None:
    """The ``engine: "xyce"`` refusal for ``corners.params``. Split out of
    :func:`_enforce_xyce_support_boundary` to keep that gate under the
    complexity limit."""
    if corners_spec.get("params") or corners_spec.get("temperature_param"):
        raise SimError(
            "engine 'xyce' does not support corners.params or "
            "corners.temperature_param: the parameter "
            "axis is applied with ngspice's `alterparam` + `reset` control "
            "commands, which Xyce has no netlist equivalent of -- use engine "
            "'ngspice'"
        )


def _refuse_xyce_alter_corner_axes(corners_spec: dict[str, Any]) -> None:
    """The ``engine: "xyce"`` refusals for corner axes that are applied with
    ngspice's ``alter`` control command (``supply_v``; ``variants``, issue
    #3252). Split out of :func:`_enforce_xyce_support_boundary` to keep that
    gate under the complexity limit."""
    if corners_spec.get("supply_v") or any(
        isinstance(unit, dict) and unit.get("supply_v")
        for unit in corners_spec.get("units") or []
    ):
        raise SimError(
            "engine 'xyce' does not support corners.supply_v: Xyce has "
            "no netlist equivalent of ngspice's `alter` control command, "
            "so per-corner supply rails cannot be patched into the deck "
            "-- leave corners.supply_v unset (fixed supply, hardcoded in "
            "the netlist body) or use engine 'ngspice'"
        )
    if corners_spec.get("variants"):
        raise SimError(
            "engine 'xyce' does not support corners.variants: variant "
            "sources are applied with ngspice's `alter` control command, "
            "which Xyce has no netlist equivalent of -- leave "
            "corners.variants unset or use engine 'ngspice'"
        )


def _refuse_xyce_nodeset(declared: bool) -> None:
    """Issue #3036: the hints are rendered as ngspice `.nodeset v(node)=`
    cards; the Xyce deck is not given an equivalent."""
    if declared:
        raise SimError(
            "options.nodeset is not supported for engine 'xyce': initial "
            "guesses are applied as ngspice `.nodeset` cards; use engine "
            "'ngspice' or omit options.nodeset"
        )


def _refuse_xyce_linearize(declared: bool) -> None:
    """Issue #4014: `linearize` is an ngspice control command; the Xyce deck
    has no `.control` block to carry it."""
    if declared:
        raise SimError(
            "analysis.linearize is not supported for engine 'xyce': it is an "
            "ngspice `.control` command, which the Xyce deck does not have; "
            "use engine 'ngspice' or drop the field"
        )


def _refuse_xyce_options_axis(corners_spec: dict[str, Any]) -> None:
    """The ``engine: "xyce"`` refusal for ``corners.options`` (issue #3125):
    the axis is a generated ngspice ``.options`` card."""
    if corners_spec.get("options"):
        raise SimError(
            "engine 'xyce' does not support corners.options: the axis is "
            "emitted as ngspice solver `.options` cards, whose names Xyce "
            "does not share -- use engine 'ngspice'"
        )


def _enforce_xyce_support_boundary(
    *,
    engine: str,
    backend: str,
    corners_spec: dict[str, Any],
    monte_carlo_declared: bool,
    fail_fast_probe: bool,
    osdi_preload_declared: bool = False,
    expr_measurements_declared: bool = False,
    save_mode_declared: bool = False,
    save_vectors_declared: bool = False,
    ngspice_options_declared: bool = False,
    nodeset_declared: bool = False,
    initial_conditions_declared: bool = False,
    analysis_steps_declared: bool = False,
    linearize_declared: bool = False,
    crossings_declared: bool = False,
    cycle_mean_declared: bool = False,
    energy_declared: bool = False,
    transfer_declared: bool = False,
    snm_declared: bool = False,
    search_declared: bool = False,
    eye_declared: bool = False,
) -> None:
    """Refuse the ``engine: "xyce"`` combinations the v1 path does not
    implement (issue #2016) -- up front, with the same clean
    :class:`SimError` the engine/backend gates use, rather than as
    per-corner errors after dispatch. Each refusal names what *is*
    supported so a caller can re-shape the request instead of guessing;
    the syntax divergences behind each refusal are recorded in
    ``_write_xyce_deck``'s docstring and docs/design/xyce-oracle.md. A
    no-op for every other engine.
    """
    if engine != "xyce":
        return
    _refuse_xyce_linearize(linearize_declared)
    if analysis_steps_declared:
        # Issue #2482: an ordered step sequence is `alter`/analysis/`let`
        # commands inside one ngspice `.control` block. The Xyce deck has no
        # control block and no `alter`, and no equivalent ordered-analysis
        # contract is implemented for it -- refused rather than run with the
        # sequence silently reduced to something else. Checked first: the
        # flattened step measurements are `expr`s too, and this is the more
        # specific refusal.
        raise SimError(
            "analysis_steps is not supported for engine 'xyce': the ordered "
            "step sequence is generated as `alter`/analysis/`let` commands in "
            "an ngspice `.control` block, which the Xyce deck does not have; "
            "use engine 'ngspice'"
        )
    if backend not in ("local", "local-parallel"):
        raise SimError(
            f"engine 'xyce' supports only the local backends (local, "
            f"local-parallel), not '{backend}' -- the remote/batch "
            "provisioning path pins the ngspice toolchain"
        )
    _refuse_xyce_alter_corner_axes(corners_spec)
    _refuse_xyce_params_axis(corners_spec)
    _refuse_xyce_options_axis(corners_spec)
    if monte_carlo_declared:
        raise SimError(
            "engine 'xyce' does not implement monte_carlo sampling -- "
            "ngspice's `.options seed=`/rndseed mechanism has no Xyce "
            "equivalent wired up yet; use engine 'ngspice'"
        )
    if fail_fast_probe:
        raise SimError(
            "options.fail_fast_probe is not implemented for engine "
            "'xyce': the calibration probe reads ngspice's own rawfile "
            "stream; use engine 'ngspice' or drop the option"
        )
    if osdi_preload_declared:
        # Issue #2513: `pre_osdi` is an ngspice control command and Xyce has
        # no `.control` block to carry it; Xyce compiles Verilog-A into a
        # plugin through its own `buildxyceplugin` flow and loads it with
        # `-plugin`, which a `.osdi` file is not. Refused by name rather
        # than silently dropped -- a deck that ran without its preload would
        # report every OSDI device as an undefined model.
        raise SimError(
            "options.osdi_preload is not supported for engine 'xyce': "
            "`pre_osdi` is an ngspice control command, and Xyce loads "
            "Verilog-A models as its own compiled plugins (`-plugin`), not "
            "OSDI shared libraries; use engine 'ngspice' or drop the option"
        )
    _refuse_xyce_control_block_measurements(
        expr_declared=expr_measurements_declared,
        crossings_declared=crossings_declared,
        cycle_mean_declared=cycle_mean_declared,
        energy_declared=energy_declared,
        transfer_declared=transfer_declared,
        snm_declared=snm_declared,
        search_declared=search_declared,
        eye_declared=eye_declared,
    )
    for declared, message in (
        # Issue #2732: `save_mode` selects whether klt's generated ngspice
        # `.control` block carries `save all`; the Xyce deck has no control
        # block and no generated save card to suppress. Refused by name,
        # even for "all", rather than silently accepted with no effect.
        (
            save_mode_declared,
            "options.save_mode is not supported for engine 'xyce': it "
            "controls the `save all` card in klt's generated ngspice "
            "`.control` block, which the Xyce deck does not have; use "
            "engine 'ngspice' or omit options.save_mode",
        ),
        # Issue #3211: `save_vectors` rewrites the ngspice `save` card.
        (
            save_vectors_declared,
            "options.save_vectors is not supported for engine 'xyce': it "
            "replaces the `save all` card in klt's generated ngspice "
            "`.control` block, which the Xyce deck does not have; use "
            "engine 'ngspice' or omit options.save_vectors",
        ),
        # Issue #3111: the names are ngspice `.options` solver controls; the
        # Xyce deck has its own `.options` namespaces, so they are refused
        # by name rather than silently mapped or dropped.
        (
            ngspice_options_declared,
            "options.ngspice_options is not supported for engine 'xyce': "
            "its names are ngspice solver `.options`; use engine 'ngspice' "
            "or omit options.ngspice_options",
        ),
    ):
        if declared:
            raise SimError(message)
    _refuse_xyce_nodeset(nodeset_declared)
    if initial_conditions_declared:
        raise SimError(
            "options.initial_conditions is not supported for engine 'xyce': "
            "start states are applied as ngspice `.ic` cards with `uic`; use "
            "engine 'ngspice' or omit options.initial_conditions"
        )


def _run_local(
    *,
    corner_points: list[CornerPoint],
    netlist_path: str,
    models_lib: str | None,
    analysis: dict[str, Any],
    measurements_spec: list[dict[str, Any]],
    timeout_s: float,
    keep_artifacts: bool,
    want_waveforms: bool,
    artifacts_dir: str,
    max_workers: int | None,
    request: dict[str, Any],
    deadline: float | None = None,
    initial_ppid: int | None = None,
    checkpoint: _Checkpoint | None = None,
    probe_abort: dict[str, Any] | None = None,
    engine: str = "ngspice",
    ngspice_binary: str | None = None,
    fail_on_diagnostic: tuple[str, ...] = (),
    ngspice_init: tuple[str, ...] = (),
    osdi_preload: tuple[str, ...] = (),
    save_mode: str = DEFAULT_SAVE_MODE,
    save_vectors: tuple[str, ...] = (),
    complex_vectors: tuple[str, ...] = (),
    waveform_max_bytes: int | None = DEFAULT_WAVEFORM_MAX_BYTES,
    ngspice_options: dict[str, Any] | None = None,
    convergence_retry: dict[str, Any] | None = None,
    portable_roots: sim_portable.Roots | None = None,
    stimulus_lines: tuple[str, ...] = (),
    threads_per_worker: int | None = None,
) -> tuple[list[dict[str, Any]], str | None, dict[str, Any] | None]:
    """The ``local`` backend: run each expanded corner sequentially in-process.

    This is the unit a backend implements -- given the expanded corner list
    plus the already-resolved netlist/model paths and run options, return
    ``(per_corner_reports, engine_version, remote_environment)``. For the
    default case (no ``deadline``/``initial_ppid``/``checkpoint``/
    ``probe_abort``) this intentionally reproduces the original in-line loop
    byte-for-byte (same ordering, same ``engine_version`` last-writer-wins
    tracking) so the report JSON and on-disk artifacts are unchanged from
    before the backend seam existed. ``max_workers``/``request`` are
    accepted for signature parity with the other backends (every entry in
    :data:`_BACKENDS` is called with the same keyword set) but are
    meaningless for a sequential local runner, so both are ignored here; the
    third return value is always ``None`` (only ``remote`` populates it).

    ``deadline``/``initial_ppid`` (issue #473) are checked before *starting*
    each corner: once either fires, every remaining corner is reported via
    :func:`_unrun_corner_report` (``budget_exceeded``/``orphaned``) instead
    of being run -- a corner already in flight is never interrupted (there
    is only ever one, for this sequential backend). ``checkpoint``, when
    given, records each corner as it completes so a later ``--resume`` run
    does not recompute it.

    ``probe_abort`` (issue #1694, the two-pass probe model's fail-fast half
    -- see ``_run_fail_fast_probe`` and docs/design/sim-corner-reached-s.md)
    is the ``{"code", "message", "reached_s", "fraction"}`` decision ``run_sim``
    already made *before* calling this backend at all: when given, **every**
    corner in ``corner_points`` is reported unrun via
    :func:`_unrun_corner_report` with that code/message/estimate, and no
    ``ngspice`` process is ever spawned for this sweep -- the whole point of
    running the calibration probe first is to make this decision before any
    real corner (not just the ones after some in-flight budget/orphan check)
    burns its own ``options.timeout_s``.
    """
    del max_workers, request  # unused: sequential local run, see docstring
    engine_version: str | None = None
    corners: list[dict[str, Any]] = []
    stop_reason: str | None = None
    abort_message: str | None = None
    abort_reached_s: float | None = None
    abort_fraction: float | None = None
    if probe_abort is not None:
        stop_reason = probe_abort["code"]
        abort_message = probe_abort["message"]
        abort_reached_s = probe_abort.get("reached_s")
        abort_fraction = probe_abort.get("fraction")
    for point in corner_points:
        if stop_reason is None:
            if deadline is not None and time.monotonic() >= deadline:
                stop_reason = "budget_exceeded"
            elif initial_ppid is not None and os.getppid() != initial_ppid:
                stop_reason = "orphaned"
        if stop_reason is not None:
            corners.append(
                _unrun_corner_report(
                    point,
                    measurements_spec,
                    stop_reason,
                    message=abort_message,
                    reached_s=abort_reached_s,
                    fraction=abort_fraction,
                )
            )
            continue
        result, version = _run_corner_with_retry(
            point=point,
            netlist_path=netlist_path,
            models_lib=models_lib,
            analysis=analysis,
            measurements_spec=measurements_spec,
            timeout_s=timeout_s,
            keep_artifacts=keep_artifacts,
            want_waveforms=want_waveforms,
            artifacts_dir=artifacts_dir,
            engine=engine,
            ngspice_binary=ngspice_binary,
            fail_on_diagnostic=fail_on_diagnostic,
            ngspice_init=ngspice_init,
            osdi_preload=osdi_preload,
            save_mode=save_mode,
            save_vectors=save_vectors,
            complex_vectors=complex_vectors,
            waveform_max_bytes=waveform_max_bytes,
            ngspice_options=ngspice_options,
            convergence_retry=convergence_retry,
            portable_roots=portable_roots,
            stimulus_lines=stimulus_lines,
            threads_per_worker=threads_per_worker,
        )
        corners.append(result)
        if version is not None:
            engine_version = version
        if checkpoint is not None:
            checkpoint.record(point.corner_id, result, version)
    return corners, engine_version, None


def _host_max_workers_cap() -> int | None:
    """The host-level ``local-parallel`` worker cap, or ``None`` when unset.

    Read from ``$KLT_SIM_MAX_WORKERS`` (:data:`MAX_WORKERS_ENV`) on every
    call -- never cached at import time -- so a library caller that sets the
    variable between two :func:`run_sim` invocations gets what it asked for,
    and so a test can set/clear it (``monkeypatch.setenv``/``delenv``)
    without any reset hook. Unset (or set to the empty/whitespace-only
    string, which is what ``KLT_SIM_MAX_WORKERS=`` in a shell env file
    produces) means "no cap": behaviour is then byte-identical to before
    issue #2286.

    Applies to whichever box is actually running the worker pool: the
    ``remote``/``batch`` backends re-invoke ``klt sim --backend
    local-parallel`` on the provisioned instance, where *that* host's own
    environment decides the cap -- the caller's cap describes the caller's
    box and deliberately does not travel with the job.

    A value that is not a positive integer is an **application error**
    (:class:`SimError`, exit 1), deliberately matching how ``run_sim``
    validates an explicit ``options.max_workers``/``--max-workers`` -- a
    typo'd cap that silently degraded to "uncapped" would defeat the entire
    point of setting it on a shared box.
    """
    raw = os.environ.get(MAX_WORKERS_ENV)
    if raw is None or not raw.strip():
        return None
    try:
        cap = int(raw.strip(), 10)
    except ValueError:
        cap = 0
    if cap < 1:
        raise SimError(f"{MAX_WORKERS_ENV} must be a positive integer (got {raw!r})")
    return cap


def _worker_cap_notice(message: str) -> None:
    """Print a clamp notice to stderr at most once per sweep.

    Deduplicated on the message itself (reset per :func:`run_sim` call by
    :func:`_reset_worker_cap_notices`) because ``hosts > 1`` re-enters
    :func:`_run_local_parallel` once per shard, from several threads at
    once -- the caller asked for one oversized pool, so they get one notice,
    not one per shard. stderr rather than the report body so a caller piping
    ``--format json`` to a file still sees it on the terminal, the same
    convention ``_provenance._warn_klayout_version_mismatch`` follows.
    """
    with _WORKER_CAP_NOTICE_LOCK:
        if message in _WORKER_CAP_NOTICES_EMITTED:
            return
        _WORKER_CAP_NOTICES_EMITTED.add(message)
    print(message, file=sys.stderr)


def _reset_worker_cap_notices() -> None:
    """Clear the per-sweep clamp-notice dedupe set (see
    :func:`_worker_cap_notice`). Called once at the top of :func:`run_sim`, so
    a long-lived process running several sweeps is told about the clamp once
    per sweep rather than once per process."""
    with _WORKER_CAP_NOTICE_LOCK:
        _WORKER_CAP_NOTICES_EMITTED.clear()


def _default_max_workers() -> int:
    """Conservative default worker count for the ``local-parallel`` backend.

    Each ``ngspice -b`` process is itself internally multi-threaded (matrix
    solve/BLAS), so naive one-worker-per-corner on a small box oversubscribes
    immediately (see #253/#168's design note). The default divides the local
    CPU count by :data:`_ASSUMED_THREADS_PER_NGSPICE`, floored to at least 1
    worker -- useful headroom on a workstation, but callers running on a
    shared/CI box should still set ``options.max_workers``/``--max-workers``
    explicitly (see docs/cli/sim.md's shared-worker warning); ``local``
    remains the default backend everywhere for exactly that reason.

    A host-level ``$KLT_SIM_MAX_WORKERS`` cap (issue #2286), when set, bounds
    this derived default too -- silently, since nobody asked for a specific
    number here. The clamp notice is reserved for the case where a caller
    *did* ask (:func:`_resolve_max_workers`).
    """
    cpu_count = os.cpu_count() or 1
    default = max(1, cpu_count // _ASSUMED_THREADS_PER_NGSPICE)
    cap = _host_max_workers_cap()
    return default if cap is None else min(default, cap)


def _resolve_max_workers(max_workers: int | None) -> int:
    """Final ``local-parallel`` pool size: ``min(requested_or_default, cap)``.

    The single place both worker-count paths converge (issue #2286): the
    "nothing given" default from :func:`_default_max_workers` (already capped
    there) and an explicit ``options.max_workers``/``--max-workers``, which
    is clamped **here**. An over-cap request is clamped rather than refused --
    the caller's number is a preference about this sweep, the cap is a fact
    about the box, and failing a sweep outright over a preference would be a
    worse outcome than running it slightly slower -- but the clamp is never
    silent: see :func:`_worker_cap_notice`.
    """
    if max_workers is None:
        return _default_max_workers()
    cap = _host_max_workers_cap()
    if cap is not None and max_workers > cap:
        _worker_cap_notice(
            f"klt: notice: max_workers={max_workers} exceeds this host's "
            f"{MAX_WORKERS_ENV}={cap} cap; clamping the local-parallel worker "
            f"pool to {cap}."
        )
        return cap
    return max_workers


def _run_local_parallel(
    *,
    corner_points: list[CornerPoint],
    netlist_path: str,
    models_lib: str | None,
    analysis: dict[str, Any],
    measurements_spec: list[dict[str, Any]],
    timeout_s: float,
    keep_artifacts: bool,
    want_waveforms: bool,
    artifacts_dir: str,
    max_workers: int | None,
    request: dict[str, Any],
    deadline: float | None = None,
    initial_ppid: int | None = None,
    checkpoint: _Checkpoint | None = None,
    probe_abort: dict[str, Any] | None = None,
    engine: str = "ngspice",
    ngspice_binary: str | None = None,
    fail_on_diagnostic: tuple[str, ...] = (),
    ngspice_init: tuple[str, ...] = (),
    osdi_preload: tuple[str, ...] = (),
    save_mode: str = DEFAULT_SAVE_MODE,
    save_vectors: tuple[str, ...] = (),
    complex_vectors: tuple[str, ...] = (),
    waveform_max_bytes: int | None = DEFAULT_WAVEFORM_MAX_BYTES,
    ngspice_options: dict[str, Any] | None = None,
    convergence_retry: dict[str, Any] | None = None,
    portable_roots: sim_portable.Roots | None = None,
    stimulus_lines: tuple[str, ...] = (),
    threads_per_worker: int | None = None,
) -> tuple[list[dict[str, Any]], str | None, dict[str, Any] | None]:
    """The ``local-parallel`` backend: fan the expanded corner list across a
    bounded local worker pool.

    Corners share nothing -- each is a pure function of netlist + models +
    corner (see this module's docstring and #253/#168) -- so this is a
    :class:`~concurrent.futures.ThreadPoolExecutor` pool over :func:`_run_corner`
    (a thread, not a process, pool: the actual work is the ``ngspice``
    subprocess call, which releases the GIL for the duration of the wait, so
    threads give the same concurrency as processes here without the
    pickling/import overhead of ``ProcessPoolExecutor``). Results are indexed
    by each corner's position in ``corner_points`` and reassembled into that
    same order after every future completes, regardless of completion order
    -- the acceptance criterion that this backend's report is order-identical
    to ``local`` for the same request. ``engine_version`` is derived the same
    "last non-``None`` wins, in corner-list order" way ``local`` computes it,
    for the same reason: deterministic given a deterministic corner list,
    independent of which corner's process happened to finish last.

    A failing/erroring corner is reported exactly as :func:`_run_corner`
    reports it (that function never raises) and does not abort any sibling
    corner -- each future is independent.

    ``request`` is accepted for signature parity with the other backends
    (only ``remote`` reads it) and ignored; the third return value is always
    ``None`` here.

    **Pool size and the host cap.** ``max_workers`` (the request's
    ``options.max_workers``, or ``--max-workers``) sizes the pool; when it is
    ``None`` the conservative CPU-derived default from
    :func:`_default_max_workers` is used instead. Either way the host-level
    ``$KLT_SIM_MAX_WORKERS`` cap, when set, is an upper bound on the result
    (issue #2286) -- a request for more workers than the cap is **clamped,
    not refused**, with a one-line stderr notice per sweep. See
    :func:`_resolve_max_workers` and docs/cli/sim.md's "Useful on a
    workstation, harmful on a shared worker" section.

    ``deadline``/``initial_ppid`` (issue #473): unlike the naive "submit
    everything up front" the pre-#473 implementation used (which lets the
    pool's own internal queue keep draining corners regardless of how long
    the sweep has been running), corners are dispatched through a bounded
    window -- never more than ``max_workers`` in flight at once -- refilled
    only when neither condition has fired. A worker that is already running
    when the deadline/orphan check fires is left to finish (see this
    module's docstring/``run_sim`` for why an already-launched ``ngspice``
    is not preemptively killed here); every corner that never got a slot is
    reported via :func:`_unrun_corner_report` instead. ``checkpoint``, when
    given, records each corner the instant its future resolves -- not
    batched at the end -- so a crash mid-sweep loses at most the corners
    still in flight at that moment.

    ``probe_abort`` (issue #1694): identical contract to
    :func:`_run_local`'s own ``probe_abort`` -- when given, ``stop_reason``
    starts pre-set to its code, so ``_fill`` never submits a single corner
    to the pool and every corner in ``corner_points`` comes back through
    :func:`_unrun_corner_report` with that code/message/estimate.
    """
    del request  # unused: local-parallel needs no request-level context
    resolved_workers = _resolve_max_workers(max_workers)

    total = len(corner_points)
    results: list[tuple[dict[str, Any], str | None] | None] = [None] * total
    stop_reason: str | None = None
    abort_message: str | None = None
    abort_reached_s: float | None = None
    abort_fraction: float | None = None
    if probe_abort is not None:
        stop_reason = probe_abort["code"]
        abort_message = probe_abort["message"]
        abort_reached_s = probe_abort.get("reached_s")
        abort_fraction = probe_abort.get("fraction")
    next_index = 0

    def _stopped() -> str | None:
        nonlocal stop_reason
        if stop_reason is None:
            if deadline is not None and time.monotonic() >= deadline:
                stop_reason = "budget_exceeded"
            elif initial_ppid is not None and os.getppid() != initial_ppid:
                stop_reason = "orphaned"
        return stop_reason

    with ThreadPoolExecutor(max_workers=resolved_workers) as pool:
        in_flight: dict[Any, int] = {}

        def _fill() -> None:
            nonlocal next_index
            while (
                next_index < total
                and len(in_flight) < resolved_workers
                and _stopped() is None
            ):
                point = corner_points[next_index]
                future = pool.submit(
                    _run_corner_with_retry,
                    point=point,
                    netlist_path=netlist_path,
                    models_lib=models_lib,
                    analysis=analysis,
                    measurements_spec=measurements_spec,
                    timeout_s=timeout_s,
                    keep_artifacts=keep_artifacts,
                    want_waveforms=want_waveforms,
                    artifacts_dir=artifacts_dir,
                    engine=engine,
                    ngspice_binary=ngspice_binary,
                    fail_on_diagnostic=fail_on_diagnostic,
                    ngspice_init=ngspice_init,
                    osdi_preload=osdi_preload,
                    save_mode=save_mode,
                    save_vectors=save_vectors,
                    complex_vectors=complex_vectors,
                    waveform_max_bytes=waveform_max_bytes,
                    ngspice_options=ngspice_options,
                    convergence_retry=convergence_retry,
                    portable_roots=portable_roots,
                    stimulus_lines=stimulus_lines,
                    threads_per_worker=threads_per_worker,
                )
                in_flight[future] = next_index
                next_index += 1

        _fill()
        while in_flight:
            done, _pending = wait(list(in_flight.keys()), return_when=FIRST_COMPLETED)
            for future in done:
                index = in_flight.pop(future)
                result = future.result()
                results[index] = result
                if checkpoint is not None:
                    corner, version = result
                    checkpoint.record(corner_points[index].corner_id, corner, version)
            _fill()

    engine_version: str | None = None
    corners: list[dict[str, Any]] = []
    for index, result in enumerate(results):
        if result is None:
            # Never got a worker slot -- `stop_reason` is always set by the
            # time any index is left `None` (that is the only way `_fill`
            # stops short of `total`).
            assert stop_reason is not None
            corners.append(
                _unrun_corner_report(
                    corner_points[index],
                    measurements_spec,
                    stop_reason,
                    message=abort_message,
                    reached_s=abort_reached_s,
                    fraction=abort_fraction,
                )
            )
            continue
        corner, version = result
        corners.append(corner)
        if version is not None:
            engine_version = version
    return corners, engine_version, None


#: Backend registry: name -> implementation. Membership is validated against
#: :data:`SUPPORTED_BACKENDS` in :func:`run_sim` before dispatch, so this only
#: ever holds implemented backends.
_BACKENDS = {
    "local": _run_local,
    "local-parallel": _run_local_parallel,
    "remote": _run_remote,
    "batch": _run_batch,
}


# --------------------------------------------------------------------------- #
# Per-corner ngspice invocation
# --------------------------------------------------------------------------- #

_ENGINE_VERSION_RE = re.compile(r"ngspice-([\w.]+)")


_WAVEFORM_KEYWORDS = frozenset(
    {"pwl", "pulse", "sin", "exp", "sffm", "am", "trnoise", "trrandom", "external"}
)
_WAVEFORM_RE = re.compile(r"^([a-z]+)\s*(?:\(|$)")


def _logical_netlist_lines(netlist_text: str) -> list[str]:
    """Netlist lines with ``+`` continuations joined and comments removed.

    Full-line ``*`` comments and inline ``;``/``$`` comments are dropped.
    Deliberately lexical, not a complete SPICE parser: ``.include``/``.lib``
    are not followed."""
    logical: list[str] = []
    for raw in netlist_text.splitlines():
        line = raw.strip()
        if not line or line.startswith("*"):
            continue
        line = re.split(r";|\s\$", line, maxsplit=1)[0].strip()
        if not line:
            continue
        if line.startswith("+"):
            if logical:
                logical[-1] += " " + line[1:].strip()
            continue
        logical.append(line)
    return logical


_NOISE_SOURCE_RE = re.compile(r"\b(trnoise|trrandom)\s*\(", re.IGNORECASE)


def _noise_sources_in_text(netlist_text: str) -> list[tuple[str, str]]:
    """``(lower-cased name, keyword)`` of every independent ``V*``/``I*``
    source declaring a ``trnoise``/``trrandom`` waveform (issue #2963),
    including inside ``.subckt`` bodies. Lexical, via
    :func:`_logical_netlist_lines`."""
    found: list[tuple[str, str]] = []
    for line in _logical_netlist_lines(netlist_text):
        if line[0].lower() not in ("v", "i"):
            continue
        match = _NOISE_SOURCE_RE.search(line)
        if match:
            found.append((line.split(None, 1)[0].lower(), match.group(1).lower()))
    return found


def _scan_noise_sources(
    netlist_path: str, *, request: dict[str, Any], repo_root: str | None
) -> tuple[set[tuple[str, str]], list[dict[str, str]]]:
    """``(sources, not_scanned)``: every ``trnoise``/``trrandom`` source
    declared in the netlist's scanned ``.include`` closure (issues #2963,
    #2985), and what part of the closure could not be scanned."""
    files, not_scanned = _mismatch_scan_closure(request, netlist_path, repo_root)
    sources: set[tuple[str, str]] = set()
    for _label, path in files:
        try:
            with open(path, encoding="utf-8") as handle:
                sources.update(_noise_sources_in_text(handle.read()))
        except (OSError, UnicodeDecodeError):
            not_scanned.append({"target": _label, "reason": "unreadable"})
    return sources, not_scanned


def _unseeded_noise_report(
    netlist_path: str, *, request: dict[str, Any], repo_root: str | None
) -> dict[str, Any] | None:
    """Additive ``environment.monte_carlo`` fields for transient-noise
    sources the ``monte_carlo.seed`` contract does not govern (issue #2963),
    or ``None`` when no scanned file declares one (shape unchanged)."""
    sources, not_scanned = _scan_noise_sources(
        netlist_path, request=request, repo_root=repo_root
    )
    if not sources:
        return None
    report: dict[str, Any] = {
        "unseeded_sources": [
            {"name": name, "keyword": keyword} for name, keyword in sorted(sources)
        ],
        "unseeded_sources_note": _UNSEEDED_NOISE_NOTE,
    }
    if not_scanned:
        report["unseeded_sources_not_scanned"] = not_scanned
    return report


def _analysis_runs_tran(analysis: dict[str, Any]) -> bool:
    """Does the resolved analysis run a transient (scalar ``tran`` or an
    ``analysis_steps[]`` tran step)?"""
    if sim_steps.is_steps_analysis(analysis):
        return any(
            str(step.get("kind", "")).lower() == "tran"
            for step in analysis.get("steps", [])
        )
    return str(analysis.get("kind", "")).lower() == "tran"


def _transient_noise_environment(
    netlist_path: str,
    *,
    request: dict[str, Any],
    analysis: dict[str, Any],
    engine: str,
    repo_root: str | None,
) -> dict[str, Any]:
    """Additive ``environment.transient_noise`` (issue #2985): what noise an
    ngspice transient contains -- only the declared ``trnoise``/``trrandom``
    sources, no intrinsic device or resistor noise. ``{}`` (field absent)
    unless the engine is ngspice, a transient runs and a scanned file
    declares such a source."""
    if engine != "ngspice" or not _analysis_runs_tran(analysis):
        return {}
    sources, not_scanned = _scan_noise_sources(
        netlist_path, request=request, repo_root=repo_root
    )
    if not sources:
        return {}
    report: dict[str, Any] = {
        "engine": engine,
        "sources": [
            {"name": name, "kind": keyword} for name, keyword in sorted(sources)
        ],
        "intrinsic_device_noise": False,
        "intrinsic_resistor_noise": False,
    }
    if not_scanned:
        report["sources_not_scanned"] = not_scanned
    return {"transient_noise": report}


def _with_unseeded_noise(
    monte_carlo_info: dict[str, Any],
    netlist_path: str,
    *,
    request: dict[str, Any],
    repo_root: str | None,
) -> dict[str, Any]:
    """``monte_carlo_info`` plus the issue #2963 unseeded-noise disclosure
    (and its stderr warning) when a scanned file declares such a source."""
    noise_report = _unseeded_noise_report(
        netlist_path, request=request, repo_root=repo_root
    )
    if noise_report is None:
        return monte_carlo_info
    print(f"warning: {_UNSEEDED_NOISE_NOTE}", file=sys.stderr)
    return {**monte_carlo_info, **noise_report}


_UNSEEDED_NOISE_NOTE = (
    "trnoise()/trrandom() sources are not governed by monte_carlo.seed "
    "(ngspice seeds them from the process id): re-running this request "
    "redraws their noise, and samples run in separate batch jobs or hosts "
    "may share a noise stream."
)


def _top_level_waveform_sources(netlist_text: str) -> dict[str, str]:
    """Map lower-cased top-level independent voltage-source names to the
    explicit waveform keyword (``pwl``/``pulse``/``sin``/...) they declare.
    Sources declared inside ``.subckt`` bodies are ignored; sources with no
    waveform (bare value or ``dc``) are absent from the result."""
    found: dict[str, str] = {}
    depth = 0
    for line in _logical_netlist_lines(netlist_text):
        lowered = line.lower()
        if lowered.startswith("."):
            word = lowered.split(None, 1)[0]
            if word == ".subckt":
                depth += 1
            elif word == ".ends":
                depth = max(0, depth - 1)
            continue
        if depth or lowered[0] != "v":
            continue
        # Name, two nodes, then value/DC/waveform specification. Spaces
        # between a keyword and its "(" are tolerated.
        tokens = lowered.replace("(", " ( ").split()
        if len(tokens) < 4:
            continue
        spec = " ".join(tokens[3:]).replace(" ( ", "(")
        for token in spec.split():
            if token in _WAVEFORM_KEYWORDS or any(
                token.startswith(k + "(") for k in _WAVEFORM_KEYWORDS
            ):
                found[tokens[0]] = re.match(r"[a-z]+", token).group(0)  # type: ignore[union-attr]
                break
    return found


#: ``.param`` assignment names: ``name=`` (not ``==``), once ``{...}`` and
#: quoted expressions have been blanked out of the line.
_PARAM_DECL_NAME_RE = re.compile(r"(?:^|[\s,])([a-z_][a-z0-9_]*)\s*=(?!=)")
_PARAM_DECL_EXPR_RE = re.compile(r"\{[^{}]*\}|'[^']*'|\"[^\"]*\"")

#: Bound on the ``.include``/``.lib`` files followed while looking for an
#: element a ``.param``-only target might really name. Past it the
#: declaration counts as unresolved (never rejected).
_MAX_DECLARATION_FILES = 256


class _NetlistDeclarations(NamedTuple):
    """Lexical top-level declarations of one netlist file (#2867)."""

    #: Lower-cased names assigned by a top-level ``.param`` card.
    params: set[str]
    #: Lower-cased names of top-level elements (first token of a card).
    elements: set[str]
    #: Top-level ``V`` source name -> its value/waveform specification.
    voltage_specs: dict[str, str]
    #: Raw file references of ``.include``/``.inc``/``.lib <file> <sect>``.
    includes: list[str]


def _netlist_declarations(netlist_text: str) -> _NetlistDeclarations:
    """Scan a netlist's top level for ``.param`` names, element names and
    include references. ``.subckt`` bodies and ``.control`` blocks are
    skipped; ``.lib`` *section* bodies (``.lib tt`` ... ``.endl``) count as
    top level, since a loaded section is."""
    decl = _NetlistDeclarations(set(), set(), {}, [])
    depth = 0
    for line in _top_level_cards(_logical_netlist_lines(netlist_text)):
        word = line.split(None, 1)[0].lower()
        if word == ".subckt":
            depth += 1
        elif word == ".ends":
            depth = max(0, depth - 1)
        elif not depth:
            _record_declaration(decl, line)
    return decl


def _top_level_cards(lines: list[str]) -> list[str]:
    """``lines`` without ``.control`` ... ``.endc`` blocks."""
    kept: list[str] = []
    in_control = False
    for line in lines:
        word = line.split(None, 1)[0].lower()
        if word == ".control":
            in_control = True
        elif word == ".endc":
            in_control = False
        elif not in_control:
            kept.append(line)
    return kept


def _record_declaration(decl: _NetlistDeclarations, line: str) -> None:
    """Record one top-level card (outside any ``.subckt``) into ``decl``."""
    lowered = line.lower()
    parts = line.split()
    word = parts[0].lower()
    if word in (".include", ".inc") and len(parts) >= 2:
        decl.includes.append(line.split(None, 1)[1].strip())
    elif word == ".lib" and len(parts) >= 3:
        decl.includes.append(" ".join(parts[1:-1]))
    elif word == ".param" and len(parts) > 1:
        body = _PARAM_DECL_EXPR_RE.sub(" 0 ", lowered.split(None, 1)[1])
        decl.params.update(_PARAM_DECL_NAME_RE.findall(body))
    elif not word.startswith("."):
        decl.elements.add(word)
        if word.startswith("v") and len(parts) >= 4:
            decl.voltage_specs[word] = " ".join(lowered.split()[3:])


def _resolve_netlist_include(reference: str, base_dir: str) -> str | None:
    path = os.path.expanduser(reference.strip().strip("\"'"))
    if not os.path.isabs(path):
        path = os.path.join(base_dir, path)
    return path if os.path.isfile(path) else None


def _included_elements(netlist_path: str, includes: list[str]) -> set[str] | None:
    """Element names declared at the top level of every file the netlist
    pulls in through ``.include``/``.lib`` (recursively), or ``None`` when
    any reference cannot be resolved and read -- an unresolved declaration
    is never assumed absent."""
    closure = _included_declarations(netlist_path, includes)
    return None if closure is None else closure[0]


def _included_declarations(
    netlist_path: str, includes: list[str]
) -> tuple[set[str], set[str]] | None:
    """``(elements, params)`` declared at the top level of every file in the
    ``.include``/``.lib`` closure (see :func:`_included_elements`; the same
    walk also collects the ``.param`` names, issue #3035)."""
    elements: set[str] = set()
    params: set[str] = set()
    seen: set[str] = set()
    pending = [
        (ref, os.path.dirname(os.path.abspath(netlist_path))) for ref in includes
    ]
    while pending:
        reference, base_dir = pending.pop()
        path = _resolve_netlist_include(reference, base_dir)
        if path is None:
            return None
        real = os.path.realpath(path)
        if real in seen:
            continue
        seen.add(real)
        if len(seen) > _MAX_DECLARATION_FILES:
            return None
        try:
            with open(path, encoding="utf-8", errors="replace") as handle:
                decl = _netlist_declarations(handle.read())
        except OSError:
            return None
        elements |= decl.elements
        params |= decl.params
        pending.extend((ref, os.path.dirname(path)) for ref in decl.includes)
    return elements, params


def _param_only_targets(netlist_path: str, targets: list[str]) -> dict[str, list[str]]:
    """The ``alter`` targets that name a top-level ``.param`` and no element
    (#2867), each mapped to the top-level voltage sources whose value
    references that parameter (a hint for the error message).

    ngspice evaluates ``.param`` expressions when it parses the deck, and
    ``alter <param>=<value>`` addresses no device: ngspice logs ``no such
    device or model name`` and simulates the nominal value, so a sweep over
    such a key reports N identical ``pass`` corners. A target is only
    reported when the lexical scan is conclusive: a name declared as an
    element anywhere the scan can see is not a ``.param``-only name, and when
    an ``.include``/``.lib`` reference cannot be resolved nothing is
    reported. Hierarchical names never match (a ``.param`` name has no dot).
    """
    try:
        with open(netlist_path, encoding="utf-8") as handle:
            decl = _netlist_declarations(handle.read())
    except (OSError, UnicodeDecodeError):
        return {}
    candidates = [
        t
        for t in targets
        if t.strip().lower() in decl.params and t.strip().lower() not in decl.elements
    ]
    if not candidates:
        return {}
    if decl.includes:
        included = _included_elements(netlist_path, decl.includes)
        if included is None:
            return {}
        candidates = [t for t in candidates if t.strip().lower() not in included]
    found: dict[str, list[str]] = {}
    for target in candidates:
        name = re.escape(target.strip().lower())
        pattern = re.compile(rf"(?<![a-z0-9_]){name}(?![a-z0-9_])")
        found[target] = sorted(
            source
            for source, spec in decl.voltage_specs.items()
            if pattern.search(spec)
        )
    return found


def _unresolved_supply_targets(netlist_path: str, targets: list[str]) -> list[str]:
    """The ``supply_v`` keys that name no element and no ``.param`` anywhere
    in a fully resolved netlist closure (#2894), so ``alter <key>=<value>``
    would silently address nothing.

    Same conclusiveness rule as :func:`_param_only_targets`: the top-level
    file must be readable and every ``.include``/``.lib`` reference must
    resolve, otherwise nothing is reported (the name may live in the part
    the scan cannot see). Matching is case-insensitive. Hierarchical keys
    (``x1.vdd``) address elements inside subcircuit instances, which the
    top-level scan does not model, so they are never reported. ``.param``
    names are left to :func:`_param_only_targets`."""
    try:
        with open(netlist_path, encoding="utf-8") as handle:
            decl = _netlist_declarations(handle.read())
    except (OSError, UnicodeDecodeError):
        return []
    included: set[str] = set()
    if decl.includes:
        resolved = _included_elements(netlist_path, decl.includes)
        if resolved is None:
            return []
        included = resolved
    return [
        t
        for t in targets
        if "." not in t
        and t.strip().lower() not in decl.elements
        and t.strip().lower() not in decl.params
        and t.strip().lower() not in included
    ]


def _param_target_hint(param: str, sources: list[str]) -> str:
    if sources:
        names = ", ".join(repr(s) for s in sources)
        return (
            f"To sweep the supply, key corners.supply_v by the source itself "
            f"({names} reads {param!r}); to sweep the parameter, use a "
            "parameter axis (corners.params, issue #3035) instead"
        )
    return (
        "Key corners.supply_v by the name of a DC voltage source, or sweep the "
        "parameter with a parameter axis (corners.params, issue #3035) instead"
    )


def _resolve_stimulus(
    request: dict[str, Any],
    analysis: dict[str, Any],
    engine: str,
    netlist_path: str,
    corner_points: list[CornerPoint],
) -> tuple[list[dict[str, Any]], dict[str, list[dict[str, Any]]] | None]:
    """The request's resolved ``stimulus`` signals (issue #2977), every
    problem raised as a coded :class:`SimError` before any corner runs.

    The second item is ``{corner_id: signals}`` when a level is written
    supply-relative (issue #3918, resolved against each corner's
    ``supply_v``), else ``None`` -- the first item is then the one schedule
    every corner shares."""
    by_corner: dict[str, list[dict[str, Any]]] | None = None
    try:
        if sim_stimulus.has_supply_relative(request):
            by_corner = {
                point.corner_id: sim_stimulus.resolve_stimulus(
                    request, analysis, point.supply_v
                )
                for point in corner_points
            }
            signals = next(iter(by_corner.values()), [])
        else:
            signals = sim_stimulus.resolve_stimulus(request, analysis)
        if not signals:
            return [], None
        if engine != "ngspice":
            raise sim_stimulus.StimulusError(
                f"request.stimulus is not supported for engine {engine!r}; "
                "it is expanded into the ngspice deck only",
                sim_stimulus.CODE_UNSUPPORTED,
            )
        existing = _stimulus_existing_elements(netlist_path)
        sim_stimulus.check_collisions(signals, existing)
        sim_stimulus.check_supply_conflict(
            signals,
            {key.strip().lower() for point in corner_points for key in point.supply_v},
        )
    except sim_stimulus.StimulusError as exc:
        raise SimError(str(exc), code=exc.code) from exc
    return signals, by_corner


def _stimulus_existing_elements(netlist_path: str) -> set[str]:
    """Lower-cased top-level element names of the netlist and every
    ``.include``/``.lib`` file the lexical scan can resolve."""
    try:
        with open(netlist_path, encoding="utf-8", errors="replace") as handle:
            decl = _netlist_declarations(handle.read())
    except OSError:
        return set()
    elements = set(decl.elements)
    if decl.includes:
        elements |= _included_elements(netlist_path, decl.includes) or set()
    return elements


def _validate_supply_override_targets(
    netlist_path: str, corner_points: list[CornerPoint]
) -> None:
    """Reject a ``supply_v`` override that ngspice's ``alter`` cannot apply.

    Two silent no-ops fail the request as a whole rather than simulating
    the wrong voltage: a source with an explicit PWL/PULSE/SIN/... waveform
    (#2706), and a name that is only a top-level ``.param`` with no element
    of that name (#2867). Validates the actual expanded points (including
    worker ``_explicit_points``). Inspection is lexical: the waveform rule
    reads the top-level netlist file only, the ``.param`` rule additionally
    follows ``.include``/``.lib`` references to look for an element of that
    name, and an unresolved declaration is never assumed DC or absent."""
    targets = sorted({key for point in corner_points for key in point.supply_v})
    if not targets:
        return
    try:
        with open(netlist_path, encoding="utf-8") as handle:
            text = handle.read()
    except (OSError, UnicodeDecodeError):
        return
    waveforms = _top_level_waveform_sources(text)
    scaled = {name for point in corner_points for name in point.supply_waveforms}
    for name in sorted(scaled):
        # Fail closed on anything the rewrite cannot reproduce exactly.
        sim_supply_waveform.find_source(
            text, name, option="corners.supply_waveforms", error_cls=SimError
        )
    for target in targets:
        waveform = waveforms.get(target.strip().lower())
        if waveform is not None and target not in scaled:
            hint = (
                "; to scale its levels per corner instead, list it in "
                "corners.supply_waveforms (supported for "
                + "/".join(k.upper() for k in sim_supply_waveform.SCALABLE_KINDS)
                + " sources)"
            )
            raise SimError(
                f"corners.supply_v override of source {target!r} is not "
                f"supported: the netlist declares it with an explicit "
                f"{waveform.upper()} waveform, and ngspice's `alter "
                f"{target}=<value>` has no effect on it (the run would "
                "silently simulate the netlist's own waveform). Declare the "
                "source as a plain DC value, or remove it from "
                "corners.supply_v" + hint
            )
    for target, sources in _param_only_targets(netlist_path, targets).items():
        raise SimError(
            f"corners.supply_v key {target!r} names a .param, not a source: "
            f"the netlist declares `.param {target}=...` and no element "
            f"{target!r}, and ngspice's `alter {target}=<value>` cannot change "
            "a .param (expressions that read it -- a source value, a SIN() "
            "amplitude -- were evaluated when the deck was parsed, so every "
            "corner would silently simulate the nominal value). "
            + _param_target_hint(target, sources)
        )
    for target in _unresolved_supply_targets(netlist_path, targets):
        raise SimError(
            f"corners.supply_v key {target!r} names no element and no .param "
            "in the netlist (including every .include/.lib it pulls in): "
            f"ngspice's `alter {target}=<value>` would address nothing, so "
            "every corner would silently simulate the nominal value. Check "
            "the spelling against the voltage-source name declared in the "
            "netlist"
        )


def _validate_param_targets(
    netlist_path: str, corner_points: list[CornerPoint]
) -> None:
    """Reject a ``corners.params`` name that no top-level ``.param`` declares
    (issue #3035), before any dispatch.

    ngspice's ``alterparam`` of an unknown name only logs ``parameter ... not
    found`` and carries on, which would let every corner simulate the
    nominal value and pass. Lexical, with the same conclusiveness rule as
    :func:`_unresolved_supply_targets`: the scan covers the top-level file
    and its resolved ``.include``/``.lib`` closure, and when any reference
    cannot be read nothing is reported here -- :func:`_run_corner` still
    fails the corner if the engine itself rejects the target. A name only
    declared inside a ``.subckt`` is not a top-level ``.param`` and is
    reported (subcircuit-local targeting is out of scope)."""
    targets = sorted({name for point in corner_points for name in point.params})
    if not targets:
        return
    try:
        with open(netlist_path, encoding="utf-8") as handle:
            decl = _netlist_declarations(handle.read())
    except (OSError, UnicodeDecodeError):
        return
    declared = set(decl.params)
    if decl.includes:
        closure = _included_declarations(netlist_path, decl.includes)
        if closure is None:
            return
        declared |= closure[1]
    for target in targets:
        if target not in declared:
            raise SimError(
                f"corners.params name {target!r} is not declared by any "
                "top-level `.param` in the netlist (including every "
                ".include/.lib it pulls in): ngspice's `alterparam` of an "
                "unknown name changes nothing, so every corner would "
                "silently simulate the nominal value. Declare "
                f"`.param {target}=<default>` at the top level (subcircuit-"
                "local parameters cannot be targeted)"
            )


_NETLIST_INCLUDE_RE = re.compile(
    r"^(?P<lead>\s*\.inc(?:lude)?\s+)(?P<target>\"[^\"]*\"|'[^']*'|\S+)(?P<tail>.*)$",
    re.IGNORECASE,
)


def _split_include_target(match: re.Match[str]) -> tuple[str, str]:
    """Return ``(quote, target)`` for an ``_NETLIST_INCLUDE_RE`` match."""
    raw = match.group("target")
    quote = raw[0] if raw[0] in "\"'" else ""
    return quote, (raw[1:-1] if quote else raw)


def _unset_include_diagnostic(
    netlist_path: str, number: int, target: str, expanded: str
) -> dict[str, str]:
    unset = sorted(set(re.findall(r"\$\{?(\w+)\}?", expanded)))
    return {
        "severity": "error",
        "code": "missing_include",
        "message": (
            f"{netlist_path} line {number}: .include target "
            f"{target!r} names environment variable(s) "
            f"{', '.join(unset) or '?'} that are not set on this "
            "host, so ngspice cannot load it"
        ),
    }


def _expand_include_line(
    line: str, number: int, netlist_path: str
) -> tuple[str | None, dict[str, str] | None]:
    """Expand ``$VAR``/``~`` in one line's ``.include`` target.

    Returns ``(rewritten_line, diagnostic)``: ``rewritten_line`` is ``None``
    when the line is left as written (not an include, nothing to expand, or
    an unset variable -- the latter also yields a diagnostic)."""
    match = _NETLIST_INCLUDE_RE.match(line)
    if match is None:
        return None, None
    quote, target = _split_include_target(match)
    if "$" not in target and not target.startswith("~"):
        return None, None
    expanded = os.path.expandvars(target)
    if "$" in expanded:
        return None, _unset_include_diagnostic(netlist_path, number, target, expanded)
    expanded = os.path.expanduser(expanded)
    if not quote and any(ch.isspace() for ch in expanded):
        quote = '"'
    return (
        f"{match.group('lead')}{quote}{expanded}{quote}{match.group('tail')}",
        None,
    )


def _absolutize_relative_include(line: str, base: str) -> str:
    """Rewrite a relative ``.include`` target that resolves beside the
    original netlist (``base``) to its absolute path."""
    match = _NETLIST_INCLUDE_RE.match(line)
    if match is None:
        return line
    quote, target = _split_include_target(match)
    if not target or os.path.isabs(target):
        return line
    candidate = os.path.join(base, target)
    if not os.path.isfile(candidate):
        return line
    q = quote or ('"' if any(c.isspace() for c in candidate) else "")
    return f"{match.group('lead')}{q}{candidate}{q}{match.group('tail')}"


def _expand_netlist_includes(
    netlist_path: str, corner_dir: str
) -> tuple[str, list[dict[str, str]]]:
    """Expand ``$VAR``/``~`` in the netlist body's own ``.include``/``.inc``
    targets on the *executing* host (issue #2926).

    ngspice does not expand environment variables in include paths, so a
    ``$PDK_ROOT/...`` include (which off-host backends deliberately leave
    verbatim for the executing host) would otherwise fail to load. Uses the
    same ``os.path.expandvars``/``expanduser`` semantics as
    ``sim_staging`` (an unset variable stays as ``$``). Returns
    ``(netlist_path_to_include, diagnostics)``: the original path unchanged
    when no include needs expanding; otherwise a rewritten copy under
    ``corner_dir`` (relative targets that resolve beside the original
    netlist are made absolute so the copy keeps their meaning). A target
    whose variable is unset is left as written and yields a
    ``missing_include`` diagnostic naming the file, line and variable.
    """
    try:
        with open(netlist_path, encoding="utf-8", errors="surrogateescape") as fh:
            text = fh.read()
    except OSError:
        return netlist_path, []
    if "$" not in text and "~" not in text:
        return netlist_path, []
    out: list[str] = []
    diagnostics: list[dict[str, str]] = []
    changed = False
    for number, line in enumerate(text.split("\n"), start=1):
        rewritten_line, diagnostic = _expand_include_line(line, number, netlist_path)
        if diagnostic is not None:
            diagnostics.append(diagnostic)
        if rewritten_line is None:
            out.append(line)
            continue
        out.append(rewritten_line)
        changed = True
    if not changed:
        return netlist_path, diagnostics
    # Keep other relative includes meaning what they meant beside the original.
    base = os.path.dirname(os.path.abspath(netlist_path))
    final = [_absolutize_relative_include(line, base) for line in out]
    rewritten = os.path.join(corner_dir, "netlist.expanded.cir")
    with open(rewritten, "w", encoding="utf-8", errors="surrogateescape") as fh:
        fh.write("\n".join(final))
    return rewritten, diagnostics


def _scale_supply_waveforms(
    run_netlist_path: str, original_path: str, corner_dir: str, point: CornerPoint
) -> tuple[str, dict[str, Any]]:
    """Write the corner's netlist copy with each ``point.supply_waveforms``
    source rescaled to its ``supply_v`` value (issue #2902); returns
    ``(copy_path, disclosure)``. Relative ``.include`` targets that resolve
    beside the original netlist are made absolute, as in
    :func:`_expand_netlist_includes`. A corner with no waveform sources
    passes the netlist through untouched with an empty disclosure."""
    if not point.supply_waveforms:
        return run_netlist_path, {}
    with open(run_netlist_path, encoding="utf-8", errors="surrogateescape") as fh:
        text = fh.read()
    scaled, disclosure = sim_supply_waveform.scale_netlist(
        text, point.supply_v, point.supply_waveforms, error_cls=SimError
    )
    base = os.path.dirname(os.path.abspath(original_path))
    final = [_absolutize_relative_include(line, base) for line in scaled.split("\n")]
    out_path = os.path.join(corner_dir, "netlist.supply_waveforms.cir")
    with open(out_path, "w", encoding="utf-8", errors="surrogateescape") as fh:
        fh.write("\n".join(final))
    return out_path, disclosure


def _prepare_corner_run(
    *,
    corner_dir: str,
    netlist_path: str,
    models_lib: str | None,
    point: CornerPoint,
    analysis: dict[str, Any],
    measurements_spec: list[dict[str, Any]],
    want_waveforms: bool,
    engine: str,
    ngspice_binary: str | None = None,
    ngspice_init: tuple[str, ...] = (),
    osdi_preload: tuple[str, ...] = (),
    save_mode: str = DEFAULT_SAVE_MODE,
    save_vectors: tuple[str, ...] = (),
    complex_vectors: tuple[str, ...] = (),
    ngspice_options: dict[str, Any] | None = None,
    stimulus_lines: tuple[str, ...] = (),
    extra_options_card: str | None = None,
) -> tuple[list[str], str, str | None, str]:
    """Write the engine's per-corner deck and build its command line.

    Returns ``(command, log_path, raw_path, deck_path)``. The engine's own
    deck writer (``_write_corner_deck`` for ngspice, ``_write_xyce_deck``
    for Xyce) owns every syntax divergence; this is only the dispatch seam.

    ``ngspice_binary`` (issue #2423) is the resolved absolute path
    ``_resolve_ngspice_binary`` settled on for this sweep -- every
    ``engine="ngspice"`` caller passes it explicitly; the ``None`` default
    exists only so a direct unit test of this function (or the ``xyce``
    branch, which never reads it) need not also stub binary resolution. A
    ``None`` reaching the ``ngspice`` branch falls back to the bare
    ``"ngspice"`` name, matching this function's pre-#2423 behaviour.

    ``ngspice_init`` (issue #2520, the already-validated
    ``options.ngspice_init`` lines -- see :func:`_validate_ngspice_init`)
    is materialized as ``corner_dir/.spiceinit`` for the ``ngspice`` branch
    only (:func:`_write_spiceinit`): Xyce has no equivalent init-file
    lookup, so the ``xyce`` branch ignores it. The caller is responsible for
    also running ``ngspice``/``Xyce`` with ``cwd=corner_dir`` -- writing the
    file here without that ``cwd=`` would leave it unread.
    """
    is_xyce = engine == "xyce"
    deck_path = os.path.join(corner_dir, "corner.cir")
    log_path = os.path.join(corner_dir, "xyce.log" if is_xyce else "ngspice.log")
    raw_path = os.path.join(corner_dir, "waveform.raw") if want_waveforms else None
    if is_xyce:
        _write_xyce_deck(
            deck_path=deck_path,
            netlist_path=netlist_path,
            models_lib=models_lib,
            point=point,
            analysis=analysis,
            measurements_spec=measurements_spec,
        )
        command = [XYCE_BINARY, "-b", deck_path, "-l", log_path]
        if raw_path is not None:
            # `-a` flips the rawfile from Xyce's default binary format to
            # the ASCII layout `parse_ascii_rawfile` reads (verified against
            # XyceNF 7.10.0; the layout is ngspice-like but not identical).
            command += ["-r", raw_path, "-a"]
    else:
        _write_corner_deck(
            deck_path=deck_path,
            netlist_path=netlist_path,
            models_lib=models_lib,
            point=point,
            analysis=analysis,
            measurements_spec=measurements_spec,
            raw_path=raw_path,
            osdi_preload=osdi_preload,
            save_mode=save_mode,
            save_vectors=save_vectors,
            complex_vectors=complex_vectors,
            ngspice_options=ngspice_options,
            stimulus_lines=stimulus_lines,
            extra_options_card=extra_options_card,
        )
        _write_spiceinit(corner_dir, ngspice_init)
        command = [ngspice_binary or "ngspice", "-b", deck_path, "-o", log_path]
    return command, log_path, raw_path, deck_path


def _parse_engine_measurements(log_text: str, engine: str) -> dict[str, float]:
    """Parse ``.measure``/``.meas`` scalar results from an engine log --
    ``_parse_xyce_measurements`` for Xyce (section-scoped, names
    lower-cased), ``_parse_measurements`` for ngspice."""
    if engine == "xyce":
        return _parse_xyce_measurements(log_text)
    return _parse_measurements(log_text)


def _classify_engine_diagnostics(
    log_text: str, netlist_path: str, engine: str
) -> list[dict[str, Any]]:
    """Engine-dispatched log diagnostics -- ``_XYCE_DIAGNOSTIC_PATTERNS``
    for Xyce, ``_DIAGNOSTIC_PATTERNS`` (plus its netlist-aware refinement)
    for ngspice."""
    if engine == "xyce":
        return _classify_xyce_diagnostics(log_text)
    return _classify_diagnostics(log_text, netlist_path)


PARAMS_UNSUPPORTED_WORKER_CODE = "params_unsupported_worker"


def _require_params_echo(
    points: list[CornerPoint],
    corners: list[dict[str, Any]],
    *,
    backend: str = "remote",
) -> None:
    """Fail the request when an off-host worker did not honor a parameter
    axis (issue #3035).

    A worker that predates ``corners.params`` ignores the field -- it would
    run every row at the netlist's default and report ``pass`` under the
    legacy corner ID. A worker that supports it echoes each point's
    normalized ``params`` and its parameter-suffixed ``corner_id``, so the
    caller compares both against what it dispatched. This is independent of
    any version check (which compares only major.minor), so it also catches
    a same-release runner image built before the feature landed.
    """
    if backend not in _OFFHOST_BACKENDS or not any(p.params for p in points):
        return
    problem: str | None = None
    if len(corners) != len(points):
        problem = (
            f"it returned {len(corners)} corner report(s) for "
            f"{len(points)} dispatched point(s)"
        )
    else:
        for point, corner in zip(points, corners, strict=True):
            if corner.get("corner_id") != point.corner_id or (
                corner.get("params") or {}
            ) != (point.params or {}):
                problem = (
                    f"corner {point.corner_id!r} came back as "
                    f"{corner.get('corner_id')!r} with params "
                    f"{corner.get('params')!r}"
                )
                break
    if problem is not None:
        raise SimError(
            "the remote/batch worker did not honor corners.params -- "
            f"{problem}. A worker built before the parameter axis (issue "
            "#3035) ignores it and would simulate the netlist default for "
            "every row; update the runner image to this klt release, or "
            "use a local backend. No result was reported",
            code=PARAMS_UNSUPPORTED_WORKER_CODE,
        )


OPTIONS_UNSUPPORTED_WORKER_CODE = "options_unsupported_worker"


def _require_options_echo(
    points: list[CornerPoint],
    corners: list[dict[str, Any]],
    *,
    backend: str = "remote",
) -> None:
    """Fail the request when an off-host worker did not honor a
    ``corners.options`` axis (issue #3125) -- the counterpart of
    :func:`_require_params_echo`. A worker that predates the axis ignores
    it and would run every rung at the default tolerance under the legacy
    corner ID; a supporting worker echoes each point's ``options`` and its
    option-suffixed ``corner_id``."""
    if backend not in _OFFHOST_BACKENDS or not any(p.options for p in points):
        return
    problem: str | None = None
    if len(corners) != len(points):
        problem = (
            f"it returned {len(corners)} corner report(s) for "
            f"{len(points)} dispatched point(s)"
        )
    else:
        for point, corner in zip(points, corners, strict=True):
            if corner.get("corner_id") != point.corner_id or (
                corner.get("options") or {}
            ) != (point.options or {}):
                problem = (
                    f"corner {point.corner_id!r} came back as "
                    f"{corner.get('corner_id')!r} with options "
                    f"{corner.get('options')!r}"
                )
                break
    if problem is not None:
        raise SimError(
            "the remote/batch worker did not honor corners.options -- "
            f"{problem}. A worker built before the options axis (issue "
            "#3125) ignores it and would simulate the default solver "
            "tolerances for every rung; update the runner image to this klt "
            "release, or use a local backend. No result was reported",
            code=OPTIONS_UNSUPPORTED_WORKER_CODE,
        )


#: ngspice's reply to an ``alterparam`` whose target is not a ``.param`` it
#: knows: ``Error: parameter 'bogus' not found,`` / ``command 'alterparam'
#: skipped`` -- printed, but the run continues and exits 0 (issue #3035).
_ALTERPARAM_FAILURE_RE = re.compile(
    r"parameter '(?P<name>[^']+)' not found|command 'alterparam' skipped",
    re.IGNORECASE,
)


def _alterparam_failures(point: CornerPoint, log_text: str) -> list[dict[str, Any]]:
    """An error diagnostic when the engine refused an ``alterparam`` target,
    so a corner that simulated the nominal value can never grade ``pass``.
    A no-op for a corner with no ``params`` axis."""
    if not point.params:
        return []
    match = _ALTERPARAM_FAILURE_RE.search(log_text)
    if match is None:
        return []
    named = match.group("name")
    return [
        {
            "severity": "error",
            "code": "netlist",
            "message": (
                "corners.params could not be applied: the engine rejected "
                "`alterparam`"
                + (f" for {named!r}" if named else "")
                + " (no such top-level .param), so this corner would have "
                "simulated the netlist's own default"
            ),
        }
    ]


def _grade_corner(
    diagnostics: list[dict[str, str]],
    measurement_results: list[dict[str, Any]],
    disqualifying: list[str],
) -> str:
    """One corner's aggregate ``status``: ``error > inconclusive > fail > pass``.

    Mirrors the response's own aggregate precedence. Any engine-level
    diagnostic still at severity ``"error"`` (timeout, an *unrecovered*
    singular matrix/nonconvergence, netlist, unresolvable measurement,
    unknown) means no trustworthy result exists for this corner, which
    always outranks a clean limit violation.

    ``disqualifying`` (issue #2492) is the sorted set of this corner's
    diagnostic codes the caller listed in ``options.fail_on_diagnostic``,
    sampled by :func:`_run_corner` **before** the recovered-stepping
    severity downgrade. When nonempty the corner grades ``inconclusive``
    and gains an :data:`INCONCLUSIVE_DIAGNOSTIC_CODE` marker diagnostic
    naming those codes -- appended here (mutating ``diagnostics`` in place)
    so the report always says *why* a corner whose numbers look fine is not
    a pass.

    The *other* route to ``inconclusive`` (issue #2493) is a measurement
    whose value fell outside its declared plausibility bound: that is graded
    onto the measurement itself by :func:`_grade_measurement_value`, which
    has already run by the time this is called, so it arrives here as a
    ``measurement_results`` entry already carrying
    :data:`_INCONCLUSIVE_STATUS` and needs no separate branch. Both reasons
    land on the same status by design -- see :data:`_INCONCLUSIVE_STATUS`.

    ``inconclusive`` is ranked directly under ``error`` and *above* ``fail``
    for the same reason ``error`` outranks ``fail``: this corner's numbers
    are not trusted, and a limit miss computed from untrusted numbers is not
    a defensible claim about the design -- reporting "the circuit missed its
    spec" would assert more than the run can support. It stays *below*
    ``error`` because an errored corner produced no number at all, which is
    strictly less information than a number the run declines to trust.
    """
    if any(d["severity"] == "error" for d in diagnostics) or any(
        m["status"] == "error" for m in measurement_results
    ):
        return "error"
    if disqualifying:
        diagnostics.append(
            {
                "severity": "warning",
                "code": INCONCLUSIVE_DIAGNOSTIC_CODE,
                "message": (
                    "graded inconclusive: options.fail_on_diagnostic lists "
                    + ", ".join(disqualifying)
                ),
            }
        )
        return _INCONCLUSIVE_STATUS
    if any(m["status"] == _INCONCLUSIVE_STATUS for m in measurement_results):
        return _INCONCLUSIVE_STATUS
    if any(m["status"] == "fail" for m in measurement_results):
        return "fail"
    return "pass"


def _run_corner_with_retry(
    *,
    convergence_retry: dict[str, Any] | None = None,
    **kwargs: Any,
) -> tuple[dict[str, Any], str | None]:
    """Run one corner point (see :func:`_run_corner`), optionally
    retrying a ``nonconvergence`` failure (issue #2973).

    With ``convergence_retry`` (the validated
    ``options.retry_on_convergence_error`` policy) a unit that ends
    ``status: "error"`` purely from a ``nonconvergence`` failure is re-run
    on the *same* :class:`CornerPoint` -- so ``sample_index`` and the Monte
    Carlo seeds are unchanged by construction -- with the policy's tighter
    solver options on an extra ``.options`` card, up to ``max_attempts``
    times. The outcome is recorded as a ``convergence_retry`` diagnostic on
    the unit. Any other failure class is never retried. ``None`` (the
    default) is exactly the single-attempt behaviour.
    """
    corner, version = _run_corner(**kwargs)
    if convergence_retry is None or not _is_retryable_convergence_failure(corner):
        return corner, version
    card = _convergence_retry_deck_options(convergence_retry)
    max_attempts = convergence_retry["max_attempts"]
    total_runtime = corner["runtime_s"]
    for attempt in range(1, max_attempts + 1):
        retried, retried_version = _run_corner(**kwargs, extra_options_card=card)
        total_runtime += retried["runtime_s"]
        recovered = retried["status"] != "error"
        retried["diagnostics"].append(
            {
                "severity": "warning",
                "code": "convergence_retry",
                "message": (
                    f"nonconvergence on the first attempt; retry {attempt}/"
                    f"{max_attempts} with `{card}` "
                    + ("recovered" if recovered else "still failed")
                ),
            }
        )
        corner, version = retried, retried_version or version
        if recovered or not _is_retryable_convergence_failure(retried):
            break
    corner["runtime_s"] = round(total_runtime, 3)
    return corner, version


def _persist_waveform_artifact(
    raw_path: str,
    raw_size: int,
    waveform_max_bytes: int | None,
    analysis: dict[str, Any],
    diagnostics: list[dict[str, Any]],
    noise_fields: dict[str, str],
) -> str | None:
    """Convert ``raw_path`` to its ``waveform.raw.json`` artifact and return
    that path, or ``None`` when ``raw_size`` is over ``waveform_max_bytes``
    (issue #3211): converting parses the whole ASCII rawfile into memory, so
    past the cap a ``waveform_too_large`` warning is appended instead. For a
    ``noise`` analysis the converted waveform also sets
    ``noise_fields["noise_representation"]`` (issue #3006).
    """
    if waveform_max_bytes is not None and raw_size > waveform_max_bytes:
        # Keep the raw file and every measurement; only the JSON conversion
        # is skipped.
        diagnostics.append(
            {
                "severity": "warning",
                "code": WAVEFORM_TOO_LARGE_CODE,
                "message": (
                    f"waveform.raw is {raw_size} bytes, over "
                    f"options.waveform_max_bytes ({waveform_max_bytes}); "
                    "skipped the waveform.raw.json conversion (the raw "
                    "file is kept at artifacts.raw, measurements are "
                    "unaffected). To shrink it, set options.save_vectors "
                    "to the signals you need, widen the tran step "
                    "(tstep/tmax in analysis.args), shorten tstop, or "
                    "raise/disable options.waveform_max_bytes"
                ),
            }
        )
        return None
    waveform_path = _parse_and_persist_waveform(raw_path)
    if analysis.get("kind") == "noise":
        with open(waveform_path, encoding="utf-8") as fh:
            noise_fields["noise_representation"] = noise_representation(json.load(fh))
    return waveform_path


def _worker_env_kwargs(threads_per_worker: int | None) -> dict[str, Any]:
    """``subprocess.run`` kwargs for one ngspice worker (issue #2970): an
    ``env`` with ``OMP_NUM_THREADS`` pinned when a thread count was resolved,
    nothing (inherit unchanged) otherwise, so a single-worker run is untouched."""
    if threads_per_worker is None:
        return {}
    return {"env": {**os.environ, "OMP_NUM_THREADS": str(threads_per_worker)}}


def _threads_environment(effective: int | None) -> dict[str, int]:
    """Additive/optional ``environment.threads_per_worker`` (issue #2970): the
    OpenMP thread count each ngspice worker actually ran with. Absent when the
    ambient environment decided (a single worker, nothing requested)."""
    return {} if effective is None else {"threads_per_worker": effective}


def _validate_threads_per_worker(value: Any) -> int | None:
    """Validate ``options.threads_per_worker`` (issue #2970)."""
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise SimError("options.threads_per_worker must be a positive integer")
    return value


def _effective_threads_per_worker(
    explicit: int | None, backend: str, max_workers: int | None, units: int
) -> int | None:
    """Resolve the per-worker OpenMP thread count (issue #2970); ``None`` for
    the off-host backends, whose worker re-resolves it from the forwarded
    request.

    An explicit ``options.threads_per_worker`` always wins. Otherwise
    workers sharing a host run single-threaded: N workers x ngspice's default
    OpenMP team oversubscribes the cores (measured 270x per-unit slowdown on
    the batch fleet). A lone worker keeps the ambient behaviour. A netlist
    that itself ``set num_threads=N`` still overrides this (it runs later).
    """
    if backend in _OFFHOST_BACKENDS:
        return None
    if explicit is not None:
        return explicit
    if (
        backend == "local-parallel"
        and min(_resolve_max_workers(max_workers), units) > 1
    ):
        return 1
    return None


def _write_corner_manifest(corner_dir: str, point: CornerPoint) -> None:
    """Record the full ``corner_id`` in ``corner.json`` when the directory
    slug is not the readable form (bounded or parameter-axis slugs)."""
    if point.slug == point.readable_slug:
        return
    with open(os.path.join(corner_dir, "corner.json"), "w", encoding="utf-8") as handle:
        json.dump(
            {"corner_id": point.corner_id, "readable_slug": point.readable_slug},
            handle,
            indent=2,
        )


def _mc_seed_optional_echo(mc_seed: dict[str, Any]) -> dict[str, Any]:
    """The per-sample ``monte_carlo`` fields that are echoed only when the
    request declared them: ``seed_scope`` (issue #2899), ``seed_mode``
    (issue #4173) and ``mismatch_scale`` (issue #3942)."""
    return {
        key: mc_seed[key]
        for key in ("seed_scope", "seed_mode", "mismatch_scale")
        if key in mc_seed
    }


def _run_corner(
    *,
    extra_options_card: str | None = None,
    point: CornerPoint,
    netlist_path: str,
    models_lib: str | None,
    analysis: dict[str, Any],
    measurements_spec: list[dict[str, Any]],
    timeout_s: float,
    keep_artifacts: bool,
    want_waveforms: bool,
    artifacts_dir: str,
    engine: str = "ngspice",
    ngspice_binary: str | None = None,
    fail_on_diagnostic: tuple[str, ...] = (),
    ngspice_init: tuple[str, ...] = (),
    osdi_preload: tuple[str, ...] = (),
    save_mode: str = DEFAULT_SAVE_MODE,
    save_vectors: tuple[str, ...] = (),
    complex_vectors: tuple[str, ...] = (),
    waveform_max_bytes: int | None = DEFAULT_WAVEFORM_MAX_BYTES,
    ngspice_options: dict[str, Any] | None = None,
    portable_roots: sim_portable.Roots | None = None,
    stimulus_lines: tuple[str, ...] = (),
    threads_per_worker: int | None = None,
) -> tuple[dict[str, Any], str | None]:
    """Run one corner point through the selected engine's batch binary and
    classify the result.

    Returns ``(corner_report, engine_version_or_none)``. Never raises: any
    failure to run (bad spawn, timeout, nonzero-but-uninformative exit) is
    folded into the corner's own ``status: "error"`` + ``diagnostics``,
    per the contract's "every corner is reported" guarantee.

    ``engine="ngspice"`` runs ``ngspice -b`` on a deck carrying a
    ``.control`` block (see ``_write_corner_deck``). ``engine="xyce"``
    (issue #2016) runs ``Xyce -b`` -- the ``-b`` is accepted-and-ignored
    SPICE-compatibility -- on a control-block-free deck (see
    ``_write_xyce_deck``), with ``-l`` for the log and ``-r ... -a`` for an
    ASCII rawfile when waveforms were requested. Both branches share the
    measurement/limits/status tail: the response contract is
    engine-shaped, not engine-specific, and the two engines are kept
    byte-comparable for the cross-validation oracle
    (tests/test_sim_xyce_oracle.py).

    ``fail_on_diagnostic`` (issue #2492, the already-validated
    ``options.fail_on_diagnostic`` codes -- see
    :func:`_validate_fail_on_diagnostic`) grades this corner
    ``status: "inconclusive"`` when it emitted any listed code. The match is
    taken on the diagnostic's ``code`` **before** the recovered-stepping
    severity downgrade below, so a ``singular_matrix`` that ngspice's own
    gmin/source stepping recovered from -- and which is therefore about to
    be demoted to ``severity: "warning"`` and would otherwise grade
    ``pass`` -- still counts. That is the whole point of the option: it is
    how a caller says "I do not trust a solve that needed stepping to
    converge". Empty (the default) leaves grading byte-identical to before.

    ``ngspice_init`` (issue #2520, the already-validated
    ``options.ngspice_init`` lines -- see :func:`_validate_ngspice_init`) is
    forwarded to :func:`_prepare_corner_run`, which materializes it as
    ``corner_dir/.spiceinit`` for the ``ngspice`` branch. This function's own
    ``subprocess.run`` call passes ``cwd=corner_dir`` (the other half of
    issue #2520) so that file -- and any relative ``.lib``/``.include`` path
    the netlist itself declares -- is read from the per-corner directory
    this run controls, never the caller's ambient working directory. That
    holds for both the ``keep_artifacts=True`` directory under
    ``artifacts_dir`` and the ``keep_artifacts=False`` :func:`_tmp_work_dir`
    scratch directory -- ``corner_dir`` names whichever one this call is
    using either way.
    """
    is_xyce = engine == "xyce"
    engine_name = XYCE_BINARY if is_xyce else "ngspice"
    if keep_artifacts:
        corner_dir = os.path.join(artifacts_dir, point.slug)
        os.makedirs(corner_dir, exist_ok=True)
        _write_corner_manifest(corner_dir, point)
    else:
        corner_dir = _tmp_work_dir()

    run_netlist_path, include_diagnostics = _expand_netlist_includes(
        netlist_path, corner_dir
    )
    run_netlist_path, supply_waveform_report = _scale_supply_waveforms(
        run_netlist_path, netlist_path, corner_dir, point
    )
    command, log_path, raw_path, deck_path = _prepare_corner_run(
        corner_dir=corner_dir,
        netlist_path=run_netlist_path,
        models_lib=models_lib,
        point=point,
        analysis=analysis,
        measurements_spec=measurements_spec,
        want_waveforms=want_waveforms,
        engine=engine,
        ngspice_binary=ngspice_binary,
        ngspice_init=ngspice_init,
        osdi_preload=osdi_preload,
        save_mode=save_mode,
        save_vectors=save_vectors,
        complex_vectors=complex_vectors,
        ngspice_options=ngspice_options,
        stimulus_lines=stimulus_lines,
        extra_options_card=extra_options_card,
    )

    diagnostics: list[dict[str, str]] = list(include_diagnostics)
    diagnostics.extend(_par_call_cap_preflight(deck_path, is_xyce))
    started = time.monotonic()
    timed_out = False
    engine_aborted = False
    analysis_never_started = False
    engine_version: str | None = None
    stdout_text = ""
    stderr_text = ""
    engine_exit_code: int | None = None
    engine_killed_by_signal: str | None = None
    launched = False
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            cwd=corner_dir,
            **_worker_env_kwargs(None if is_xyce else threads_per_worker),
        )
        stdout_text = completed.stdout or ""
        stderr_text = completed.stderr or ""
        launched = True
        engine_exit_code, engine_killed_by_signal = _split_returncode(
            completed.returncode
        )
    except subprocess.TimeoutExpired:
        timed_out = True
    except FileNotFoundError as exc:
        diagnostics.append(
            {
                "severity": "error",
                "code": "unknown",
                "message": f"could not launch {engine_name}: {exc}",
            }
        )
    runtime_s = round(time.monotonic() - started, 3)

    log_text = _read_log_file(log_path)

    if is_xyce:
        # Xyce stamps its version banner into the `-l` log (ngspice's lands
        # on stdout, read below); the log is read either way.
        engine_version = _extract_xyce_version(log_text)
    elif not timed_out:
        engine_version = _extract_engine_version(stdout_text)

    if timed_out:
        diagnostics.append(
            {
                "severity": "error",
                "code": "timeout",
                "message": (
                    f"{engine_name} did not complete within {timeout_s}s, killed"
                ),
            }
        )
    else:
        output_tail = _engine_abort_tail(stderr_text, stdout_text, log_text)
        output_tail = _prepend_engine_log_error(output_tail, log_text)
        tail_suffix = f"; last output:\n{output_tail}" if output_tail else ""
        if engine_killed_by_signal is not None:
            diagnostics.append(
                {
                    "severity": "error",
                    "code": "engine_killed",
                    "message": (
                        f"{engine_name} was killed by {engine_killed_by_signal} "
                        "(not by the harness timeout) -- e.g. the OOM killer, "
                        "a host reaper, or a reclaimed worker" + tail_suffix
                    ),
                }
            )
            engine_aborted = True
        classified = _classify_engine_diagnostics(log_text, netlist_path, engine)
        diagnostics.extend(classified)
        analysis_never_started = not is_xyce and _mark_analysis_not_started(
            classified, log_text
        )
        no_log_reported = False
        diagnostics.extend(_alterparam_failures(point, log_text + "\n" + stdout_text))
        diagnostics.extend(_nodeset_failures(point, log_text + "\n" + stdout_text))
        if engine_exit_code and not log_text and not os.path.exists(log_path):
            diagnostics.append(
                {
                    "severity": "error",
                    "code": "unknown",
                    "message": (
                        f"{engine_name} exited with code {engine_exit_code} "
                        "without writing its log (deck or log path not found?)"
                        f"{_engine_output_tail(stdout_text, stderr_text)}"
                    ),
                }
            )
            no_log_reported = True
        aborted = (
            None
            if engine_aborted or no_log_reported
            else _engine_aborted_diagnostic(
                engine_name, engine_exit_code, classified, tail_suffix
            )
        )
        if aborted is not None:
            diagnostics.append(aborted)
            engine_aborted = True

    # Both engines fold a `.meas`/`.measure` card's name to lower-case in
    # their own report output (Xyce actually upper-cases, but
    # `_parse_xyce_measurements` lower-cases its keys to normalize that);
    # `_parse_engine_measurements` -> `_parse_measurements` for ngspice does
    # the same, storing keys exactly as the (already lower-cased) log
    # printed them. `_harvest_measurement_value` folds the requested `name`
    # to lower-case on a miss for both engines and both measurement forms
    # (#2546) -- an upper/mixed-case `spice`-form name (e.g. `Vout`) still
    # harvests even though ngspice's own report prints `vout = ...`. The
    # *response* below still echoes the caller's original `name` spelling --
    # only the internal lookup is case-folded.
    measurement_values = _parse_engine_measurements(log_text, engine)
    # Issue #2868: crossings series are read from this corner's own working
    # directory now, before it is cleaned up below -- so the reduced array is
    # what leaves the corner, on every backend, whatever `keep_artifacts` is.
    crossings = sim_crossings.extract_corner_crossings(
        measurements_spec, corner_dir, point.supply_v
    )
    # Issue #3033: same for the energy reduction.
    energy = sim_energy.extract_corner_energy(measurements_spec, corner_dir)
    cycle_means = sim_cycle_mean.extract_corner_cycle_means(
        measurements_spec, corner_dir
    )
    transfers = sim_transfer_meas.extract_corner_transfers(
        measurements_spec, corner_dir
    )
    snms = sim_snm_meas.extract_corner_snms(measurements_spec, corner_dir)
    eyes = sim_eye.extract_corner_eyes(measurements_spec, corner_dir)
    searches = sim_search.extract_corner_searches(measurements_spec, log_text)
    # Issue #3160: each pass's rawfile is decoded once, shared by the tian
    # reduction below and the corner's `passes` field.
    pass_results = (
        sim_passes.extract_corner_passes(analysis, corner_dir)
        if sim_passes.is_passes_analysis(analysis)
        else []
    )
    tians = sim_tian.extract_corner_tians(measurements_spec, pass_results)
    measurement_results, measurement_diagnostics, step_fields = (
        _corner_measurement_results(
            measurement_values,
            measurements_spec,
            analysis=analysis,
            log_text=log_text,
            is_xyce=is_xyce,
            save_mode=_effective_save_mode(save_mode, diagnostics),
            crossings=crossings,
            energy=energy,
            cycle_means=cycle_means,
            transfers=transfers,
            snms=snms,
            eyes=eyes,
            searches=searches,
            tians=tians,
            engine_diagnostics=diagnostics,
        )
    )
    measurement_diagnostics = _demote_abort_cascade(
        measurement_diagnostics, engine_aborted or analysis_never_started
    )
    diagnostics.extend(measurement_diagnostics)
    pass_fields, pass_diagnostics = sim_passes.corner_fields(
        analysis, corner_dir, keep_artifacts=keep_artifacts, results=pass_results
    )
    diagnostics.extend(pass_diagnostics)
    # Issue #3337: decode the declared vectors into the sidecar before the
    # corner is graded, so a missing or non-finite one fails it.
    complex_outcome = sim_complex_vectors.extract_corner(complex_vectors, corner_dir)
    diagnostics.extend(complex_outcome.diagnostics)
    # Issue #3338: reduce a two-port corner's two excitations to S; a missing
    # or non-finite rawfile is an error diagnostic, so it cannot grade `pass`.
    twoport_outcome = sim_twoport.extract_corner(analysis, corner_dir)
    diagnostics.extend(twoport_outcome.diagnostics)

    # Issue #3016: the engine ran (any exit status, including 0) yet left no
    # log text and not a single requested measurement has a value -- the
    # bare N x "produced no value" says nothing about why, so attach the
    # exit status and the engine's own stdout/stderr tail.
    if (
        launched
        and not timed_out
        and engine_killed_by_signal is None
        and not no_log_reported
        and not engine_aborted
        and not log_text.strip()
        and measurement_results
        and all(m.get("value") is None for m in measurement_results)
    ):
        diagnostics.append(
            {
                "severity": "error",
                "code": "unknown",
                "message": (
                    f"{engine_name} exited with code {engine_exit_code} but "
                    "wrote no log output and produced no measurement values"
                    f"{_engine_output_tail(stdout_text, stderr_text)}"
                ),
            }
        )

    # Issue #2846: the engine's abort trailer gets its own analysis-level
    # diagnostic, always at `severity: "error"`, reported alongside (not
    # instead of) the per-measurement ones. Appended before
    # `disqualifying` is sampled so `fail_on_diagnostic` can name it.
    aborted_re = _XYCE_SIMULATION_ABORTED_RE if is_xyce else _SIMULATION_ABORTED_RE
    aborted_diagnostic = _analysis_aborted_diagnostic(log_text, aborted_re)
    if aborted_diagnostic is not None:
        diagnostics.append(aborted_diagnostic)

    # A recovered `singular_matrix`/`nonconvergence` classification (the
    # engine's own stepping-recovery narration -- routine noise on the way
    # to a *successful* analysis, not evidence the run failed) does not
    # count as fatal on its own: downgrade its severity in place before
    # computing the corner's aggregate status. See
    # `_recovered_from_stepping` and issue #205;
    # `timeout`/`netlist`/`measurement`/`no_such_vector`/`unknown` are never
    # downgraded.
    # Xyce (issue #2016) gets the same rule against its own abort trailer:
    # its Amesos "numerically singular matrix, returning zero" warnings can
    # narrate intermediate solver trouble on a run that ultimately produced
    # every requested measurement.
    # Issue #2492: sampled *before* the downgrade immediately below, on the
    # diagnostic's `code` rather than its severity. A caller who listed
    # `singular_matrix` is saying they distrust exactly the case the
    # downgrade exists to forgive, so reading this after the downgrade (or
    # reading `severity` instead of `code`) would make the option a no-op
    # for its single most important use.
    disqualifying = sorted(
        {d["code"] for d in diagnostics if d["code"] in fail_on_diagnostic}
    )

    waveform_complete = _capture_only_waveform_complete(
        measurements_spec, raw_path, diagnostics, analysis
    )

    if _recovered_from_stepping(
        measurements_spec,
        measurement_results,
        log_text,
        aborted_re=aborted_re,
        waveform_complete=waveform_complete,
    ):
        _downgrade_stepping_diagnostics(diagnostics, bool(measurements_spec))

    status = _grade_corner(diagnostics, measurement_results, disqualifying)

    artifacts: dict[str, str | None] = {
        "log": None,
        "raw": None,
        "waveform": None,
        "deck": None,
    }
    # Additive (issue #3006): only for a `noise` corner whose rawfile parsed.
    noise_fields: dict[str, str] = {}
    artifacts.update(
        sim_complex_vectors.artifact_fields(complex_vectors, complex_outcome)
    )
    artifacts.update(sim_twoport.artifact_fields(analysis, twoport_outcome))
    if keep_artifacts:
        if os.path.isfile(log_path):
            artifacts["log"] = log_path
        if raw_path is not None and os.path.isfile(raw_path):
            artifacts["raw"] = raw_path
            raw_size = os.path.getsize(raw_path)
            try:
                waveform_path = _persist_waveform_artifact(
                    raw_path,
                    raw_size,
                    waveform_max_bytes,
                    analysis,
                    diagnostics,
                    noise_fields,
                )
                if waveform_path is not None:
                    artifacts["waveform"] = waveform_path
            except (SimError, ValueError) as exc:
                # Generic defensive catch, not expected in normal operation:
                # `parse_ascii_rawfile` understands both plain-real (`tran`/
                # `dc`) and `Flags: complex` (`ac`/`sp`) rawfiles (issue
                # #1756), but a still-malformed or otherwise unrecognised
                # rawfile should degrade to a diagnostic rather than crash
                # the whole corner. `_run_corner`'s own contract is "never
                # raises", and a corner that ran its analysis successfully
                # should not have its `status` invalidated just because one
                # *optional* artifact could not be parsed.
                #
                # Which is also why this one `"unknown"` lands *after*
                # `_grade_corner` and so escapes `options.fail_on_diagnostic`
                # (issue #2492): a corner whose analysis and every
                # measurement succeeded is not made untrustworthy by an
                # unparseable optional waveform dump, so it is deliberately
                # outside the grading window rather than an oversight.
                diagnostics.append(
                    {
                        "severity": "warning",
                        "code": "unknown",
                        "message": f"could not parse waveform rawfile: {exc}",
                    }
                )
        # Issue #3052: the engine has exited and its log is parsed, so the
        # persisted deck/log can now be rewritten to stable tokens.
        diagnostics.extend(
            sim_portable.rewrite_corner_artifacts(
                corner_dir, deck_path, log_path, portable_roots
            )
        )
        if os.path.isfile(deck_path):
            artifacts["deck"] = deck_path
    else:
        _cleanup_dir(corner_dir)

    monte_carlo: dict[str, Any] | None = None
    if point.sample_index is not None:
        assert point.mc_seed is not None  # every sampled point carries one
        monte_carlo = {
            "sample_index": point.sample_index,
            "seed": point.mc_seed["rndseed"],
            "process_seed": point.mc_seed["process_seed"],
            "mismatch_seed": point.mc_seed["mismatch_seed"],
        }
        monte_carlo.update(_mc_seed_optional_echo(point.mc_seed))
        if point.mc_injected is not None:
            monte_carlo["injected"] = [
                {k: v for k, v in item.items() if k not in ("param", "scale")}
                for item in point.mc_injected
            ]

    corner = {
        "corner_id": point.corner_id,
        "process": point.process,
        "supply_v": point.supply_v,
        "temperature_c": point.temperature_c,
        **_params_report_field(point),
        **_nodeset_report_field(point),
        **_ic_report_field(point),
        **_options_report_field(point),
        "status": status,
        "runtime_s": runtime_s,
        # Additive (issue #2848): the engine process's raw outcome. Both are
        # null when the harness itself ended the run (timeout) or the engine
        # never launched.
        "engine_exit_code": engine_exit_code,
        "engine_killed_by_signal": engine_killed_by_signal,
        "measurements": measurement_results,
        "diagnostics": diagnostics,
        "artifacts": artifacts,
        **noise_fields,
        "monte_carlo": monte_carlo,
        # Additive/optional (issue #3252): the `corners.variants[]` name,
        # present only for a variant-axis request.
        **({"variant": point.variant} if point.variant is not None else {}),
        # Additive/optional (issue #2482): `steps` -- per-step outcomes --
        # only for an `analysis_steps[]` request; empty for every other one.
        **step_fields,
        # Additive/optional (issue #3159): per-pass status for an
        # `analysis.passes` request; absent for every other one.
        **pass_fields,
        # Additive/optional (issue #3337): the declared complex vectors'
        # names and point count; the arrays are in `artifacts.complex_vectors`.
        **sim_complex_vectors.corner_fields(complex_vectors, complex_outcome),
        # Additive/optional (issue #3338): per-frequency S-parameters of an
        # `analysis.kind: "twoport"` request; the `.s2p` is in `artifacts`.
        **sim_twoport.corner_fields(analysis, twoport_outcome),
        # Additive/optional (issue #2902): the effective per-corner waveform
        # levels, only for a request that scales a PWL/PULSE supply source.
        **(
            {"supply_waveforms": supply_waveform_report}
            if supply_waveform_report
            else {}
        ),
    }
    return corner, engine_version


def _engine_aborted_diagnostic(
    engine_name: str,
    engine_exit_code: int | None,
    classified: list[dict],
    tail_suffix: str,
) -> dict | None:
    """Issue #3067: the ``engine_aborted`` diagnostic for an engine that
    exited non-zero with no classifiable cause in its log, else ``None``.
    It is the corner's cause, instead of leaving only N consequential
    ``produced no value`` errors. (A clean exit with no log is #3016's
    ``unknown`` diagnostic.)"""
    if not engine_exit_code or any(d["severity"] == "error" for d in classified):
        return None
    return {
        "severity": "error",
        "code": "engine_aborted",
        "message": f"{engine_name} exited with code {engine_exit_code}" + tail_suffix,
    }


def _demote_abort_cascade(
    measurement_diagnostics: list[dict], engine_aborted: bool
) -> list[dict]:
    """After an ``engine_aborted`` -- or an ngspice deck-abort cause with no
    analysis ever started (#3879) -- the per-measurement "produced no value"
    errors are consequences of the abort (the measurement results themselves
    stay ``error``), so drop them rather than crowd ``diagnostics[0]``
    (#3067)."""
    if not engine_aborted:
        return measurement_diagnostics
    return [d for d in measurement_diagnostics if d.get("code") != "measurement"]


#: Lines of engine output quoted in an ``engine_killed``/``engine_aborted``
#: diagnostic message (issue #3067).
_ENGINE_TAIL_LINES = 20


def _engine_abort_tail(*sources: str) -> str:
    """The last :data:`_ENGINE_TAIL_LINES` non-blank lines of the first
    non-blank source (stderr, then stdout, then the engine log)."""
    for text in sources:
        lines = [ln.rstrip() for ln in text.splitlines() if ln.strip()]
        if lines:
            return "\n".join(lines[-_ENGINE_TAIL_LINES:])
    return ""


def _engine_log_error(log_text: str) -> str:
    """The first ``ERROR:``/``Error:`` line of the engine log plus its indented
    continuation lines, or ``""`` when the log has none (issue #4218). Keeps an
    unclassified abort's message from showing only the stdout banner."""
    lines = log_text.splitlines()
    for i, line in enumerate(lines):
        if line.lstrip().startswith(("ERROR:", "Error:")):
            out = [line.rstrip()]
            if (
                i + 1 < len(lines)
                and lines[i + 1][:1].isspace()
                and lines[i + 1].strip()
            ):
                out.append(lines[i + 1].rstrip())
            return "\n".join(out)
    return ""


def _prepend_engine_log_error(output_tail: str, log_text: str) -> str:
    """``output_tail`` led by the engine log's ERROR line, unless it already
    shows it (issue #4218)."""
    log_error = _engine_log_error(log_text)
    if not log_error or log_error in output_tail:
        return output_tail
    return f"{log_error}\n{output_tail}" if output_tail else log_error


_ENGINE_OUTPUT_TAIL_BYTES = 2048


def _engine_output_tail(stdout_text: str, stderr_text: str) -> str:
    """``"; <stream> tail: ..."`` fragments for whichever of the engine's
    stderr/stdout captured anything, each bounded to its last
    :data:`_ENGINE_OUTPUT_TAIL_BYTES` bytes (issue #3016). Empty when both
    streams are empty. Never raises on undecodable bytes."""
    parts = []
    for label, text in (("stderr", stderr_text), ("stdout", stdout_text)):
        text = text.strip()
        if not text:
            continue
        raw = text.encode("utf-8", errors="replace")
        if len(raw) > _ENGINE_OUTPUT_TAIL_BYTES:
            text = "..." + raw[-_ENGINE_OUTPUT_TAIL_BYTES:].decode(
                "utf-8", errors="replace"
            )
        parts.append(f"; {label} tail: {text}")
    return "".join(parts)


def _split_returncode(returncode: int) -> tuple[int | None, str | None]:
    """``(engine_exit_code, engine_killed_by_signal)`` from a POSIX
    ``subprocess`` return code (issue #2848): a negative code is the killing
    signal's number (``None`` exit code, signal name), anything else a plain
    exit code. An unnamed signal number falls back to ``SIG<n>``.
    """
    if returncode >= 0:
        return returncode, None
    try:
        return None, signal.Signals(-returncode).name
    except ValueError:
        return None, f"SIG{-returncode}"


def _corner_measurement_results(
    measurement_values: dict[str, float],
    measurements_spec: list[dict[str, Any]],
    *,
    analysis: dict[str, Any],
    log_text: str,
    is_xyce: bool,
    save_mode: str,
    crossings: dict[int, sim_crossings.CrossingsOutcome] | None = None,
    energy: dict[int, sim_energy.EnergyOutcome] | None = None,
    cycle_means: dict[int, sim_cycle_mean.CycleMeanOutcome] | None = None,
    transfers: dict[int, sim_transfer_meas.TransferOutcome] | None = None,
    snms: dict[int, sim_snm_meas.SnmOutcome] | None = None,
    eyes: dict[int, sim_eye.EyeOutcome] | None = None,
    searches: dict[int, sim_search.SearchOutcome] | None = None,
    tians: dict[int, sim_tian.TianOutcome] | None = None,
    engine_diagnostics: list[dict[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    """One corner's ``(measurement_results, diagnostics, extra_corner_fields)``
    from its harvested log values.

    ``crossings`` (issue #2868) maps a ``measurements_spec`` position to that
    ``crossings`` entry's extracted series (see
    :func:`klayout_tools.sim_crossings.extract_corner_crossings`); such an
    entry is graded by :func:`_crossings_measurement_result` instead of being
    looked up in the log. ``energy`` (issue #3033) does the same for ``energy``
    entries, graded by :func:`_energy_measurement_result`.

    An ``analysis_steps[]`` corner (issue #2482) is graded by
    :func:`klayout_tools.sim_steps.grade_step_measurements`, which adds the
    per-step outcome list and the step/derived failure propagation. Every
    other corner takes the original single-analysis path below, unchanged:
    one result per spec in declared order, a ``measurement`` diagnostic for
    each one that produced no value, and ``extra_corner_fields`` empty.

    ``engine_diagnostics`` (issue #3108) are the corner's diagnostics so far
    (solver/timeout classification); a ``spice`` measurement declaring
    ``on_unreached: "null"`` is graded :data:`NOT_REACHED_STATUS` instead of
    ``error`` only when none of them is error-severity. Not applied to an
    ``analysis_steps[]`` corner.
    """
    if sim_steps.is_steps_analysis(analysis):
        return sim_steps.grade_step_measurements(
            measurement_values,
            measurements_spec,
            analysis=analysis,
            log_text=log_text,
            save_mode=save_mode,
        )
    measurement_results: list[dict[str, Any]] = []
    diagnostics: list[dict[str, Any]] = []
    meas_errors = {} if is_xyce else _parse_meas_errors(log_text)
    ac_too_few_points = (
        not is_xyce
        and analysis.get("kind") == "ac"
        and _ac_sweep_too_few_points(log_text)
    )
    unreached_names = set() if is_xyce else _parse_meas_unreached_names(log_text)
    attributed: set[str] = set()
    # Forms whose value comes from a rawfile reduction rather than the log:
    # (spec predicate, response-entry builder, position -> outcome map).
    sidecar_forms: tuple[
        tuple[
            Callable[[dict[str, Any]], bool],
            Callable[..., tuple[dict[str, Any], list[dict[str, Any]]]],
            dict[int, Any] | None,
        ],
        ...,
    ] = (
        (sim_crossings.is_crossings, _crossings_measurement_result, crossings),
        (sim_energy.is_energy, _energy_measurement_result, energy),
        (sim_cycle_mean.is_cycle_mean, _cycle_mean_measurement_result, cycle_means),
        (sim_transfer_meas.is_transfer, _transfer_measurement_result, transfers),
        (sim_tian.is_tian, _tian_measurement_result, tians),
        (sim_snm_meas.is_snm, _snm_measurement_result, snms),
        (sim_eye.is_eye, _eye_measurement_result, eyes),
        (sim_search.is_search, _search_measurement_result, searches),
    )
    for position, spec in enumerate(measurements_spec):
        sidecar = next(
            (
                (render, outcomes)
                for is_form, render, outcomes in sidecar_forms
                if is_form(spec)
            ),
            None,
        )
        if sidecar is not None:
            render, outcomes = sidecar
            result, extra = render(
                spec, (outcomes or {}).get(position), save_mode=save_mode
            )
            measurement_results.append(result)
            diagnostics.extend(extra)
            continue
        name = spec["name"]
        value = _harvest_measurement_value(measurement_values, spec, is_xyce=is_xyce)
        unit = spec.get("unit")
        if value is None and _is_unreached(
            spec,
            unreached_names=unreached_names,
            diagnostics=engine_diagnostics or [],
        ):
            attributed.add(str(name).lower())
            measurement_results.append(
                {
                    "name": name,
                    "value": None,
                    "unit": unit,
                    "status": NOT_REACHED_STATUS,
                    "margin": None,
                }
            )
            diagnostics.append(
                {
                    "severity": "info",
                    "code": NOT_REACHED_DIAGNOSTIC_CODE,
                    "message": f"measurement '{name}' target not reached: the "
                    "analysis completed but ngspice's .meas never satisfied "
                    f"its condition ({spec['spice'].strip()})",
                }
            )
            continue
        if value is None:
            result, diagnostic, was_attributed = _no_value_measurement(
                spec,
                meas_errors,
                save_mode=save_mode,
                too_few_points=ac_too_few_points,
            )
            measurement_results.append(result)
            diagnostics.append(diagnostic)
            if was_attributed:
                attributed.add(str(name).lower())
            continue
        # `_grade_measurement_value` owns the plausibility-pre-check ->
        # `limits` ordering (issue #2493) in one place, so there is no
        # second grading path where the pre-check could be skipped.
        m_status, margin, grading_diagnostics = _grade_measurement_value(spec, value)
        measurement_results.append(
            {
                "name": name,
                "value": value,
                "unit": unit,
                "status": m_status,
                "margin": margin,
            }
        )
        diagnostics.extend(grading_diagnostics)
    # Corner-level fallback (issue #2826): engine `.meas` errors that could
    # not be tied to a missing result by name are still surfaced, as a
    # warning so grading is unchanged, but only when something is missing.
    if diagnostics and any(d["code"] == "measurement" for d in diagnostics):
        unattributed = [
            f"{n}: {t}" for n, t in meas_errors.items() if n not in attributed
        ]
        if unattributed:
            diagnostics.append(
                {
                    "severity": "warning",
                    "code": "measurement",
                    "message": "ngspice reported .meas errors not matched to a "
                    "missing measurement: "
                    + "; ".join(unattributed[:_MEAS_ERROR_MAX_ENTRIES]),
                }
            )
    return measurement_results, diagnostics, {}


#: The measurement ``status`` of an ``optional`` measurement that produced no
#: value (issue #2925): data ("this figure does not exist at this point"),
#: not a failure -- it never makes its corner ``error``/``fail``.
_ABSENT_STATUS = "absent"
ABSENT_DIAGNOSTIC_CODE = "measurement_absent"


#: ngspice's reason text when a ``.meas`` search (``WHEN``/``TRIG``/``TARG``/
#: ``FIND ... WHEN``) never found its crossing in the analysis interval, e.g.
#: ``Error: measure  x  when(WHEN) : out of interval`` (verified on ngspice-46).
#: The one engine reason that means "this figure does not exist at this point";
#: any other ``Error: measure`` reason is a malformed card and stays an error.
_MEAS_ABSENT_CAUSE_RE = re.compile(r"out of interval", re.IGNORECASE)


#: ngspice's data-row count line, e.g. ``No. of Data Rows : 1`` (verified on
#: ngspice-46). An ``ac lin 2 ...`` sweep leaves one row, so ``.meas ac ...
#: AT=`` cannot interpolate and fails without an ``Error: measure`` line
#: (issue #4084).
_DATA_ROWS_RE = re.compile(r"No\. of Data Rows\s*:\s*(\d+)", re.IGNORECASE)
_AC_TOO_FEW_POINTS_HINT = (
    " -- the ac sweep produced fewer than 2 data points, so AT=/interpolation "
    "has nothing to interpolate between: use a denser sweep (e.g. `dec`)"
)


def _ac_sweep_too_few_points(log_text: str) -> bool:
    """Whether the log's (last) ``No. of Data Rows`` is below 2."""
    rows = _DATA_ROWS_RE.findall(log_text or "")
    return bool(rows) and int(rows[-1]) < 2


def _is_legitimately_absent(cause: str | None) -> bool:
    """Whether a value-less ``optional`` measurement is absent data rather than
    an engine failure: no ngspice ``Error: measure`` reason at all, or the
    "out of interval" (never crossed) reason."""
    return cause is None or _MEAS_ABSENT_CAUSE_RE.search(cause) is not None


def _absent_measurement(
    spec: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """``(result, diagnostic)`` for an ``optional: true`` measurement with no
    value: ``value``/``margin`` null, ``status: "absent"``, and an
    info-severity diagnostic. ``limits`` are not evaluated (no pass/fail claim)."""
    name = spec["name"]
    result = {
        "name": name,
        "value": None,
        "unit": spec.get("unit"),
        "status": _ABSENT_STATUS,
        "margin": None,
    }
    diagnostic = {
        "severity": "info",
        "code": ABSENT_DIAGNOSTIC_CODE,
        "message": f"optional measurement '{name}' produced no value "
        "(reported absent; not an error)",
    }
    return result, diagnostic


def _no_value_measurement(
    spec: dict[str, Any],
    meas_errors: dict[str, str],
    *,
    save_mode: str,
    too_few_points: bool = False,
) -> tuple[dict[str, Any], dict[str, Any], bool]:
    """``(result, diagnostic, attributed)`` for a scalar measurement whose log
    carried no value: ``absent`` when it is ``optional`` and the engine gave no
    reason other than "never crossed" (issue #2925), else an ``error`` entry
    with a ``measurement`` diagnostic. ``attributed`` is whether an ngspice
    ``.meas`` error was tied to this measurement by name (issue #2826)."""
    name = spec["name"]
    cause = meas_errors.get(str(name).lower()) if spec.get("spice") else None
    if spec.get("optional") is True and _is_legitimately_absent(cause):
        result, diagnostic = _absent_measurement(spec)
        return result, diagnostic, cause is not None
    message = _no_value_message(spec, save_mode=save_mode)
    if cause is not None:
        message += f" -- ngspice reported: {cause}"
    elif too_few_points and spec.get("spice"):
        message += _AC_TOO_FEW_POINTS_HINT
    result = {
        "name": name,
        "value": None,
        "unit": spec.get("unit"),
        "status": "error",
        "margin": None,
    }
    diagnostic = {"severity": "error", "code": "measurement", "message": message}
    return result, diagnostic, cause is not None


def _crossings_measurement_result(
    spec: dict[str, Any],
    outcome: sim_crossings.CrossingsOutcome | None,
    *,
    save_mode: str,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """One ``crossings`` measurement's response entry (issue #2868).

    ``value`` is the number of crossings found inside the window -- a plain
    number, graded through :func:`_grade_measurement_value` exactly like any
    scalar -- and the series itself rides the additive ``crossings`` sibling.
    No series (signal did not resolve, rawfile unreadable) is an ordinary
    ``status: "error"`` entry with a ``code: "measurement"`` diagnostic and
    no ``crossings`` member, like any other measurement with no value."""
    name = spec["name"]
    unit = spec.get("unit")
    if outcome is None or outcome.error is not None:
        message = (
            outcome.error
            if outcome is not None and outcome.error is not None
            else f"measurement '{name}' produced no crossings series"
        )
        if save_mode == "netlist":
            message += SAVE_MODE_NETLIST_NO_VALUE_HINT
        result = {
            "name": name,
            "value": None,
            "unit": unit,
            "status": "error",
            "margin": None,
        }
        return result, [
            {"severity": "error", "code": "measurement", "message": message}
        ]
    m_status, margin, grading_diagnostics = _grade_measurement_value(
        spec, outcome.total
    )
    result = {
        "name": name,
        "value": outcome.total,
        "unit": unit,
        "status": m_status,
        "margin": margin,
        "crossings": outcome.report(),
    }
    return result, grading_diagnostics


def _cycle_mean_measurement_result(
    spec: dict[str, Any],
    outcome: sim_cycle_mean.CycleMeanOutcome | None,
    *,
    save_mode: str,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """One ``cycle_mean`` measurement's response entry (issue #2947).

    ``value`` is the time-weighted mean, graded through
    :func:`_grade_measurement_value` like any scalar; the window rides the
    additive ``cycle_mean`` sibling. A failure is ``status: "error"``,
    ``value: null`` and a ``code: "measurement"`` diagnostic whose message
    starts with a stable ``[<reason>]`` token (one of
    :data:`klayout_tools.sim_cycle_mean.ERROR_CODES`), so the existing
    ``diagnostics[].code`` vocabulary is unchanged."""
    name = spec["name"]
    unit = spec.get("unit")
    if outcome is None or outcome.mean is None:
        message = (
            outcome.error
            if outcome is not None and outcome.error is not None
            else f"measurement '{name}' produced no cycle_mean value"
        )
        reason = (
            outcome.code
            if outcome is not None and outcome.code is not None
            else "cycle_mean_vector_missing"
        )
        if save_mode == "netlist" and reason == "cycle_mean_vector_missing":
            message += SAVE_MODE_NETLIST_NO_VALUE_HINT
        result = {
            "name": name,
            "value": None,
            "unit": unit,
            "status": "error",
            "margin": None,
        }
        return result, [
            {
                "severity": "error",
                "code": "measurement",
                "message": f"[{reason}] {message}",
            }
        ]
    m_status, margin, grading_diagnostics = _grade_measurement_value(spec, outcome.mean)
    result = {
        "name": name,
        "value": outcome.mean,
        "unit": unit,
        "status": m_status,
        "margin": margin,
        "cycle_mean": outcome.report(),
    }
    return result, grading_diagnostics


def _transfer_measurement_result(
    spec: dict[str, Any],
    outcome: sim_transfer_meas.TransferOutcome | None,
    *,
    save_mode: str,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """One ``transfer`` measurement's response entry (issue #2923).

    ``value`` is the metric, graded through :func:`_grade_measurement_value`
    like any scalar; the request echo rides the additive ``transfer`` sibling.
    A failure is ``status: "error"``, ``value: null`` (so it can never pass a
    limit) with a ``code: "measurement"`` diagnostic whose message starts with
    a ``[<reason>]`` token from
    :data:`klayout_tools.sim_transfer_meas.ERROR_CODES`.

    The all-crossing margin tokens (issue #3088) are graded by
    :func:`_margin_measurement_result`."""
    name = spec["name"]
    unit = spec.get("unit")
    if outcome is not None and outcome.margins is not None:
        return _margin_measurement_result(spec, outcome)
    if outcome is None or outcome.value is None:
        message = (
            outcome.error
            if outcome is not None and outcome.error is not None
            else f"measurement '{name}' produced no transfer value"
        )
        reason = (
            outcome.code
            if outcome is not None and outcome.code is not None
            else "transfer_vector_missing"
        )
        if save_mode == "netlist" and reason == "transfer_vector_missing":
            message += SAVE_MODE_NETLIST_NO_VALUE_HINT
        result = {
            "name": name,
            "value": None,
            "unit": unit,
            "status": "error",
            "margin": None,
        }
        return result, [
            {
                "severity": "error",
                "code": "measurement",
                "message": f"[{reason}] {message}",
            }
        ]
    m_status, margin, grading_diagnostics = _grade_measurement_value(
        spec, outcome.value
    )
    result = {
        "name": name,
        "value": outcome.value,
        "unit": unit,
        "status": m_status,
        "margin": margin,
        "transfer": outcome.report(spec),
    }
    return result, grading_diagnostics


def _snm_measurement_result(
    spec: dict[str, Any],
    outcome: sim_snm_meas.SnmOutcome | None,
    *,
    save_mode: str,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """One ``snm`` measurement's response entry (issue #3895).

    ``value`` is the butterfly SNM in volts, graded through
    :func:`_grade_measurement_value` like any scalar; the request echo and the
    two per-eye sides ride the additive ``snm`` sibling. A failure is
    ``status: "error"``, ``value: null`` (so it can never pass a limit) with a
    ``code: "measurement"`` diagnostic whose message starts with a
    ``[<reason>]`` token from :data:`klayout_tools.sim_snm_meas.ERROR_CODES`."""
    name = spec["name"]
    unit = spec.get("unit")
    if outcome is None or outcome.value is None:
        message = (
            outcome.error
            if outcome is not None and outcome.error is not None
            else f"measurement '{name}' produced no snm value"
        )
        reason = (
            outcome.code
            if outcome is not None and outcome.code is not None
            else "snm_vector_missing"
        )
        if save_mode == "netlist" and reason == "snm_vector_missing":
            message += SAVE_MODE_NETLIST_NO_VALUE_HINT
        result = {
            "name": name,
            "value": None,
            "unit": unit,
            "status": "error",
            "margin": None,
        }
        return result, [
            {
                "severity": "error",
                "code": "measurement",
                "message": f"[{reason}] {message}",
            }
        ]
    m_status, margin, grading_diagnostics = _grade_measurement_value(
        spec, outcome.value
    )
    result = {
        "name": name,
        "value": outcome.value,
        "unit": unit,
        "status": m_status,
        "margin": margin,
        "snm": outcome.report(spec),
    }
    return result, grading_diagnostics


def _eye_measurement_result(
    spec: dict[str, Any],
    outcome: sim_eye.EyeOutcome | None,
    *,
    save_mode: str,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """One ``eye`` measurement's response entry (issue #4292).

    ``value`` is the metric the entry selects (eye height by default), graded
    through :func:`_grade_measurement_value` like any scalar; height, width and
    best phase ride the additive ``eye`` sibling. A failure is
    ``status: "error"``, ``value: null`` with a ``code: "measurement"``
    diagnostic whose message starts with a ``[<reason>]`` token from
    :data:`klayout_tools.sim_eye.ERROR_CODES`."""
    name = spec["name"]
    unit = spec.get("unit")
    if outcome is None or outcome.value is None:
        message = (
            outcome.error
            if outcome is not None and outcome.error is not None
            else f"measurement '{name}' produced no eye value"
        )
        reason = (
            outcome.code
            if outcome is not None and outcome.code is not None
            else "eye_vector_missing"
        )
        if save_mode == "netlist" and reason == "eye_vector_missing":
            message += SAVE_MODE_NETLIST_NO_VALUE_HINT
        result = {
            "name": name,
            "value": None,
            "unit": unit,
            "status": "error",
            "margin": None,
        }
        return result, [
            {
                "severity": "error",
                "code": "measurement",
                "message": f"[{reason}] {message}",
            }
        ]
    m_status, margin, grading_diagnostics = _grade_measurement_value(
        spec, outcome.value
    )
    result = {
        "name": name,
        "value": outcome.value,
        "unit": unit,
        "status": m_status,
        "margin": margin,
        "eye": outcome.report(),
    }
    return result, grading_diagnostics


def _search_measurement_result(
    spec: dict[str, Any],
    outcome: sim_search.SearchOutcome | None,
    *,
    save_mode: str,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """One ``search`` measurement's response entry (issue #4121).

    ``value`` is the midpoint of the converged bracket, graded through
    :func:`_grade_measurement_value` like any scalar; the additive ``search``
    sibling carries ``{source, direction, iterations, lo, hi, converged}``. A
    failure is ``status: "error"``, ``value: null`` (so it can never pass a
    limit) with a ``code: "measurement"`` diagnostic whose message starts with
    a ``[<reason>]`` token from
    :data:`klayout_tools.sim_search.ERROR_CODES`."""
    name = spec["name"]
    unit = spec.get("unit")
    if outcome is None or outcome.value is None:
        message = (
            outcome.error
            if outcome is not None and outcome.error is not None
            else f"measurement '{name}' produced no search value"
        )
        reason = (
            outcome.code
            if outcome is not None and outcome.code is not None
            else "search_no_result"
        )
        if save_mode == "netlist" and reason == "search_probe_failed":
            message += SAVE_MODE_NETLIST_NO_VALUE_HINT
        result: dict[str, Any] = {
            "name": name,
            "value": None,
            "unit": unit,
            "status": "error",
            "margin": None,
        }
        if outcome is not None and outcome.iterations is not None:
            result["search"] = outcome.report(spec)
        return result, [
            {
                "severity": "error",
                "code": "measurement",
                "message": f"[{reason}] {message}",
            }
        ]
    m_status, margin, grading_diagnostics = _grade_measurement_value(
        spec, outcome.value
    )
    result = {
        "name": name,
        "value": outcome.value,
        "unit": unit,
        "status": m_status,
        "margin": margin,
        "search": outcome.report(spec),
    }
    return result, grading_diagnostics


def _tian_measurement_result(
    spec: dict[str, Any],
    outcome: sim_tian.TianOutcome | None,
    *,
    save_mode: str,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """One ``tian`` measurement's response entry (issue #3160).

    ``value`` is the metric, graded through :func:`_grade_measurement_value`
    like any scalar; the request echo rides the additive ``tian`` sibling. A
    failure is ``status: "error"``, ``value: null`` (so it can never pass a
    limit) with a ``code: "measurement"`` diagnostic whose message starts with
    a ``[<reason>]`` token from :data:`klayout_tools.sim_tian.ERROR_CODES`."""
    name = spec["name"]
    unit = spec.get("unit")
    if outcome is None or outcome.value is None:
        message = (
            outcome.error
            if outcome is not None and outcome.error is not None
            else f"measurement '{name}' produced no tian value"
        )
        reason = (
            outcome.code
            if outcome is not None and outcome.code is not None
            else "tian_pass_missing"
        )
        if save_mode == "netlist" and reason == "tian_vector_missing":
            message += SAVE_MODE_NETLIST_NO_VALUE_HINT
        result = {
            "name": name,
            "value": None,
            "unit": unit,
            "status": "error",
            "margin": None,
        }
        return result, [
            {
                "severity": "error",
                "code": "measurement",
                "message": f"[{reason}] {message}",
            }
        ]
    m_status, margin, grading_diagnostics = _grade_measurement_value(
        spec, outcome.value
    )
    result = {
        "name": name,
        "value": outcome.value,
        "unit": unit,
        "status": m_status,
        "margin": margin,
        "tian": outcome.report(spec),
    }
    return result, grading_diagnostics


def _margin_measurement_result(
    spec: dict[str, Any], outcome: sim_transfer_meas.TransferOutcome
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """A ``transfer`` margin-metric entry (issue #3088).

    ``value`` is graded through :func:`_grade_measurement_value` like any
    scalar, then the AC band's verdict is applied: a limit that is *met* on a
    band that cannot support a pass is :data:`_INCONCLUSIVE_STATUS` with a
    :data:`MARGIN_AMBIGUOUS_DIAGNOSTIC_CODE` warning -- never ``pass`` -- and a
    violation is a sound ``fail`` only when ``grade_margins`` found it among
    the crossings actually seen (a hidden crossing can only lower a minimum).
    A metric with no crossing at all is vacuously ``pass`` on a clean band.
    Without ``limits`` the entry is report-only and the band never downgrades
    it; a valueless metric is then an ordinary ``transfer_metric_unavailable``
    error."""
    assert outcome.margins is not None
    name = spec["name"]
    metric = spec["transfer"]["metric"]
    limits = spec.get("limits") or {}
    has_limits = limits.get("min") is not None or limits.get("max") is not None
    ambiguity = outcome.margins.ambiguity
    diagnostics: list[dict[str, Any]] = []
    status: str
    margin: float | None = None
    downgrade = False
    if outcome.value is None:
        if has_limits and sim_transfer_meas.is_vacuous(outcome, spec):
            status = "pass"
        elif has_limits and ambiguity:
            status, downgrade = _INCONCLUSIVE_STATUS, True
        else:
            reason = sim_transfer_meas.unavailable_reason(outcome, metric)
            return (
                {
                    "name": name,
                    "value": None,
                    "unit": spec.get("unit"),
                    "status": "error",
                    "margin": None,
                    "transfer": outcome.report(spec),
                },
                [
                    {
                        "severity": "error",
                        "code": "measurement",
                        "message": (
                            f"[transfer_metric_unavailable] measurement "
                            f"'{name}': {metric} is unavailable ({reason})"
                        ),
                    }
                ],
            )
    else:
        status, margin, diagnostics = _grade_measurement_value(spec, outcome.value)
        if (
            has_limits
            and ambiguity
            and (status == "pass" or (status == "fail" and outcome.verdict != "fail"))
        ):
            status, margin, downgrade = _INCONCLUSIVE_STATUS, None, True
    if downgrade:
        diagnostics.append(
            {
                "severity": "warning",
                "code": MARGIN_AMBIGUOUS_DIAGNOSTIC_CODE,
                "message": (
                    f"measurement {name!r}: {metric} cannot be graded -- the AC "
                    f"band is ambiguous ({', '.join(ambiguity)}) -- graded "
                    f"{_INCONCLUSIVE_STATUS} rather than pass/fail"
                ),
            }
        )
    return (
        {
            "name": name,
            "value": outcome.value,
            "unit": spec.get("unit"),
            "status": status,
            "margin": margin,
            "transfer": outcome.report(spec),
        },
        diagnostics,
    )


def _energy_measurement_result(
    spec: dict[str, Any],
    outcome: sim_energy.EnergyOutcome | None,
    *,
    save_mode: str,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """One ``energy`` measurement's response entry (issue #3033).

    ``value`` is the net energy in joules -- a plain number, graded through
    :func:`_grade_measurement_value` like any scalar -- and the
    net/gross/recovered/charge breakdown rides the additive ``energy``
    sibling. A source or voltage that did not resolve is an ordinary
    ``status: "error"`` entry with a ``code: "measurement"`` diagnostic and
    no ``energy`` member."""
    name = spec["name"]
    unit = spec.get("unit")
    if outcome is None or outcome.error is not None:
        message = (
            outcome.error
            if outcome is not None and outcome.error is not None
            else f"measurement '{name}' produced no energy"
        )
        if save_mode == "netlist":
            message += SAVE_MODE_NETLIST_NO_VALUE_HINT
        result = {
            "name": name,
            "value": None,
            "unit": unit,
            "status": "error",
            "margin": None,
        }
        return result, [
            {"severity": "error", "code": "measurement", "message": message}
        ]
    m_status, margin, grading_diagnostics = _grade_measurement_value(spec, outcome.net)
    result = {
        "name": name,
        "value": outcome.net,
        "unit": unit,
        "status": m_status,
        "margin": margin,
        "energy": outcome.report(),
    }
    return result, grading_diagnostics


#: ngspice's own ``.meas`` failure line, e.g.
#: ``Error: measure  bad  when(WHEN) : Expressions like 0.5*1.62 are not
#: supported.`` (verified on ngspice-46). Group 1 is the card's (lower-cased)
#: name, group 2 the engine's reason.
_MEAS_ERROR_RE = re.compile(
    r"^\s*Error:\s*measure\s+(\S+)\s+\S+\s*:\s*(.+?)\s*$", re.MULTILINE
)
_MEAS_ERROR_MAX_ENTRIES = 5
_MEAS_ERROR_MAX_CHARS = 200


def _parse_meas_errors(log_text: str) -> dict[str, str]:
    """``{lower-cased measurement name: bounded ngspice error text}`` for the
    ``.meas`` failures the ngspice log names (first one per name wins; at
    most :data:`_MEAS_ERROR_MAX_ENTRIES` entries, each truncated). An unknown
    log format simply yields an empty map -- the generic message stands."""
    errors: dict[str, str] = {}
    for match in _MEAS_ERROR_RE.finditer(log_text or ""):
        if len(errors) >= _MEAS_ERROR_MAX_ENTRIES:
            break
        text = match.group(2)
        if len(text) > _MEAS_ERROR_MAX_CHARS:
            text = text[: _MEAS_ERROR_MAX_CHARS - 3] + "..."
        errors.setdefault(match.group(1).lower(), text)
    return errors


#: The additive measurement status (and ``code``) for an unreached target.
NOT_REACHED_STATUS = "not_reached"
NOT_REACHED_DIAGNOSTIC_CODE = "measurement_not_reached"


#: ngspice-46's reason line for a ``.meas`` target the completed sweep never
#: reached (``WHEN``/``AT``/``TRIG``/``TARG`` outside the swept range), e.g.
#: ``Error: measure  x  when(WHEN) : out of interval`` (verified empirically:
#: a malformed card or a missing vector logs a different reason, or none).
#: Unlike :data:`_MEAS_ERROR_RE` this is uncapped -- a deck can have dozens.
_MEAS_UNREACHED_RE = re.compile(
    r"^\s*Error:\s*measure\s+(\S+)\s+\S+\s*:\s*out of interval\s*$",
    re.MULTILINE | re.IGNORECASE,
)


def _parse_meas_unreached_names(log_text: str) -> set[str]:
    """Lower-cased names of ``.meas`` cards that ngspice logged as both
    ``out of interval`` and ``failed!`` (the latter guards against the
    reason text appearing without the card actually failing)."""
    text = log_text or ""
    failed = {
        match.group(1).lower()
        for raw in text.splitlines()
        if (match := _MEAS_FAILED_RE.match(raw.strip()))
    }
    return {
        match.group(1).lower()
        for match in _MEAS_UNREACHED_RE.finditer(text)
        if match.group(1).lower() in failed
    }


def _is_unreached(
    spec: dict[str, Any],
    *,
    unreached_names: set[str],
    diagnostics: list[dict[str, Any]],
) -> bool:
    """True when this value-less ``spice`` measurement opted into
    ``on_unreached: "null"``, ngspice reported exactly "out of interval" for
    it, and the corner carries no error-severity diagnostic (solver
    divergence, timeout, ...) -- i.e. the analysis itself completed."""
    if spec.get("on_unreached") != "null" or spec.get("spice") is None:
        return False
    if str(spec["name"]).lower() not in unreached_names:
        return False
    return not any(d["severity"] == "error" for d in diagnostics)


def _harvest_measurement_value(
    measurement_values: dict[str, float],
    spec: dict[str, Any],
    *,
    is_xyce: bool,
) -> float | None:
    """This measurement's parsed value from the engine log's harvested
    ``{name: value}`` map, or ``None`` when the engine printed none.

    Two lookups, because the emitting paths differ in how they case the name
    they print:

    - **Xyce** upper-cases measurement names in its own output (``Vout`` ->
      ``VOUT``), and :func:`_parse_xyce_measurements` lower-cases its keys to
      compensate, so a Xyce lookup is always on the lower-cased name.
    - **ngspice** (both an ``.meas`` card and an ``expr``, issue #2533) lower-
      cases the name it prints regardless of how the request spelled it
      (``.meas tran Vout ...`` prints back as ``vout = ...``; ``let Vmid =
      ...`` / ``print Vmid`` prints ``vmid = 2.000000e+00``, verified against
      ngspice 46) -- issue #2546 fixed the ``.meas`` case by folding the
      lookup, and an ``expr`` name needs the identical fold or a mixed-case
      one that :data:`_EXPR_MEAS_NAME_RE` accepts could never be harvested.
      So the verbatim lookup is tried first (the common case where the
      request already spelled the name lower-case, byte-identical to
      before either fix) and a miss falls back to the lower-cased name for
      both measurement forms. Safe against picking up the wrong measurement
      because :func:`_validate_expr_names_unique` rejects an ``expr`` whose
      name collides case-insensitively with any other entry's.
    """
    name = spec["name"]
    if is_xyce:
        return measurement_values.get(str(name).lower())
    value = measurement_values.get(name)
    if value is None:
        return measurement_values.get(str(name).lower())
    return value


def _effective_save_mode(save_mode: str, diagnostics: list[dict[str, Any]]) -> str:
    """The save mode the no-value wording should assume (issue #3963).

    The ``save_mode: "netlist"`` hint blames a signal the netlist's ``.save``
    cards no longer retain. When the corner already carries an error-severity
    engine diagnostic (``timeout``, ``engine_killed``, ``timestep_too_small``,
    ...), the missing value has a known cause and the hint would mislead, so
    the default wording is used instead. ``no_such_vector`` is exempt: it is
    the retention symptom the hint exists to explain. The diagnostics
    themselves are kept."""
    if any(
        d.get("severity") == "error" and d.get("code") != "no_such_vector"
        for d in diagnostics
    ):
        return DEFAULT_SAVE_MODE
    return save_mode


def _no_value_message(
    spec: dict[str, Any], *, save_mode: str = DEFAULT_SAVE_MODE
) -> str:
    """The ``code: "measurement"`` diagnostic text for a measurement that
    came back with no value.

    Byte-identical to pre-#2533 for a ``spice`` (``.meas`` card)
    measurement -- callers and committed evidence records match on that
    string. An ``expr`` measurement gets the two causes named instead,
    because they are *its* failure modes and neither is visible in the
    generic wording: the expression did not resolve at all (ngspice logs its
    own ``Error: RHS "..." invalid`` for that), or it resolved to a
    multi-point vector, which ``print`` renders as an indexed table rather
    than the ``<name> = <value>`` line the harvest reads. Both are the
    single most likely first-use mistake with this field, and the raw log
    alone does not distinguish "your expression is wrong" from "this node
    does not exist".

    Under ``options.save_mode: "netlist"`` (issue #2732) the message gains a
    trailing hint naming the opt-out, because the most likely cause there is
    a signal the netlist's own ``.save`` cards no longer retain. The default
    mode's text is unchanged.
    """
    name = spec["name"]
    if spec.get("expr") is None:
        message = f"measurement '{name}' produced no value"
    else:
        message = (
            f"measurement '{name}' produced no value: its 'expr' "
            f"({spec['expr']!r}) printed no '{name} = <value>' line -- check "
            "that the expression resolves against this analysis's saved "
            "vectors and reduces to a single scalar (a multi-point or "
            "complex result prints as a table instead; reduce it with e.g. "
            "vecmax()/mean()/db()/<expr>[0])"
        )
        message += _unsupported_function_hint(spec["expr"])
    if save_mode == "netlist":
        message += SAVE_MODE_NETLIST_NO_VALUE_HINT
    return message


#: ngspice control-language functions that do not exist, with the working
#: replacement (issue #4290; docs/cli/sim.md "Functions `expr` does not have").
_UNSUPPORTED_EXPR_FUNCTIONS = {
    "sum": "mean(x)*length(x)",
    "sgn": "(x gt 0) - (x lt 0)",
    "max": "vecmax(x) (max() takes two arguments)",
    "min": "vecmin(x) (min() takes two arguments)",
}


def _unsupported_function_hint(expr: str) -> str:
    """Name the replacement for any unsupported ngspice function in ``expr``."""
    found = [
        f"{name}() -> {fix}"
        for name, fix in _UNSUPPORTED_EXPR_FUNCTIONS.items()
        if re.search(rf"(?<![\w.]){name}\(", expr)
    ]
    if not found:
        return ""
    return " -- ngspice has no such function; use: " + "; ".join(found)


#: The trailing hint :func:`_no_value_message` (and the step-sequence
#: equivalent in :mod:`klayout_tools.sim_steps`, issue #2482) appends under
#: ``options.save_mode: "netlist"`` (issue #2732).
SAVE_MODE_NETLIST_NO_VALUE_HINT = (
    ' -- options.save_mode is "netlist", so klt emitted no `save '
    "all` and only the vectors the netlist's own .save cards retain "
    "were available: add every signal this measurement reads to the "
    "netlist's .save card, or omit options.save_mode (default "
    '"all")'
)


def _read_log_file(path: str) -> str:
    """Best-effort whole-file read of an engine log: a missing or unreadable
    file yields empty text, never an exception (``_run_corner``'s
    "never raises" contract -- the corner-classified diagnostics carry the
    failure instead)."""
    if not os.path.isfile(path):
        return ""
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            return handle.read()
    except OSError:
        return ""


def _tmp_work_dir() -> str:
    import tempfile

    return tempfile.mkdtemp(prefix="klt-sim-")


def _cleanup_dir(path: str) -> None:
    import shutil

    shutil.rmtree(path, ignore_errors=True)


def _write_spiceinit(run_dir: str, ngspice_init: tuple[str, ...]) -> None:
    """Materialize (or remove) ``run_dir/.spiceinit`` from ``options.ngspice_init``
    (issue #2520), one requested line per line of the file.

    ngspice reads ``.spiceinit`` from its own current working directory
    (then ``$HOME``) before touching anything else -- the file this
    function writes is how a caller reliably sets a compatibility mode
    (``set ngbehavior=hsa`` for vendor decks written in HSPICE style, most
    notably) from the *request* rather than from whatever ``$HOME`` happens
    to hold on the box that ran ``klt sim``. Paired with ``cwd=run_dir`` on
    the actual ``ngspice`` invocation (:func:`_run_corner`,
    :func:`_run_calibration_probe`) -- writing the file without that
    ``cwd=`` would leave it unread.

    A no-op when ``ngspice_init`` is empty *except* that a stale
    ``.spiceinit`` already sitting in ``run_dir`` is removed first: a
    ``keep_artifacts`` corner directory can be reused across requests (same
    ``point.slug`` under the same ``artifacts_dir``), so an earlier request
    that *did* set ``options.ngspice_init`` must not leave a file behind
    that silently keeps altering a later request's ngspice behaviour even
    though that later request never opted in -- see this issue's
    "unchanged without the field" acceptance criterion.
    """
    spiceinit_path = os.path.join(run_dir, ".spiceinit")
    if not ngspice_init:
        if os.path.isfile(spiceinit_path):
            os.remove(spiceinit_path)
        return
    with open(spiceinit_path, "w", encoding="utf-8") as handle:
        for line in ngspice_init:
            handle.write(line + "\n")


def _write_corner_deck(
    *,
    deck_path: str,
    netlist_path: str,
    models_lib: str | None,
    point: CornerPoint,
    analysis: dict[str, Any],
    measurements_spec: list[dict[str, Any]],
    raw_path: str | None,
    osdi_preload: tuple[str, ...] = (),
    save_mode: str = DEFAULT_SAVE_MODE,
    save_vectors: tuple[str, ...] = (),
    complex_vectors: tuple[str, ...] = (),
    ngspice_options: dict[str, Any] | None = None,
    stimulus_lines: tuple[str, ...] = (),
    extra_options_card: str | None = None,
) -> None:
    """Generate the corner-specific ngspice deck: an optional Monte Carlo
    seed card, ``.lib``/``.include``/``.temp``, the request's verbatim
    ``.meas`` cards, and a ``.control`` block that ``pre_osdi``-loads any
    declared OSDI libraries (issue #2513, ``options.osdi_preload``), then
    (when this corner has measurements or a rawfile to capture) restores
    the full saved set, ``alter``s the supply sources, optionally captures
    an ASCII rawfile, runs the declared analysis, and evaluates any
    ``measurements[].expr`` (issue #2533). The ``.control`` block itself is
    built by :func:`_corner_deck_control_lines`, which owns the ordering
    contract between those commands.

    Only ``measurements[].spice`` entries land at file scope here -- an
    ``expr`` measurement has no file-scope card at all (there is no
    top-level ngspice syntax for it; that is the whole reason issue #2533
    exists), so it is emitted by :func:`_corner_deck_control_lines` instead.
    A request with no ``expr`` measurement therefore produces a
    byte-identical deck to before that issue.
    """
    lines = ["* klt sim -- generated corner deck, do not edit"]
    if point.mc_seed is not None:
        # `.options seed=` is ngspice's documented mechanism for seeding the
        # AGAUSS/GAUSS/random() functions a mismatch-aware model library's
        # behavioral parameters call -- it must appear before any such
        # function is evaluated, i.e. before `.lib`/`.include` below (see
        # `_expand_monte_carlo`'s seed contract and docs/cli/sim.md's
        # "Monte Carlo" section). `mc_process_seed`/`mc_mismatch_seed` are
        # also exposed as plain `.param`s -- available to a netlist body
        # that wants to reference the per-axis seed directly (e.g. deriving
        # its own AGAUSS `N` grouping argument) rather than relying solely
        # on ngspice's single global seed.
        lines.append(f".options seed={point.mc_seed['rndseed']}")
        lines.append(f".param mc_sample_index={point.sample_index}")
        lines.append(f".param mc_process_seed={point.mc_seed['process_seed']}")
        lines.append(f".param mc_mismatch_seed={point.mc_seed['mismatch_seed']}")
        if point.mc_injected is not None:
            lines.extend(sim_injected.deck_lines(point.mc_injected))
    if point.options:
        # Issue #3125: the swept solver options. ngspice honors the FIRST
        # `.options` card that sets a name (measured on ngspice-46: a card
        # after the `.include`d body, or after `options.ngspice_options`,
        # is ignored), so this sits ahead of both -- the corner value wins
        # over a body-level or request-level setting of the same option.
        lines.append(
            ".options "
            + " ".join(
                f"{name}={_format_param_value(value)}"
                for name, value in sorted(point.options.items())
            )
        )
    # The corner's `.lib` cards: one per section for a multi-section bundle
    # (e.g. gf180mcu's per-device-family cards, each optionally against its
    # own library file -- issue #2522), a single card for a bare-string
    # process corner, none without a process axis. See `_corner_lib_lines`.
    lines.extend(_corner_lib_lines(point, models_lib))
    lines.append(f".include {netlist_path}")
    if point.mc_seed is not None and "mismatch_scale" in point.mc_seed:
        # Issue #3942: the sigma scale. Must come after the `.lib` (whose
        # section sets the switch itself, e.g. sky130 `.param mc_mm_switch=0`
        # or `=1` in the `_mm` sections) so the request's value is the last
        # definition and wins.
        lines.append(
            f".param {point.mc_seed['mismatch_param']}="
            f"{_format_param_value(point.mc_seed['mismatch_scale'])}"
        )
    # Issue #2977: the request's declared `stimulus`, expanded to PWL sources.
    lines.extend(sim_stimulus.lines_for_corner(stimulus_lines, point.supply_v))
    # Issue #3338: the two-port's per-port z0 + AC-source cards.
    lines.extend(sim_twoport.deck_lines(analysis))
    lines.append(f".temp {point.temperature_c}")
    options_card = _ngspice_options_card(ngspice_options)
    if options_card is not None:
        # Issue #3111: `options.ngspice_options`, after `.temp`; omitted
        # entirely when unset so existing decks stay byte-identical.
        lines.append(options_card)
    if extra_options_card is not None:
        # Issue #2973: the solver options of a convergence retry. Emitted
        # last of the `.options` cards (after the netlist body's `.include`
        # and `options.ngspice_options`) so that, when two cards set the
        # same key, the retry policy wins and the second attempt actually
        # differs from the first.
        lines.append(extra_options_card)
    # Issue #3036: after the `.include`, so a request hint beats a body-level
    # `.nodeset` on the same node; nothing is emitted for a legacy point.
    lines.extend(_nodeset_cards(point))
    # Issue #3881: file-scope `.ic` cards, after the `.include` so a request
    # start state beats a body-level `.ic` on the same node; the matching
    # `uic` is added to the analysis command in `_corner_deck_control_lines`.
    lines.extend(_ic_cards(point))

    for spec in measurements_spec:
        if spec.get("spice") is not None:
            lines.append(spec["spice"])

    lines.extend(
        _corner_deck_control_lines(
            point=point,
            analysis=analysis,
            measurements_spec=measurements_spec,
            raw_path=raw_path,
            osdi_preload=osdi_preload,
            save_mode=save_mode,
            save_vectors=save_vectors,
            complex_vectors=complex_vectors,
            corner_dir=os.path.dirname(os.path.abspath(deck_path)),
        )
    )
    lines.append(".end")

    with open(deck_path, "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")


def _param_reset_lines(point: CornerPoint) -> list[str]:
    """Issue #3035: `alterparam` rewrites the named top-level `.param`
    defaults, and ONE `reset` re-parses the deck so every expression that
    reads them is re-evaluated. Everything after these lines (`save all`, the
    supply `alter`s, the analysis) must come after the reset, because reset
    reloads the circuit and drops instance-level `alter`s. The `.temp`/
    `.options` cards are file-scope and survive the reload. Empty for a
    point without a parameter axis."""
    if not point.params:
        return []
    return [
        *(
            f"alterparam {name}={_format_param_value(value)}"
            for name, value in sorted(point.params.items())
        ),
        "reset",
    ]


def _save_control_lines(
    measurements_spec: list[dict[str, Any]],
    raw_path: str | None,
    save_mode: str,
    save_vectors: tuple[str, ...],
) -> list[str]:
    """The ``save`` card :func:`_corner_deck_control_lines` emits (step 2 of
    its ordering contract), or no lines when the corner has nothing to save
    or ``save_mode`` opts out."""
    if save_mode != DEFAULT_SAVE_MODE or not (
        measurements_spec or raw_path is not None
    ):
        return []
    if save_vectors:
        return [
            "save " + " ".join(_required_save_vectors(save_vectors, measurements_spec))
        ]
    return ["save all"]


def _supply_alter_lines(point: CornerPoint) -> list[str]:
    """One ``alter`` per swept supply rail, skipping the sources rescaled in
    the corner's netlist copy instead (issue #2902)."""
    return [
        f"alter {key}={value}"
        for key, value in sorted(point.supply_v.items())
        if key not in point.supply_waveforms
    ]


def _corner_deck_control_lines(
    *,
    point: CornerPoint,
    analysis: dict[str, Any],
    measurements_spec: list[dict[str, Any]],
    raw_path: str | None,
    osdi_preload: tuple[str, ...],
    save_mode: str = DEFAULT_SAVE_MODE,
    save_vectors: tuple[str, ...] = (),
    complex_vectors: tuple[str, ...] = (),
    corner_dir: str = ".",
) -> list[str]:
    """The ``.control``/``.endc`` block of :func:`_write_corner_deck`'s
    generated deck, in emission order -- this function owns the ordering
    contract between the commands inside it, which is where two otherwise
    independent features (``options.osdi_preload``, issue #2513, and the
    default ``save all``, issue #2521) both write.

    Emission order, and why:

    1. ``pre_osdi <lib>`` per ``options.osdi_preload`` entry, in declared
       order. Order-free in practice -- a ``pre_``-prefixed command is
       hoisted out of the block and executed before the circuit is even
       parsed, wherever it sits -- but kept first so the preload reads as
       visibly ahead of every ``alter`` and the analysis command.
    2. ``save all``, when this corner has anything to save. A netlist
       body's own ``.save`` card *restricts* ngspice's saved node/branch
       set -- schematic netlisters emit one routinely (e.g. for a
       supply-current probe), and until issue #2521 that restriction won
       silently, starving every ``.meas`` card and any ``options.waveforms``
       capture whose signal fell outside it (both surfaced identically as a
       plain "produced no value" measurement error, indistinguishable from
       a genuine measurement failure). ngspice applies ``save`` cards in
       the order encountered, so this one only has to land *after* the
       ``.include`` (already emitted at file scope, by the caller) to
       supersede the body's, and *before* the ``alter``/analysis lines that
       read the saved set -- which the ``pre_osdi`` lines above it cannot
       affect either way. Emitted only when this corner actually has
       something to save (``measurements_spec`` or ``raw_path``, mirroring
       ``measurements[]``/``options.waveforms`` at the request level), so
       the probe-deck caller and the historical
       no-measurement/no-waveform request stay byte-identical to
       pre-#2521 behavior. ``save_mode="netlist"`` (``options.save_mode``,
       issue #2732) suppresses the card unconditionally, leaving the
       included netlist's own ``.save`` cards in charge of the resident
       vector set -- the request-level opt-out for a caller who wants to
       trade completeness for memory on a large transient deck; see
       docs/cli/sim.md's "Saved signal set" section.
    3. One ``alter`` per supply rail, then the analysis command, wrapped by
       the ASCII-rawfile ``set filetype``/``write`` pair when a rawfile was
       requested.
    4. One ``let <name> = <expr>`` / ``print <name>`` pair per
       ``measurements[].expr`` entry (issue #2533), in declared order, then
       ``quit``.

       **After the analysis**, necessarily: the expression reads the vectors
       the analysis produced. **After the rawfile ``write``**, deliberately:
       ``let`` adds its result to the current plot and ``write`` dumps every
       vector in that plot, so evaluating before the write would silently
       add the derived vectors to the ``options.waveforms`` artifact -- an
       ``expr`` measurement must not change what the rawfile/waveform
       artifact contains. In *declared* order, so a later expression may
       reference an earlier one's name (see :func:`_expr_measurements`).

       One ``print`` per measurement rather than a single multi-vector
       ``print``: ngspice renders a multi-vector ``print`` as an indexed
       table, whereas a lone scalar prints as ``<name> = <value>`` -- the
       exact shape :data:`_MEAS_VALUE_RE` harvests (verified against ngspice
       46). A non-scalar expression prints a table too, which yields no
       value for that name; :func:`_run_corner` turns that into the
       measurement's own ``status: "error"`` with an ``expr``-specific hint.

       No lines at all when no ``expr`` is declared, so every pre-#2533
       request's deck is byte-identical.
    5. Per ``measurements[].crossings`` entry (issue #2868), a
       ``let __klt_xing_<k> = <signal>`` / ``write <corner_dir>/crossings_<k>.raw``
       pair (:func:`klayout_tools.sim_crossings.control_lines`), last before
       ``quit`` -- after the waveform ``write`` for the same reason as the
       ``expr`` lines, and after them so a signal may read an ``expr``
       vector. ``set filetype=ascii`` is emitted before the analysis when
       any are declared (the rawfile reader is ASCII-only). None of this
       appears without a crossings entry.
    6. Per ``measurements[].energy`` entry (issue #3033), a ``let`` of the
       voltage vector, a ``let`` of ``i(<source>)`` and one ``write
       <corner_dir>/energy_<k>.raw`` of both
       (:func:`klayout_tools.sim_energy.control_lines`), after the crossings
       lines; same ``set filetype=ascii`` rule. None of this appears without
       an energy entry.
    7. Per ``measurements[].cycle_mean`` entry (issue #2947), two ``let``
       lines and one ``write`` of both vectors
       (:func:`klayout_tools.sim_cycle_mean.control_lines`), after the
       energy lines; same ``set filetype=ascii`` rule. None of this
       appears without a cycle_mean entry.
    8. Per unique ``measurements[].transfer`` ``num``/``den`` pair (issue
       #2923), two ``let`` lines and one ``write`` of both complex vectors
       (:func:`klayout_tools.sim_transfer_meas.control_lines`), after the
       cycle_mean lines; same ``set filetype=ascii`` rule. None of this
       appears without a transfer entry.
    9. Per unique ``measurements[].snm`` ``x``/``y1``/``y2`` triple (issue
       #3895), three ``let`` lines and one ``write`` of the vectors
       (:func:`klayout_tools.sim_snm_meas.control_lines`), after the transfer
       lines; same ``set filetype=ascii`` rule. None of this appears without
       an snm entry.

    An ``analysis_steps[]`` request (issue #2482) replaces step 3's analysis
    and 4's ``expr`` lines with the ordered step sequence
    :func:`klayout_tools.sim_steps.control_lines` generates -- after the same
    ``pre_osdi``/``save all``/corner-supply ``alter`` prefix, so every step
    solves the corner the prefix set up. (``options.waveforms`` is refused
    for a step sequence, so ``raw_path`` is always ``None`` there.)
    """
    lines = [".control"]
    for path in osdi_preload:
        lines.append(f"pre_osdi {path}")
    # Issue #3035: the parameter reset must come first -- `reset` reloads the
    # circuit, so everything after it (save/alter/analysis) re-applies on top.
    prefix: list[str] = _param_reset_lines(point)
    prefix.extend(
        _save_control_lines(measurements_spec, raw_path, save_mode, save_vectors)
    )
    prefix.extend(_supply_alter_lines(point))
    lines.extend(prefix)
    if sim_steps.is_steps_analysis(analysis):
        # Issue #3103: a `reset: true` step re-emits this same prefix after
        # ngspice's `reset` (which re-parses the circuit, reverting every
        # `alter` and dropping the saved set).
        lines.extend(
            sim_steps.control_lines(analysis, measurements_spec, reset_prefix=prefix)
        )
        lines.extend(["quit", ".endc"])
        return lines
    if sim_passes.is_passes_analysis(analysis):
        # Issue #3159: one `alter`/`setplot new`/`ac`/`write` group per
        # pass; the corner prefix above already set the corner up.
        lines.extend(sim_passes.control_lines(analysis, corner_dir, measurements_spec))
        lines.extend(["quit", ".endc"])
        return lines
    if sim_twoport.is_twoport(analysis):
        # Issue #3338: one `alter`/`setplot new`/`ac`/`write` group per port.
        lines.extend(sim_twoport.control_lines(analysis, corner_dir))
        lines.extend(["quit", ".endc"])
        return lines
    crossings_lines = sim_crossings.control_lines(measurements_spec, corner_dir)
    energy_lines = sim_energy.control_lines(measurements_spec, corner_dir)
    cycle_mean_lines = sim_cycle_mean.control_lines(measurements_spec, corner_dir)
    transfer_lines = sim_transfer_meas.control_lines(measurements_spec, corner_dir)
    snm_lines = sim_snm_meas.control_lines(measurements_spec, corner_dir)
    eye_lines = sim_eye.control_lines(measurements_spec, corner_dir)
    search_lines = sim_search.control_lines(measurements_spec)
    complex_vector_lines = sim_complex_vectors.control_lines(
        complex_vectors, corner_dir
    )
    if (
        raw_path is not None
        or crossings_lines
        or energy_lines
        or cycle_mean_lines
        or transfer_lines
        or snm_lines
        or eye_lines
        or complex_vector_lines
    ):
        lines.append("set filetype=ascii")
    lines.append(f"{analysis['kind']} {_ic_analysis_args(point, analysis['args'])}")
    if raw_path is not None:
        lines.append(f"write {raw_path}")
    if analysis.get("linearize") is True:
        # Issue #4014: resample onto a uniform grid, after any rawfile
        # `write` (which keeps the solver's own points) and before the
        # `expr` lets, so time-weighted sums are not biased by the
        # adaptive timestep distribution.
        lines.append("linearize")
    expr_specs = _expr_measurements(measurements_spec)
    if expr_specs and analysis["kind"] == "noise":
        # Issue #2893: after `noise`, ngspice's current plot is the integrated
        # one (noise2: onoise_total/inoise_total); the spectra live in noise1.
        # Alias them into the current plot (after any `write`, so the rawfile
        # is unchanged) so bare names resolve while totals stay bare too.
        lines.extend(f"let {vec} = noise1.{vec}" for vec in NOISE_SPECTRUM_VECTORS)
    for spec in expr_specs:
        lines.extend(spec.get("expr_prelude", ()))
        lines.append(f"let {spec['name']} = {spec['expr']}")
        lines.append(f"print {spec['name']}")
    lines.extend(crossings_lines)
    lines.extend(energy_lines)
    lines.extend(cycle_mean_lines)
    lines.extend(transfer_lines)
    lines.extend(snm_lines)
    lines.extend(eye_lines)
    lines.extend(complex_vector_lines)
    # Issue #4121: last, so no later line reads the main analysis' plot after
    # the search has moved ngspice's current plot.
    lines.extend(search_lines)
    lines.append("quit")
    lines.append(".endc")
    return lines


#: Spectrum vectors of ngspice's first noise plot, aliased by bare name into
#: the current (totals) plot for ``expr`` measurements (issue #2893).
NOISE_SPECTRUM_VECTORS = ("inoise_spectrum", "onoise_spectrum")


def _write_xyce_deck(
    *,
    deck_path: str,
    netlist_path: str,
    models_lib: str | None,
    point: CornerPoint,
    analysis: dict[str, Any],
    measurements_spec: list[dict[str, Any]],
) -> None:
    """Generate the corner-specific Xyce deck (issue #2016).

    Deliberately *not* a transcription of ``_write_corner_deck``: Xyce is a
    from-scratch SPICE implementation, and several cards the ngspice deck
    relies on parse differently or not at all (all verified against
    XyceNF 7.10.0 while building this):

    - No ``.control`` block and no ``alter``: the analysis card is a plain
      top-level dot card, and per-corner supply patching is impossible --
      which is why ``run_sim`` refuses ``corners.supply_v`` for this engine.
    - ``.temp`` is silently ignored (the device report still shows the
      27 C default afterwards), so temperature rides
      ``.options device temp=<T>`` -- verified to move device physics (a
      diode's forward drop at fixed current dropped ~104 mV from 27 C to
      85 C) where ``.temp`` provably did not.
    - ``.lib <file> <section>`` is supported, but the library file's
      sections must close with a *named* ``.endl <section>`` (a bare
      ``.endl`` is a parse error inside an included library -- divergence
      from ngspice, documented in docs/design/xyce-oracle.md).
    - The request's ``.meas`` cards are inserted verbatim: Xyce accepts the
      ``.meas`` abbreviation, and its ``.MEASURE`` implements the same
      DC/AC/TRAN/NOISE set ngspice's does (no ``.measure op`` -- same
      ``_validate_meas_card`` gate as ngspice).
    - ``measurements[].expr`` (issue #2533) has no Xyce form at all, for the
      same reason as the first bullet: it is emitted as ngspice
      ``let``/``print`` commands inside a ``.control`` block this deck does
      not have. ``_enforce_xyce_support_boundary`` refuses such a request
      up front by name; the loop below skips ``expr`` entries only so this
      writer stays total rather than raising ``KeyError`` if a future caller
      reaches it directly.

    Monte Carlo seed cards are never emitted: ``run_sim`` refuses
    ``monte_carlo`` for this engine before any deck exists.
    """
    lines = ["* klt sim -- generated Xyce corner deck, do not edit"]
    # Same `.lib` cards as the ngspice deck, from the same helper (multi-
    # section bundles and issue #2522's per-section libraries included) --
    # the oracle must read the same models as the run it cross-validates.
    lines.extend(_corner_lib_lines(point, models_lib))
    lines.append(f".include {netlist_path}")
    # Xyce's documented circuit-wide device-temperature control; `.temp`
    # would be a silent no-op (see this docstring's list).
    lines.append(f".options device temp={point.temperature_c}")
    # The analysis card precedes the measurement cards (the order every
    # probe against XyceNF 7.10.0 used while building this). Top-level dot
    # card, with the dot -- there is no `.control` block for a bare
    # `dc`/`tran`/`op` command to live in.
    lines.append("." + f"{analysis['kind']} {analysis['args']}".strip())
    for spec in measurements_spec:
        if spec.get("spice") is not None:
            lines.append(spec["spice"])
    lines.append(".end")

    with open(deck_path, "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")


#: Bound on the recorded ``--version`` banner; matches the worker capture's
#: ``WORKER_CAPTURE_BANNER_MAX`` so the two sides compare like for like.
_ENGINE_BANNER_MAX = 2000
_ENGINE_BANNER_TIMEOUT_S = 10


def _engine_build_with_comparison(
    ngspice_binary: str | None,
    remote_environment: dict[str, Any] | None,
    backend: str,
    staged_model_inputs: list[dict[str, Any]] | None = None,
    request: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Probe the local build and, for a batch sweep, compare it with each
    worker's build in ``remote_environment`` (issue #3095), and the staged
    model digests with what each worker hashed (issue #4020)."""
    engine_build = _probe_engine_build(ngspice_binary)
    if remote_environment is not None and backend == "batch":
        attach_engine_build_comparison(remote_environment, engine_build)
        attach_staged_model_comparison(remote_environment, staged_model_inputs, request)
    return engine_build


def _probe_engine_build(ngspice_binary: str | None) -> dict[str, Any] | None:
    """``environment.engine_build`` (issue #3095): the local ngspice build
    identity -- ``--version`` banner plus binary sha256 -- probed once per
    sweep. ``engine_version`` is the major version only and two builds of one
    major can differ in options (FFTW vs. padded FFT). ``None`` for engines
    without a probe (xyce). A failed probe never fails the run: the banner is
    ``None`` and ``probe`` / ``reason`` say why."""
    if ngspice_binary is None:
        return None
    build: dict[str, Any] = {
        "version_banner": None,
        "binary_sha256": None,
        "probe": "ok",
        "reason": None,
    }
    try:
        build["binary_sha256"] = sha256_file(ngspice_binary)
    except OSError:
        pass
    try:
        proc = subprocess.run(
            [ngspice_binary, "--version"],
            capture_output=True,
            text=False,
            timeout=_ENGINE_BANNER_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        build.update(
            probe="timeout", reason=f"--version exceeded {_ENGINE_BANNER_TIMEOUT_S}s"
        )
        return build
    except Exception as exc:  # the probe must never fail the sweep
        build.update(probe="error", reason=f"could not run --version: {exc}"[:200])
        return build
    raw = proc.stdout or proc.stderr or b""
    text: str = (
        raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)
    )[: _ENGINE_BANNER_MAX * 4]
    banner = "".join(ch if (ch >= " " or ch in "\n\t") else "?" for ch in text).strip()[
        :_ENGINE_BANNER_MAX
    ]
    if proc.returncode != 0 or not banner:
        build.update(
            probe="error",
            reason=(
                f"--version exited {proc.returncode}"
                if proc.returncode != 0
                else "--version printed no banner"
            ),
        )
        return build
    build["version_banner"] = banner
    return build


def _extract_engine_version(stdout: str) -> str | None:
    match = _ENGINE_VERSION_RE.search(stdout or "")
    return match.group(1) if match else None


# --------------------------------------------------------------------------- #
# Per-corner Xyce invocation (issue #2016)
# --------------------------------------------------------------------------- #

#: The Xyce batch binary. PATH-discovered like ``ngspice`` -- the binary is
#: an external oracle dependency (see scripts/install-xyce.sh), never a
#: runtime dependency of ``klt`` itself. Tests monkeypatch this name (the
#: same seam the ngspice path's literal ``"ngspice"`` argv entry provides).
XYCE_BINARY = "Xyce"

#: Xyce stamps ``***** This is version XyceNF Release 7.10.0`` (the NF/NORAD
#: distribution labels itself ``XyceNF``; the open-source build reads
#: ``Xyce Release ...``) into the log file ``-l`` writes -- unlike ngspice,
#: whose banner lands on stdout. Version is read from the log for exactly
#: that reason; see ``_run_corner``.
_XYCE_VERSION_RE = re.compile(
    r"^\*+ This is version Xyce(?:NF)? Release ([\w.]+)", re.MULTILINE
)

#: Xyce's own "this run never completed" trailer (``Simulation aborted due
#: to error. There are N MSG_FATAL ...``) -- the abort-marker
#: `_recovered_from_stepping` checks for this engine.
_XYCE_SIMULATION_ABORTED_RE = re.compile(
    r"Simulation aborted due to error", re.IGNORECASE
)

#: A successful Xyce ``.measure`` scalar line, in the "Measure Functions"
#: log section. Xyce upper-cases the measurement name and appends
#: analysis-specific trailers after the value, all of which are ignored:
#: ::
#:
#:     VMAX = 9.691320e-01 at time = 6.544640e-09
#:     VOUT_MID = 5.000000e-01 for AT = 2.500000e+00
#:     TPHL = 1.391548e-09 with targ = 3.641548e-09 and trig = 2.250000e-09
#:     VOUT_FINAL = 9.000024e-03
_XYCE_MEAS_VALUE_RE = re.compile(
    r"^([A-Za-z_]\w*)\s*=\s*([+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)"
    r"(?:\s|$)"
)

#: A failed Xyce ``.measure`` line: ``TPHL = FAILED with targ = not found
#: and trig = not found``. Like ngspice's ``... failed!``, the failed name
#: is excluded from the parsed values (the caller treats an absent name as
#: "no value" -> ``status: "error"``).
_XYCE_MEAS_FAILED_RE = re.compile(r"^([A-Za-z_]\w*)\s*=\s*FAILED\b", re.IGNORECASE)


def _extract_xyce_version(log_text: str) -> str | None:
    match = _XYCE_VERSION_RE.search(log_text or "")
    return match.group(1) if match else None


def _xyce_measure_section(log_text: str) -> str:
    """The text of Xyce's ``***** Measure Functions *****`` log section, or
    ``""`` when the run produced none (a section-scoped parse, rather than
    scanning the whole log, so unrelated ``name = value``-shaped lines
    elsewhere in Xyce's verbose output can never masquerade as a
    measurement). The section ends at the next ``*****`` banner.
    """
    marker = log_text.find("Measure Functions")
    if marker < 0:
        return ""
    rest = log_text[marker:]
    banner = rest.find("\n*****")
    return rest[:banner] if banner >= 0 else rest


def _parse_xyce_measurements(log_text: str) -> dict[str, float]:
    """Parse ``.measure`` scalar results from the Xyce log's "Measure
    Functions" section.

    Xyce upper-cases measurement names in its own output; the returned
    dict's keys are lower-cased so the caller's case-insensitive lookup
    (see ``_run_corner``) can match the request's ``measurements[].name``
    verbatim. A failed measurement (``NAME = FAILED ...``) is excluded, the
    same "absent means error" convention `_parse_measurements` follows for
    ngspice's ``... failed!`` lines.
    """
    values: dict[str, float] = {}
    failed: set[str] = set()
    for raw_line in _xyce_measure_section(log_text).splitlines():
        line = raw_line.strip()
        if not line:
            continue
        failed_match = _XYCE_MEAS_FAILED_RE.match(line)
        if failed_match:
            failed.add(failed_match.group(1).lower())
            continue
        value_match = _XYCE_MEAS_VALUE_RE.match(line)
        if value_match:
            name, value = value_match.groups()
            values[name.lower()] = float(value)
    for name in failed:
        values.pop(name, None)
    return values


#: Ordered diagnostic classifiers for the Xyce log -- the Xyce-native
#: counterpart of ``_DIAGNOSTIC_PATTERNS``, matched against what XyceNF
#: 7.10.0 actually emits (not transcriptions of ngspice's text). Order
#: matters, most specific first: a fatal netlist parse error usually also
#: produces a trailing abort banner, and the more specific code should win.
#:
#: - ``Netlist error``: Xyce's fatal parse/topology failures (e.g. the
#:   bare-``.endl`` library syntax divergence this engine's docs record).
#: - ``Numerically singular matrix found by Amesos``: Xyce's singular-matrix
#:   narration (it "returns zero solution to nonlinear solver!" and
#:   continues) -- downgradeable per ``_recovered_from_stepping`` when every
#:   requested measurement still came back.
#: - ``Time step too small``: the classic transient nonconvergence death.
#: - ``Simulation aborted due to error``: the generic fatal trailer, for
#:   solver/method failures none of the more specific patterns caught.
_XYCE_DIAGNOSTIC_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("netlist", re.compile(r"^\s*Netlist error", re.IGNORECASE | re.MULTILINE)),
    (
        "singular_matrix",
        re.compile(r"Numerically singular matrix", re.IGNORECASE),
    ),
    (
        "nonconvergence",
        re.compile(r"Time step too small|failed to converge", re.IGNORECASE),
    ),
    ("unknown", re.compile(r"Simulation aborted due to error", re.IGNORECASE)),
)


def _classify_xyce_diagnostics(log_text: str) -> list[dict[str, str]]:
    """Classify structured diagnostics from a Xyce log, per
    ``_XYCE_DIAGNOSTIC_PATTERNS`` (one match per code, same shape and
    severity conventions as :func:`_classify_diagnostics`).
    """
    diagnostics: list[dict[str, str]] = []
    for code, pattern in _XYCE_DIAGNOSTIC_PATTERNS:
        match = pattern.search(log_text)
        if match is None:
            continue
        diagnostics.append(
            {
                "severity": "error",
                "code": code,
                "message": _line_containing(log_text, match.start()),
            }
        )
    return diagnostics


# --------------------------------------------------------------------------- #
# Timeout-budget preflight (issue #1686)
# --------------------------------------------------------------------------- #
#
# NOTE on scope: this issue's first two acceptance criteria (a per-corner
# `reached_s`/`fraction` progress diagnostic sourced from ngspice's `-r`
# rawfile, and a dispatch-time fail-fast abort built on it) turned out not
# to be safely implementable against this module's actual per-corner deck
# shape -- verified empirically while building this:
#
# 1. ngspice's command-line `-r <path>` only streams incrementally when the
#    run is "autorun" (a bare top-level `.tran` card, no `.control` block).
#    Every real corner deck :func:`_write_corner_deck` generates wraps the
#    analysis in a `.control` block (for the `alter` supply-override
#    cards), and *any* `.control` block -- even one that never calls
#    `write` -- silently disables `-r`'s streaming behavior entirely: a
#    `.control`-wrapped `tran` killed mid-run (`SIGTERM` or `SIGKILL`)
#    leaves no rawfile at all, while the exact same analysis as a bare
#    top-level card leaves a large partial rawfile behind. So `-r` would
#    report `reached_s: null` on literally every real invocation, not "most".
# 2. The seemingly-obvious workaround -- periodic `stop when time > <t>` /
#    `write` / `resume` checkpoints inside the `.control` block, without an
#    autorun card -- corrupts `.meas` correctness instead: ngspice
#    evaluates (and prints) `.meas` results once, tied to the *first*
#    `doAnalyses` completion (including an early `stop` pause), not to the
#    analysis's true final completion. A `.meas ... AT=<t>` beyond the
#    first checkpoint fails with "out of interval" at that first pause and
#    is never re-evaluated once the run actually finishes -- silently
#    breaking measurement extraction for the overwhelming majority of real
#    (non-timeout) sweeps, which is unacceptable.
#
# Both are real, reproducible findings (not merely "harder than expected"),
# so a prior Builder pass shipped only the parts of the issue's acceptance
# criteria that were safe at the time -- the coarse pre-grid preflight
# heuristic below -- and filed issue #1694 for the harder,
# deck-architecture-level question that `reached_s` recovery actually
# raises. Issue #1694 -- see docs/design/sim-corner-reached-s.md for the
# design decision -- ships the dispatch-time fail-fast half of that
# acceptance criterion via a *different* mechanism than the blocked
# per-corner `reached_s` recovery: a two-pass calibration probe (this
# section's "Two-pass fail-fast probe" below), run once per grid rather
# than instrumenting every corner. A corner actually dispatched and then
# killed by its own `options.timeout_s` still reports `reached_s: null`
# in its `timeout` diagnostic -- that per-corner recovery gap is
# unchanged by #1694 and remains exactly as described above.
# --------------------------------------------------------------------------- #


def _parse_spice_seconds(raw: str) -> float | None:
    """Parse a SPICE time literal (``100u``, ``1e-4``, ``5n``) to seconds,
    via the shared :mod:`._spice_number` engineering-notation table, the
    same one :func:`_parse_width_um` uses for geometry. Unlike a bare
    geometry literal, a SPICE time literal has no ambiguous "bare" case --
    seconds is always the implicit base unit -- so, unlike
    :func:`_parse_width_um`, no additional unit conversion is applied on top
    of the suffix multiplier. Returns ``None`` for anything that is not a
    plain numeric literal.
    """
    return spice_number.parse(raw)


#: Deliberately generous floor used only by :func:`_preflight_timeout_warning`
#: -- NOT a real per-engine throughput estimate. ngspice's actual rate
#: depends heavily on circuit size/complexity/convergence behavior and is
#: not knowable without running it (the incident behind issue #1686
#: observed ~18us of simulated time per wall-clock *hour*, three orders of
#: magnitude below this). This floor exists only to catch the most
#: obviously-impossible requests before the grid starts -- see this
#: module's "Timeout-budget preflight" section header for why this
#: coarse, advisory-only heuristic is what this issue ships, rather than
#: the more impactful dispatch-time fail-fast guard it also asked for.
_PREFLIGHT_MIN_TIMEPOINTS_PER_S = 50.0


def _parse_tran_window(
    analysis: dict[str, Any],
) -> tuple[str, str, float, float] | None:
    """Parse a ``tran`` analysis's own ``step``/``window`` tokens (the first
    two space-separated tokens of ``analysis['args']``), shared by
    :func:`_preflight_timeout_warning` (issue #1686) and
    :func:`_run_fail_fast_probe` (issue #1694) -- both need the same "is
    this a `tran` analysis with parseable timing tokens" gate.

    Returns ``(step_token, window_token, step_s, window_s)`` (the original
    token strings, for messages, alongside their parsed seconds), or
    ``None`` when ``analysis['kind']`` isn't ``"tran"`` or the tokens can't
    be parsed as positive SPICE time literals.
    """
    if analysis.get("kind") != "tran":
        return None
    tokens = (analysis.get("args") or "").split()
    if len(tokens) < 2:
        return None
    step_s = _parse_spice_seconds(tokens[0])
    window_s = _parse_spice_seconds(tokens[1])
    if step_s is None or window_s is None or step_s <= 0 or window_s <= 0:
        return None
    return tokens[0], tokens[1], step_s, window_s


def _preflight_timeout_warning(
    *, analysis: dict[str, Any], timeout_s: float
) -> str | None:
    """Best-effort, pre-grid sanity check of ``options.timeout_s`` against a
    ``tran`` analysis's own declared step/window (issue #1686, third
    acceptance criterion). Never raises and never blocks the sweep -- only
    returns an advisory string (surfaced via
    ``environment.timeout_preflight_warning``) when the declared timeout
    looks implausible even under the deliberately generous per-timepoint
    floor above.

    Scoped to ``kind == "tran"`` (the only analysis with a "window of
    simulated time" concept); returns ``None`` for anything else, or when
    ``args``'s step/window tokens cannot be parsed (see
    :func:`_parse_tran_window`).

    This is a static, structural check (step/window vs. a generous
    timepoints-per-second floor) -- it never runs ``ngspice`` and so cannot
    know this deck's *actual* rate. :func:`_run_fail_fast_probe` (issue
    #1694) is the complementary, evidence-based check: it actually measures
    a rate before the grid runs, and can abort the sweep outright, not just
    warn.
    """
    parsed = _parse_tran_window(analysis)
    if parsed is None:
        return None
    step_token, window_token, step_s, window_s = parsed
    timepoints = window_s / step_s
    min_plausible_s = timepoints / _PREFLIGHT_MIN_TIMEPOINTS_PER_S
    if timeout_s >= min_plausible_s:
        return None
    return (
        f"options.timeout_s ({timeout_s:g}s) looks implausible for a tran "
        f"analysis with ~{timepoints:.3g} timepoints ({step_token} step, "
        f"{window_token} window) -- even {_PREFLIGHT_MIN_TIMEPOINTS_PER_S:.0f} "
        "timepoints/s (a generous floor, not a real engine-rate estimate) "
        f"would need {min_plausible_s:.3g}s per corner. This is only a "
        "coarse, advisory pre-grid heuristic -- it does not block the "
        "sweep, and a plausible-looking budget can still turn out to be "
        "too short in practice."
    )


# --------------------------------------------------------------------------- #
# Two-pass fail-fast probe (issue #1694)
# --------------------------------------------------------------------------- #
#
# See docs/design/sim-corner-reached-s.md for the design decision this
# implements. Summary: rather than recovering a real per-corner `reached_s`
# (blocked -- see the "Timeout-budget preflight" section above), run one
# short, bounded calibration `tran` slice on the grid's first corner before
# dispatching any real corner, measure that slice's own
# simulated-seconds-per-wall-clock-second rate, and use it to estimate
# whether `options.timeout_s` can plausibly cover the *full* analysis
# window. When the estimate says no -- by more than `_PROBE_ABORT_MARGIN`,
# to absorb the probe's own sampling noise -- the entire grid is aborted
# before a single real corner is dispatched, each reported with an
# *estimated* `reached_s`/`fraction` derived from the measured rate (never
# a real per-corner recovery, and clearly distinct from one -- see
# `_unrun_corner_report`'s docstring). This is the guard that would have
# caught the motivating incident (2AMLogic/sky130-pll#103: 45 corners each
# burning their full 10800s `timeout_s` while covering only ~54% of a
# widened analysis window) after the very first corner's probe, rather than
# after all 45 corners had already run to completion.
#
# Opt-in via `options.fail_fast_probe` (default `False`, like
# `options.resume`/`options.wall_clock_budget_s`) -- see `run_sim`'s
# docstring. Only engaged for `backend in ("local", "local-parallel")`
# (including any `hosts > 1` shard built on them); see `_run_remote`'s
# docstring for why the `remote` backend does not probe.
# --------------------------------------------------------------------------- #

#: Fraction of the full `tran` analysis window used for the calibration
#: probe's own (much shorter) `tran` slice. Small enough that the probe
#: itself never becomes a material fraction of the grid's total cost, large
#: enough to be a representative sample of this corner/deck's own
#: convergence behaviour rather than pure startup-transient noise.
_PROBE_WINDOW_FRACTION = 0.02

#: Bounds on the probe's own wall-clock budget, independent of the corner's
#: real `options.timeout_s`: a fraction of it, floored so a tiny `timeout_s`
#: still gives the probe a moment to run, and capped so a huge `timeout_s`
#: does not make the probe itself an hours-long liability -- the entire
#: point of probing first is to spend a small, predictable amount of
#: wall-clock time finding out whether `timeout_s` is achievable, not to
#: recreate the same problem at a smaller scale.
_PROBE_TIMEOUT_FRACTION = 0.1
_PROBE_TIMEOUT_FLOOR_S = 1.0
_PROBE_TIMEOUT_CAP_S = 60.0

#: How much worse than `options.timeout_s` the probe's estimate must say the
#: full run would be before the fail-fast actually aborts the grid. A
#: margin above 1.0 absorbs the probe's own sampling noise (a short,
#: possibly-transient-heavy slice at the start of a `tran` can under- or
#: over-estimate the deck's steady-state rate) without masking the kind of
#: gross mismatch the motivating incident showed: 45 corners that only
#: reached ~54% of the window in their full budget imply the true run
#: needed roughly 1.85x `timeout_s`, comfortably past this margin.
_PROBE_ABORT_MARGIN = 1.5


def _probe_window_and_timeout(window_s: float, timeout_s: float) -> tuple[float, float]:
    """The calibration probe's own bounded ``(window_s, timeout_s)`` pair,
    derived from the *real* analysis window and per-corner timeout -- see
    this section's module-level constants for the rationale.
    """
    probe_window_s = window_s * _PROBE_WINDOW_FRACTION
    probe_timeout_s = min(
        max(timeout_s * _PROBE_TIMEOUT_FRACTION, _PROBE_TIMEOUT_FLOOR_S),
        _PROBE_TIMEOUT_CAP_S,
        timeout_s,
    )
    return probe_window_s, probe_timeout_s


def _run_calibration_probe(
    *,
    point: CornerPoint,
    netlist_path: str,
    models_lib: str | None,
    step_token: str,
    probe_window_s: float,
    probe_timeout_s: float,
    artifacts_dir: str,
    keep_artifacts: bool,
    ngspice_binary: str | None = None,
    ngspice_init: tuple[str, ...] = (),
    osdi_preload: tuple[str, ...] = (),
    save_mode: str = DEFAULT_SAVE_MODE,
    ngspice_options: dict[str, Any] | None = None,
    stimulus_lines: tuple[str, ...] = (),
) -> dict[str, Any]:
    """Run one short, bounded ``tran`` slice -- the calibration half of the
    two-pass probe model -- and report how it went.

    Reuses :func:`_write_corner_deck` unmodified for the deck shape (same
    ``alter`` cards, same ``.control`` wrapper -- a probe on a corner whose
    supply override changes convergence behaviour needs that same override
    active to be representative) with an empty ``measurements_spec`` (the
    probe's own pass/fail is irrelevant, and skipping ``.meas`` cards avoids
    the "out of interval" corruption this module's "Timeout-budget
    preflight" section documents for a truncated window) and a scaled-down
    ``tran`` window in place of the real ``analysis``.

    Returns ``{"wall_s", "timed_out", "error"}``; never raises. A timed-out
    probe (``timed_out=True``) still yields a usable rate estimate at the
    call site -- an *upper bound*, since ``probe_window_s`` of simulated
    time provably was not reached within ``probe_timeout_s`` of wall time,
    even though (mirroring this module's core finding) exactly how far it
    did get cannot be recovered from a ``.control``-wrapped run killed
    mid-flight. ``error`` (non-``None``) means the probe itself is
    inconclusive (could not launch ngspice, or its own shortened analysis
    did not complete cleanly) -- the caller treats that exactly like "no
    evidence either way" and skips the fail-fast check entirely, never
    aborting a grid on untrustworthy evidence.

    ``ngspice_binary`` (issue #2423) is the resolved absolute path
    ``_resolve_ngspice_binary`` settled on for this sweep -- the probe uses
    the same binary the real corners will, so a ``None``/broken-override
    misconfiguration surfaces at the (cheap) probe stage rather than only
    after every real corner is dispatched.

    ``ngspice_init`` (issue #2520, the already-validated
    ``options.ngspice_init`` lines -- see :func:`_validate_ngspice_init`) is
    materialized as ``probe_dir/.spiceinit`` the same way :func:`_run_corner`
    does for a real corner, and the probe's own ``subprocess.run`` call
    passes ``cwd=probe_dir`` so it is actually read. The probe otherwise
    runs the same compatibility mode a real corner would, and this issue's
    other half (the missing ``cwd=``) applied here too -- without it, the
    probe's own convergence behaviour (and thus its rate estimate) would
    silently diverge from the real corners it is meant to be representative
    of whenever a caller sets ``options.ngspice_init``.
    """
    probe_dir = (
        os.path.join(artifacts_dir, f"{point.slug}__probe")
        if keep_artifacts
        else _tmp_work_dir()
    )
    if keep_artifacts:
        os.makedirs(probe_dir, exist_ok=True)
    deck_path = os.path.join(probe_dir, "probe.cir")
    log_path = os.path.join(probe_dir, "probe.log")

    _write_corner_deck(
        deck_path=deck_path,
        netlist_path=netlist_path,
        models_lib=models_lib,
        point=point,
        analysis={"kind": "tran", "args": f"{step_token} {probe_window_s:.9g}"},
        measurements_spec=[],
        raw_path=None,
        osdi_preload=osdi_preload,
        save_mode=save_mode,
        ngspice_options=ngspice_options,
        stimulus_lines=stimulus_lines,
    )
    _write_spiceinit(probe_dir, ngspice_init)

    started = time.monotonic()
    timed_out = False
    error: str | None = None
    try:
        subprocess.run(
            [ngspice_binary or "ngspice", "-b", deck_path, "-o", log_path],
            capture_output=True,
            text=True,
            timeout=probe_timeout_s,
            cwd=probe_dir,
        )
    except subprocess.TimeoutExpired:
        timed_out = True
    except FileNotFoundError as exc:
        error = f"could not launch ngspice for the calibration probe: {exc}"
    wall_s = round(time.monotonic() - started, 3)

    if not timed_out and error is None:
        log_text = _read_log_file(log_path)
        if _SIMULATION_ABORTED_RE.search(log_text):
            # The probe's own (shortened) analysis never completed cleanly
            # -- e.g. a genuine convergence failure at this corner, not the
            # probe window being too generous. Its wall time is not a
            # trustworthy rate sample either way.
            error = "the calibration probe's own analysis did not complete cleanly"

    if not keep_artifacts:
        _cleanup_dir(probe_dir)

    return {"wall_s": wall_s, "timed_out": timed_out, "error": error}


def _run_fail_fast_probe(
    *,
    corner_points: list[CornerPoint],
    netlist_path: str,
    models_lib: str | None,
    analysis: dict[str, Any],
    timeout_s: float,
    artifacts_dir: str,
    keep_artifacts: bool,
    ngspice_binary: str | None = None,
    ngspice_init: tuple[str, ...] = (),
    osdi_preload: tuple[str, ...] = (),
    save_mode: str = DEFAULT_SAVE_MODE,
    ngspice_options: dict[str, Any] | None = None,
    stimulus_lines: tuple[str, ...] = (),
) -> dict[str, Any] | None:
    """The two-pass probe model's dispatch-time entry point (issue #1694):
    run one short, bounded calibration ``tran`` slice on the grid's first
    corner, measure that corner/deck's own simulated-time-per-wall-clock-
    second rate, and use it to estimate whether ``timeout_s`` can plausibly
    cover the *full* analysis window -- before committing the rest of the
    grid to it. See docs/design/sim-corner-reached-s.md for the rationale
    and docs/cli/sim.md's "Timeout-budget preflight" section for the
    reported shape.

    Returns ``None`` when there is nothing to probe (``analysis.kind``
    isn't ``"tran"``, its step/window tokens don't parse, or the grid is
    empty) or when the probe itself was inconclusive (spawn failure, or its
    own shortened analysis did not complete cleanly) -- in every such case
    the sweep proceeds exactly as it would have before this issue, with no
    probe surfaced in the response at all. Otherwise returns a dict with
    the measured rate, the estimated full-window wall-clock cost, and an
    ``"abort"`` boolean the caller uses to decide whether to skip the whole
    grid; the caller (``run_sim``) is responsible for turning ``"abort":
    True`` into the actual dispatch-time skip (see ``_run_local``'s
    ``probe_abort`` parameter).
    """
    parsed = _parse_tran_window(analysis)
    if parsed is None or not corner_points:
        return None
    step_token, _window_token, _step_s, window_s = parsed

    point = corner_points[0]
    probe_window_s, probe_timeout_s = _probe_window_and_timeout(window_s, timeout_s)
    if probe_window_s <= 0:
        return None

    probe = _run_calibration_probe(
        point=point,
        netlist_path=netlist_path,
        models_lib=models_lib,
        step_token=step_token,
        probe_window_s=probe_window_s,
        probe_timeout_s=probe_timeout_s,
        artifacts_dir=artifacts_dir,
        keep_artifacts=keep_artifacts,
        ngspice_binary=ngspice_binary,
        ngspice_init=ngspice_init,
        osdi_preload=osdi_preload,
        save_mode=save_mode,
        ngspice_options=ngspice_options,
        stimulus_lines=stimulus_lines,
    )
    if probe["error"] is not None:
        return None

    if probe["timed_out"]:
        rate_s_per_s: float | None = probe_window_s / probe_timeout_s
        rate_is_upper_bound = True
    elif probe["wall_s"] > 0:
        rate_s_per_s = probe_window_s / probe["wall_s"]
        rate_is_upper_bound = False
    else:
        # Immeasurably fast (a near-zero wall time for a nonzero probe
        # window) -- no usable rate, but also no reason to suspect a
        # feasibility problem; treated the same as any other inconclusive
        # probe.
        rate_s_per_s = None
        rate_is_upper_bound = False

    if rate_s_per_s is None:
        return None

    estimated_wall_s = window_s / rate_s_per_s
    estimated_reached_s = min(window_s, rate_s_per_s * timeout_s)
    abort = estimated_wall_s > timeout_s * _PROBE_ABORT_MARGIN

    return {
        "corner_id": point.corner_id,
        "probe_window_s": probe_window_s,
        "probe_timeout_s": probe_timeout_s,
        "probe_wall_s": probe["wall_s"],
        "measured_rate_s_per_s": rate_s_per_s,
        "rate_is_upper_bound": rate_is_upper_bound,
        "analysis_window_s": window_s,
        "timeout_s": timeout_s,
        "estimated_wall_s": estimated_wall_s,
        "estimated_reached_s": estimated_reached_s,
        "estimated_fraction": (estimated_reached_s / window_s if window_s else None),
        "abort_margin": _PROBE_ABORT_MARGIN,
        "abort": abort,
    }


#: The structured detail of ngspice's ``Timestep too small`` transient abort
#: (issue #2918): the simulation time it gave up at, the timestep it had
#: shrunk to, and the node/branch it blamed, e.g.
#: ``Timestep too small; time = 1.0001e-09, timestep = 5e-23: trouble with
#: node "v1#branch"``. Each part is optional so a shortened or
#: version-variant line still classifies (the missing field is ``None``).
_SPICE_NUMBER = r"[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?"
_TIMESTEP_TOO_SMALL_RE = re.compile(
    r"Timestep too small"
    rf"(?:\s*[;,]?\s*time\s*=\s*(?P<time>{_SPICE_NUMBER}))?"
    rf"(?:\s*[;,]?\s*timestep\s*=\s*(?P<timestep>{_SPICE_NUMBER}))?"
    r"(?:\s*:?\s*trouble with node\s*\"(?P<node>[^\"]+)\")?",
    re.IGNORECASE,
)


def _netlist_mentions_trnoise(netlist_path: str) -> bool:
    """Best-effort: does the request's top-level netlist text contain a
    ``trnoise`` source? Never raises; an unreadable/empty path is ``False``."""
    if not netlist_path:
        return False
    try:
        with open(netlist_path, encoding="utf-8", errors="replace") as handle:
            return "trnoise" in handle.read().lower()
    except OSError:
        return False


def _timestep_too_small_diagnostic(
    log_text: str, raw_line: str, netlist_path: str = ""
) -> dict[str, Any]:
    """Build the ``timestep_too_small`` diagnostic (issue #2918) from the
    first ``Timestep too small`` line in ``log_text``: always ``error``
    (ngspice ends the transient there -- the analysis is not recoverable),
    with ``time_s``/``timestep_s``/``node`` carried as structured fields
    (``None`` when ngspice's line omitted that part) and repeated in
    ``message`` so the report names the abort without ``keep_artifacts``.
    """
    match = _TIMESTEP_TOO_SMALL_RE.search(log_text)
    time_s: float | None = None
    timestep_s: float | None = None
    node: str | None = None
    if match is not None:
        if match.group("time") is not None:
            time_s = float(match.group("time"))
        if match.group("timestep") is not None:
            timestep_s = float(match.group("timestep"))
        node = match.group("node")
    where = f" at time = {time_s:g} s" if time_s is not None else ""
    culprit = f' (trouble with node "{node}")' if node is not None else ""
    hint = ""
    if _netlist_mentions_trnoise(netlist_path):
        hint = (
            " The netlist uses trnoise(): combined with any control source "
            "that is not DC from t = 0 (PWL/PULSE/EXP/B-source gate, supply "
            "ramp) this is a known ngspice limitation -- see docs/cli/sim.md "
            '"Known limitation: trnoise() with non-DC control sources".'
        )
    message = (
        f"ngspice aborted the transient: timestep too small{where}{culprit}; "
        "no results exist past the abort, so measurements beyond it produce "
        f"no value (engine log: {raw_line.strip()}).{hint}"
    )
    return {
        "severity": "error",
        "code": "timestep_too_small",
        "message": message,
        "time_s": time_s,
        "timestep_s": timestep_s,
        "node": node,
    }


#: ngspice's per-file ceiling on `par()` calls (issue #4084).
_PAR_CALL_CAP = 99
_PAR_CALL_RE = re.compile(r"\bpar\s*\(", re.IGNORECASE)

_PAR_CALL_CAP_WORKAROUND = (
    "expose the quantity as a node voltage (e.g. a 1 ohm CCVS) and measure "
    "`v(node)`, or capture the sweep with `options.waveforms` (+ "
    "`capture_only`) instead of one `.meas` card per point"
)
_PAR_CALL_CAP_MESSAGE = (
    "ngspice aborted at its limit of " + str(_PAR_CALL_CAP) + " par() calls per "
    "input file ({raw}); every measurement then reports no value. To stay "
    "under the cap, " + _PAR_CALL_CAP_WORKAROUND
)


def _par_call_cap_preflight(deck_path: str, is_xyce: bool) -> list[dict[str, str]]:
    """A one-item ``warning`` diagnostic list when the emitted deck holds more
    than :data:`_PAR_CALL_CAP` ``par(`` calls (issue #4084), else ``[]`` (always
    ``[]`` for Xyce, which has no such cap). Counts on the already-written deck
    text; no extra netlist pass."""
    if is_xyce:
        return []
    count = len(_PAR_CALL_RE.findall(_read_log_file(deck_path)))
    if count <= _PAR_CALL_CAP:
        return []
    return [
        {
            "severity": "warning",
            "code": "par_call_cap",
            "message": f"the corner deck contains {count} par() calls; ngspice "
            f"allows at most {_PAR_CALL_CAP} per input file and aborts the run "
            "beyond that. To stay under the cap, " + _PAR_CALL_CAP_WORKAROUND,
        }
    ]


def _classify_diagnostics(
    log_text: str, netlist_path: str = ""
) -> list[dict[str, Any]]:
    """Classify structured diagnostics from ``log_text``, per this module's
    ordered ``_DIAGNOSTIC_PATTERNS`` (most specific first, one match per
    code). ``netlist_path``, when provided, lets the ``model_bin_range``
    code replace ngspice's own uninformative message with one naming a
    likely culprit instance (see :func:`_diagnose_modelname_error`); omitted
    (the default -- also what every pre-#1214 caller/test still passes) it
    falls back to ngspice's raw log line, same as every other code.
    """
    diagnostics: list[dict[str, Any]] = []
    for code, pattern in _DIAGNOSTIC_PATTERNS:
        match = pattern.search(log_text)
        if match is None:
            continue
        message = _line_containing(log_text, match.start())
        if code == "timestep_too_small":
            diagnostics.append(
                _timestep_too_small_diagnostic(log_text, message, netlist_path)
            )
            continue
        if code == "model_bin_range":
            if any(d["code"] == "unsupported_model_level" for d in diagnostics):
                # The follow-on "could not find a valid modelname" is a
                # consequence of the rejected model level, not a bin range.
                continue
            message = _diagnose_modelname_error(netlist_path) or message
        elif code == "par_call_cap":
            message = _PAR_CALL_CAP_MESSAGE.format(raw=message.strip())
        elif code in _PREREQUISITE_MESSAGES:
            message = _PREREQUISITE_MESSAGES[code].format(raw=message.strip())
        if code == "ngspice_fatal" and any(
            d["code"] in _NGSPICE_FATAL_CODES for d in diagnostics
        ):
            continue
        diagnostics.append({"severity": "error", "code": code, "message": message})
    return diagnostics


#: Width, in micrometres, above which a single MOS instance's total width
#: (``w``, or ``w * nf`` when fingered) is a plausible culprit for ngspice's
#: "could not find a valid modelname" failure. The true per-instance bin
#: range is a property of each PDK's own BSIM model cards and cannot be
#: derived from the netlist alone -- this is a conservative empirical floor
#: from the gf180mcu `nfet_06v0` case (observed ceiling ~100-110 um,
#: undocumented in the model file, see issue #1214), used only to pick a
#: plausible culprit instance for the diagnostic message below, never to
#: reject a netlist outright.
_MODEL_WIDTH_CEILING_UM = 100.0

#: An ``M`` (plain-element) or ``X`` (subcircuit-call) MOS-shaped instance
#: line: ``<name> node node node node <model-or-subckt> [params...]``. Both
#: card kinds share this 4-terminal-then-model shape -- gf180mcu's own
#: netlists instantiate library cells like ``nfet_06v0`` via the ``X`` form
#: (see ``decks/gf180mcu.py``), while a normalized reference netlist uses
#: the plain ``M`` form (``netlist_normalize.py``). Best-effort: a 5+
#: terminal subckt call can still match (the trailing text is just parsed
#: as params, harmlessly), since this diagnostic only ever *suggests* a
#: culprit, it never rejects a netlist.
_MOS_INSTANCE_RE = re.compile(r"^\s*([MX]\S+)\s+(?:\S+\s+){4}(\S+)(.*)$", re.IGNORECASE)

_PARAM_ASSIGN_RE = re.compile(r"(\w+)\s*=\s*(\S+)")


def _parse_plain_number(raw: str) -> float | None:
    """Parse a bare, dimensionless SPICE numeric literal (e.g. an ``nf=``
    or ``m=`` instance-multiplier value). Returns ``None`` for anything
    that is not a plain number -- an expression like ``{n*2}`` this
    diagnostic cannot evaluate.
    """
    match = spice_number.NUMBER_RE.match(raw.strip().strip("'\""))
    if match is None:
        return None
    return float(match.group(1))


def _parse_width_um(raw: str) -> float | None:
    """Parse a SPICE ``W``/``L``-style geometry literal (``50u``, ``5e-5``,
    ``100n``) to micrometres. Returns ``None`` for anything that is not a
    plain numeric literal.
    """
    match = spice_number.NUMBER_RE.match(raw.strip().strip("'\""))
    if match is None:
        return None
    return float(match.group(1)) * spice_number.suffix_scale(match.group(2)) * 1e6


def _find_width_ceiling_culprit(
    netlist_text: str,
) -> tuple[str, str, float, float] | None:
    """Scan ``netlist_text`` for the MOS instance most likely to have
    tripped a model card's undocumented per-instance width bin range: the
    largest total width (``w``, or ``w * nf`` when fingered) among
    instances that do not already use ``m=`` to parallel it -- ``m=``
    parallels whole instances *outside* the model-card width check, so an
    instance already using ``m=`` (with its own ``w=`` inside the bin
    range) is never the failure's cause (issue #1214).

    Returns ``(instance_name, model_name, w_um, nf)`` for the worst
    offender at or above :data:`_MODEL_WIDTH_CEILING_UM`, or ``None`` if no
    instance meets that bar (ngspice's failure has some other cause this
    heuristic cannot identify).
    """
    worst: tuple[str, str, float, float] | None = None
    worst_effective_um = -1.0
    for line in netlist_text.splitlines():
        match = _MOS_INSTANCE_RE.match(line)
        if match is None:
            continue
        inst_name, model_name = match.group(1), match.group(2)
        params = dict(_PARAM_ASSIGN_RE.findall(match.group(3)))
        w_raw = params.get("w") or params.get("W")
        if w_raw is None:
            continue
        w_um = _parse_width_um(w_raw)
        if w_um is None:
            continue
        m_raw = params.get("m") or params.get("M")
        m_count = _parse_plain_number(m_raw) if m_raw is not None else 1.0
        if m_count is None:
            m_count = 1.0
        if m_count > 1.0:
            continue  # already parallelized outside the width-bin check
        nf_raw = params.get("nf") or params.get("NF")
        nf_count = _parse_plain_number(nf_raw) if nf_raw is not None else 1.0
        if nf_count is None:
            nf_count = 1.0
        effective_um = w_um * nf_count
        if (
            effective_um >= _MODEL_WIDTH_CEILING_UM
            and effective_um > worst_effective_um
        ):
            worst_effective_um = effective_um
            worst = (inst_name, model_name, w_um, nf_count)
    return worst


def _diagnose_modelname_error(netlist_path: str) -> str | None:
    """Build an actionable message for ngspice's own uninformative
    ``could not find a valid modelname`` failure by naming the netlist
    instance most likely responsible (see
    :func:`_find_width_ceiling_culprit`) -- e.g. gf180mcu's undocumented
    ~100 um per-instance width bin range, which fails identically whether
    exceeded via a raw large ``w=`` or via ``nf=`` (issue #1214).

    Returns ``None`` (never raises) when ``netlist_path`` is empty/unreadable
    or no culprit instance is found; the caller falls back to ngspice's raw
    log line in that case.
    """
    if not netlist_path:
        return None
    try:
        with open(netlist_path, encoding="utf-8", errors="replace") as handle:
            netlist_text = handle.read()
    except OSError:
        return None
    culprit = _find_width_ceiling_culprit(netlist_text)
    if culprit is None:
        return None
    inst_name, model_name, w_um, nf_count = culprit
    if nf_count > 1:
        effective_um = w_um * nf_count
        cause = f"w={w_um:g}u nf={nf_count:g} ({effective_um:g} um effective width)"
    else:
        cause = f"w={w_um:g}u"
    return (
        f'ngspice reported "could not find a valid modelname" -- likely '
        f"cause: instance '{inst_name}' ({model_name}) with {cause} exceeds "
        f"the model card's per-instance width bin range (undocumented, "
        f"~{_MODEL_WIDTH_CEILING_UM:g} um on gf180mcu); use m= to parallel "
        "multiple smaller-width instances instead of a single large w= or "
        "nf= (see issue #1214)"
    )


#: ngspice's own terminal trailer when an analysis never actually completed
#: (e.g. ``tran simulation(s) aborted``) -- verified against ngspice 46's
#: output for a genuinely unrecoverable singular matrix/nonconvergence (see
#: issue #205). Its presence always means "no trustworthy result", regardless
#: of what measurement values happen to be parseable.
_SIMULATION_ABORTED_RE = re.compile(r"simulation\(s\)\s+aborted", re.IGNORECASE)


def _recovered_from_stepping(
    measurements_spec: list[dict[str, Any]],
    measurement_results: list[dict[str, Any]],
    log_text: str,
    aborted_re: re.Pattern[str] = _SIMULATION_ABORTED_RE,
    waveform_complete: bool = False,
) -> bool:
    """True when a ``singular_matrix``/``nonconvergence`` classification for
    this corner should be treated as recovered (non-fatal) rather than fatal.

    ngspice tries dynamic gmin stepping, true gmin stepping, and source
    stepping in turn to recover from a singular DC operating-point matrix
    before falling back to the requested analysis directly -- each stepping
    attempt logs its own ``Warning: singular matrix`` and (when that stepping
    attempt itself doesn't fully converge) ``... stepping failed`` text, even
    on a run that ultimately succeeds (verified against ngspice 46 running
    the sky130 5T OTA composed by ``klt gen-compose``, see issue #205). That
    narration is expected noise on the path to a correct result, not evidence
    the analysis failed -- so it is only treated as fatal when there is no
    other way to tell the run actually succeeded.

    Xyce (issue #2016) gets the same rule against its own narration: its
    "Numerically singular matrix found by Amesos, returning zero solution to
    nonlinear solver!" warnings likewise narrate intermediate solver trouble
    rather than a final verdict. ``aborted_re`` carries the engine's own
    "this run never completed" trailer text.

    Conservative by construction: recovery requires *every* requested
    measurement to have actually come back with a value, and the engine's
    own simulation-aborted trailer must be absent -- either one failing
    means the analysis never produced a trustworthy result and the
    classification stays fatal.

    A corner with no ``measurements[]`` (a waveform-capture-only request,
    issue #3246) has no measurement to check; its independent signal is
    ``waveform_complete`` -- the captured ``tran`` waveform reached the
    analysis stop time (:func:`_waveform_reached_tran_window`). Without it
    such a corner is never considered recovered.
    """
    if not measurements_spec:
        return waveform_complete and not aborted_re.search(log_text)
    if any(m["status"] == "error" for m in measurement_results):
        return False
    if aborted_re.search(log_text):
        return False
    return True


_ABORT_CONTEXT_LINES = 12
_ABORT_MAX_QUOTED = 4
_ABORT_ERROR_LINE_RE = re.compile(r"\b(?:error|fatal)\b", re.IGNORECASE)


def _analysis_aborted_diagnostic(
    log_text: str,
    aborted_re: re.Pattern[str],
) -> dict[str, str] | None:
    """The analysis-level ``analysis_aborted`` diagnostic (issue #2846), or
    ``None`` when the engine's abort trailer is absent.

    Quotes the engine's own error line(s) preceding the trailer. Always
    ``severity: "error"``: the trailer means the analysis never produced a
    trustworthy result regardless of which measurement values happen to be
    parseable -- the same rule :func:`_recovered_from_stepping` applies
    (issue #205). A stepping-recovered corner has no trailer, so it never
    gets this diagnostic at all.
    """
    match = aborted_re.search(log_text or "")
    if match is None:
        return None
    lines = log_text[: match.end()].splitlines()
    window = lines[-_ABORT_CONTEXT_LINES:]
    quoted = [ln.strip() for ln in window if _ABORT_ERROR_LINE_RE.search(ln)]
    trailer = lines[-1].strip() if lines else ""
    if trailer and trailer not in quoted:
        quoted.append(trailer)
    quoted = quoted[-_ABORT_MAX_QUOTED:]
    return {
        "severity": "error",
        "code": "analysis_aborted",
        "message": "analysis aborted by the engine: " + " | ".join(quoted),
    }


def _capture_only_waveform_complete(
    measurements_spec: list[dict[str, Any]],
    raw_path: str | None,
    diagnostics: list[dict[str, Any]],
    analysis: dict[str, Any],
) -> bool:
    """Issue #3246: for a waveform-capture-only ``tran`` request (no
    ``measurements[]``) the captured waveform is the independent completion
    signal. Parse the rawfile -- only when a ``singular_matrix`` /
    ``nonconvergence`` diagnostic makes it matter -- and report whether it
    reached the transient stop time. A truncated/unparseable rawfile stays
    fatal (``False``).
    """
    if (
        measurements_spec
        or raw_path is None
        or not any(
            d["code"] in ("singular_matrix", "nonconvergence") for d in diagnostics
        )
        or not os.path.isfile(raw_path)
    ):
        return False
    try:
        waveform = parse_ascii_rawfile(raw_path)
    except (SimError, ValueError):
        return False
    return _waveform_reached_tran_window(waveform, analysis)


def _downgrade_stepping_diagnostics(
    diagnostics: list[dict[str, Any]], has_measurements: bool
) -> None:
    """Downgrade recovered ``singular_matrix``/``nonconvergence`` diagnostics
    to warnings; a capture-only corner also gets the reason appended."""
    for diagnostic in diagnostics:
        if diagnostic["code"] in ("singular_matrix", "nonconvergence"):
            diagnostic["severity"] = "warning"
            if not has_measurements:
                diagnostic["message"] += (
                    " (treated as recovered: the captured waveform "
                    "reached the transient stop time)"
                )


def _waveform_reached_tran_window(
    waveform: dict[str, Any] | None, analysis: dict[str, Any]
) -> bool:
    """True when ``waveform`` (a :func:`parse_ascii_rawfile` result) is a
    ``tran`` waveform whose last time point reaches the analysis stop time
    (issue #3246). ``False`` for a missing waveform, a non-``tran`` analysis
    or unparseable ``args`` (see :func:`_parse_tran_window`), a non-time
    sweep variable, or a waveform cut short of the window.
    """
    if not waveform or not waveform["points"] or not waveform["variables"]:
        return False
    window = _parse_tran_window(analysis)
    if window is None or waveform["variables"][0]["name"].lower() != "time":
        return False
    return waveform["points"][-1][0] >= window[3] * (1 - 1e-4)


def _parse_measurements(log_text: str) -> dict[str, float]:
    """Parse ``.meas`` scalar results from the ngspice log.

    A successful measurement prints ``<name> = <value>`` (optionally with a
    ``from=... to=...`` trailer, ignored); a failed one prints a distinct
    ``... <name> ... failed!`` line and is *not* included in the result --
    the caller treats an absent name as "no value" (see ``run_sim``'s "a
    missing measurement is an error" handling).
    """
    values: dict[str, float] = {}
    failed: set[str] = set()
    for raw_line in log_text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        failed_match = _MEAS_FAILED_RE.match(line)
        if failed_match:
            failed.add(failed_match.group(1))
            continue
        value_match = _MEAS_VALUE_RE.match(line)
        if value_match:
            name, value = value_match.groups()
            values[name] = float(value)
    for name in failed:
        values.pop(name, None)
    return values


#: The only ``measurements[].limits`` keys :func:`_evaluate_limits` applies.
#: Anything else in a ``limits`` object is silently inert -- which is exactly
#: why :func:`_build_coverage` reports it (issue #1996).
_RECOGNISED_LIMIT_KEYS = ("min", "max")


def _measurement_coverage(
    spec: dict[str, Any], corner: dict[str, Any], corner_index: int
) -> tuple[list[str], list[dict[str, str]], list[dict[str, str]]]:
    """Count actual observations or applied bounds, never corner declarations.

    ``corner_index`` (this corner's position in the report's own ``corners``
    list) scopes every identity built here -- ``corner_id`` alone is a
    *display* label (e.g. supply voltage rounded to 3 decimals) and distinct
    corners can legitimately round to the same one, which would otherwise
    collide as the same "unique" work identity and make
    :func:`~klayout_tools.coverage.build_check_coverage` reject two genuinely
    separate checks as a duplicate.
    """
    name = spec["name"]
    identity = work_id("measurement", corner_index, corner["corner_id"], name)
    limits = spec.get("limits") or {}
    measurement = next(
        (m for m in corner.get("measurements", []) if m["name"] == name), {}
    )
    has_value = measurement.get("value") is not None
    # Issue #2493: a measurement graded `_INCONCLUSIVE_STATUS` is a real
    # observation (a value was produced) that the plausibility pre-check
    # deliberately kept out of the `limits` comparison -- distinct from
    # `measured` below, which is specifically "produced a value *and* was
    # actually graded against `limits`". Such an observation still counts
    # for the `/observation` row (something was measured), but never for a
    # `/limit/...` row (that bound was never applied to it).
    #
    # Only the plausibility route reaches this: issue #2492's
    # `options.fail_on_diagnostic` disqualifies the *corner*, leaving each
    # measurement's own `pass`/`fail` (and so its coverage rows) intact --
    # correctly, since coverage answers "was this bound evaluated?", which
    # it was. Whether the evaluated result is trustworthy is what `status`
    # and the top-level `inconclusive` count report, and conflating the two
    # would make a distrusted corner look uncovered instead.
    implausible = measurement.get("status") == _INCONCLUSIVE_STATUS
    measured = has_value and measurement.get("status") in {"pass", "fail"}
    # Issue #3108: an unreached target is an answered question (an
    # observation) but no bound was applied to it, like `implausible`.
    not_reached = measurement.get("status") == NOT_REACHED_STATUS
    checked = []
    skipped = []
    inapplicable = []
    if not limits:
        inapplicable.append(
            {"id": identity + "/limits", "reason": "characterization_without_limits"}
        )
        if measured or implausible or not_reached:
            checked.append(identity + "/observation")
        else:
            skipped.append(
                {"id": identity + "/observation", "reason": "unavailable_measurement"}
            )
    for key, value in limits.items():
        bound_id = work_id("limit", corner_index, corner["corner_id"], name, key)
        if key not in _RECOGNISED_LIMIT_KEYS:
            skipped.append({"id": bound_id, "reason": "unrecognized_limit_key"})
        elif value is None:
            skipped.append({"id": bound_id, "reason": "missing_limit_value"})
        elif implausible:
            # The skip *reason* keeps naming the plausibility violation
            # specifically (not the merged `inconclusive` status), because
            # that is the question a coverage reader is asking: which bound
            # was never applied, and why.
            # Issue #3088: an ambiguous AC band (not a plausibility bound) is
            # the other route here, and its skip names the real cause.
            reason = (
                MARGIN_AMBIGUOUS_DIAGNOSTIC_CODE
                if (measurement.get("transfer") or {}).get("verdict") == "ambiguous"
                else IMPLAUSIBLE_DIAGNOSTIC_CODE
            )
            skipped.append({"id": bound_id, "reason": reason})
        elif not_reached:
            skipped.append({"id": bound_id, "reason": NOT_REACHED_STATUS})
        elif not measured:
            skipped.append({"id": bound_id, "reason": "unavailable_measurement"})
        else:
            checked.append(bound_id)
    return checked, skipped, inapplicable


def _capture_only_requested(
    options: dict[str, Any],
    *,
    want_waveforms: bool,
    measurements_spec: list[dict[str, Any]],
) -> bool:
    """Validate and return ``request.options.capture_only`` (issue #3158).

    Absent means ``False``. ``true`` declares "capture the waveforms, check
    nothing", so it only makes sense for a request that does capture them
    (``options.waveforms`` or ``--plot``) and declares no measurements --
    anything else would turn the flag into a general "suppress exit 4"
    switch for a genuinely unchecked run, so it is a request error.
    """
    value = options.get("capture_only", False)
    if not isinstance(value, bool):
        raise SimError(
            f"request.options.capture_only must be a boolean (got {value!r})"
        )
    if not value:
        return False
    if not want_waveforms:
        raise SimError(
            "request.options.capture_only requires options.waveforms: true "
            "(or --plot): there is nothing to capture otherwise"
        )
    if measurements_spec:
        raise SimError(
            "request.options.capture_only conflicts with a non-empty "
            "`measurements` list: declare measurements to be checked, or "
            "none to capture waveforms only"
        )
    return True


def _corner_without_measurements(
    corner: dict[str, Any], corner_index: int, capture_only: bool
) -> tuple[list[str], list[dict[str, Any]]]:
    """Coverage for one corner of a request that declares no measurements.

    Returns ``(checked, skipped)``. Under ``capture_only`` (issue #3158) a
    corner that ran clean counts its waveform capture as checked work; any
    other corner is a ``no_requested_measurements`` skip.
    """
    corner_work = work_id("corner", corner_index, corner["corner_id"])
    if capture_only and corner["status"] == "pass":
        return [corner_work + "/waveform_capture"], []
    return [], [{"id": corner_work, "reason": "no_requested_measurements"}]


def _summarise_limits(
    measurements_spec: list[dict[str, Any]],
) -> tuple[int, int, list[dict[str, Any]]]:
    """Count declared limits and collect unrecognised limit keys per measurement.

    Returns ``(limits_declared, measurements_with_limits, unrecognized)``.
    """
    unrecognized: list[dict[str, Any]] = []
    limits_declared = 0
    measurements_with_limits = 0
    for spec in measurements_spec:
        limits = spec.get("limits")
        if not isinstance(limits, dict) or not limits:
            continue
        limits_declared += 1
        unknown = sorted(k for k in limits if k not in _RECOGNISED_LIMIT_KEYS)
        if unknown:
            unrecognized.append({"measurement": spec.get("name"), "keys": unknown})
        if any(limits.get(k) is not None for k in _RECOGNISED_LIMIT_KEYS):
            measurements_with_limits += 1

    return limits_declared, measurements_with_limits, unrecognized


def _build_coverage(
    measurements_spec: list[dict[str, Any]],
    corners: list[dict[str, Any]],
    *,
    capture_only: bool = False,
) -> dict[str, Any]:
    """Common checked-work v1 plus the existing simulation coverage counters.

    ``capture_only`` (issue #3158) is the request's declared intent to
    capture waveforms with no measurements: each corner that ran clean then
    counts its capture as the checked work, and ``capture_only: true`` is
    recorded on the block. A corner that did not pass stays a skip, so a run
    failure is never waived.
    """
    limits_declared, measurements_with_limits, unrecognized = _summarise_limits(
        measurements_spec
    )

    checked = []
    skipped = []
    inapplicable = []
    for corner_index, corner in enumerate(corners):
        if not measurements_spec:
            corner_checked, corner_skipped = _corner_without_measurements(
                corner, corner_index, capture_only
            )
            checked.extend(corner_checked)
            skipped.extend(corner_skipped)
        for spec in measurements_spec:
            actual, missing, irrelevant = _measurement_coverage(
                spec, corner, corner_index
            )
            checked.extend(actual)
            skipped.extend(missing)
            inapplicable.extend(irrelevant)
    reasons = []
    if not corners:
        reasons.append(REASON_EMPTY_CORNER_MATRIX)
        skipped.append(
            {"id": "simulation:corner_matrix", "reason": REASON_EMPTY_CORNER_MATRIX}
        )
    if limits_declared and not measurements_with_limits and unrecognized:
        reasons.append(REASON_UNRECOGNIZED_LIMIT_KEYS)

    block = {
        "corners_simulated": len(corners),
        "measurements_declared": len(measurements_spec),
        "measurements_with_limits": measurements_with_limits,
        "unrecognized_limit_keys": unrecognized,
        **build_check_coverage(
            checked=checked,
            skipped=skipped,
            inapplicable=inapplicable,
            nothing_checked_reasons=reasons,
        ),
    }
    if capture_only:
        block["capture_only"] = True
    return block


def _evaluate_limits(
    value: float, limits: dict[str, float] | None
) -> tuple[str, float | None]:
    """Evaluate ``value`` against ``limits`` (``min``/``max``, either optional).

    Returns ``(status, margin)``. No ``limits`` -> reported but never fails
    (``margin`` reflects the nearer bound if any bound exists, else
    ``None``). ``margin``: positive is headroom, negative is violation --
    the nearest *binding* limit when passing, the nearest *violated* limit
    when failing.
    """
    if not limits:
        return "pass", None

    min_limit = limits.get("min")
    max_limit = limits.get("max")

    violations = []
    if min_limit is not None and value < min_limit:
        violations.append(value - min_limit)  # negative
    if max_limit is not None and value > max_limit:
        violations.append(max_limit - value)  # negative

    if violations:
        # Worst (most negative) violation.
        return "fail", min(violations)

    headrooms = []
    if min_limit is not None:
        headrooms.append(value - min_limit)
    if max_limit is not None:
        headrooms.append(max_limit - value)
    margin = min(headrooms) if headrooms else None
    return "pass", margin


def _grade_measurement_value(
    spec: dict[str, Any], value: float
) -> tuple[str, float | None, list[dict[str, Any]]]:
    """Grade one extracted measurement ``value`` against its ``spec``.

    Returns ``(status, margin, diagnostics)`` -- ``diagnostics`` is empty
    except on the plausibility-violation path, where it carries the
    ``severity: "warning"``, ``code: "implausible_solution"`` entry the
    caller extends the corner's own ``diagnostics`` with. That code is the
    *reason* half of the status; the status itself is the shared
    :data:`_INCONCLUSIVE_STATUS` (see its note on why the two "produced a
    number I will not vouch for" routes are one token).

    **This function owns the grading order**, which is the whole point of
    issue #2493: a declared plausibility bound
    (``measurements[].plausible_range``, or an inherited
    ``options.node_voltage_bounds`` -- both already resolved into ``spec``
    by :func:`_resolve_plausible_ranges`) is checked **first**, and an
    out-of-range value short-circuits :func:`_evaluate_limits` entirely
    rather than being graded against ``limits`` as if it were a real
    result. ``margin`` stays ``None`` there: margin is defined relative to
    ``limits``, which never ran.

    The reason the ordering lives in one named function rather than inline
    at the call site is that the failure mode this closes is *silent*. An
    ngspice Newton solve can converge onto a numerically valid but
    physically implausible operating point (a compact model evaluated far
    outside its fitted range) without emitting any distinguishing log line
    for :func:`_classify_diagnostics` to match -- so the only thing standing
    between that solve and an ordinary-looking ``pass``/``fail`` verdict is
    this check running before :func:`_evaluate_limits`. A second grading
    path that reached ``_evaluate_limits`` without passing through here
    would silently reopen the gap.

    Never fires for a request that declares no plausibility bound: with
    ``spec["plausible_range"]`` absent, this is exactly
    :func:`_evaluate_limits` plus an empty diagnostic list.
    """
    plausible_range = spec.get("plausible_range")
    if plausible_range is not None and not _within_plausible_range(
        value, plausible_range
    ):
        return (
            _INCONCLUSIVE_STATUS,
            None,
            [
                {
                    "severity": "warning",
                    "code": IMPLAUSIBLE_DIAGNOSTIC_CODE,
                    "message": (
                        f"measurement {spec['name']!r} = {value!r} is outside its "
                        f"declared plausible range {plausible_range!r} -- graded "
                        f"{_INCONCLUSIVE_STATUS} rather than pass/fail"
                    ),
                }
            ],
        )
    status, margin = _evaluate_limits(value, spec.get("limits"))
    return status, margin, []


def _quantile_key(percentile: float) -> str:
    """Response key for a percentile: ``5 -> "p5"``, ``2.5 -> "p2.5"``."""
    return f"p{percentile:g}"


def _percentile(sorted_values: list[float], percentile: float) -> float:
    """Linearly-interpolated percentile of ``sorted_values`` (ascending).

    The "inclusive"/linear method (numpy's default, and
    :func:`statistics.quantiles`' ``method="inclusive"``): the requested
    percentile indexes ``(n - 1) * p / 100`` into the sorted sample and
    interpolates between the two neighbouring order statistics. Hand-rolled
    rather than routed through :func:`statistics.quantiles` because that API
    returns a fixed set of equally-spaced cut points, not an arbitrary
    caller-declared percentile list (``request.monte_carlo.quantiles``).
    ``p0``/``p100`` degenerate to the sample min/max.
    """
    if len(sorted_values) == 1:
        return sorted_values[0]
    position = (len(sorted_values) - 1) * (percentile / 100.0)
    lower = int(position)
    upper = min(lower + 1, len(sorted_values) - 1)
    weight = position - lower
    return sorted_values[lower] * (1.0 - weight) + sorted_values[upper] * weight


def _evaluate_sigma_window(
    mean: float,
    stddev: float,
    k_sigma: float,
    limits: dict[str, float] | None,
) -> dict[str, Any]:
    """Evaluate whether the ``mean +/- k_sigma * stddev`` window stays inside
    ``limits``, returning ``{k, low, high, status, margin}``.

    Both window endpoints are scored with :func:`_evaluate_limits` -- the
    same function (and therefore the same ``min``/``max`` handling and
    ``margin`` sign convention) a single deterministic value goes through --
    rather than a parallel comparison path. The window is inside the limits
    iff *both* endpoints are, so the endpoint statuses combine as
    ``fail`` > ``pass`` and ``margin`` is the worse (smaller) of the two: the
    nearest binding limit when passing, the worst violation when failing. No
    ``limits`` -> ``pass`` with a ``null`` margin, exactly as for a single
    value.

    This function deliberately has no ``inconclusive`` branch (issues #2492
    + #2493): it scores a window, and by the time it is called the
    ``mean``/``stddev`` it is handed were already computed over the
    *trusted* samples only -- distrusted samples are excluded upstream, in
    :func:`_monte_carlo_rollup`/
    :func:`_sample_statistics`, and counted there. Grading here would
    double-count the same fact and leave the window's own endpoints
    describing a population the verdict then disowned.
    """
    low = mean - k_sigma * stddev
    high = mean + k_sigma * stddev
    low_status, low_margin = _evaluate_limits(low, limits)
    high_status, high_margin = _evaluate_limits(high, limits)
    margins = [m for m in (low_margin, high_margin) if m is not None]
    return {
        "k": k_sigma,
        "low": low,
        "high": high,
        "status": "fail" if "fail" in (low_status, high_status) else "pass",
        "margin": min(margins) if margins else None,
    }


def _sample_statistics(
    values: list[float],
    *,
    errored: int,
    inconclusive: int = 0,
    absent: int | None = None,
    quantiles: tuple[float, ...],
    k_sigma: float | None,
    limits: dict[str, float] | None,
) -> dict[str, Any]:
    """Reduce one measurement's Monte Carlo sample ``values`` to the reported
    statistics block: ``{n, errored, inconclusive, mean, stddev, min, max,
    quantiles, sigma_window}``.

    ``n`` counts only samples that produced a number *and* were trusted
    enough to grade; ``errored`` counts the samples whose value was
    unextractable (``null``) and were therefore excluded from every
    statistic -- a sample set is never silently reduced without saying so.

    ``inconclusive`` (issues #2492 + #2493) counts the samples that *did*
    produce a number the run declines to trust -- either the sample's corner
    emitted a diagnostic code the request listed in
    ``options.fail_on_diagnostic``, or the sampled value fell outside a
    declared ``options.node_voltage_bounds``/``measurements[].plausible_range``.
    Those samples are excluded from
    ``mean``/``stddev``/``min``/``max``/``quantiles`` and therefore from the
    ``sigma_window`` derived from them, and counted here instead: keeping
    them would let a sample the run has explicitly disowned move the very
    mean and sigma the window verdict is computed from, which is strictly
    worse than reporting the window over the trusted subset and saying how
    many samples were set aside. ``n + errored + inconclusive`` always
    equals the number of samples drawn for this measurement.

    ``absent`` (issue #2925) counts the samples of an ``optional`` measurement
    that legitimately produced no value; the key is present only for such a
    measurement (``absent is None`` omits it, keeping every other request's
    block unchanged) and the identity becomes ``n + errored + inconclusive +
    absent``. Absent samples are excluded from every statistic and are *not*
    ``errored``, which keeps meaning "the simulation failed".

    ``stddev`` is the **sample** standard deviation (Bessel-corrected,
    ``n - 1``), the estimator appropriate for a finite Monte Carlo draw from
    a population, and is ``null`` for ``n < 2`` (undefined, never faked as
    ``0.0`` -- a fabricated zero sigma would read as "no variation" and
    silently pass any window check). ``sigma_window`` is ``null`` unless a
    sigma multiple was declared *and* ``mean``/``stddev`` both exist.
    """
    ordered = sorted(values)
    n = len(ordered)
    mean = statistics.fmean(ordered) if n else None
    stddev = statistics.stdev(ordered) if n >= 2 else None

    sigma_window: dict[str, Any] | None = None
    if k_sigma is not None and mean is not None and stddev is not None:
        sigma_window = _evaluate_sigma_window(mean, stddev, k_sigma, limits)

    absent_field = {} if absent is None else {"absent": absent}
    return {
        "n": n,
        "errored": errored,
        "inconclusive": inconclusive,
        **absent_field,
        "mean": mean,
        "stddev": stddev,
        "min": ordered[0] if n else None,
        "max": ordered[-1] if n else None,
        "quantiles": {
            _quantile_key(q): (_percentile(ordered, q) if n else None)
            for q in quantiles
        },
        "sigma_window": sigma_window,
    }


def _base_corner_id(corner: dict[str, Any]) -> str:
    """The corner a Monte Carlo sample was drawn from: its ``corner_id`` with
    the ``/mc<sample_index>`` suffix removed (see :attr:`CornerPoint.corner_id`).

    Derived from the sample's own ``monte_carlo.sample_index`` rather than by
    pattern-matching the id, so a process/supply label that happens to
    contain ``/mc`` can never be mis-split.
    """
    corner_id = corner["corner_id"]
    suffix = f"/mc{corner['monte_carlo']['sample_index']}"
    if corner_id.endswith(suffix):
        return corner_id[: -len(suffix)]
    return corner_id


MC_WINDOW_SCOPES = ("pooled", "per_corner")


def _validate_mc_window_scope(mc_spec: dict[str, Any]) -> str:
    """Validate ``request.monte_carlo.window_scope`` (issue #2960): which
    population's ``mean +/- k*sigma`` window gates a measurement's status.
    ``"pooled"`` (the default; an explicit ``null`` reads as not declared)
    grades the union across corners, ``"per_corner"`` grades every
    ``by_corner[]`` population on its own."""
    raw = mc_spec.get("window_scope")
    if raw is None:
        return "pooled"
    if not isinstance(raw, str) or raw not in MC_WINDOW_SCOPES:
        raise SimError(
            "request.monte_carlo.window_scope must be one of "
            f"{', '.join(repr(v) for v in MC_WINDOW_SCOPES)} (got {raw!r})"
        )
    return raw


def _mc_window_scope_echo(mc_spec: dict[str, Any], window_scope: str) -> dict[str, str]:
    """``environment.monte_carlo`` echo for ``window_scope``: only when the
    request declared it, so a sampling-only request keeps its shape."""
    if mc_spec.get("window_scope") is None:
        return {}
    return {"window_scope": window_scope}


def _monte_carlo_rollup(
    entries: list[tuple[dict[str, Any], dict[str, Any]]],
    spec: dict[str, Any],
    quantiles: tuple[float, ...],
    default_k_sigma: float | None,
) -> dict[str, Any] | None:
    """Monte Carlo statistics for one measurement, or ``None`` when no
    sampled corner contributed to it.

    Top-level statistics pool **every** sample of every corner; ``by_corner``
    breaks the same samples down per originating (pre-sampling) corner, in
    corner order. For the common single-corner Monte Carlo request the two
    describe the same draw; for a request that combines a PVT matrix *with*
    ``monte_carlo``, ``by_corner`` is the statistically meaningful view (each
    corner is its own population) and the pooled block is the union across
    corners -- see docs/cli/sim.md.
    """
    sampled = [(c, m) for c, m in entries if c.get("monte_carlo") is not None]
    if not sampled:
        return None

    limits = spec.get("limits")
    # An explicit `null` reads as "not declared" (the house convention for
    # optional fields), so it falls back to the run-wide default.
    override = spec.get("k_sigma")
    k_sigma = default_k_sigma if override is None else float(override)

    def _stats(pairs: list[tuple[dict[str, Any], dict[str, Any]]]) -> dict[str, Any]:
        # Issues #2492 + #2493: a distrusted sample is set aside *before*
        # any statistic is computed (see `_sample_statistics`), not merely
        # reported alongside one -- an untrusted number must not be allowed
        # to move the mean/sigma the window verdict rests on. Both reasons
        # are screened here, and they need screening at two different
        # levels: `options.fail_on_diagnostic` disqualifies the sample's
        # whole *corner* (its measurement can still read `pass`/`fail`,
        # graded against `limits` before the corner was disqualified), while
        # a plausibility-bound violation is graded onto the *measurement*
        # itself. Neither is caught by the pre-existing `value is not None`
        # filter, since a distrusted sample has a real, non-null value --
        # that is exactly what makes it dangerous to the pooled statistics.
        absent = sum(1 for _, m in pairs if m["status"] == _ABSENT_STATUS)
        # The `absent` count is reported only for a measurement declared
        # `optional`, so every other request keeps its exact statistics shape.
        report_absent = absent if spec.get("optional") is True else None
        trusted = [
            (c, m)
            for c, m in pairs
            if c.get("status") != _INCONCLUSIVE_STATUS
            and m["status"] not in (_INCONCLUSIVE_STATUS, _ABSENT_STATUS)
        ]
        values = [m["value"] for _, m in trusted if m["value"] is not None]
        return _sample_statistics(
            values,
            errored=len(trusted) - len(values),
            inconclusive=len(pairs) - len(trusted) - absent,
            absent=report_absent,
            quantiles=quantiles,
            k_sigma=k_sigma,
            limits=limits,
        )

    grouped: dict[str, list[tuple[dict[str, Any], dict[str, Any]]]] = {}
    for corner, m in sampled:
        grouped.setdefault(_base_corner_id(corner), []).append((corner, m))

    rollup = _stats(sampled)
    rollup["by_corner"] = [
        {"corner_id": corner_id, **_stats(pairs)}
        for corner_id, pairs in grouped.items()
    ]
    return rollup


EXPR_RUNNER_MAYBE_OLD_CODE = "expr_runner_may_predate"


def _attach_expr_runner_hint(
    report: dict[str, Any],
    measurements_spec: list[dict[str, Any]],
    corners: list[dict[str, Any]],
    *,
    backend: str | None,
) -> None:
    """Additive/optional (issue #4097): on an off-host run, a
    ``measurements[].expr`` measurement that came back with no value may mean
    the runner image predates ``expr`` (its version compare is major.minor
    only, so the pre-launch check cannot prove it). Name that suspect beside
    the per-measurement "produced no value"."""
    if backend not in _OFFHOST_BACKENDS:
        return
    expr_names = {
        spec["name"]
        for spec in measurements_spec
        if isinstance(spec, dict) and spec.get("expr") is not None and "name" in spec
    }
    if not expr_names:
        return
    silent = sorted(
        {
            m["name"]
            for corner in corners
            for m in corner.get("measurements") or []
            if m.get("name") in expr_names and m.get("value") is None
        }
    )
    if not silent:
        return
    report.setdefault("diagnostics", []).append(
        {
            "code": EXPR_RUNNER_MAYBE_OLD_CODE,
            "severity": "warning",
            "message": (
                f"measurements[].expr measurement(s) {', '.join(silent)} "
                "produced no value on the off-host runner, which may predate "
                "`expr` (runner may predate expr; compare "
                "environment.remote.runner_klt_version with the client "
                "version and rebuild the runner image if older)"
            ),
            "measurements": silent,
        }
    )


def _attach_not_reached_count(
    report: dict[str, Any],
    measurements_spec: list[dict[str, Any]],
    corners: list[dict[str, Any]],
) -> None:
    """Additive/optional (issue #3108): the top-level ``not_reached`` count,
    present only when some measurement declared ``on_unreached`` so a request
    that never opts in keeps its exact report shape. Counts corner x
    measurement results, kept apart from ``errored`` (which counts corners)."""
    if not any("on_unreached" in spec for spec in measurements_spec):
        return
    report["not_reached"] = sum(
        1
        for corner in corners
        for m in corner["measurements"]
        if m["status"] == NOT_REACHED_STATUS
    )


def _aggregate_measurement_status(
    entries: list[tuple[dict[str, Any], dict[str, Any]]],
) -> str:
    """One measurement's aggregate status across every corner that reported
    it: ``error > inconclusive > fail > pass``.

    On the plausibility route (issue #2493) a measurement carries
    ``inconclusive`` on its own, so the plain ``m["status"]`` scan already
    catches it. On the ``options.fail_on_diagnostic`` route (issue #2492) it
    does not: the disqualification applies to the whole corner, while the
    measurement's own status is still whatever ``limits`` said -- so the
    corner's status is substituted for it here. Without that, a measurement
    whose only data came from distrusted corners would roll up as a confident
    ``pass``, or as a ``fail`` asserting a spec miss the run cannot actually
    vouch for.

    The substitution is deliberately asymmetric: only ``inconclusive`` is
    pulled down from the corner, not ``error``. An errored corner's
    measurements are themselves ``error`` (``_run_corner`` writes that status
    on every measurement it could not extract), so ``error`` already
    propagates through ``m["status"]`` and a second path would be redundant.
    """
    statuses = {
        (
            _INCONCLUSIVE_STATUS
            if corner.get("status") == _INCONCLUSIVE_STATUS
            else m["status"]
        )
        for corner, m in entries
    }
    if "error" in statuses:
        return "error"
    if _INCONCLUSIVE_STATUS in statuses:
        return _INCONCLUSIVE_STATUS
    if "fail" in statuses:
        return "fail"
    if statuses == {NOT_REACHED_STATUS}:
        return NOT_REACHED_STATUS
    if statuses == {_ABSENT_STATUS}:
        return _ABSENT_STATUS
    return "pass"


def _rollup_measurements(
    measurements_spec: list[dict[str, Any]],
    corners: list[dict[str, Any]],
    monte_carlo: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Per-measurement rollup across all corners: aggregate status and the
    worst-case corner (smallest/most-negative margin; ``None`` margins --
    unextractable values, and plausibility-disqualified ones whose ``limits``
    comparison never ran -- are treated as worst of all).

    Aggregate status follows the same ``error > inconclusive > fail > pass``
    precedence :func:`_run_corner` applies per corner (issues #2492 +
    #2493) -- see :func:`_aggregate_measurement_status`;
    ``inconclusive`` is unreachable unless the request declared
    ``options.fail_on_diagnostic`` or a plausibility bound.

    ``monte_carlo`` (the validated ``{quantiles, k_sigma}`` statistics config,
    ``None`` when the request declared no ``monte_carlo`` block) additively
    attaches a ``monte_carlo`` statistics block to each entry that had
    sampled corners -- see :func:`_monte_carlo_rollup`. A declared sigma
    window that the sample set violates makes the entry's aggregate
    ``status`` ``"fail"``, exactly as a single corner missing its limits
    does (except when the entry is already ``"error"``/``"inconclusive"``,
    both of which outrank it); without a declared ``k_sigma`` nothing about
    the existing rollup changes.

    A corner graded ``inconclusive`` contributes ``inconclusive`` to the
    aggregate regardless of what its own measurement-vs-``limits``
    comparison said -- so one distrusted corner masks a concurrent clean
    ``fail`` at *this* aggregate, exactly as one errored corner already
    does. That is deliberate (the aggregate reports the strongest claim
    about trustworthiness, not the union of every corner's verdict);
    ``corners[]`` and the top-level ``failed``/``inconclusive`` counts retain
    the per-corner detail. ``worst_case`` still scans every corner,
    distrusted ones included: it is a pointer to the corner worth looking
    at, and hiding the worst margin would make an inconclusive rollup harder
    to debug, not easier.
    """
    rollup: list[dict[str, Any]] = []
    for spec in measurements_spec:
        name = spec["name"]
        entries = []
        for corner in corners:
            for m in corner["measurements"]:
                if m["name"] == name:
                    entries.append((corner, m))
                    break

        agg_status = _aggregate_measurement_status(entries)

        worst_case = None
        worst_margin = None
        for corner, m in entries:
            if m["status"] == _ABSENT_STATUS:
                continue
            margin = m["margin"]
            sort_key = margin if margin is not None else float("-inf")
            if worst_case is None or sort_key < worst_margin:
                worst_margin = sort_key
                worst_case = {
                    "corner_id": corner["corner_id"],
                    "value": m["value"],
                    "margin": margin,
                }

        entry: dict[str, Any] = {
            "name": name,
            "unit": spec.get("unit"),
            "limits": spec.get("limits"),
            "status": agg_status,
            "worst_case": worst_case,
            # Additive/optional (issue #2482): `step`/`derived_from` for an
            # `analysis_steps[]` measurement; nothing for any other one.
            **sim_steps.measurement_provenance(spec),
        }
        plausible_range = spec.get("plausible_range")
        if plausible_range is not None:
            # Additive/optional (issue #2493): only present when this
            # measurement declared (or inherited from
            # `options.node_voltage_bounds`) a plausibility bound -- echoes
            # the resolved, normalised bound so a caller can see why an
            # entry graded `"inconclusive"` with an `implausible_solution`
            # diagnostic, without re-deriving the default-vs-override
            # resolution itself.
            entry["plausible_range"] = plausible_range
        if spec.get("spec_row") is not None:
            # Additive/optional (issue #3778): the spec row this measurement
            # grades, echoed only when declared so `klt signoff` can derive
            # row coverage from the envelope.
            entry["spec_row"] = spec["spec_row"]

        if monte_carlo is not None:
            mc_stats = _monte_carlo_rollup(
                entries,
                spec,
                monte_carlo["quantiles"],
                monte_carlo["k_sigma"],
            )
            if mc_stats is not None:
                # Additive: the key only exists for a measurement that
                # actually ran under `request.monte_carlo`, so a plain corner
                # matrix keeps today's exact entry shape.
                entry["monte_carlo"] = mc_stats
                window = mc_stats["sigma_window"]
                if monte_carlo.get("window_scope") == "per_corner":
                    # Issue #2960: every corner's own population is graded;
                    # the pooled window is still reported but no longer
                    # gates. A corner without a window (`null`) is skipped.
                    failing_corners = [
                        c["corner_id"]
                        for c in mc_stats["by_corner"]
                        if c["sigma_window"] is not None
                        and c["sigma_window"]["status"] == "fail"
                    ]
                    mc_stats["failing_corners"] = failing_corners
                    window_failed = bool(failing_corners)
                else:
                    window_failed = window is not None and window["status"] == "fail"
                if window_failed:
                    # `error` still outranks a limit violation, per the
                    # response's aggregate precedence -- and so does
                    # `inconclusive` (issues #2492 + #2493), for the same
                    # reason: the window is computed only over this
                    # measurement's *trusted* samples (see
                    # `_monte_carlo_rollup`'s `_stats`), so a measurement
                    # that also had a distrusted sample is still not a clean,
                    # fully-trustworthy `fail` and the window verdict must
                    # not overwrite that stronger claim.
                    if entry["status"] not in ("error", _INCONCLUSIVE_STATUS):
                        entry["status"] = "fail"

        rollup.append(entry)
    return rollup


# --------------------------------------------------------------------------- #
# Waveform artifact (rawfile -> JSON)
# --------------------------------------------------------------------------- #


def parse_ascii_rawfile(raw_path: str) -> dict[str, Any]:
    """Parse an ngspice ASCII rawfile (``set filetype=ascii`` + ``write``)
    into the documented waveform JSON shape (see ``docs/cli/sim.md``)::

        {
            "plotname": str,
            "variables": [{"index": int, "name": str, "type": str}, ...],
            "points": [[v0, v1, ...], ...],
        }

    ``variables[0]`` is always the sweep variable (``time`` for ``.tran``,
    frequency for ``.ac``, the swept source for ``.dc``). Raises
    :class:`SimError` if the file is not a recognisable ngspice ASCII
    rawfile.

    ``ac``/``sp`` analyses declare ``Flags: complex`` in the header, and
    every value in the ``Values:`` section is then written as a
    comma-separated ``real,imag`` pair rather than a bare float (verified
    against ngspice 46). Per the JSON contract's additive-only convention,
    ``points[]`` always holds plain real-valued floats -- for a ``Flags:
    complex`` rawfile, each column's magnitude (``hypot(real, imag)``) is
    emitted instead of the raw pair, applied uniformly across all columns
    including the sweep variable (whose imaginary part is always ``0``, so
    its magnitude equals its real part). See ``docs/cli/sim.md``'s waveform
    JSON shape section for the documented representation.
    """
    with _open_ascii_rawfile(raw_path) as (plotname, variables, points):
        return {"plotname": plotname, "variables": variables, "points": list(points)}


def _note_rawfile_header_field(meta: dict[str, Any], line: str) -> None:
    """Record the first ``Plotname:`` / ``Flags:`` / ``No. Variables:`` header
    line seen into ``meta`` (later duplicates are ignored)."""
    if meta["plotname"] is None and line.startswith("Plotname:"):
        meta["plotname"] = line[len("Plotname:") :].strip()
    elif meta["is_complex"] is None and line.startswith("Flags:"):
        meta["is_complex"] = "complex" in line.lower()
    elif meta["var_count"] is None:
        count_match = re.match(r"No\. Variables:\s*(\d+)$", line)
        if count_match is not None:
            meta["var_count"] = int(count_match.group(1))


def _scan_rawfile_header(
    handle: IO[str],
) -> tuple[dict[str, Any], bool, bool, list[str]]:
    """Read ``handle`` up to the ``Values:`` marker.

    Returns ``(meta, in_variables, saw_values, variable_lines)``.
    """
    meta: dict[str, Any] = {"plotname": None, "is_complex": None, "var_count": None}
    in_variables = False
    saw_values = False
    variable_lines: list[str] = []
    try:
        for raw_line in handle:
            line = raw_line.rstrip("\r\n")
            if line == "Values:":
                saw_values = True
                break
            if in_variables:
                variable_lines.append(line)
            elif line == "Variables:":
                in_variables = True
            else:
                _note_rawfile_header_field(meta, line)
    except OSError as exc:
        raise SimError(f"could not read rawfile: {exc}") from exc
    return meta, in_variables, saw_values, variable_lines


def _parse_rawfile_variables(
    variable_lines: list[str], var_count: int
) -> list[dict[str, Any]]:
    """Parse the ``Variables:`` table lines, checking the declared count."""
    variables: list[dict[str, Any]] = []
    for line in variable_lines:
        parts = line.split()
        if len(parts) < 3:
            continue
        variables.append({"index": int(parts[0]), "name": parts[1], "type": parts[2]})
    if len(variables) != var_count:
        raise SimError(
            f"malformed rawfile: declared {var_count} variables, "
            f"parsed {len(variables)}"
        )
    return variables


def _parse_rawfile_value(token: str, complex_values: bool) -> float:
    if not complex_values:
        return float(token)
    # "real,imag" -- reduce to a single real-valued magnitude, applied
    # uniformly to every column (including the sweep variable, whose
    # imaginary part is always 0 so its magnitude equals its real part).
    real_str, _, imag_str = token.partition(",")
    real = float(real_str)
    imag = float(imag_str) if imag_str else 0.0
    return math.hypot(real, imag)


def _iter_rawfile_points(
    handle: IO[str], var_count: int, complex_values: bool
) -> Iterator[list[float]]:
    """Lazily yield one ``list[float]`` per point from the ``Values:`` body."""
    current: list[float] = []
    for raw_line in handle:
        tokens = raw_line.split()
        if not tokens:
            continue
        if len(tokens) == 2 and current == []:
            # "<point_index> <value0>" -- start of a new point.
            current = [_parse_rawfile_value(tokens[1], complex_values)]
        elif len(tokens) == 1:
            current.append(_parse_rawfile_value(tokens[0], complex_values))
        else:
            continue
        if len(current) == var_count:
            yield current
            current = []


@contextlib.contextmanager
def _open_ascii_rawfile(
    raw_path: str,
) -> Iterator[tuple[str, list[dict[str, Any]], Iterator[list[float]]]]:
    """Open an ngspice ASCII rawfile and yield ``(plotname, variables,
    points)``, where ``points`` lazily yields one ``list[float]`` per point.

    The header is read line by line up to the ``Values:`` marker, so a
    non-rawfile or malformed header raises :class:`SimError` before any point
    is produced, and peak memory does not scale with the size of the
    ``Values:`` section (issue #3360). See :func:`parse_ascii_rawfile` for the
    format notes (``Flags: complex`` magnitude reduction in particular).
    """
    try:
        handle = open(raw_path, encoding="utf-8", errors="replace")  # noqa: SIM115
    except OSError as exc:
        raise SimError(f"could not read rawfile: {exc}") from exc
    with handle:
        meta, in_variables, saw_values, variable_lines = _scan_rawfile_header(handle)
        plotname = meta["plotname"]
        var_count = meta["var_count"]
        if plotname is None:
            raise SimError(f"not a recognisable ngspice rawfile: {raw_path}")
        if var_count is None:
            raise SimError(f"malformed rawfile (no variable count): {raw_path}")
        if not (in_variables and saw_values):
            raise SimError(
                f"malformed rawfile (no Variables/Values section): {raw_path}"
            )
        variables = _parse_rawfile_variables(variable_lines, var_count)
        points = _iter_rawfile_points(handle, var_count, bool(meta["is_complex"]))
        yield plotname, variables, points


def noise_representation(waveform: dict[str, Any]) -> str:
    """``"power"`` or ``"amplitude"`` for a parsed ``noise`` waveform dict
    (issue #3006).

    ngspice's ``sqrnoise`` squares every noise output vector. The rawfile
    records which mode produced it: the integrated plot is named
    ``Integrated Noise`` with ``voltage``/``current`` variables, or
    ``Integrated Noise - V^2 or A^2`` with ``voltage^2``/``current^2`` ones.
    """
    if str(waveform.get("plotname", "")).strip().endswith("V^2 or A^2"):
        return "power"
    for var in waveform.get("variables", []):
        if str(var.get("type", "")).endswith("^2"):
            return "power"
    return "amplitude"


def _parse_and_persist_waveform(raw_path: str) -> str:
    """Parse the rawfile at ``raw_path`` to the waveform JSON shape and write
    it alongside as ``<raw_path>.json``, returning that path.

    Kept as its own step (rather than inlining waveform data into the
    response) so the JSON contract's own response never grows to
    waveform-shaped size -- per the spike, waveform data is an optional
    *artifact*, referenced by path, not inlined.
    """
    json_path = raw_path + ".json"
    # Streamed point by point (issue #3360) so peak memory does not scale with
    # the rawfile; the output is byte-identical to `json.dump` of the dict
    # `parse_ascii_rawfile` returns.
    try:
        with _open_ascii_rawfile(raw_path) as (plotname, variables, points):
            with open(json_path, "w", encoding="utf-8") as handle:
                handle.write('{"plotname": ' + json.dumps(plotname))
                handle.write(', "variables": ' + json.dumps(variables))
                handle.write(', "points": [')
                for index, point in enumerate(points):
                    handle.write((", " if index else "") + json.dumps(point))
                handle.write("]}")
    except (SimError, ValueError):
        # Do not leave a truncated artifact behind a failed parse.
        with contextlib.suppress(OSError):
            os.remove(json_path)
        raise
    return json_path


# --------------------------------------------------------------------------- #
# Waveform plots (`--plot`, issue #1723)
# --------------------------------------------------------------------------- #

#: The first `v(...)`/`i(...)`-shaped (optionally derivative-prefixed, e.g.
#: `vd(...)`/`ir(...)`) parenthesized token in a `.meas` card's `spice` text
#: -- the signal that card is actually about. ngspice itself always lowercases
#: node/signal names in its rawfile `variables[].name` (see
#: `parse_ascii_rawfile`'s docstring example), so the reconstructed name below
#: is lowercased to match, regardless of how the request's own `.meas` text
#: capitalizes it.
_MEAS_SIGNAL_TOKEN_RE = re.compile(r"\b([a-z]{1,3})\(([^)]*)\)", re.IGNORECASE)

#: `from=<value>` / `to=<value>` trailers on an `AVG`/`RMS`/`PP`/`MIN`/`MAX`/
#: `INTEG` measurement -- the "measurement window" #1723 asks to shade on a
#: `tran` plot.
_MEAS_FROM_RE = re.compile(r"\bfrom\s*=\s*(\S+)", re.IGNORECASE)
_MEAS_TO_RE = re.compile(r"\bto\s*=\s*(\S+)", re.IGNORECASE)

#: `WHEN <signal>=<value>` on a threshold-crossing measurement -- the
#: `.meas` target #1723 asks to draw as a reference line. `\S+` between
#: `when` and the final `=` is deliberately greedy-then-backtracking so it
#: still matches when the signal token itself carries no space before its
#: own `=` (e.g. `when v(out)=1.5`, not `when v(out) = 1.5`).
_MEAS_WHEN_TARGET_RE = re.compile(
    r"\bwhen\s+\S+\s*=\s*([-+]?[\d.]+(?:[eE][-+]?\d+)?[a-zA-Z]*)", re.IGNORECASE
)


def _measurement_diagnostic_hints(spice: str) -> dict[str, Any]:
    """Best-effort extraction of the plotting context a `.meas` card's own
    ``spice`` text implies: which signal it is about, the time window it
    integrates/searches over (``from=``/``to=``), and the threshold value it
    searches for (``WHEN <signal>=<value>``).

    Never raises -- an unrecognised card shape (e.g. a ``TRIG``/``TARG``
    delay measurement, which has no single window or target this simple
    parser understands) yields ``{"signal": None, "window": None, "target":
    None}``, and the caller (:func:`_write_corner_plots`) just renders that
    signal's curve without the extra context rather than failing the whole
    plot batch. This is deliberately not a general SPICE expression
    evaluator -- see :mod:`klayout_tools.sim_plot`'s module docstring on
    scope.
    """
    signal_match = _MEAS_SIGNAL_TOKEN_RE.search(spice)
    signal = (
        f"{signal_match.group(1).lower()}({signal_match.group(2).lower()})"
        if signal_match
        else None
    )

    window: tuple[float, float] | None = None
    from_match = _MEAS_FROM_RE.search(spice)
    to_match = _MEAS_TO_RE.search(spice)
    if from_match is not None and to_match is not None:
        from_s = _parse_spice_seconds(from_match.group(1))
        to_s = _parse_spice_seconds(to_match.group(1))
        if from_s is not None and to_s is not None:
            window = (from_s, to_s)

    target: float | None = None
    when_match = _MEAS_WHEN_TARGET_RE.search(spice)
    if when_match is not None:
        target = _parse_plain_number(when_match.group(1))

    return {"signal": signal, "window": window, "target": target}


def _slugify_label(label: str) -> str:
    label = label.replace("/", "_")
    return label.replace(".", "p").replace("=", "").replace("-", "n").replace("@", "v_")


def _readable_slug(corner_id: str) -> str:
    return _slugify_label(corner_id)


#: ``<pvt>[/params:<pairs>][/options:<pairs>][/mc<n>]`` with at least one of
#: the two axis segments -- the shape of a parameter- or options-axis ID.
_PARAMS_ID_RE = re.compile(
    r"(?P<pvt>.*?)(?:/params:[^/]*/options:[^/]*|/params:[^/]*|/options:[^/]*)"
    r"(?:/mc\d+)?"
)

#: A readable slug longer than this many UTF-8 bytes is replaced by a bounded
#: form (readable prefix + hash of the full corner id) so the artifact
#: directory name stays well under the filesystem's ``NAME_MAX`` (255 bytes)
#: even with the ``__probe`` suffix or a ``__<signal>.svg`` plot filename.
_SLUG_MAX_BYTES = 100
_SLUG_PREFIX_BYTES = 80
_SLUG_HASH_CHARS = 12


def _bound_slug(readable: str) -> str:
    """Cut ``readable`` to ``_SLUG_PREFIX_BYTES`` UTF-8 bytes when it exceeds
    ``_SLUG_MAX_BYTES``; shorter slugs are returned unchanged."""
    encoded = readable.encode("utf-8")
    if len(encoded) <= _SLUG_MAX_BYTES:
        return readable
    return encoded[:_SLUG_PREFIX_BYTES].decode("utf-8", errors="ignore")


def _slugify_corner_id(corner_id: str) -> str:
    """Filesystem-safe, bounded transform of a corner's own ``corner_id``
    (e.g. ``ss/1.620V/125C`` -> ``ss_1p620V_125C``). Shared by
    :attr:`CornerPoint.slug` and the plot writer (which only has the report
    dict's ``corner_id``) so the two cannot drift.

    A parameter point (issue #3035) keeps its PVT slug (bounded like the
    readable form below) and appends the full
    SHA-256 of the whole ID (MC suffix included) -- parameter values never
    reach the filesystem, so close values cannot collide and no value can
    inject a path character.

    Otherwise short ids keep their readable form. When that exceeds
    ``_SLUG_MAX_BYTES`` bytes (many swept ``supply_v`` keys), it becomes a
    readable prefix plus a deterministic SHA-256 digest of the full
    ``corner_id`` (so ``mc<N>`` samples stay distinct); the full id is
    recoverable from the directory's ``corner.json``."""
    match = _PARAMS_ID_RE.fullmatch(corner_id)
    digest = hashlib.sha256(corner_id.encode("utf-8")).hexdigest()
    if match is not None:
        return f"{_bound_slug(_slugify_label(match.group('pvt')))}_params_{digest}"
    readable = _readable_slug(corner_id)
    prefix = _bound_slug(readable)
    if prefix == readable:
        return readable
    return f"{prefix}_{digest[:_SLUG_HASH_CHARS]}"


#: Any run of characters that is not alphanumeric/underscore, collapsed to a
#: single underscore -- turns a waveform variable name like ``v(out)`` into
#: a filesystem-safe ``v_out_`` for a deterministic plot filename.
_NON_FILENAME_CHARS_RE = re.compile(r"[^0-9A-Za-z_]+")


def _slugify_signal(signal_name: str) -> str:
    return _NON_FILENAME_CHARS_RE.sub("_", signal_name.lower()).strip("_") or "signal"


def _write_corner_plots(
    *,
    corner: dict[str, Any],
    plot_dir: str,
    analysis_kind: str | None,
    measurements_spec: list[dict[str, Any]],
) -> list[dict[str, str]]:
    """Render one SVG per non-sweep signal captured in ``corner``'s persisted
    waveform artifact, writing each to ``plot_dir`` with a deterministic
    filename (``<corner_slug>__<signal_slug>.svg``).

    Any `tran` measurement whose own signal (see
    :func:`_measurement_diagnostic_hints`) matches a rendered signal gets its
    ``from=``/``to=`` window shaded and its ``WHEN``-target drawn as a
    reference line on that signal's plot -- issue #1723's "the `.meas`
    target drawn as a line" / "the measurement window shaded" acceptance
    criteria. `ac` plots use a log-scaled frequency axis; `dc`/`tran` use a
    linear sweep axis (window shading only applies to `tran`, since
    ``from=``/``to=`` on a `dc`/`ac` measurement is over the swept
    source/frequency, not simulated time, and this issue's shading is
    specifically the *time* window the Proposal describes).

    Returns ``[]`` -- writing nothing -- when this corner has no persisted
    waveform artifact (an errored corner that never produced a rawfile) or
    that artifact cannot be read/parsed; never raises, since one corner's
    bad artifact should not abort plotting every other corner.
    """
    waveform_path = corner["artifacts"].get("waveform")
    if not waveform_path or not os.path.isfile(waveform_path):
        return []
    try:
        with open(waveform_path, encoding="utf-8") as handle:
            waveform = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return []

    variables = waveform.get("variables") or []
    points = waveform.get("points") or []
    if len(variables) < 2 or not points:
        return []

    sweep_name = variables[0]["name"]
    x_values = [point[0] for point in points]
    log_x = analysis_kind == "ac"

    hints_by_signal: dict[str, dict[str, Any]] = {}
    for spec in measurements_spec:
        hints = _measurement_diagnostic_hints(spec.get("spice", ""))
        if hints["signal"] is None:
            continue
        # Merge across every measurement naming this signal (e.g. a `PP`
        # window-measurement and a separate `WHEN` threshold-measurement
        # both over `v(out)`) -- the first *non-None* window/target found
        # wins for each field independently, rather than one measurement's
        # hints unconditionally shadowing another's.
        merged = hints_by_signal.setdefault(
            hints["signal"], {"window": None, "target": None}
        )
        if merged["window"] is None and hints["window"] is not None:
            merged["window"] = hints["window"]
        if merged["target"] is None and hints["target"] is not None:
            merged["target"] = hints["target"]

    corner_slug = _slugify_corner_id(corner["corner_id"])
    written: list[dict[str, str]] = []
    for var in variables[1:]:
        name = var["name"]
        y_values = [point[var["index"]] for point in points]
        hint = hints_by_signal.get(name.lower())
        window = hint["window"] if hint and analysis_kind == "tran" else None
        target = hint["target"] if hint else None
        svg = render_waveform_svg(
            signal_name=name,
            corner_id=corner["corner_id"],
            x_values=x_values,
            y_values=y_values,
            x_axis_label=sweep_name,
            log_x=log_x,
            window=window,
            target=target,
        )
        path = os.path.join(plot_dir, f"{corner_slug}__{_slugify_signal(name)}.svg")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(svg)
        written.append({"signal": name, "path": path})
    return written


def _measurement_plot_path(
    entry: dict[str, Any],
    spec: dict[str, Any] | None,
    plots_by_corner: dict[str, list[dict[str, str]]],
) -> str | None:
    """The SVG path (if any) belonging next to one measurement rollup
    ``entry`` -- its own ``.meas`` card's signal, rendered at its
    ``worst_case`` corner -- so a `--plot` run's miss report names the
    picture to look at next to the number that missed (issue #1723).

    Returns ``None`` when there is no worst-case corner (an empty corner
    matrix), ``spec`` is missing (should not happen -- every rollup entry
    comes from a ``measurements_spec`` entry), that card names no
    recognisable signal, or that corner rendered no matching plot.
    """
    worst_case = entry.get("worst_case")
    if worst_case is None or spec is None:
        return None
    corner_plots = plots_by_corner.get(worst_case["corner_id"])
    if not corner_plots:
        return None
    signal = _measurement_diagnostic_hints(spec.get("spice", ""))["signal"]
    if signal is None:
        return None
    for plot in corner_plots:
        if plot["signal"].lower() == signal:
            return plot["path"]
    return None
