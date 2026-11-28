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
import functools
import json
import math
import os
import re
import signal
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass
from typing import Any, NamedTuple

from . import _spice_number as spice_number

#: ``batch`` backend entry points (issue #2080) -- the `klt sim` consumer of
#: 2am's EDA batch fleet. Like ``sim_remote``'s: `run_sim` dispatches into them
#: and re-exports their helpers (self-aliased) as ``klayout_tools.sim.<name>``.
from . import (
    _warn,
    env_provenance,
    output_lock,
    sim_ac_waveform,
    sim_batch_history,
    sim_batch_pdk,
    sim_batch_resume,
    sim_commands,
    sim_complex_vectors,
    sim_corner_extremes,
    sim_crossings,
    sim_cycle_mean,
    sim_decks,
    sim_derive,
    sim_derive_rollup,
    sim_energy,
    sim_eye,
    sim_form_signals,
    sim_homotopy,
    sim_injected,
    sim_mismatch_declarations,
    sim_mismatch_gate,
    sim_mismatch_scale,
    sim_noise,
    sim_noise_mechanism,
    sim_noise_spectrum,
    sim_op_args,
    sim_passes,
    sim_plot,
    sim_portable,
    sim_probit_sigma,
    sim_search,
    sim_settled,
    sim_slope_band_meas,
    sim_snm_meas,
    sim_spiceinit,
    sim_step_resolution,
    sim_steps,
    sim_stimulus,
    sim_stress,
    sim_subckt_override,
    sim_supply_relative,
    sim_supply_waveform,
    sim_tian,
    sim_transfer_meas,
    sim_twoport,
    sim_waveform_opts,
    sim_xyce_refusals,
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
from . import sim_adapt as sim_adapt
from . import sim_analysis_overrides as _sao
from . import sim_bin_range as sim_bin_range
from . import sim_cause as sim_cause
from . import sim_environment_report as sim_env_report
from . import sim_netlist_identity as sim_netlist_identity
from . import sim_op_check as sim_op_check
from . import sim_peak_rss as sim_peak_rss
from . import sim_post_commands as sim_post_commands
from . import sim_provenance as sim_provenance
from . import sim_save_diagnostic as sim_save_diagnostic
from . import sim_staging as sim_staging
from . import sim_stepping_narration as sim_stepping_narration
from . import sim_transient_noise as sim_tn
from ._jsonnum import is_int, is_number
from ._paths import (
    _load_request_json,
    _resolve_relative,
    resolve_tool_binary,
    validate_request_shape,
)
from ._provenance import INPUT_ROLE_NETLIST, build_provenance
from ._provenance import sha256_file as sha256_file
from ._spice_number import parse_plain as _parse_plain_number
from ._spice_text import circuit_lines
from ._spice_text import logical_lines as _spice_text_logical_lines
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
from .ngspice_build import ENGINE_BANNER_MAX
from .ngspice_build import (
    engine_version as _extract_engine_version,
)
from .ngspice_build import (
    probe_engine_build as _probe_engine_build,
)
from .pdk import PdkNotFoundError, find_pdk
from .pdk_families import pdk_variant_family
from .remote_launcher import RemoteLauncher as RemoteLauncher
from .remote_launcher import RemoteLaunchError as RemoteLaunchError
from .sim_attach import apply_attach as _apply_attach
from .sim_batch import _build_batch_job_spec as _build_batch_job_spec
from .sim_batch import (
    _remove_shard_manifest,
    _run_batch,
    _run_batch_fleet,
    _validate_batch_models_lib,
    _validate_batch_pdk_root,
    attach_engine_build_comparison,
    attach_staged_model_comparison,
)
from .sim_batch import _resolve_batch_config as _resolve_batch_config
from .sim_batch_async import (
    SUBMIT_ONLY_UNSUPPORTED_CODE,
    refuse_hosts_over_units,
    refuse_sharded_deck_submit,
    shard_scoped_points,
)
from .sim_batch_async import submit_only as _submit_batch_only
from .sim_batch_features import (
    apply_worker_engine_build,
    finalize_batch_remote,
    pop_remote_diagnostics,
    worker_ngspice_binary,
)
from .sim_batch_hints import attach_runner_hints as _attach_runner_hints
from .sim_batch_knobs import _resolve_max_launch_attempts
from .sim_batch_plan import dry_run_job_ceiling
from .sim_batch_resume import _is_unrun_batch_corner, _record_batch_checkpoint
from .sim_checkpoint import (  # noqa: F401 -- keep runner checkpoint imports working
    DEFAULT_MC_SEED_MODE,
    DEFAULT_MC_SEED_SCOPE,
    DEFAULT_SAVE_MODE,
    _Checkpoint,
    _checkpoint_fingerprint,
    _checkpoint_path,
    _key_in_initial_conditions,
    _key_in_mc_mismatch_scale,
    _key_in_mc_seed_options,
    _load_checkpoint,
    restore_checkpoint,
)
from .sim_convergence_retry import (
    DEFAULT_CONVERGENCE_RETRY as DEFAULT_CONVERGENCE_RETRY,
)
from .sim_convergence_retry import (
    _validate_convergence_retry,
    is_retryable_convergence_failure,
    note_not_retried,
)

#: Moved to the ``sim_corner_*`` modules (issue #5428) so this module stays
#: under its per-file size ratchet; re-exported here -- self-aliased where
#: nothing below calls them -- so ``klayout_tools.sim.<name>`` keeps working.
from .sim_corner_axes import CORNER_OPTION_NAMES as CORNER_OPTION_NAMES
from .sim_corner_axes import NGSPICE_SOLVER_OPTIONS as NGSPICE_SOLVER_OPTIONS
from .sim_corner_axes import _matches_exclude as _matches_exclude
from .sim_corner_axes import _parse_process_entry as _parse_process_entry
from .sim_corner_axes import _validate_ngspice_options
from .sim_corner_keys import unknown_key_warnings
from .sim_corner_point import (
    CornerPoint,
    _format_param_value,
    _ic_report_field,
    _nodeset_report_field,
    _options_report_field,
    _params_report_field,
)
from .sim_corner_point import (
    _validate_param_name as _validate_param_name,
)
from .sim_corner_point import (
    _validate_param_scalar as _validate_param_scalar,
)
from .sim_corner_rules import (  # noqa: F401 -- retain the runner's selector imports
    _NODESET_MATCH_KEYS,
    _NODESET_MATCH_VALIDATORS,
    _apply_nodeset_rules,
    _check_nodeset_selector_names,
    _nodeset_match_number,
    _nodeset_match_params,
    _nodeset_match_process,
    _nodeset_match_supply,
    _validate_nodeset_match,
)
from .sim_corner_variants import _parse_variants as _parse_variants
from .sim_corners import (
    _corner_points_from_wire,
    _expand_corners,
    _normalize_nodeset_values,
)
from .sim_corners import _corner_points_to_wire as _corner_points_to_wire
from .sim_decks import run_deck_request
from .sim_echo_checks import (  # noqa: F401 -- codes stay public on ``sim``
    NODESET_UNSUPPORTED_WORKER_CODE,
    OFFHOST_BACKENDS,
    OPTIONS_UNSUPPORTED_WORKER_CODE,
    PARAMS_UNSUPPORTED_WORKER_CODE,
    SUPPLY_PRECISION_UNSUPPORTED_WORKER_CODE,
    _require_nodeset_echo,
    _require_options_echo,
    _require_params_echo,
    _require_supply_echo,
    check_ngspice_options_echo,
)

#: Moved to ``sim_environment_report`` (issue #5663) so this module stays
#: under its per-file size ratchet; the coverage helper is self-aliased so
#: ``klayout_tools.sim``'s historical attribute (and existing tests) keeps
#: working unchanged.
from .sim_environment_report import _corner_process_sections
from .sim_environment_report import (
    corner_coverage_environment as _corner_coverage_environment,
)
from .sim_expr_hint import unsupported_function_hint
from .sim_expr_log import expr_control_lines

#: Moved to ``sim_mismatch_activity`` (issue #8020) so this module stays
#: under its per-file size ratchet; re-exported here -- self-aliased where
#: nothing below calls them -- so ``klayout_tools.sim.<name>`` keeps working.
from .sim_mismatch_activity import _MISMATCH_ACTIVITY as _MISMATCH_ACTIVITY
from .sim_mismatch_activity import _classify_device_family as _classify_device_family
from .sim_mismatch_activity import (
    _detect_device_families,
    _mismatch_family_report,
    _mismatch_scan_closure,
)
from .sim_model_paths import resolve_lib_ref as _resolve_lib_ref

#: Moved to ``sim_monte_carlo`` / ``sim_monte_carlo_rollup`` (issue #8020)
#: so this module stays under its per-file size ratchet; re-exported here --
#: self-aliased where nothing below calls them -- so
#: ``klayout_tools.sim.<name>`` keeps working.
from .sim_monte_carlo import DEFAULT_MC_QUANTILES as DEFAULT_MC_QUANTILES
from .sim_monte_carlo import SUPPORTED_MC_SEED_MODES as SUPPORTED_MC_SEED_MODES
from .sim_monte_carlo import SUPPORTED_MC_SEED_SCOPES as SUPPORTED_MC_SEED_SCOPES
from .sim_monte_carlo import SUPPORTED_MC_VARY as SUPPORTED_MC_VARY
from .sim_monte_carlo import _derive_mc_seed as _derive_mc_seed
from .sim_monte_carlo import (
    _expand_monte_carlo,
    _resolve_mismatch_scale,
    _validate_mc_statistics_spec,
)
from .sim_monte_carlo import _validate_monte_carlo_spec as _validate_monte_carlo_spec
from .sim_monte_carlo_rollup import MC_WINDOW_SCOPES as MC_WINDOW_SCOPES
from .sim_monte_carlo_rollup import _base_corner_id as _base_corner_id
from .sim_monte_carlo_rollup import _evaluate_sigma_window as _evaluate_sigma_window
from .sim_monte_carlo_rollup import (
    _mc_window_scope_echo,
    _monte_carlo_rollup,
    _validate_mc_window_scope,
)
from .sim_monte_carlo_rollup import _percentile as _percentile
from .sim_monte_carlo_rollup import _quantile_key as _quantile_key
from .sim_monte_carlo_rollup import _sample_statistics as _sample_statistics
from .sim_netlist_includes import (
    absolutize_relative_include as _absolutize_relative_include,
)
from .sim_netlist_includes import expand_netlist_includes as _expand_netlist_includes
from .sim_osdi_pin import osdi_pdk_root_pin, resolve_request_osdi
from .sim_osdi_settle import (
    finalize_osdi_preload,
    osdi_runner_environment_fields,
    resettle_osdi_preload_for_local,
)
from .sim_pdk_ambiguity import warn_pdk_ambiguity as _warn_pdk_ambiguity
from .sim_pin_settle import (
    check_local_engine_version_pin,
    check_offhost_engine_version_pin,
    check_pdk_version_pin_early,
    settle_version_pins,
)
from .sim_process_legs import attach_process_leg_check
from .sim_spiceinit import with_thread_pin as _with_thread_pin

from .sim_analysis_overrides import (  # isort: skip
    AO_LABEL as _AO_LABEL,  # noqa: F401
    AO_UNSUPPORTED_WORKER_CODE as AO_UNSUPPORTED_WORKER_CODE,
    IC_LABEL as _IC_LABEL,
    IC_UNSUPPORTED_WORKER_CODE as IC_UNSUPPORTED_WORKER_CODE,
    _reject_param_braces,
    effective_analysis_args as _effective_analysis_args,
    ic_analysis_args as _ic_analysis_args,  # noqa: F401
    ic_cards as _ic_cards,
    report_field as _ao_report_field,
    require_plain_tran_for_ic as _require_plain_tran_for_ic,
    validate_analysis_overrides as _validate_analysis_overrides,
)

#: Moved to `sim_validate.py` / `sim_errors.py` (issue #3689); re-exported self-aliased.
from . import sim_errors, sim_population_limits, sim_regrade
from .sim_errors import SimError as SimError
from .sim_measurement_grading import (
    _ABSENT_STATUS as _ABSENT_STATUS,
)
from .sim_measurement_grading import (
    _INCONCLUSIVE_STATUS as _INCONCLUSIVE_STATUS,
)
from .sim_measurement_grading import (
    _RECOGNISED_LIMIT_KEYS as _RECOGNISED_LIMIT_KEYS,
)

#: Moved to `sim_measurement_grading.py` (issue #7320) so the measurement
#: sidecars import the grading primitives and shared response builders at
#: module scope instead of back-importing this runner; re-exported
#: (self-aliased where nothing below calls them) so
#: ``klayout_tools.sim.<name>`` keeps working.
from .sim_measurement_grading import (
    IMPLAUSIBLE_DIAGNOSTIC_CODE as IMPLAUSIBLE_DIAGNOSTIC_CODE,
)
from .sim_measurement_grading import (
    NOT_REACHED_STATUS as NOT_REACHED_STATUS,
)
from .sim_measurement_grading import (
    SAVE_MODE_NETLIST_NO_VALUE_HINT as SAVE_MODE_NETLIST_NO_VALUE_HINT,
)
from .sim_measurement_grading import (
    _cycle_mean_measurement_result as _cycle_mean_measurement_result,
)
from .sim_measurement_grading import (
    _energy_measurement_result as _energy_measurement_result,
)
from .sim_measurement_grading import (
    _evaluate_limits as _evaluate_limits,
)
from .sim_measurement_grading import (
    _eye_measurement_result as _eye_measurement_result,
)
from .sim_measurement_grading import (
    _grade_measurement_value as _grade_measurement_value,
)
from .sim_measurement_grading import (
    _settled_measurement_result as _settled_measurement_result,
)
from .sim_measurement_grading import (
    _step_resolution_measurement_result as _step_resolution_measurement_result,
)
from .sim_measurement_grading import (
    _stress_measurement_result as _stress_measurement_result,
)
from .sim_measurement_grading import (
    _tian_measurement_result as _tian_measurement_result,
)
from .sim_measurement_grading import (
    _vector_measurement_result as _vector_measurement_result,
)
from .sim_measurement_grading import (
    _within_plausible_range as _within_plausible_range,
)
from .sim_measurement_results import (
    MARGIN_AMBIGUOUS_DIAGNOSTIC_CODE as MARGIN_AMBIGUOUS_DIAGNOSTIC_CODE,
)
from .sim_measurement_results import (
    NOT_REACHED_DIAGNOSTIC_CODE as NOT_REACHED_DIAGNOSTIC_CODE,
)
from .sim_measurement_results import _corner_measurement_results, expand_request
from .sim_param_file import corner_lib_lines as corner_lib_lines
from .sim_param_file import param_file_environment, resolve_param_file
from .sim_plot import _measurement_diagnostic_hints as _measurement_diagnostic_hints
from .sim_plot import render_waveform_svg as render_waveform_svg
from .sim_population_limits import validate_k_sigma as _validate_k_sigma

#: Moved to `sim_raw.py` (issue #6752) so measurement modules read a waveform
#: without importing this runner; re-exported self-aliased for existing callers.
from .sim_raw import _parse_and_persist_waveform as _parse_and_persist_waveform
from .sim_raw import parse_ascii_rawfile as parse_ascii_rawfile
from .sim_raw import persist_waveform_with_header

#: The remaining names below are not called directly from this module -- they
#: are re-exported (self-aliased so linters recognise the re-export as
#: intentional) so ``klayout_tools.sim.<name>`` keeps working (issue #1764).
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
from .sim_request_overrides import (
    _check_capacity_wait_applies,
    _refuse_corner_only_deck_flags,
    _with_capacity_retries_override,
    _with_capacity_wait_override,
    refuse_corner_only_flags,
)
from .sim_request_overrides import (
    _with_max_concurrent_override as _with_max_concurrent_override,
)
from .sim_request_overrides import _with_max_units_override as _with_max_units_override
from .sim_request_overrides import _with_pdk_root_override as _with_pdk_root_override
from .sim_request_overrides import _with_timeout_override as _with_timeout_override
from .sim_slug import _NON_FILENAME_CHARS_RE as _NON_FILENAME_CHARS_RE
from .sim_slug import _PARAMS_ID_RE as _PARAMS_ID_RE
from .sim_slug import _SLUG_HASH_CHARS as _SLUG_HASH_CHARS
from .sim_slug import _SLUG_MAX_BYTES as _SLUG_MAX_BYTES
from .sim_slug import _SLUG_PREFIX_BYTES as _SLUG_PREFIX_BYTES
from .sim_slug import _bound_slug as _bound_slug
from .sim_slug import _readable_slug as _readable_slug
from .sim_slug import _slugify_corner_id as _slugify_corner_id
from .sim_slug import _slugify_label as _slugify_label
from .sim_slug import _slugify_signal as _slugify_signal
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
from .sim_window import parse_tran_window as _parse_tran_window

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
#: #2492), and the table ``docs/cli/sim.md``'s "Failure classification"
#: section documents. An explicit literal, so the vocabulary is readable in
#: one place; ``test_sim.py`` asserts it stays a superset of every code the
#: emitting modules (classifier tables, unrun-report paths, report-level
#: hints) can produce, resolving codes emitted through a named constant or
#: ``_unrun_corner_report`` (see :data:`_GRADING_MARKER_CODES`).
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
        "model_bin_range_preflight",
        "save_all_supersedes",
        "ngspice_fatal",
        "netlist_title_line",
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
        "op_metastable",  # `analysis.op_check` (issue #5077); a warning
        # Opt-in convergence retry record (`options.retry_on_convergence_error`,
        # issue #2973), attached by `_run_corner_with_retry` after the retry ran.
        "convergence_retry",
        # Waveform JSON conversion skipped over options.waveform_max_bytes
        # (issue #3211); always a warning.
        "waveform_too_large",
        # Incomplete `noise.mechanism` decomposition (#5553); warning.
        "noise_mechanism_incomplete",
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
        "pdk_stamp_mismatch",
        "pdk_stamp_not_comparable",
        "batch_job_interrupted",
        # Report-level (`environment.job_diagnostic`, issue #2970).
        "all_units_at_timeout",
        # Report-level batch runner version and request-feature checks (issue #2901).
        "batch_runner_request_feature_predated",
        "batch_runner_request_feature_unverified",
        "batch_runner_version_mismatch",
        sim_spiceinit.BATCH_INIT_UNVERIFIED,
        sim_spiceinit.MODEL_SELECTION_INIT_HINT,
        # Off-host expression diagnostics use the recorded runner identity.
        "expr_runner_may_predate",
        "expr_no_value",
        # Report-level (top-level `diagnostics[]`, issue #5385): process legs
        # with bit-identical results; a warning.
        "process_leg_check",
        # Report-level (top-level `diagnostics[]`, issue #5486): a selected
        # `*_mismatch` section whose family `mismatch_scale` did not reach.
        "mismatch_scale_unreached_family",
        # Report-level (top-level `diagnostics[]`, issue #6720); a warning.
        "batch_convergence_all_failed",
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

#: Grading markers and post-grading information are emitted downstream of
#: ``options.fail_on_diagnostic`` matching. Keep these codes outside the
#: selectable vocabulary. The drift guard derives them from emission sites.
_GRADING_MARKER_CODES: frozenset[str] = frozenset(
    {
        INCONCLUSIVE_DIAGNOSTIC_CODE,
        IMPLAUSIBLE_DIAGNOSTIC_CODE,
        MARGIN_AMBIGUOUS_DIAGNOSTIC_CODE,
        sim_homotopy.DC_HOMOTOPY_RECOVERY_CODE,
    }
)

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
#: caller-declared provenance of ``request.netlist``: a pre-layout schematic
#: netlist (S6 in the stage graph), a post-layout extracted netlist (S9),
#: or a gate-level netlist (structural netlist plus timing library,
#: issue #5638). Mirrors the caller-declares-intent pattern already used
#: for ``engine``/``backend`` -- rather than auto-detecting provenance from
#: the netlist file itself (a schematic and an extracted netlist can look
#: syntactically identical). See docs/cli/sim.md and docs/design/design-pipeline.md.
SUPPORTED_NETLIST_SOURCES = ("schematic", "extracted", "gate-level")

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

#: Host-level default backend (2am#1004): consulted only when neither the
#: ``--backend`` flag nor the request's own ``backend`` field names one. A
#: fleet host that should not run SPICE grids itself (the AWS dispatch
#: workers) sets ``KLT_SIM_BACKEND=batch`` in its daemon environment so every
#: sweep's ``klt sim`` goes to the Spot batch fleet without editing ~20
#: repos' request files. It is a *preference*, not a requirement -- see
#: :func:`resolve_backend` and ``yield_host_default_backend`` for the cases
#: where it steps aside to ``local``.
BACKEND_ENV = "KLT_SIM_BACKEND"


def resolve_backend(backend: str | None, request: dict[str, Any]) -> tuple[str, bool]:
    """``(backend, from_host_default)`` by the precedence ``--backend`` flag >
    ``request["backend"]`` > ``$KLT_SIM_BACKEND`` > ``"local"``.

    ``from_host_default`` is ``True`` only when the value came from the
    environment -- the one source that may later be overridden back to
    ``local`` by :func:`yield_host_default_backend`. An explicit flag or
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


def yield_host_default_backend(
    backend: str, from_host_default: bool, *, reason_ok: bool
) -> str:
    """Step an env-sourced off-host default back to ``local`` when
    ``reason_ok`` is ``False`` -- i.e. the run cannot or should not leave
    this host (an engine the off-host toolchain does not ship, a resumed
    checkpoint, a single-unit probe that would spend minutes waiting on a
    Spot instance to answer one corner). A no-op for an explicit choice.
    """
    if from_host_default and backend in OFFHOST_BACKENDS and not reason_ok:
        return "local"
    return backend


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
        sim_bin_range.MODELNAME_ABORT_RE,
    ),
    (
        # The generic shapes of an ngspice deck abort (issue #3879): `Error
        # on line N ...` followed by `Simulation interrupted due to error!`,
        # or `fatal error in ngspice`, or the library-parse form `Error in
        # netlist line no. N, new internal line no. M:` (issue #4603).
        # Last on purpose: every specific
        # pattern above names a cause, so this one is only the fallback cause
        # (`_classify_diagnostics` drops it when a specific fatal code was
        # already classified). The message is the first matching log line.
        "ngspice_fatal",
        re.compile(
            r"^[ \t]*Error on line \d+"
            r"|^[ \t]*Error in netlist line no\. \d+"
            r"|Simulation interrupted due to error"
            r"|fatal error in ngspice",
            re.IGNORECASE | re.MULTILINE,
        ),
    ),
)

#: Causes that supersede the `model_bin_range` reading of the same abort: a
#: rejected MOS level (#2814) and a mis-parsed title line (#5433) both make
#: ngspice say "could not find a valid modelname" with no bin range involved.
#: `model_bin_range` is itself a specific code, so the "a specific fatal code
#: already matched" rule below cannot drop it.
_BIN_RANGE_SUPERSEDERS = frozenset({"unsupported_model_level", "netlist_title_line"})

#: Moved to :mod:`sim_cause` (issue #6401); aliased for the callers here and
#: in :mod:`sim_batch`.
_NGSPICE_FATAL_CODES = sim_cause._NGSPICE_FATAL_CODES

#: Moved to :mod:`sim_cause` (issue #6401's helper shares it); aliased below.
_ANALYSIS_STARTED_RE = sim_cause._ANALYSIS_STARTED_RE

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
    """Read leading dotted integers, including from versions with a suffix."""
    match = re.match(r"\s*(\d+(?:\.\d+)*)", text)
    return tuple(int(part) for part in match.group(1).split(".")) if match else None


def _version_satisfies_pin(pin: tuple[int, ...], found: str | None) -> bool | None:
    """Compare the pinned components, returning None for an unreadable version."""
    parsed = _version_components(found) if found else None
    return parsed[: len(pin)] == pin if parsed is not None else None


def _validate_engine_version_pin(request: dict[str, Any]) -> tuple[int, ...] | None:
    """Parse an optional major or major.minor engine pin (issue #3041)."""
    pin = request.get("engine_version")
    if pin is None:
        return None
    if not isinstance(pin, str) or not re.fullmatch(r"\d+(?:\.\d+)?", pin.strip()):
        expected = "a string of the form 'MAJOR' or 'MAJOR.MINOR'"
        raise SimError(f"request.engine_version must be {expected} (got {pin!r})")
    return _version_components(pin) or None


def _check_engine_version_pin(
    pin: tuple[int, ...], found: str | None, engine: str, *, where: str
) -> None:
    """Raise engine_version_unmet when the reported version cannot satisfy the pin."""
    matches = _version_satisfies_pin(pin, found)
    if matches:
        return
    required = ".".join(str(part) for part in pin)
    if found is None:
        detail = f"{where} reported no {engine} version"
    elif matches is None:
        detail = f"{where} reported an unparseable {engine} version {found!r}"
    else:
        detail = f"{where} found {engine} {found}"
    raise SimError(
        f"pinned simulator unavailable: request requires {engine} {required}; {detail}",
        code=ENGINE_VERSION_UNMET_CODE,
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
    if not osdi_preload or engine != "ngspice" or backend in OFFHOST_BACKENDS:
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


def apply_engine_host(
    environment: dict[str, Any], *, backend: str, client_ngspice_binary: str | None
) -> None:
    """Stamp ``environment.engine_host``; keep ``ngspice_binary`` (#3110) and
    ``engine_build`` (#5566) honest -- off-host ``"worker"`` makes both the
    worker's, moving the submitter's to the matching ``client_*`` key. Batch
    adds ``models_lib_comparison`` (#5321) and the sharded roll-ups (#5522)."""
    if backend not in OFFHOST_BACKENDS:
        environment["engine_host"] = "local"
        return
    environment["engine_host"] = "worker"
    environment["client_ngspice_binary"] = client_ngspice_binary
    environment["ngspice_binary"] = worker_ngspice_binary(environment.get("remote"))
    apply_worker_engine_build(environment)
    finalize_batch_remote(environment, backend)


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

    Raises :class:`SimError` if the file is missing/unreadable, invalid JSON,
    not an object, or missing a required top-level field (``netlist``, one of
    ``analysis`` or -- #2482 -- ``analysis_steps``; which one, and that not
    both are declared, is validated in :func:`run_sim`). ``models`` is
    validated in :func:`run_sim`, only for a ``corners.process`` axis (see
    ``_resolve_models_lib``). No ``schema`` field is required.
    """
    request = _load_request_json(request_path, SimError)
    if not isinstance(request, dict):  # #7720: guard before membership tests
        raise SimError("request file must contain a JSON object")
    if sim_commands.COMMANDS_FIELD in request or "decks" in request:
        sim_errors.request_options(request)  # #7720: before first read
        if sim_commands.COMMANDS_FIELD in request:
            return request
        if not isinstance(request.get("decks"), list) or not request["decks"]:
            raise SimError("request.decks must be a non-empty list of deck objects")
        return request
    request = validate_request_shape(
        request, "request file", error_cls=SimError, required_fields=("netlist",)
    )
    if "analysis" not in request and "analysis_steps" not in request:
        raise SimError(
            "request is missing required field: analysis (or analysis_steps "
            "for an ordered multi-step sequence)"
        )
    for field in ("options", "monte_carlo"):
        value = request.get(field)
        if value is not None and not isinstance(value, dict):
            raise SimError(
                f"request.{field} must be an object (got {type(value).__name__})"
            )
    return expand_request(request)  # issue #5731; see sim_measurement_template


def _validate_search_targets(
    measurements_spec: list[dict[str, Any]],
    netlist_path: str,
    corner_points: list[Any],
) -> None:
    """Issues #4121/#5391: pre-dispatch check of every search target."""
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
        {name for point in corner_points for name in point.params},
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
        sim_post_commands.reject_in_steps(request)  # issues #4014/#6080
        sim_op_check.reject_in_steps(request)  # issue #5077
        return sim_steps.resolve_analysis_steps(request)
    analysis = request.get("analysis") or {}
    # #3338: `twoport` compiles to `ac`; #5078: `op` defaults `args` to "".
    analysis = sim_op_args.default_op_args(sim_twoport.resolve_analysis(analysis))
    if "kind" not in analysis or "args" not in analysis:
        raise SimError("request.analysis requires 'kind' and 'args'")
    reject_unsupported_analysis(analysis["kind"])
    # Issue #3159: `analysis.passes` (AC only) is validated and normalised
    # here, before any engine launch; absent `passes` returns `analysis` as is.
    analysis = sim_passes.resolve_analysis(analysis)
    sim_post_commands.validate_linearize(analysis)
    sim_op_check.validate(analysis)  # issue #5077
    measurements_spec = request.get("measurements", [])
    # Issue #2533: `spice` (a verbatim top-level `.meas` card) and `expr` (an
    # expression evaluated after the analysis, inside the generated
    # `.control` block) are the two declarable forms, exactly one per entry
    # -- see `_validate_measurement_forms` for the full rule set.
    _validate_measurement_forms(measurements_spec)
    sim_post_commands.validate(analysis, measurements_spec)  # issue #6080
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
    sim_steps.refuse_scalar_intermediate(measurements_spec)
    # Issue #2868: a crossings series is a transient-time quantity.
    sim_crossings.validate_crossings_context(measurements_spec, analysis)
    # Issue #3033: an energy window is a transient-time quantity too.
    sim_energy.validate_energy_context(measurements_spec, analysis)
    # Issue #4592: likewise a stress window.
    sim_stress.validate_stress_context(measurements_spec, analysis)
    # Issue #2947: so is a cycle-aligned mean.
    sim_cycle_mean.validate_cycle_mean_context(measurements_spec, analysis)
    # Issue #2923: and a transfer metric is an AC-sweep quantity.
    sim_transfer_meas.validate_transfer_context(measurements_spec, analysis)
    # Issue #3160: a tian metric combines the named passes of `analysis.passes`.
    sim_tian.validate_tian_context(measurements_spec, analysis)
    # Issue #3895: and an snm margin is a DC-sweep quantity.
    sim_snm_meas.validate_snm_context(measurements_spec, analysis)
    sim_slope_band_meas.validate_slope_band_context(measurements_spec, analysis)
    # Issue #4292: an eye fold is a transient-time quantity too.
    sim_eye.validate_eye_context(measurements_spec, analysis)
    # Issue #4773: a step-resolution report is a transient-time quantity too.
    sim_step_resolution.validate_step_resolution_context(measurements_spec, analysis)
    sim_settled.validate_settled_context(measurements_spec, analysis)
    # Issue #4121: a search entry needs the scalar single-analysis deck.
    sim_search.validate_search_context(measurements_spec, analysis)
    # Issue #2938: a `noise` entry is lowered to an `expr` (plus helper
    # lines) so everything downstream treats it as one.
    sim_noise.validate_noise_context(measurements_spec, analysis)
    return analysis, sim_noise.lower_measurements(measurements_spec, analysis)


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
        _warn.warn(f"warning: {diag['message']}")
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
            "stands_for": env_provenance.repo_relative_path(root, repo_root=repo_root),
        }
        if token == "$MODELS_DIR" and models_lib_sha256 is not None:
            entry["models_lib_sha256"] = models_lib_sha256
        out.append(entry)
    return out


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


DEFAULT_WAVEFORM_MAX_BYTES = sim_waveform_opts.DEFAULT_WAVEFORM_MAX_BYTES


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


def _required_save_vectors(
    save_vectors: tuple[str, ...], measurements_spec: list[dict[str, Any]]
) -> list[str]:
    """``options.save_vectors`` plus every ``v(x)``/``i(x)`` vector the
    declared measurements reference, de-duplicated, in first-seen order.

    Every form that names a signal contributes its texts -- free-text fields
    verbatim, structured-form fields via :mod:`sim_form_signals`.

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
        step_res = spec.get("step_resolution")
        if isinstance(step_res, dict):
            texts.append(step_res.get("signal"))
        texts.extend(sim_settled.signal_texts(spec))
        texts.extend(sim_form_signals.signal_texts(spec))
        for text in texts:
            if not isinstance(text, str):
                continue
            for prefix, args in sim_form_signals.SAVE_TOKEN_RE.findall(text):
                kind = prefix[0].lower()
                for arg in args.split(","):
                    arg = arg.strip().lower()
                    if arg:
                        vector = f"{kind}({arg})"
                        if vector not in vectors:
                            vectors.append(vector)
    return vectors


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
    require_pdk_match: bool | None = None,
    submit_only: bool = False,
    attach_job_id: str | None = None,
    capacity_wait_s: float | None = None,
    capacity_retries: int | None = None,
    command_approvals: str | None = None,
    pdk_root: str | None = None,
    collect_partial: bool = False,
    timeout_s: float | None = None,
    _ownership: contextlib.ExitStack | None = None,
) -> dict[str, Any]:
    """Run the request at ``request_path`` and return its report.

    A request carrying ``commands`` (issue #4443) is a command-list request:
    operator-approved argv steps run in isolated unit directories, locally
    or as one batch job -- see :mod:`klayout_tools.sim_commands`.
    ``command_approvals`` is the trusted approvals file (else
    ``$KLT_SIM_COMMAND_APPROVALS``). ``pdk_root`` (issue #4758) pins the PDK
    install root like ``models.pdk_root`` and wins over it; corner requests only.

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

    With ``submit_only=True``, batch returns a job identity for later status
    or attachment. A deck-list job has a null ceiling. Unsupported combinations
    raise ``submit_only_unsupported`` before upload.

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

    A dry run validates and expands corner requests, including off-host staging
    and the batch ceiling/split plan. It returns the planned units and seeds
    without a probe, an output lock or a dispatch.
    """
    request = sim_batch_pdk.apply_request_gate(
        load_request(request_path), require_pdk_match
    )
    # Issue #2892: the engine runs with ``cwd=<corner dir>``, so a relative
    # ``artifacts_dir`` baked into the command line would resolve against the
    # corner dir and ngspice would never find its deck. Absolutise once here.
    if artifacts_dir is not None:
        artifacts_dir = os.path.abspath(artifacts_dir)
    refuse_corner_only_flags(request, pdk_root, timeout_s, collect_partial)
    if sim_commands.COMMANDS_FIELD in request:
        return _dispatch_command_request(
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
            dry_run=dry_run,
            submit_only=submit_only,
            attach_job_id=attach_job_id,
            capacity_wait_s=capacity_wait_s,
            command_approvals=command_approvals,
            _ownership=_ownership,
        )
    if command_approvals is not None:
        raise SimError(
            "--command-approvals only applies to command-list requests (`commands`)"
        )
    if sim_decks.DECKS_FIELD in request:
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
            max_units_per_job=max_units_per_job,
            max_concurrent_jobs=max_concurrent_jobs,
            collect_partial=collect_partial,
            dry_run=dry_run,
            submit_only=submit_only,
            attach_job_id=attach_job_id,
            capacity_wait_s=capacity_wait_s,
            _ownership=_ownership,
        )
    return _run_sim_corners(
        request_path,
        request=request,
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
        capacity_retries=capacity_retries,
        pdk_root=pdk_root,
        collect_partial=collect_partial,
        timeout_s=timeout_s,
        _ownership=_ownership,
    )


def _dispatch_command_request(
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
    dry_run: bool,
    submit_only: bool,
    attach_job_id: str | None,
    capacity_wait_s: float | None,
    command_approvals: str | None,
    _ownership: contextlib.ExitStack | None,
) -> dict[str, Any]:
    """Route a command-list request (issue #4443) to
    :mod:`klayout_tools.sim_commands`: backend resolved as for deck lists
    (a single unit stays local under a host default), and every flag with
    no command-list meaning refused by name."""
    for flag, given in (
        ("--dry-run", dry_run),
        ("--submit-only", submit_only),
        ("--attach", attach_job_id is not None),
        ("--wait-for-capacity", capacity_wait_s is not None),
        ("--resume/options.resume", bool(resume)),
        ("--hosts", hosts not in (None, 1)),
    ):
        if given:
            raise SimError(f"{flag} does not apply to command-list requests")
    _refuse_corner_only_deck_flags(
        budget_s, plot_dir, fail_fast_probe, fail_on_diagnostic
    )
    resolved_backend, from_host_default = resolve_backend(backend, request)
    if resolved_backend not in SUPPORTED_BACKENDS:
        source = f" (from ${BACKEND_ENV})" if from_host_default else ""
        raise SimError(
            f"unsupported backend '{resolved_backend}'{source} "
            f"(supported: {', '.join(SUPPORTED_BACKENDS)})"
        )
    resolved_backend = yield_host_default_backend(
        resolved_backend,
        from_host_default,
        reason_ok=len(request.get(sim_commands.COMMANDS_FIELD) or ()) > 1,
    )
    request_dir = os.path.dirname(os.path.abspath(request_path))
    if _ownership is not None:
        _own_persistent_roots(
            _ownership,
            sim_commands.resolve_outdir(artifacts_dir, request_dir),
            None,
            persistent=True,
        )
    return sim_commands.run_command_request(
        request,
        request_dir=request_dir,
        backend=resolved_backend,
        approvals_path=command_approvals,
        max_workers=max_workers,
        artifacts_dir=artifacts_dir,
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
    dry_run: bool = False,
    submit_only: bool = False,
    attach_job_id: str | None = None,
    capacity_wait_s: float | None = None,
    max_units_per_job: int | None = None,
    max_concurrent_jobs: int | None = None,
    collect_partial: bool = False,
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
    resolved_backend = yield_host_default_backend(
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
        if not resume and plot_dir is None:
            refuse_sharded_deck_submit(hosts)
    _apply_attach(
        {},
        attach_job_id,
        resolved_backend,
        1 if hosts is None else hosts,
        submit_only,
        collect_partial,
    )
    # Issue #5378: the split knobs fold into `request.batch` for a deck list.
    request = _with_max_units_override(request, max_units_per_job)
    request = _with_max_concurrent_override(request, max_concurrent_jobs)
    _check_capacity_wait_applies(capacity_wait_s, resolved_backend, attach_job_id)
    # Issue #4543: the CLI value wins over `batch.capacity_wait_s`, and is
    # validated before any S3 write.
    request = _with_capacity_wait_override(request, capacity_wait_s)
    if dry_run and (submit_only or attach_job_id is not None):
        raise SimError("--dry-run cannot be combined with --submit-only or --attach")
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
    # Issue #3625: the `engine_version` pin (issue #3041) applies to decks too
    # -- they run on ngspice just like corners. Local backends are probed
    # before anything runs; off-host ones are checked against the report below.
    engine_version_pin = _validate_engine_version_pin(request)
    if engine_version_pin is not None and resolved_backend not in OFFHOST_BACKENDS:
        check_local_engine_version_pin(
            engine_version_pin,
            resolved_backend,
            "ngspice",
            _resolve_ngspice_binary(request.get("options") or {}, request_dir),
        )
    # Issue #3168: a deck run always writes (and a remote/batch pull replaces)
    # unit trees under its outdir, so it owns that root unconditionally --
    # after the flag checks above, which must fail without locking anything.
    # A submit-only run collects nothing, so it owns no unit trees.
    if _ownership is not None and not submit_only and not dry_run:
        _own_persistent_roots(
            _ownership,
            sim_decks.resolve_deck_outdir(artifacts_dir, request_dir),
            None,
            persistent=True,
        )
    report = run_deck_request(
        request,
        request_dir=request_dir,
        backend=resolved_backend,
        max_workers=max_workers,
        artifacts_dir=artifacts_dir,
        resume=bool(resume),
        submit_only=submit_only,
        attach_job_id=attach_job_id,
        dry_run=dry_run,
        collect_partial=collect_partial,
    )
    check_offhost_engine_version_pin(
        engine_version_pin,
        resolved_backend,
        "ngspice",
        (report.get("environment") or {}).get("engine_version"),
    )
    return report


def _refuse_unsupported_submit_only(
    backend: str, hosts: int, resume: bool | None, plot_dir: str | None
) -> None:
    """``--submit-only`` is one batch job: refuse everything else by name,
    before the first S3 write (issue #3073). ``hosts`` is unused (#5281)."""
    reason = None
    if backend != "batch":
        reason = f"requires backend 'batch' (resolved backend: '{backend}')"
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
    if backend not in OFFHOST_BACKENDS:
        return request
    return {**request, "options": {**options, "wall_clock_budget_s": budget_s}}


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
    adapt_environment: Any
    netlist_overrides_report: Any
    netlist_path: Any
    netlist_source: Any
    ngspice_binary: Any
    ngspice_init_lines: Any
    ngspice_options: Any
    transient_noise_seed: int | None
    osdi_preload: Any
    osdi_runner_environment: Any
    param_file: Any
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
    waveform_decimate: Any


def _prepare_sim_request(
    request_path: str,
    *,
    request: dict[str, Any],
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
    pdk_root: str | None = None,
    capacity_retries: int | None = None,
    timeout_s: float | None = None,
) -> _ResolvedSimRequest:
    """The up-front validate-and-resolve phase of :func:`_run_sim_corners`.

    ``request`` is the caller's already-loaded request (the
    :func:`load_request` result from :func:`run_sim` or batch preflight);
    the file is read and its measurement templates expanded exactly once
    per entry point, never re-read here. ``request_path`` is still used to
    resolve relative inputs and provenance.

    Everything here runs before the first artifact write or backend dispatch;
    the order of the checks is the error precedence the JSON reports, so it
    must not be reordered."""
    request = _with_max_units_override(request, max_units_per_job)
    request = _with_max_concurrent_override(request, max_concurrent_jobs)
    request = _with_capacity_wait_override(request, capacity_wait_s)
    request = _with_capacity_retries_override(request, capacity_retries)
    request = _with_pdk_root_override(request, pdk_root)
    request = _with_timeout_override(request, timeout_s)
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
            f"unsupported engine '{engine}' "
            f"(supported: {', '.join(SUPPORTED_ENGINES)}); "
            "to run a non-SPICE simulator (e.g. iverilog) use a `commands` request "
            "(docs/cli/sim.md)"
        )

    backend_source = _backend_source(backend, request)
    backend, backend_from_host_default = resolve_backend(backend, request)
    requested_backend = backend
    backend_from_host_default = sim_batch_pdk.allow_backend_fallback(
        request, backend, backend_from_host_default
    )
    if backend not in SUPPORTED_BACKENDS:
        source = f" (from ${BACKEND_ENV})" if backend_from_host_default else ""
        raise SimError(
            f"unsupported backend '{backend}'{source} "
            f"(supported: {', '.join(SUPPORTED_BACKENDS)})"
        )
    # Off-host workers use ngspice. Keep a valid Xyce request local when
    # the host default selects an off-host backend.
    backend = yield_host_default_backend(
        backend, backend_from_host_default, reason_ok=engine == "ngspice"
    )

    hosts = (
        hosts if hosts is not None else (request.get("remote") or {}).get("hosts", 1)
    )
    if not is_int(hosts) or hosts < 1:
        raise SimError("remote.hosts must be a positive integer")

    # Issue #3873: opt-in `request.adapt` (private adapted copies, host-local).
    request, adapt_env, backend = sim_adapt.prepare(
        request,
        request_dir,
        engine,
        backend,
        backend_from_host_default,
        _resolve_relative,
    )

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
    sim_netlist_identity.check_expected_netlist_identity(
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
    # Its ambiguity warning is emitted once the backend is final (#5449).
    provenance_pdk: dict[str, Any] | None = None
    if models.get("pdk") or models.get("pdk_root"):
        try:
            provenance_pdk = find_pdk(
                variant=models.get("pdk"), root=models.get("pdk_root")
            )
        except PdkNotFoundError:
            provenance_pdk = None

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
        # Issue #6967: the per-measurement dispersion bound, validated (and
        # normalised in place) before any corner is dispatched, like every
        # other measurement field above.
        sim_population_limits.validate_spec(spec, request.get("monte_carlo"))

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
    # `models.lib` -- see `resolve_request_osdi`. Validated for every engine
    # so a typo'd path is always named; `engine: "xyce"` then refuses the
    # option outright in `sim_xyce_refusals.enforce_support_boundary` rather than
    # silently dropping it.
    osdi_runner_resolved = sim_staging.runner_resolved_osdi(request, request_dir)
    osdi_preload = resolve_request_osdi(
        request, request_dir, frozenset(osdi_runner_resolved)
    )
    # Issue #2668 staging and issue #5325's `options.pdk_revision`: both are
    # validated for every backend (a typo'd value is always named, never
    # silently dropped) but only *act* off-host, in `stage_sim_job`.
    stage_model_inputs = sim_staging.stage_model_inputs_requested(request)
    sim_provenance.pdk_revision_asserted(request)
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
    backend = yield_host_default_backend(
        backend,
        backend_from_host_default,
        reason_ok=not osdi_preload or stage_model_inputs,
    )
    if osdi_preload and backend in OFFHOST_BACKENDS and not stage_model_inputs:
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
    osdi_preload, osdi_runner_environment = finalize_osdi_preload(
        options,
        request_dir,
        backend in OFFHOST_BACKENDS,
        osdi_preload,
        osdi_runner_resolved,
        pdk_root=osdi_pdk_root_pin(request),
        pdk_variant=models.get("pdk"),
    )
    # Issue #3041: an `engine_version` pin is checked before any corner runs
    # where this process can see the engine (local backends); off-host
    # backends are checked against the report's `engine_version` afterwards.
    engine_version_pin = _validate_engine_version_pin(request)
    check_local_engine_version_pin(
        engine_version_pin, backend, engine, ngspice_binary or XYCE_BINARY
    )
    # Issue #4438: an OSDI ABI the local ngspice cannot load is refused up
    # front rather than discovered inside ngspice, once per corner.
    _check_local_osdi_abi(osdi_preload, backend, engine, ngspice_binary or XYCE_BINARY)
    # Issue #4046: a `models.pdk_version` pin is checked the same way, before
    # any corner runs; off-host backends reject it rather than pass silently.
    # Issue #5500: unless the off-host backend is a host default a late yield
    # may still step back to `local` -- then only the pin's *value* is
    # validated here and the comparison waits for `settle_version_pins` at
    # the last yield, so a host default cannot refuse a valid local run.
    check_pdk_version_pin_early(models, backend, backend_from_host_default)
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
        if not is_int(max_workers) or max_workers < 1:
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
        if not is_number(budget_s) or budget_s <= 0:
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
    waveform_max_bytes = sim_waveform_opts.validate_max_bytes(options)
    waveform_decimate = sim_waveform_opts.validate_decimate(options)
    # Issue #3111: same up-front validation; off-host workers re-read it from
    # the forwarded request document, and the echo check below verifies it.
    ngspice_options = _validate_ngspice_options(options.get("ngspice_options"))
    transient_noise_seed = sim_tn.validate_seed(
        options.get("transient_noise_seed"), engine
    )
    # Issue #3036: the rule grammar is validated up front; the rules are
    # resolved onto points after expansion, before any dispatch.
    nodeset_rules = _validate_nodeset_rules(options.get("nodeset"))
    # Issue #3881: same grammar, applied as `.ic` + `uic` on a transient.
    ic_rules = _validate_nodeset_rules(
        options.get("initial_conditions"), label=_IC_LABEL
    )
    _require_plain_tran_for_ic(bool(ic_rules), analysis)
    analysis_override_rules = _validate_analysis_overrides(
        request.get("analysis_overrides"), analysis, engine
    )

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
    sim_derive.validate_grid(measurements_spec, corner_points, request)
    sim_post_commands.refuse_xyce(engine, analysis)  # issue #6080
    sim_xyce_refusals.enforce_support_boundary(
        engine=engine,
        backend=backend,
        corners_spec=corners_spec,
        monte_carlo_declared=monte_carlo_spec is not None,
        fail_fast_probe=fail_fast_probe,
        measurements_spec=measurements_spec,
        osdi_preload_declared=bool(osdi_preload or osdi_runner_resolved),
        save_mode_declared=save_mode_declared,
        save_vectors_declared=save_vectors_declared,
        ngspice_options_declared=bool(ngspice_options),
        nodeset_declared=bool(nodeset_rules),
        initial_conditions_declared=bool(ic_rules),
        analysis_steps_declared=sim_steps.is_steps_analysis(analysis),
        linearize_declared=analysis.get("linearize") is True,
        op_check_declared=sim_op_check.is_declared(analysis),
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
    _sao.resolve(corner_points, [] if explicit_points_wire else analysis_override_rules)
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
            mismatch_scale=_resolve_mismatch_scale(
                monte_carlo_spec,
                models.get("pdk"),
                corner_points=corner_points,
                models=_model_resolution_models(request, models),
                request_dir=request_dir,
            ),
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
        monte_carlo_info = sim_tn.with_unseeded_noise(
            monte_carlo_info, netlist_path, request=request, repo_root=repo_root
        )
        quantiles, k_sigma = _validate_mc_statistics_spec(monte_carlo_spec)
        window_scope = _validate_mc_window_scope(monte_carlo_spec)
        monte_carlo_stats = {
            "quantiles": quantiles,
            "k_sigma": k_sigma,
            "window_scope": window_scope,
            "probit_sigma": sim_probit_sigma.validate(
                monte_carlo_spec, measurements_spec
            ),
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

    # Issue #6778: a `--regrade` second pass rides this same request -- the
    # internal `_regrade` field selects the re-run units (after this
    # module's own expansion, so corner identities and Monte Carlo seeds are
    # preserved), `options.tightened_rerun` is validated on every run, and a
    # request without either returns the expansion unchanged.
    corner_points = sim_regrade.prepare_regrade(
        corner_points, request, analysis, engine
    )

    # Issue #2995: per-instance mismatch gating. Validated before any probe or
    # backend dispatch; the gated netlist copy is what local backends run.
    dispatch_netlist_path, monte_carlo_info = _maybe_gate_instances(
        netlist_path,
        monte_carlo_spec,
        monte_carlo_info,
        models.get("pdk"),
        backend=backend,
        explicit_points=explicit_points_wire is not None,
        gate_context=sim_mismatch_declarations.gate_context(
            models, request_dir, corner_points, _resolve_lib_ref
        ),
    )

    # Issue #3919: `.subckt` override of the include closure. Applied on top of
    # the (possibly gated) netlist; refused up front on an off-host backend.
    dispatch_netlist_path, netlist_overrides_report = _maybe_override_subckts(
        request,
        dispatch_netlist_path,
        request_dir=request_dir,
        # The rewritten closure cannot leave this host, so an env-sourced
        # off-host default yields to local (an explicit choice is refused).
        backend=yield_host_default_backend(
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
    supply_maps = [point.supply_v for point in corner_points]
    sim_crossings.validate_supply_relative_levels(measurements_spec, supply_maps)
    sim_supply_relative.validate_limits(
        measurements_spec, supply_maps, monte_carlo_spec
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
    param_file = _resolve_param_file(corner_points, resolution_models, request_dir)

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
    offhost_before_yield = backend in OFFHOST_BACKENDS
    backend = yield_host_default_backend(
        backend, backend_from_host_default, reason_ok=not resume
    )
    # Issue #5492: that yield can land a run whose OSDI list was settled
    # off-host back on this host -- re-settle it against what executes.
    yielded_on_host = offhost_before_yield and backend not in OFFHOST_BACKENDS
    # A conditional expression, not an `if`: a new branch here would push this
    # function past its `complexity-baseline.json` value without sign-off.
    osdi_preload, osdi_runner_environment = (
        resettle_osdi_preload_for_local(
            request, request_dir, backend, engine, ngspice_binary or XYCE_BINARY
        )
        if yielded_on_host and (osdi_preload or osdi_runner_environment)
        else (osdi_preload, osdi_runner_environment)
    )
    # Issue #5500: the same yield decides which engine and PDK the
    # `engine_version` / `models.pdk_version` pins must be checked against.
    settle_version_pins(
        request,
        backend,
        engine,
        ngspice_binary or XYCE_BINARY,
        engine_version_pin,
        settle=yielded_on_host,
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
        adapt_environment=adapt_env,
        netlist_overrides_report=netlist_overrides_report,
        netlist_path=netlist_path,
        netlist_source=netlist_source,
        ngspice_binary=ngspice_binary,
        ngspice_init_lines=ngspice_init_lines,
        ngspice_options=ngspice_options,
        transient_noise_seed=transient_noise_seed,
        osdi_preload=osdi_preload,
        osdi_runner_environment=osdi_runner_environment,
        param_file=param_file,
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
        waveform_decimate=waveform_decimate,
    )


def _open_resume_checkpoint(
    checkpoint_path: str,
    *,
    resume: bool,
    fingerprint: Callable[[], str],
) -> tuple[dict[str, dict[str, Any]], str | None, _Checkpoint | None]:
    """Hash the request inputs and open a checkpoint only when resuming."""
    if not resume:
        return {}, None, None
    return restore_checkpoint(checkpoint_path, fingerprint())


def _run_sim_corners(
    request_path: str,
    *,
    request: dict[str, Any],
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
    capacity_retries: int | None = None,
    pdk_root: str | None = None,
    collect_partial: bool = False,
    timeout_s: float | None = None,
    _ownership: contextlib.ExitStack | None = None,
) -> dict[str, Any]:
    """The corner-sweep body of :func:`run_sim` -- every parameter, the
    request/response contract, and the deck-list dispatch alternative
    are documented there. Kept as its own function so the deck dispatch
    in ``run_sim`` adds nothing to this sweep's own complexity budget.
    ``request`` is ``run_sim``'s already-loaded request, passed through so
    the request file is read and expanded once per run."""
    resolved = _prepare_sim_request(
        request_path,
        request=request,
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
        capacity_retries=capacity_retries,
        pdk_root=pdk_root,
        timeout_s=timeout_s,
    )
    analysis = resolved.analysis
    artifacts_dir = resolved.artifacts_dir
    backend = resolved.backend
    backend_from_host_default = resolved.backend_from_host_default
    backend_source = resolved.backend_source
    budget_s = resolved.budget_s
    capture_only = resolved.capture_only
    convergence_retry = resolved.convergence_retry
    corner_points = shard_scoped_points(resolved, attach_job_id)
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
    per_corner_specs = sim_derive.per_corner_measurements(measurements_spec)
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
    transient_noise_seed = resolved.transient_noise_seed
    osdi_preload = resolved.osdi_preload
    osdi_runner_environment = resolved.osdi_runner_environment
    param_file = resolved.param_file
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
    waveform_decimate = resolved.waveform_decimate

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
        persistent=bool(keep_artifacts or resume or backend in OFFHOST_BACKENDS),
        dry_run=dry_run,
    )
    checkpoint_path = _checkpoint_path(artifacts_dir)
    fingerprint_inputs: dict[str, Any] = {
        "pdk_requirement": sim_batch_pdk.checkpoint_requirement(request),
        "netlist_path": dispatch_netlist_path,
        "models_lib": models_lib,
        "analysis": analysis,
        "measurements_spec": per_corner_specs,
        "corner_points": corner_points,
        "timeout_s": timeout_s,
        "engine": engine,
        "osdi_preload": osdi_preload,
        "corner_section_libs": corner_section_libs,
        "save_mode": save_mode,
        "save_vectors": save_vectors,
        "complex_vectors": complex_vectors,
        "ngspice_options": ngspice_options,
        "transient_noise_seed": transient_noise_seed,
        "stimulus_lines": stimulus_lines,
    }
    # The checkpoint, run history and shard map share one digest of these inputs.
    shard_fingerprint = functools.cache(
        functools.partial(_checkpoint_fingerprint, **fingerprint_inputs)
    )
    pre_completed, loaded_engine_version, checkpoint = _open_resume_checkpoint(
        checkpoint_path, resume=bool(resume), fingerprint=shard_fingerprint
    )

    # Issue #5775: a sharded batch run keeps its shard map even without
    # `--resume` (issue #3896), so `--resume` harvests it *before* the
    # dispatch set is computed -- collecting the shards whose remote jobs
    # are already terminal and shrinking `dispatch_points` to what is
    # genuinely outstanding. Skipped for `--attach` (the explicit re-attach
    # form), `--submit-only` (refused with `--resume` before any S3 write)
    # and `--dry-run` (which must not touch S3 at all).
    harvested_version, harvested_env = sim_batch_resume.maybe_harvest_resume_shards(
        resume=resume,
        backend=backend,
        checkpoint=checkpoint,
        dry_run=dry_run,
        submit_only=submit_only,
        attach_job_id=attach_job_id,
        artifacts_dir=artifacts_dir,
        corner_points=corner_points,
        request=request,
        netlist_path=dispatch_netlist_path,
        timeout_s=timeout_s,
        keep_artifacts=keep_artifacts,
        want_waveforms=want_waveforms,
        measurements_spec=per_corner_specs,
        request_dir=request_dir,
        pre_completed=pre_completed,
    )
    if loaded_engine_version is None:
        # An all-harvested rerun dispatches nothing, so the harvested jobs'
        # engine version is the only one this report can carry.
        loaded_engine_version = harvested_version

    dispatch_points = (
        [point for point in corner_points if point.corner_id not in pre_completed]
        if resume
        else corner_points
    )
    fleet_hosts = sim_batch_resume.resume_hosts(hosts, corner_points, dispatch_points)
    sim_subckt_override.refuse_on_host_default(
        netlist_overrides_report,
        backend_is_offhost=backend in OFFHOST_BACKENDS,
        backend=backend,
        backend_from_host_default=backend_from_host_default,
        n_points=len(dispatch_points),
    )
    # A single-unit run under a host default stays here: one corner does not
    # repay a Spot instance's minutes of boot, and it is exactly the "probe
    # one corner locally" case 2am#1004's ruling keeps local.
    offhost_before_yield = backend in OFFHOST_BACKENDS
    backend = yield_host_default_backend(
        backend, backend_from_host_default, reason_ok=len(dispatch_points) > 1
    )
    # Issue #5492: the unit count is only known here, so this is the one yield
    # that can move the run on-host after the OSDI list was already settled
    # for an off-host backend -- re-settle it against the backend that runs.
    yielded_on_host = offhost_before_yield and backend not in OFFHOST_BACKENDS
    osdi_preload, osdi_runner_environment = (  # expression, see the resume site
        resettle_osdi_preload_for_local(
            request, request_dir, backend, engine, ngspice_binary or XYCE_BINARY
        )
        if yielded_on_host and (osdi_preload or osdi_runner_environment)
        else (osdi_preload, osdi_runner_environment)
    )
    # The final backend determines which OSDI files are local. Freeze all
    # input digests together before the probe or any corner can run.
    netlist_digests = sim_env_report.NetlistDigests.capture(
        request, netlist_path, repo_root=repo_root
    )
    model_digests = sim_env_report.ModelInputDigests.capture(
        models_lib, osdi_preload, corner_section_libs
    )
    # Issue #5500: this is the *last* yield, so `backend` is final here. Both
    # version pins are settled against it when the yield just moved the run
    # on-host, and the `models.pdk_version` comparison deferred by
    # `check_pdk_version_pin_early` is due now for a run that really is
    # leaving this host -- either way before anything is dispatched.
    settle_version_pins(
        request,
        backend,
        engine,
        ngspice_binary or XYCE_BINARY,
        engine_version_pin,
        settle=yielded_on_host
        or (backend_from_host_default and backend in OFFHOST_BACKENDS),
    )
    # Issue #2977: which backend actually ran, and where the request for it
    # came from -- so a host-policy audit can spot a `local` run although
    # `$KLT_SIM_BACKEND=batch` asked otherwise.
    backend_provenance = {
        "resolved": backend,
        "source": backend_source,
        "requested": requested_backend,
    }
    # Issue #5449: this warning's remedy is backend-dependent, and the backend
    # is final only after the yields above. Keyed on the pre-yield value, a run
    # that executes locally under `$KLT_SIM_BACKEND=batch` was told `$PDK_ROOT`
    # was its only pin -- false there -- disagreeing with the `--format text`
    # `pdk: WARNING` line. Emitted from the recorded value so they cannot drift.
    _warn_pdk_ambiguity(provenance_pdk, backend=backend_provenance["resolved"])
    # Issue #2924: off-host workers run the whole `klt sim` request and return
    # only the scalar results (the AC rawfile never leaves the worker), so the
    # `transfer` form is gated by the mandatory runner-feature check in
    # `sim_batch`/`sim_remote` rather than refused here.
    # Issues #3159/#3161: `analysis.passes` / `measurements[].tian` are gated
    # the same way (the `dual_injection` mandatory runner feature).

    # Hoisted above the probe (issue #6047): it takes the corners' pinned init.
    effective_threads = _effective_threads_per_worker(
        threads_per_worker, backend, max_workers, len(dispatch_points)
    )
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
        and backend not in OFFHOST_BACKENDS
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
            ngspice_init=_with_thread_pin(
                ngspice_init_lines, effective_threads, engine
            ),
            osdi_preload=osdi_preload,
            save_mode=save_mode,
            ngspice_options=ngspice_options,
            transient_noise_seed=transient_noise_seed,
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

    # Off-host workers resolve the engine, diagnostic policy and ngspice init
    # from their staged request. These local kwargs describe the client host.
    # Forwarding the resolved fail_on_diagnostic policy would grade those corners twice.
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
    ngspice_init_env = sim_spiceinit.record(  # Issue #5648
        ngspice_init_lines, effective_threads, engine, backend in OFFHOST_BACKENDS
    )
    local_engine_kwargs = (
        {
            "portable_roots": portable_roots,
            "threads_per_worker": effective_threads,
            "pdk_root": provenance_pdk["root"]
            if provenance_pdk and engine == "ngspice"
            else None,
            "engine": engine,
            "ngspice_binary": ngspice_binary,
            "fail_on_diagnostic": fail_on_diagnostic_codes,
            "ngspice_init": ngspice_init_lines,
            "osdi_preload": osdi_preload,
            "save_mode": save_mode,
            "save_vectors": save_vectors,
            "complex_vectors": complex_vectors,
            "waveform_max_bytes": waveform_max_bytes,
            "waveform_decimate": waveform_decimate,
            "ngspice_options": ngspice_options,
            "transient_noise_seed": transient_noise_seed,
            "stimulus_lines": stimulus_lines,
            "convergence_retry": convergence_retry,
        }
        if backend not in OFFHOST_BACKENDS
        else {"request_dir": request_dir}
    )
    _apply_attach(
        local_engine_kwargs,
        attach_job_id,
        backend,
        hosts,
        submit_only,
        collect_partial,
    )
    _check_capacity_wait_applies(capacity_wait_s, backend, attach_job_id)

    # Issue #2668: stage the complete job closure once, before any backend
    # runs; see `_preflight_staged_model_inputs` for the rationale.
    staged_model_environment = _preflight_staged_model_inputs(
        request,
        netlist_path,
        request_dir=request_dir,
        backend=backend,
        enabled=stage_model_inputs,
        dry_run=dry_run,
    )

    bin_diagnostic = sim_bin_range.preflight_with_bins(
        netlist_path, models_lib, _find_width_ceiling_culprit, _MODEL_WIDTH_CEILING_UM
    )
    save_diagnostic = sim_save_diagnostic.preflight(
        netlist_path, engine, save_mode, save_vectors, want_waveforms
    )
    if dry_run:
        published_runner: dict[str, Any] = {}
        feature_diagnostic: dict[str, Any] | None = None
        job_ceiling: dict[str, Any] | None = None
        if backend == "batch":
            feature_diagnostic, job_ceiling = dry_run_job_ceiling(
                request,
                corner_count=len(dispatch_points),
                timeout_s=timeout_s,
                runner_out=published_runner,
                hosts=fleet_hosts,
            )
        return _build_dry_run_report(
            request,
            feature_diagnostic=feature_diagnostic,
            job_ceiling=job_ceiling,
            bin_range_diagnostic=bin_diagnostic,
            save_diagnostic=save_diagnostic,
            published_runner=published_runner or None,
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
        refuse_hosts_over_units(hosts, len(dispatch_points))
        return _submit_batch_only(
            hosts=hosts,
            request=request,
            corner_points=dispatch_points,
            netlist_path=netlist_path,
            netlist_sha256=netlist_digests.sha256,
            timeout_s=timeout_s,
            keep_artifacts=keep_artifacts,
            want_waveforms=want_waveforms,
            request_dir=request_dir,
            artifacts_dir=artifacts_dir,
            shard_fingerprint=shard_fingerprint,
        )

    history = sim_batch_history.begin(
        artifacts_dir,
        shard_fingerprint if backend == "batch" else None,
        request,
        hosts,
        bool(resume),
        bool(pre_completed),
        attach_job_id,
    )
    if not dispatch_points and backend in OFFHOST_BACKENDS and attach_job_id is None:
        # An all-covered resume dispatches nothing (#5775).
        corners_new, backend_engine_version, remote_environment = [], None, None
    elif hosts == 1 or attach_job_id is not None:  # #5777: attach never fans out
        corners_new, backend_engine_version, remote_environment = _BACKENDS[backend](
            corner_points=dispatch_points,
            netlist_path=dispatch_netlist_path,
            models_lib=models_lib,
            analysis=analysis,
            measurements_spec=per_corner_specs,
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
            **(
                {
                    "shard_fingerprint": shard_fingerprint,
                    "netlist_sha256": netlist_digests.sha256,
                    "explicit_points": dispatch_points
                    if len(dispatch_points) < len(corner_points)
                    and attach_job_id is None
                    else None,
                }
                if backend == "batch"
                else {}
            ),
        )
    elif backend in OFFHOST_BACKENDS:
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
        # The explicit fleet needs its fingerprint for the shard-to-job-id map.
        shard_kwargs: dict[str, Any] = (
            {
                "shard_fingerprint": shard_fingerprint(),
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
            hosts=fleet_hosts,
            measurements_spec=per_corner_specs,
            request_dir=request_dir,
            **shard_kwargs,
        )
    else:

        def _shard_runner(
            shard_points: list[CornerPoint],
        ) -> tuple[list[dict[str, Any]], str | None, dict[str, Any] | None]:
            return _BACKENDS[backend](
                corner_points=shard_points,
                concurrent_shards=len(dispatch_points) > 1,
                netlist_path=dispatch_netlist_path,
                models_lib=models_lib,
                analysis=analysis,
                measurements_spec=per_corner_specs,
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
            measurements_spec=per_corner_specs,
        )

    _require_params_echo(
        dispatch_points, corners_new, backend=backend, env=remote_environment
    )
    _require_supply_echo(
        dispatch_points, corners_new, backend=backend, env=remote_environment
    )
    _require_nodeset_echo(dispatch_points, corners_new, backend=backend)
    _require_nodeset_echo(
        dispatch_points,
        corners_new,
        backend=backend,
        label=_IC_LABEL,
        attr="initial_conditions",
        code=IC_UNSUPPORTED_WORKER_CODE,
    )
    _sao.require_echo(dispatch_points, corners_new, backend)
    sim_op_check.require_echo(analysis, corners_new, backend, OFFHOST_BACKENDS)
    _require_options_echo(
        dispatch_points, corners_new, backend=backend, env=remote_environment
    )
    # Issue #6778: a runner that predates `--regrade` ignores the request's
    # selection field and runs the whole matrix -- refuse that by id.
    sim_regrade.require_regrade_echo(request, dispatch_points, corners_new, backend)
    _record_batch_checkpoint(checkpoint, backend, corners_new, backend_engine_version)
    remote_environment = sim_batch_resume.merge_resume_environment(
        harvested_env, remote_environment
    )

    corners = sim_batch_resume.reassemble_corners(
        bool(resume), corner_points, corners_new, pre_completed
    )
    _attach_meas_lint(corners, analysis, per_corner_specs)
    engine_version = (
        backend_engine_version
        if backend_engine_version is not None
        else loaded_engine_version
    )
    check_offhost_engine_version_pin(
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
    # Issue #3367: a corner that never ran on `batch` (`_UNRUN_BATCH_CODES`)
    # is outstanding too, so a checkpoint holding the rest must outlive this
    # run. Kept out of `corners_skipped` (the reported
    # `environment.budget.corners_skipped`) to leave its meaning unchanged.
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

    remote_environment = sim_batch_history.finish(
        history, remote_environment, corners, len(corner_points)
    )
    sharded_attach = attach_job_id is not None and attach_job_id.endswith(".json")
    if (
        backend == "batch"
        and corners_skipped == 0
        and corners_lost == 0
        and not sharded_attach
    ):
        # Issue #3300: nothing left to re-attach to. A sharded `--attach`
        # keeps its map (issue #4721): a multi-leg caller (`klt pex --attach`)
        # interrupted after this leg collected still needs it on the next
        # attach, and removes the maps once its whole run succeeded.
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
        signals_by_name, hints_by_signal = sim_plot.prepare_annotations(
            measurements_spec
        )
        os.makedirs(plot_dir, exist_ok=True)
        for corner in corners:
            corner_plots = sim_plot.write_corner_plots(
                corner=corner,
                plot_dir=plot_dir,
                analysis_kind=analysis.get("kind"),
                hints_by_signal=hints_by_signal,
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
        for entry in measurements_rollup:
            entry["plot"] = sim_plot.measurement_plot_path(
                entry, signals_by_name.get(entry["name"]), plots_by_corner
            )

    grid_stats = sim_probit_sigma.rollup(monte_carlo_stats, corner_points, corners)
    coverage = _build_coverage(
        measurements_rollup, corners, capture_only=capture_only, grid_stats=grid_stats
    )
    measurements_rollup.extend(grid_stats)
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
        "ngspice_init": ngspice_init_env,
        # Issue #1274: normalised like every other path field this response
        # echoes -- the resolved library usually lives outside the repo (a
        # PDK under the user's home directory), which reports
        # `{"path": null, "scope": "external"}` rather than the absolute
        # path. `models_lib_sha256` right below (plus `provenance.deck`)
        # still pins exactly which library ran.
        "models_lib": env_provenance.repo_relative_path(
            models_lib, repo_root=repo_root
        ),
        "models_lib_sha256": model_digests.models_lib_sha256,
        # Additive: `models_lib_sha256` is always hashed on the submitting
        # host, so for an off-host backend it is the *client's* resolution,
        # not necessarily what the runner loaded (see
        # `environment.remote.runner_model_inputs`).
        "models_lib_sha256_scope": (
            "client" if remote_environment is not None else "local"
        ),
        **netlist_digests.environment(attach_job_id, resume, remote_environment),
    }
    environment.update(
        param_file_environment(
            param_file,
            lambda p: env_provenance.repo_relative_path(p, repo_root=repo_root),
        )
    )
    environment.update(_threads_environment(effective_threads))
    environment.update(_corner_coverage_environment(request, corner_points))
    environment.update(netlist_digests.closure)
    # Additive/optional (issue #3111): the declared solver options, verbatim,
    # so the setting is part of the report's evidence.
    environment.update({"ngspice_options": ngspice_options} if ngspice_options else {})
    sim_netlist_identity.stamp_netlist_identity(
        environment, netlist_identity, netlist_identity_error
    )
    environment.update(sim_env_report.netlist_source_environment(netlist_source))
    environment.update(model_digests.environment(repo_root=repo_root))
    # Additive/optional (issue #3819): `.osdi` entries the executing host
    # resolved itself -- named, never hashed; the worker's own report is
    # the source of what actually loaded.
    environment.update(osdi_runner_environment_fields(osdi_runner_environment))
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
        sim_tn.transient_noise_environment(
            netlist_path,
            transient_noise_seed=transient_noise_seed,
            request=request,
            analysis=analysis,
            engine=engine,
            repo_root=repo_root,
        )
    )
    # Additive/optional (issue #5779): the device-multiplier lint's findings
    # -- warning-only, never a failing exit.
    environment.update(
        sim_env_report.device_multiplier_environment(
            netlist_path, request=request, repo_root=repo_root
        )
    )
    environment.update(
        sim_env_report.remote_entry_environment(
            remote_environment,
            request=request,
            corners_lost=corners_lost,
            artifacts_dir=artifacts_dir,
            checkpoint_path=checkpoint_path if checkpoint_retained else None,
        )
    )
    apply_engine_host(
        environment, backend=backend, client_ngspice_binary=ngspice_binary
    )
    environment.update(sim_env_report.backend_environment(backend_provenance))
    # Issue #2977: additive, only when declared; the schedule as applied (skew
    # and transition times resolved, PWL points included) -- see docs/cli/sim.md.
    environment.update(
        sim_stimulus.environment_entry(stimulus_signals, stimulus_by_corner)
    )
    # Additive/optional (issue #3919): only present when the request declares
    # `netlist_overrides` -- see docs/cli/sim.md.
    environment.update(_netlist_overrides_environment(netlist_overrides_report))
    environment.update(resolved.adapt_environment)
    environment.update(sim_env_report.monte_carlo_environment(monte_carlo_info))
    # Additive/optional (issue #2973): the resolved retry policy, only when
    # `options.retry_on_convergence_error` opted in.
    environment.update(_convergence_retry_environment(convergence_retry))
    # Issue #6778: the declared tightened-rerun settings (present in both
    # passes) and, for a regrade run itself, the applied selection.
    environment.update(
        sim_regrade.regrade_environment_entries(request, analysis, engine)
    )
    environment.update(
        sim_env_report.budget_environment(
            budget_s,
            elapsed_s=round(time.monotonic() - sweep_started, 3),
            exceeded=budget_exceeded,
            corners_skipped=corners_skipped,
        )
    )
    # `orphaned` reflects the always-on orphan check (see `initial_ppid` above).
    environment.update(sim_env_report.orphaned_environment(orphaned))
    environment.update(sim_env_report.timeout_preflight_environment(preflight_warning))
    environment.update(sim_env_report.fail_fast_probe_environment(probe_result))
    environment.update(
        sim_env_report.resume_environment(
            resume,
            resumed_corners=len(pre_completed),
            checkpoint_path=checkpoint_path,
            checkpoint_retained=checkpoint_retained,
            repo_root=repo_root,
        )
    )
    _attach_timeout_budget_hint(environment, corners, timeout_s)

    remote = environment.get("remote")
    remote_diagnostics = pop_remote_diagnostics(remote)

    report = {
        "schema_version": SCHEMA_VERSION,
        "netlist": env_provenance.repo_relative_path(netlist_path, repo_root=repo_root),
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
            deck_sha256=model_digests.models_lib_sha256,
            pdk=provenance_pdk,
            input_path=netlist_path,
            input_role=INPUT_ROLE_NETLIST,
        ),
        "measurements": measurements_rollup,
        "corners": corners,
    }
    netlist_digests.pin_provenance_input(report["provenance"])
    # Additive/optional (#2901 runner feature, #4976 bin-range preflight).
    diag_candidates = (
        *remote_diagnostics,
        sim_spiceinit.report_init_diagnostic(
            backend, engine, ngspice_init_lines, remote, corners
        ),
        bin_diagnostic,
        save_diagnostic,
    )
    if top_diags := [d for d in diag_candidates if d]:
        report["diagnostics"] = top_diags
    _attach_runner_hints(report, per_corner_specs, corners, backend=backend)
    attach_process_leg_check(report, corners)
    sim_mismatch_scale.attach_unreached_families(
        report, monte_carlo_info, dispatch_points
    )
    _attach_not_reached_count(report, per_corner_specs, corners)
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
    # Count distrusted corners separately from report-wide population verdicts.
    inconclusive = sum(1 for c in corners if c["status"] == _INCONCLUSIVE_STATUS)
    # Sigma windows, probit estimates and derived measurements can fail across
    # the grid even when all individual corners pass.
    rollup_statuses = {m["status"] for m in measurements_rollup}
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
        failed=bool(failed) or "fail" in rollup_statuses,
        errored=bool(errored) or "error" in rollup_statuses,
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
    if status != "error" and (inconclusive or _INCONCLUSIVE_STATUS in rollup_statuses):
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


#: Top-level request keys ``klt sim`` reads (or documents). A dry run
#: reports anything else as a warning (issue #3064) -- never an error, so no
#: existing request starts failing.
_KNOWN_REQUEST_KEYS = frozenset(
    (
        "adapt analysis analysis_steps analysis_overrides backend batch commands"
        " confidence corners decks engine engine_version exclude"
        " expect_netlist_identity measurements models monte_carlo netlist"
        " netlist_overrides netlist_source options remote schema"
        " schema_version target_ci_halfwidth _explicit_points _regrade"
    ).split()
    + [sim_staging.STAGED_MODEL_INPUTS_FIELD, sim_stimulus.REQUEST_KEY]
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
    staged_model_environment: dict[str, Any],
    feature_diagnostic: dict[str, Any] | None = None,
    job_ceiling: dict[str, Any] | None = None,
    bin_range_diagnostic: dict[str, Any] | None = None,
    save_diagnostic: dict[str, Any] | None = None,
    published_runner: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """The ``klt sim --dry-run`` response (issue #3064): the request as
    :func:`_run_sim_corners` expanded it, with nothing simulated or submitted.

    Built from the same ``corner_points`` / ``monte_carlo_info`` the real run
    dispatches, so the per-sample seeds are exactly the ones a real run uses.
    ``job_ceiling`` (issue #5674) is the submit path's own whole-job-ceiling
    assessment, emitted additively under the ``environment.remote.job_ceiling``
    name and shape; ``None`` leaves the field out.
    """
    max_launch_attempts = None
    if backend == "batch":
        # Validate the model paths (#3917, #5449) and resolve the per-job limit
        # even on hosts where the provision script or bucket is unavailable.
        _validate_batch_models_lib(request)
        _validate_batch_pdk_root(request)
        max_launch_attempts = _resolve_max_launch_attempts(request.get("batch") or {})
    units: list[dict[str, Any]] = []
    for point in corner_points:
        unit: dict[str, Any] = {"corner_id": point.corner_id}
        unit.update(_nodeset_report_field(point))
        unit.update(_ic_report_field(point))
        unit.update(_ao_report_field(point))
        if point.sample_index is not None:
            unit["sample_index"] = point.sample_index
        if point.mc_seed is not None:
            for key in ("rndseed", "process_seed", "mismatch_seed"):
                unit[key] = point.mc_seed[key]
        units.append(unit)
    warnings = unknown_key_warnings(request, _KNOWN_REQUEST_KEYS)
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
    if max_launch_attempts is not None:
        report["batch"] = {"max_launch_attempts": max_launch_attempts}
    if job_ceiling is not None:
        report["job_ceiling"] = job_ceiling
    if published_runner is not None:
        report["published_runner"] = published_runner
    if dry_diags := [
        d for d in (feature_diagnostic, bin_range_diagnostic, save_diagnostic) if d
    ]:
        report["diagnostics"] = dry_diags
    return report


def _preflight_staged_model_inputs(
    request: dict[str, Any],
    netlist_path: str,
    *,
    request_dir: str,
    backend: str,
    enabled: bool,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Stage the complete off-host job closure once, before any backend runs
    (issue #2668), so a missing model input, unresolvable dependency or
    exceeded cap is a named :class:`SimError` before any S3 write, SSH push
    or instance launch -- single job, remote fleet and batch shards alike.

    Returns the additive ``environment`` keys describing what the worker
    receives -- ``staged_model_inputs``, and ``pdk_revision_asserted`` when
    the request declared one (:mod:`sim_provenance` owns both shapes) -- or
    ``{}`` on a local run, which also leaves a declared
    ``options.pdk_revision`` validated but unechoed.

    A dry run stages the netlist closure even with the option off, so it
    reports the same netlist refusals the real submit would, before any run
    directory exists.
    """
    if backend not in OFFHOST_BACKENDS or not (enabled or dry_run):
        return {}
    staged_job = sim_staging.stage_sim_job(
        request,
        netlist_path,
        request_dir=request_dir,
        netlist_staged_name=remote_transport.REMOTE_NETLIST_FILENAME,
        reserved_names=(remote_transport.REMOTE_REQUEST_FILENAME,),
        backend=backend,
    )
    if not enabled:
        return {}
    return {
        "staged_model_inputs": [asset.to_json() for asset in staged_job.model_assets],
        **sim_provenance.pdk_revision_environment(request),
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


def _resolve_param_file(points: list[CornerPoint], models: Any, request_dir: str):
    """``models.param_file`` (#4759); see :mod:`klayout_tools.sim_param_file`."""
    return resolve_param_file(points, models, request_dir, _resolve_lib_ref)


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
    :func:`resolve_request_osdi`, rather than left to surface as a
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


# --------------------------------------------------------------------------- #
# Initial-guess hints: `options.nodeset` (issue #3036)
# --------------------------------------------------------------------------- #

_NODESET_RULE_KEYS = ("match", "values")


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


# --------------------------------------------------------------------------- #
# Transient start states: `options.initial_conditions` (issue #3881)
# --------------------------------------------------------------------------- #
#
# Same rule grammar and per-point resolution as `options.nodeset`, but the
# map is a transient *start state*: `.ic v(node)=volts` cards plus `uic` on
# the `tran` command (a `.nodeset` is only a DC-solve guess and is dropped
# once `uic` skips the operating point).


# --------------------------------------------------------------------------- #
# Monte Carlo sampling
# --------------------------------------------------------------------------- #
#
# Two halves of #344's decomposition, each in its own leaf module since
# issue #8020 and re-exported by this one. *Sampling* (phase 1, #348) is
# ``sim_monte_carlo``: request schema, seed handling, fan-out mechanics --
# N reproducibly-seeded samples per corner point. *Statistics* (phase 2,
# #349) reduce those samples to a verdict in ``sim_monte_carlo_rollup``:
# see :func:`_sample_statistics` (mean/sigma/quantiles) and
# :func:`_evaluate_sigma_window` (the optional `mean +/- k*sigma` inside
# `measurements[].limits` check), both reached from
# :func:`_monte_carlo_rollup`. What stays here is the runner glue around
# them: instance gating, netlist subckt overrides, spec-row and
# plausibility-range validation, and the injected / per-family mismatch
# report.
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
    gate_context: tuple[Any, ...] = (),
) -> tuple[str, dict[str, Any] | None]:
    """Apply ``request.monte_carlo.instances`` when present (issue #2995):
    dispatch a gated copy of the netlist in which every instance not selected
    has its local mismatch switched off, by whichever mechanism the PDK family
    uses -- a shadowed model-level ``.param`` inside a generated wrapper
    subcircuit, or ``<param>=0`` on the instance line itself (#5487; see
    :mod:`klayout_tools.sim_mismatch_gate`). Returns ``(netlist_to_dispatch,
    monte_carlo_info)`` with the gating report attached as
    ``instance_mismatch``, unchanged when absent; raises :class:`SimError` for
    anything unsupported, before any corner is dispatched."""
    import atexit

    if mc_spec is None or "instances" not in mc_spec:
        return netlist_path, monte_carlo_info
    if backend in OFFHOST_BACKENDS or explicit_points:
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
    instance_param = sim_mismatch_gate.MISMATCH_INSTANCE_PARAM.get(family or "")
    if switch is None and instance_param is None:
        known_pdks = sim_mismatch_gate.MISMATCH_GATED_FAMILIES
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
        declared = sim_mismatch_declarations.collect(
            netlist_path, instance_param, *gate_context
        )
        gated, rows = sim_mismatch_gate.gate_netlist(
            text, include, exclude, switch, instance_param, declared
        )
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
        # Whichever gate this run used: the `.param`, or the instance parameter.
        "switch": switch or instance_param,
        "include": include,
        "exclude": exclude,
        "instances": rows,
        # All excluded: the run must equal the mismatch-off run (negative control).
        "negative_control": not any(r["randomised"] for r in rows),
    }
    if monte_carlo_info is not None:
        monte_carlo_info = {**monte_carlo_info, "instance_mismatch": report}
    return gated_path, monte_carlo_info


def _netlist_overrides_environment(report: Any) -> dict[str, Any]:
    return {} if report is None else {"netlist_overrides": report}


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
    if backend in OFFHOST_BACKENDS or explicit_points:
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
        if not is_number(entry):
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


# --------------------------------------------------------------------------- #
# Execution backends
# --------------------------------------------------------------------------- #


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
    waveform_decimate: int | None = None,
    ngspice_options: dict[str, Any] | None = None,
    transient_noise_seed: int | None = None,
    convergence_retry: dict[str, Any] | None = None,
    portable_roots: sim_portable.Roots | None = None,
    stimulus_lines: tuple[str, ...] = (),
    threads_per_worker: int | None = None,
    pdk_root: str | None = None,
    parallel: bool = False,
    concurrent_shards: bool = False,
) -> tuple[list[dict[str, Any]], str | None, dict[str, Any] | None]:
    """The ``local`` backend -- and, since issue #6896, the one shared local
    entry point both local backends bind their corner runner at.

    The per-sweep values are bound **once** here, into ``run_corner(point)``
    -- the single keyword forwarding into :func:`_run_corner_with_retry`, so
    adding a simulator option is one forwarding change for both local
    modes. ``parallel=False`` (the default) runs each corner sequentially
    on the calling thread via :func:`_run_local_corners_sequentially`;
    ``parallel=True`` (forwarded by :func:`_run_local_parallel`) fans them
    across a bounded local pool via :func:`_run_local_corners_pooled`.

    This is the unit a backend implements: given the expanded corner list
    plus the resolved netlist/model paths and run options, return
    ``(per_corner_reports, engine_version, remote_environment)``. A calibration
    probe can cancel the sweep before scheduling. In that case each pending
    corner receives the probe diagnostic in input order. Otherwise the scheduler
    tracks the engine version from the last corner that reports one.
    """
    del request  # unused by either local scheduling strategy, see docstring
    workers = _resolve_max_workers(max_workers) if parallel else 1
    measure_peak_rss = not concurrent_shards and min(workers, len(corner_points)) <= 1

    if probe_abort is not None:
        if parallel:
            _resolve_max_workers(max_workers)  # Keep the host's clamp notice.
        corners = [
            _unrun_corner_report(
                point,
                measurements_spec,
                probe_abort["code"],
                message=probe_abort["message"],
                reached_s=probe_abort.get("reached_s"),
                fraction=probe_abort.get("fraction"),
            )
            for point in corner_points
        ]
        return corners, None, None

    def run_corner(point: CornerPoint) -> tuple[dict[str, Any], str | None]:
        return _run_corner_with_retry(
            point=point,
            measure_peak_rss=measure_peak_rss,
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
            waveform_decimate=waveform_decimate,
            ngspice_options=ngspice_options,
            transient_noise_seed=transient_noise_seed,
            convergence_retry=convergence_retry,
            portable_roots=portable_roots,
            stimulus_lines=stimulus_lines,
            threads_per_worker=threads_per_worker,
            pdk_root=pdk_root,
        )

    schedule = (
        _run_local_corners_pooled if parallel else _run_local_corners_sequentially
    )
    corners, engine_version = schedule(
        run_corner,
        corner_points=corner_points,
        measurements_spec=measurements_spec,
        max_workers=workers,
        deadline=deadline,
        initial_ppid=initial_ppid,
        checkpoint=checkpoint,
    )
    return corners, engine_version, None


def _run_local_corners_sequentially(
    run_corner: Callable[[CornerPoint], tuple[dict[str, Any], str | None]],
    *,
    corner_points: list[CornerPoint],
    measurements_spec: list[dict[str, Any]],
    max_workers: int | None,
    deadline: float | None,
    initial_ppid: int | None,
    checkpoint: _Checkpoint | None,
) -> tuple[list[dict[str, Any]], str | None]:
    """The ``local`` scheduling strategy: call ``run_corner`` (the runner
    :func:`_run_local` bound once for the whole sweep, issue #6896) with
    each corner in list order, on the calling thread.

    ``deadline``/``initial_ppid`` (issue #473) are checked before *starting*
    each corner: once either fires, every remaining corner is reported via
    :func:`_unrun_corner_report` (``budget_exceeded``/``orphaned``) instead
    of being run -- a corner already in flight is never interrupted (there
    is only ever one, for this sequential backend). ``checkpoint``, when
    given, records each corner as it completes for a later ``--resume``.
    """
    del max_workers  # unused: sequential local run, see docstring
    engine_version: str | None = None
    corners: list[dict[str, Any]] = []
    stop_reason: str | None = None
    for point in corner_points:
        if stop_reason is None:
            if deadline is not None and time.monotonic() >= deadline:
                stop_reason = "budget_exceeded"
            elif initial_ppid is not None and os.getppid() != initial_ppid:
                stop_reason = "orphaned"
        if stop_reason is not None:
            corners.append(_unrun_corner_report(point, measurements_spec, stop_reason))
            continue
        result, version = run_corner(point)
        corners.append(result)
        if version is not None:
            engine_version = version
        if checkpoint is not None:
            checkpoint.record(point.corner_id, result, version)
    return corners, engine_version


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
    _warn.warn(message)


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


def _run_local_corners_pooled(
    run_corner: Callable[[CornerPoint], tuple[dict[str, Any], str | None]],
    *,
    corner_points: list[CornerPoint],
    measurements_spec: list[dict[str, Any]],
    max_workers: int,
    deadline: float | None,
    initial_ppid: int | None,
    checkpoint: _Checkpoint | None,
) -> tuple[list[dict[str, Any]], str | None]:
    """The ``local-parallel`` scheduling strategy: fan the expanded corner
    list across a bounded local worker pool, submitting ``run_corner`` (the
    runner :func:`_run_local` bound once for the whole sweep, issue #6896)
    and the corner's point to the pool.

    Corners share nothing -- each is a pure function of netlist + models +
    corner (see this module's docstring and #253/#168) -- so this is a
    :class:`~concurrent.futures.ThreadPoolExecutor` pool over ``run_corner``
    (a thread, not a process, pool: the actual work is the ``ngspice``
    subprocess call, which releases the GIL for the duration of the wait, so
    threads give the same concurrency as processes here without the
    pickling/import overhead of ``ProcessPoolExecutor``). Results are indexed
    by each corner's position in ``corner_points`` and reassembled into that
    same order after every future completes, regardless of completion order
    -- the acceptance criterion that this backend's report is order-identical
    to ``local`` for the same request. ``engine_version`` is derived the same
    "last non-``None`` wins, in corner-list order" way the sequential
    scheduler computes it, for the same reason: deterministic given a
    deterministic corner list, independent of which corner's process
    happened to finish last. A failing/erroring corner is reported exactly
    as ``run_corner`` reports it (:func:`_run_corner` never raises) and
    does not abort any sibling corner -- each future is independent.

    ``max_workers`` is the pool size resolved by :func:`_resolve_max_workers`
    from the request or CPU-derived default and the ``KLT_SIM_MAX_WORKERS`` cap.

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
    """
    total = len(corner_points)
    results: list[tuple[dict[str, Any], str | None] | None] = [None] * total
    stop_reason: str | None = None
    next_index = 0

    def _stopped() -> str | None:
        nonlocal stop_reason
        if stop_reason is None:
            if deadline is not None and time.monotonic() >= deadline:
                stop_reason = "budget_exceeded"
            elif initial_ppid is not None and os.getppid() != initial_ppid:
                stop_reason = "orphaned"
        return stop_reason

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        in_flight: dict[Any, int] = {}

        def _fill() -> None:
            nonlocal next_index
            while (
                next_index < total
                and len(in_flight) < max_workers
                and _stopped() is None
            ):
                point = corner_points[next_index]
                future = pool.submit(run_corner, point)
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
                    corner_points[index], measurements_spec, stop_reason
                )
            )
            continue
        corner, version = result
        corners.append(corner)
        if version is not None:
            engine_version = version
    return corners, engine_version


def _run_local_parallel(
    **kwargs: Any,
) -> tuple[list[dict[str, Any]], str | None, dict[str, Any] | None]:
    """The ``local-parallel`` backend: :func:`_run_local` with
    ``parallel=True`` (issue #6896 hoisted the shared signature and
    corner-argument binding there); the pool itself is
    :func:`_run_local_corners_pooled`.
    """
    return _run_local(parallel=True, **kwargs)


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


_WAVEFORM_KEYWORDS = frozenset(
    {"pwl", "pulse", "sin", "exp", "sffm", "am", "trnoise", "trrandom", "external"}
)


def _logical_netlist_lines(netlist_text: str) -> list[str]:
    """Netlist lines with ``+`` continuations joined and comments removed.

    Comments and ``+`` continuations go through the shared helpers, so a
    ``$`` glued to the next token (``.include $PDK_ROOT/...``) survives and
    an orphan ``+`` is dropped, as it always was here. Deliberately lexical,
    not a complete SPICE parser: ``.include``/``.lib`` are not followed.
    Shared implementation in :func:`_spice_text.logical_lines` (issue #5779
    moved it out so this module keeps its size ratchet while the
    device-multiplier lint reads cards the same way)."""
    return _spice_text_logical_lines(netlist_text)


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
    for line in circuit_lines(_logical_netlist_lines(netlist_text)):
        word = line.split(None, 1)[0].lower()
        if word == ".subckt":
            depth += 1
        elif word == ".ends":
            depth = max(0, depth - 1)
        elif not depth:
            _record_declaration(decl, line)
    return decl


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


def resolve_netlist_include(reference: str, base_dir: str) -> str | None:
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
        path = resolve_netlist_include(reference, base_dir)
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
    if (decl.params - decl.elements).isdisjoint(t.strip().lower() for t in targets):
        return {}
    param_only, _ = _classify_supply_targets(netlist_path, decl, targets)
    return param_only


def _classify_supply_targets(
    netlist_path: str, decl: _NetlistDeclarations, targets: list[str]
) -> tuple[dict[str, list[str]], list[str]]:
    """Return parameter-only targets with source hints, and unknown supply names.

    An incomplete include closure cannot establish that an element is absent.
    Hierarchical names can address elements outside the top-level scan.
    """
    included = _included_elements(netlist_path, decl.includes)
    if included is None:
        return {}, []
    elements = decl.elements | included
    param_only: dict[str, list[str]] = {}
    unknown: list[str] = []
    for target in targets:
        name = target.strip().lower()
        if name in elements:
            continue
        if name in decl.params:
            pattern = re.compile(rf"(?<![a-z0-9_]){re.escape(name)}(?![a-z0-9_])")
            param_only[target] = sorted(
                source
                for source, spec in decl.voltage_specs.items()
                if pattern.search(spec)
            )
        elif "." not in name:
            unknown.append(target)
    return param_only, unknown


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
    param_only, unknown = _classify_supply_targets(
        netlist_path, _netlist_declarations(text), targets
    )
    for target, sources in param_only.items():
        raise SimError(
            f"corners.supply_v key {target!r} names a .param, not a source: "
            f"the netlist declares `.param {target}=...` and no element "
            f"{target!r}, and ngspice's `alter {target}=<value>` cannot change "
            "a .param (expressions that read it -- a source value, a SIN() "
            "amplitude -- were evaluated when the deck was parsed, so every "
            "corner would silently simulate the nominal value). "
            + _param_target_hint(target, sources)
        )
    for target in unknown:
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
    :func:`_classify_supply_targets`: the scan covers the top-level file
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


def scale_supply_waveforms(
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
    transient_noise_seed: int | None = None,
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
            transient_noise_seed=transient_noise_seed,
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
    if convergence_retry is None:
        return corner, version
    if not is_retryable_convergence_failure(corner):
        note_not_retried(corner)
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
        if recovered or not is_retryable_convergence_failure(retried):
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
    waveform_decimate: int | None = None,
) -> str | None:
    """Convert ``raw_path`` to its ``waveform.raw.json`` artifact and return
    that path, or ``None`` when ``raw_size`` is over ``waveform_max_bytes``
    (issue #3211). Past the cap, append a ``waveform_too_large`` warning.
    Otherwise stream the points and use the header to classify noise units.
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
    waveform_path, header = persist_waveform_with_header(raw_path, waveform_decimate)
    if analysis.get("kind") == "noise":
        noise_fields["noise_representation"] = noise_representation(header)
    return waveform_path


def _worker_env_kwargs(
    threads_per_worker: int | None, pdk_root: str | None = None
) -> dict[str, Any]:
    """Build a child environment only when a local run pins a worker setting."""
    overrides: dict[str, str] = {}
    if threads_per_worker is not None:
        overrides["OMP_NUM_THREADS"] = str(threads_per_worker)
    if pdk_root is not None:
        overrides["PDK_ROOT"] = pdk_root
    return {"env": {**os.environ, **overrides}} if overrides else {}


def _threads_environment(effective: int | None) -> dict[str, int]:
    """Additive/optional ``environment.threads_per_worker`` (issue #2970): the
    OpenMP thread count each ngspice worker actually ran with. Absent when the
    ambient environment decided (a single worker, nothing requested)."""
    return {} if effective is None else {"threads_per_worker": effective}


def _validate_threads_per_worker(value: Any) -> int | None:
    """Validate ``options.threads_per_worker`` (issue #2970)."""
    if value is None:
        return None
    if not is_int(value) or value < 1:
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
    if backend in OFFHOST_BACKENDS:
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
    measure_peak_rss: bool = True,
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
    waveform_decimate: int | None = None,
    ngspice_options: dict[str, Any] | None = None,
    transient_noise_seed: int | None = None,
    portable_roots: sim_portable.Roots | None = None,
    stimulus_lines: tuple[str, ...] = (),
    threads_per_worker: int | None = None,
    pdk_root: str | None = None,
) -> tuple[dict[str, Any], str | None]:
    """Run one corner point through the selected engine's batch binary and
    classify the result.

    Returns ``(corner_report, engine_version_or_none)``. A missing executable,
    timeout or unsuccessful engine exit is recorded in the corner report.
    Unexpected exceptions propagate after the scratch directory is released.

    ``ngspice -b`` reads a deck with a control block from ``_write_corner_deck``.
    Xyce accepts ``-b`` for compatibility and reads ``_write_xyce_deck``'s
    control-block-free deck, with ``-l`` for logs and ``-r ... -a`` for ASCII
    waveforms. Both engines share grading and the response contract exercised
    by ``tests/test_sim_xyce_oracle.py`` (#2016).

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

    ``_prepare_corner_run`` writes the validated ``ngspice_init`` lines to
    the ngspice corner's ``.spiceinit`` (#2520). The simulator runs with
    that directory as its cwd, so init and relative include paths resolve
    there for both retained artifacts and temporary runs.
    """
    measurements_spec, limit_echoes = sim_supply_relative.resolve_limits(
        measurements_spec, point.supply_v
    )
    is_xyce = engine == "xyce"
    engine_name = XYCE_BINARY if is_xyce else "ngspice"
    with _runner_work_dir(artifacts_dir, point.slug, keep_artifacts) as corner_dir:
        if keep_artifacts:
            _write_corner_manifest(corner_dir, point)

        run_netlist_path, include_diagnostics = _expand_netlist_includes(
            netlist_path, corner_dir, pdk_root
        )
        run_netlist_path, supply_waveform_report = scale_supply_waveforms(
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
            ngspice_init=_with_thread_pin(ngspice_init, threads_per_worker, engine),
            osdi_preload=osdi_preload,
            save_mode=save_mode,
            save_vectors=save_vectors,
            complex_vectors=complex_vectors,
            ngspice_options=ngspice_options,
            transient_noise_seed=transient_noise_seed,
            stimulus_lines=stimulus_lines,
            extra_options_card=extra_options_card,
        )

        diagnostics: list[dict[str, str]] = list(include_diagnostics)
        diagnostics.extend(_par_call_cap_preflight(deck_path, is_xyce))
        started = sim_peak_rss.RunWatch(measure_peak_rss=measure_peak_rss)
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
                **_worker_env_kwargs(None if is_xyce else threads_per_worker, pdk_root),
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
        runtime_s = round(started.elapsed(), 3)

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
            diagnostics.extend(
                _alterparam_failures(point, log_text + "\n" + stdout_text)
            )
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
            measurements_spec, corner_dir, point.supply_v, analysis.get("kind")
        )
        # Issue #3033: same for the energy reduction.
        energy = sim_energy.extract_corner_energy(measurements_spec, corner_dir)
        # Issue #4592: and for the windowed stress reduction.
        stress = sim_stress.extract_corner_stress(measurements_spec, corner_dir)
        cycle_means = sim_cycle_mean.extract_corner_cycle_means(
            measurements_spec, corner_dir
        )
        transfers = sim_transfer_meas.extract_corner_transfers(
            measurements_spec, corner_dir
        )
        snms = sim_snm_meas.extract_corner_snms(measurements_spec, corner_dir)
        slope_bands = sim_slope_band_meas.extract_corner_slope_bands(
            measurements_spec, corner_dir
        )
        eyes = sim_eye.extract_corner_eyes(measurements_spec, corner_dir)
        step_resolutions = sim_step_resolution.extract_corner_step_resolutions(
            measurements_spec, corner_dir
        )
        settleds = sim_settled.extract_corner_settleds(measurements_spec, corner_dir)
        searches = sim_search.extract_corner_searches(measurements_spec, log_text)
        nzm = sim_noise_mechanism.extract_corner(
            measurements_spec, corner_dir, analysis
        )
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
                stress=stress,
                cycle_means=cycle_means,
                transfers=transfers,
                snms=snms,
                slope_bands=slope_bands,
                eyes=eyes,
                step_resolutions=step_resolutions,
                settleds=settleds,
                searches=searches,
                tians=tians,
                noise_mechanisms=nzm,
                engine_diagnostics=diagnostics,
            )
        )
        sim_supply_relative.attach_limits(measurement_results, limit_echoes)
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
        complex_outcome = sim_complex_vectors.extract_corner(
            complex_vectors, corner_dir
        )
        diagnostics.extend(complex_outcome.diagnostics)
        # Issue #3338: reduce a two-port corner's two excitations to S; a missing
        # or non-finite rawfile is an error diagnostic, so it cannot grade `pass`.
        twoport_outcome = sim_twoport.extract_corner(analysis, corner_dir)
        diagnostics.extend(twoport_outcome.diagnostics)
        # Issue #5077: metastable-op detection, before `disqualifying` is sampled.
        op_check_field, op_check_diagnostics = sim_op_check.evaluate(
            analysis,
            point.supply_v,
            log_text + "\n" + stdout_text,
            skip=timed_out or engine_aborted or analysis_never_started,
        )
        diagnostics.extend(op_check_diagnostics)

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

        # Both engines may narrate failed stepping attempts on the path to a
        # successful solve. Only these two codes are eligible for recovery.
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
            measurements_spec, raw_path, diagnostics, analysis, engine, log_text
        )

        if _recovered_from_stepping(
            measurements_spec,
            measurement_results,
            log_text,
            aborted_re=aborted_re,
            waveform_complete=waveform_complete,
        ):
            _downgrade_stepping_diagnostics(
                diagnostics, bool(measurements_spec), analysis["kind"]
            )

        status = _grade_corner(diagnostics, measurement_results, disqualifying)
        diagnostics.extend(sim_homotopy.recovery_diagnostics(engine, log_text))

        artifacts: dict[str, str | int | None] = {
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
                artifacts["raw_bytes"] = raw_size  # issue #5622
                try:
                    waveform_path = _persist_waveform_artifact(
                        raw_path,
                        raw_size,
                        waveform_max_bytes,
                        analysis,
                        diagnostics,
                        noise_fields,
                        waveform_decimate,
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
                # Issue #7866: a `noise` corner's per-frequency spectrum, a
                # sibling of the (totals-only) waveform artifact above.
                artifacts.update(
                    sim_noise_spectrum.persist(
                        analysis,
                        raw_path,
                        waveform_max_bytes,
                        waveform_decimate,
                        diagnostics,
                    )
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
        **_ao_report_field(point),
        **_options_report_field(point),
        **op_check_field,
        "status": status,
        "runtime_s": runtime_s,
        **started.report_field(),  # issue #4867: best-effort, may be absent
        # Additive (issue #2848): the engine process's raw outcome. Both are
        # null when the harness itself ended the run (timeout) or the engine
        # never launched.
        "engine_exit_code": engine_exit_code,
        "engine_killed_by_signal": engine_killed_by_signal,
        "measurements": measurement_results,
        "diagnostics": diagnostics,
        "artifacts": artifacts,
        **noise_fields,
        **sim_waveform_opts.report_field(waveform_decimate, artifacts),
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


ABSENT_DIAGNOSTIC_CODE = "measurement_absent"


_AC_TOO_FEW_POINTS_HINT = (
    " -- the ac sweep produced fewer than 2 data points, so AT=/interpolation "
    "has nothing to interpolate between: use a denser sweep (e.g. `dec`)"
)


def _ac_sweep_too_few_points(log_text: str) -> bool:
    """Whether the log's (last) ``No. of Data Rows`` is below 2."""
    rows = sim_ac_waveform.DATA_ROWS_RE.findall(log_text or "")
    return bool(rows) and int(rows[-1]) < 2


def _is_legitimately_absent(cause: str | None) -> bool:
    """Whether a value-less ``optional`` measurement is absent data rather than
    an engine failure: no ngspice ``Error: measure`` reason at all, or the
    "out of interval" (never crossed) reason."""
    return cause is None or sim_cause._MEAS_ABSENT_CAUSE_RE.search(cause) is not None


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
        message += sim_cause._cause_suffix(cause)
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

    ``value`` is the number of window crossings (on ``dc``, the first
    crossing's sweep abscissa; with ``value_from``, the selected reduction,
    #6913), graded through :func:`_grade_measurement_value` like any
    scalar; the series rides the additive ``crossings`` sibling. No series
    (unresolved signal, unreadable rawfile, too few edges for a selected
    reduction) is an ordinary ``status: "error"`` entry with no value."""
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
        spec, outcome.graded_value()
    )
    result = {
        "name": name,
        "value": outcome.graded_value(),
        "unit": unit,
        "status": m_status,
        "margin": margin,
        "crossings": outcome.report(),
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
    log_text: str,
) -> bool:
    """True when this value-less ``spice`` measurement opted into
    ``on_unreached: "null"``, ngspice reported exactly "out of interval" for
    it, and no blocking error-severity diagnostic (solver divergence, timeout,
    ...; not unaborted stepping narration, #7906) -- the analysis completed."""
    if spec.get("on_unreached") != "null" or spec.get("spice") is None:
        return False
    if str(spec["name"]).lower() not in unreached_names:
        return False
    return not sim_stepping_narration.blocks_not_reached(diagnostics, log_text)


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
        message += unsupported_function_hint(spec["expr"])
    if save_mode == "netlist":
        message += SAVE_MODE_NETLIST_NO_VALUE_HINT
    return message


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


def _runner_work_dir(
    artifacts_dir: str, name: str, keep_artifacts: bool
) -> contextlib.AbstractContextManager[str]:
    if not keep_artifacts:
        return tempfile.TemporaryDirectory(
            prefix="klt-sim-", ignore_cleanup_errors=True
        )
    path = os.path.join(artifacts_dir, name)
    os.makedirs(path, exist_ok=True)
    return contextlib.nullcontext(path)


def _tmp_work_dir() -> str:
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
    transient_noise_seed: int | None = None,
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
    # process corner, none without a process axis. See `corner_lib_lines`.
    lines.extend(corner_lib_lines(point, models_lib))
    lines.append(f".include {netlist_path}")
    # Issues #3942/#5486: the sigma scale -- one `.param` card for a switch
    # family, one per scanned sigma for sg13g2. Must come after the `.lib`
    # (whose section sets the switch or defines the sigmas itself, e.g.
    # sky130 `.param mc_mm_switch=1` in the `_mm` sections) so the request's
    # value is the last definition and wins.
    lines.extend(sim_mismatch_scale.deck_lines(point.mc_seed, _format_param_value))
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
            transient_noise_seed=transient_noise_seed,
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
    force: bool = False,
) -> list[str]:
    """The ``save`` card :func:`_corner_deck_control_lines` emits (step 2 of
    its ordering contract), or no lines when the corner has nothing to save
    or ``save_mode`` opts out. ``force`` (``analysis.op_check``, issue #5077)
    counts as something to save."""
    if save_mode != DEFAULT_SAVE_MODE or not (
        measurements_spec or raw_path is not None or force
    ):
        return []
    if save_vectors:
        return [
            "save " + " ".join(_required_save_vectors(save_vectors, measurements_spec))
        ]
    return ["save all"]


def supply_alter_lines(point: CornerPoint) -> list[str]:
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
    transient_noise_seed: int | None = None,
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
    10. The ``mechanism`` noise-spectra dump (#5553): order-free, restores $curplot.

    An ``analysis_steps[]`` request (issue #2482) replaces step 3's analysis
    and 4's ``expr`` lines with the ordered step sequence
    :func:`klayout_tools.sim_steps.control_lines` generates -- after the same
    ``pre_osdi``/``save all``/corner-supply ``alter`` prefix, so every step
    solves the corner the prefix set up. (``options.waveforms`` is refused
    for a step sequence, so ``raw_path`` is always ``None`` there.)
    """
    lines = [".control", *sim_tn.seed_lines(transient_noise_seed)]
    for path in osdi_preload:
        lines.append(f"pre_osdi {path}")
    # Issue #3035: the parameter reset must come first -- `reset` reloads the
    # circuit, so everything after it (save/alter/analysis) re-applies on top.
    prefix: list[str] = _param_reset_lines(point)
    prefix.extend(
        _save_control_lines(
            measurements_spec,
            raw_path,
            save_mode,
            save_vectors,
            force=sim_op_check.is_declared(analysis),
        )
    )
    prefix.extend(supply_alter_lines(point))
    lines.extend(prefix)
    if sim_steps.is_steps_analysis(analysis):
        # Issue #3103: a `reset: true` step re-emits this same prefix after
        # ngspice's `reset` (which re-parses the circuit, reverting every
        # `alter` and dropping the saved set).
        lines.extend(
            sim_steps.control_lines(analysis, measurements_spec, prefix, corner_dir)
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
    stress_lines = sim_stress.control_lines(measurements_spec, corner_dir)
    cycle_mean_lines = sim_cycle_mean.control_lines(measurements_spec, corner_dir)
    transfer_lines = sim_transfer_meas.control_lines(measurements_spec, corner_dir)
    snm_lines = sim_snm_meas.control_lines(measurements_spec, corner_dir)
    sband_lines = sim_slope_band_meas.control_lines(measurements_spec, corner_dir)
    eye_lines = sim_eye.control_lines(measurements_spec, corner_dir)
    step_resolution_lines = sim_step_resolution.control_lines(
        measurements_spec, corner_dir
    )
    settled_lines = sim_settled.control_lines(measurements_spec, corner_dir)
    search_lines = sim_search.control_lines(measurements_spec, prefix)
    complex_vector_lines = sim_complex_vectors.control_lines(
        complex_vectors, corner_dir
    )
    nz_lines = sim_noise_mechanism.control_lines(measurements_spec, corner_dir)
    if (
        raw_path is not None
        or crossings_lines
        or energy_lines
        or stress_lines
        or cycle_mean_lines
        or transfer_lines
        or snm_lines
        or sband_lines
        or eye_lines
        or step_resolution_lines
        or settled_lines
        or complex_vector_lines
        or nz_lines
    ):
        lines.append("set filetype=ascii")
    lines.append(f"{analysis['kind']} {_sao.deck_args(point, analysis)}")
    if raw_path is not None:
        lines.append(f"write {raw_path}")
        lines.extend(sim_noise_spectrum.control_lines(analysis, raw_path))  # #7866
        if analysis["kind"] == "ac" and not measurements_spec:
            # An aborted sweep has no frequency vector to count (#7293).
            lines.extend(["if length(frequency) > 0", "display frequency", "end"])
    if analysis.get("linearize") is True:
        # Resample after saving the solver's points and before expression measurements
        # so adaptive timesteps do not bias time-weighted sums (#4014).
        lines.append("linearize")
    lines.extend(sim_post_commands.control_lines(analysis))  # issue #6080
    expr_specs = sim_noise.emittable_exprs(_expr_measurements(measurements_spec))
    lines.extend(sim_noise.alias_lines(analysis, expr_specs))
    for spec in expr_specs:
        lines.extend(expr_control_lines(spec, spec.get("expr_prelude", ())))
    lines.extend(sim_op_check.control_lines(analysis))  # issue #5077
    lines.extend(crossings_lines)
    lines.extend(energy_lines)
    lines.extend(stress_lines)
    lines.extend(cycle_mean_lines)
    lines.extend(transfer_lines)
    lines.extend(snm_lines)
    lines.extend(sband_lines)
    lines.extend(eye_lines)
    lines.extend(step_resolution_lines)
    lines.extend(settled_lines)
    lines.extend(complex_vector_lines)
    lines.extend(nz_lines)
    # Issue #4121: last, so no later line reads the main analysis' plot after
    # the search has moved ngspice's current plot.
    lines.extend(search_lines)
    lines.append("quit")
    lines.append(".endc")
    return lines


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
      not have. ``sim_xyce_refusals.enforce_support_boundary`` refuses such a request
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
    lines.extend(corner_lib_lines(point, models_lib))
    lines.append(f".include {netlist_path}")
    # Xyce's documented circuit-wide device-temperature control; `.temp`
    # would be a silent no-op (see this docstring's list).
    lines.append(f".options device temp={point.temperature_c}")
    # The analysis card precedes the measurement cards (the order every
    # probe against XyceNF 7.10.0 used while building this). Top-level dot
    # card, with the dot -- there is no `.control` block for a bare
    # `dc`/`tran`/`op` command to live in.
    lines.append(
        "." + f"{analysis['kind']} {_effective_analysis_args(point, analysis)}".strip()
    )
    for spec in measurements_spec:
        if spec.get("spice") is not None:
            lines.append(spec["spice"])
    lines.append(".end")

    with open(deck_path, "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")


_ENGINE_BANNER_MAX = ENGINE_BANNER_MAX


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
    transient_noise_seed: int | None = None,
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

    Returns ``{"wall_s", "timed_out", "error"}`` for a completed run, timeout
    or missing executable. A timed-out probe still yields a rate estimate at the
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

    ``ngspice_init`` (issues #2520, #6047) is the init-file lines the real
    corners get, thread pin included (:func:`_with_thread_pin`; a run-dir
    ``.spiceinit`` replaces ``$HOME``'s). It is materialized as
    ``probe_dir/.spiceinit`` and the probe's ``subprocess.run`` passes
    ``cwd=probe_dir`` so it is read -- otherwise the probe's convergence
    behaviour (and rate estimate) would diverge from the corners it is meant
    to be representative of.
    """
    with _runner_work_dir(
        artifacts_dir, f"{point.slug}__probe", keep_artifacts
    ) as probe_dir:
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
            transient_noise_seed=transient_noise_seed,
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
    transient_noise_seed: int | None = None,
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
        transient_noise_seed=transient_noise_seed,
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
    code). ``netlist_path``, when provided, also lets two codes read the
    netlist itself: ``model_bin_range`` names a likely culprit instance
    (:func:`_diagnose_modelname_error`, #1214) and ``netlist_title_line``
    matches the abort's echoed line against line 1 (:func:`_title_line_cause`,
    #5433). Omitted -- the default -- each falls back to today's behaviour.
    """
    diagnostics: list[dict[str, Any]] = _title_line_cause(log_text, netlist_path)
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
            if any(d["code"] in _BIN_RANGE_SUPERSEDERS for d in diagnostics):
                # The follow-on "could not find a valid modelname" is a
                # consequence of the superseding cause, not a bin range.
                continue
            message = _diagnose_modelname_error(netlist_path) or _line_containing(
                log_text, match.end() - 1
            )
        elif code == "par_call_cap":
            message = _PAR_CALL_CAP_MESSAGE.format(raw=message.strip())
        elif code in _PREREQUISITE_MESSAGES:
            message = _PREREQUISITE_MESSAGES[code].format(raw=message.strip())
        if code == "ngspice_fatal":
            fatal = sim_cause._netlist_fatal_message(
                message, log_text, match.start(), diagnostics
            )
            if fatal is None:
                # Superseded by a specific fatal code, or the benign
                # measure-substitute block of a completed run (#6401).
                continue
            message = fatal
        diagnostics.append({"severity": "error", "code": code, "message": message})
    return diagnostics


def _title_line_cause(log_text: str, netlist_path: str) -> list[dict[str, Any]]:
    """Issue #5433's ``netlist_title_line`` cause, or ``[]`` -- see
    :func:`sim_cause._netlist_title_line` for when it fires and why."""
    title = sim_cause._netlist_title_line(log_text, netlist_path)
    if title is None:
        return []
    message = sim_cause._title_line_message(netlist_path, *title)
    return [{"severity": "error", "code": "netlist_title_line", "message": message}]


#: Width, in micrometres, above which a MOS instance's ``w`` as written
#: (``nf`` not multiplied in, issue #4976) is a plausible culprit for
#: ngspice's "could not find a valid modelname" failure. The true
#: per-instance bin range is a property of each PDK's own BSIM model cards and cannot be
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


def _parse_width_um(raw: str, bare_is_um: bool = False) -> float | None:
    """Parse a SPICE ``W``/``L``-style geometry literal (``50u``, ``5e-5``,
    ``100n``) to micrometres (``None`` for non-numerics); ``bare_is_um``
    reads a bare literal as micrometres (#5802 X-card PDK wrappers)."""
    match = spice_number.NUMBER_RE.match(raw.strip().strip("'\""))
    if match is None:
        return None
    value = float(match.group(1)) * spice_number.suffix_scale(match.group(2))
    return value * (1 if match.group(2) == "" and bare_is_um else 1e6)


def _find_width_ceiling_culprit(
    netlist_text: str,
) -> tuple[str, str, float, float] | None:
    """Scan ``netlist_text`` for the MOS instance most likely to have
    tripped a model card's undocumented per-instance width bin range: the
    ``w`` as written (``nf`` ignored; ngspice 42/46, issue #4976) among
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
    worst_w_um = -1.0
    for line in netlist_text.splitlines():
        match = _MOS_INSTANCE_RE.match(line)
        if match is None:
            continue
        inst_name, model_name = match.group(1), match.group(2)
        params = dict(_PARAM_ASSIGN_RE.findall(match.group(3)))
        w_raw = params.get("w") or params.get("W")
        if w_raw is None:
            continue
        w_um = _parse_width_um(w_raw, bare_is_um=inst_name[:1].upper() == "X")
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
        if w_um >= _MODEL_WIDTH_CEILING_UM and w_um > worst_w_um:
            worst_w_um = w_um
            worst = (inst_name, model_name, w_um, nf_count)
    return worst


def _diagnose_modelname_error(netlist_path: str) -> str | None:
    """Build an actionable message for ngspice's own uninformative
    ``could not find a valid modelname`` failure by naming the netlist
    instance most likely responsible (see
    :func:`_find_width_ceiling_culprit`) -- e.g. gf180mcu's undocumented
    ~100 um per-instance width bin range, keyed on ``w`` as written
    (issues #1214, #4976).

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
    return sim_bin_range.message(culprit, _MODEL_WIDTH_CEILING_UM)


#: ngspice's own "analysis never completed" trailer (issue #205); the one
#: definition lives with the stepping-code pair it qualifies (issue #7906).
_SIMULATION_ABORTED_RE = sim_stepping_narration.SIMULATION_ABORTED_RE


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

    For a capture-only request, the original waveform can establish that the
    transient reached its stop time or ngspice completed the AC sweep.
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
    engine: str,
    log_text: str,
) -> bool:
    """Check the original waveform only when it could explain stepping recovery."""
    if (
        measurements_spec
        or raw_path is None
        or not any(
            d["code"] in sim_stepping_narration.STEPPING_CODES for d in diagnostics
        )
        or not os.path.isfile(raw_path)
    ):
        return False
    if analysis["kind"] == "ac" and engine == "ngspice":
        return sim_ac_waveform.complete(raw_path, analysis.get("args", ""), log_text)
    try:
        waveform = parse_ascii_rawfile(raw_path)
    except (SimError, ValueError):
        return False
    return _waveform_reached_tran_window(waveform, analysis)


def _downgrade_stepping_diagnostics(
    diagnostics: list[dict[str, Any]], has_measurements: bool, analysis_kind: str
) -> None:
    """Downgrade recovered ``singular_matrix``/``nonconvergence`` diagnostics
    to warnings; a capture-only corner also gets the reason appended."""
    for diagnostic in diagnostics:
        if diagnostic["code"] in sim_stepping_narration.STEPPING_CODES:
            diagnostic["severity"] = "warning"
            if not has_measurements:
                reached = (
                    "completed the AC sweep"
                    if analysis_kind == "ac"
                    else "reached the transient stop time"
                )
                diagnostic["message"] += (
                    f" (treated as recovered: the captured waveform {reached})"
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


def _measurement_coverage(
    spec: dict[str, Any], corner: dict[str, Any], corner_index: int
) -> tuple[list[str], list[dict[str, str]], list[dict[str, str]]]:
    """Count actual observations or applied bounds, never corner declarations.

    ``corner_index`` (this corner's position in the report's own ``corners`` list)
    scopes every identity built here -- ``corner_id`` alone is a *display* label (e.g.
    supply voltage rounded to 3 decimals) and distinct corners can legitimately round to
    the same one, which would otherwise collide as the same "unique" work identity and
    make :func:`~klayout_tools.coverage.build_check_coverage` reject two genuinely
    separate checks as a duplicate.
    """
    name = spec["name"]
    identity = work_id("measurement", corner_index, corner["corner_id"], name)
    limits = sim_supply_relative.coverage_bounds(spec.get("limits"))
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

    Returns ``(checked, skipped)``. Under ``capture_only`` (issue #3158) a corner that
    ran clean counts its waveform capture as checked work; any other corner is a
    ``no_requested_measurements`` skip."""
    corner_work = work_id("corner", corner_index, corner["corner_id"])
    if capture_only and corner["status"] == "pass":
        return [corner_work + "/waveform_capture"], []
    return [], [{"id": corner_work, "reason": "no_requested_measurements"}]


def _summarise_limits(
    measurements_spec: list[dict[str, Any]],
) -> tuple[int, int, list[dict[str, Any]]]:
    """Count declared limits and collect unrecognised limit keys per measurement.
    Returns ``(limits_declared, measurements_with_limits, unrecognized)``."""
    unrecognized: list[dict[str, Any]] = []
    limits_declared = 0
    measurements_with_limits = 0
    for spec in measurements_spec:
        limits = sim_supply_relative.coverage_bounds(spec.get("limits"))
        if not limits:
            continue
        limits_declared += 1
        unknown = sorted(limits.keys() - _RECOGNISED_LIMIT_KEYS)
        if unknown:
            unrecognized.append({"measurement": spec.get("name"), "keys": unknown})
        if any(limits.get(k) is not None for k in _RECOGNISED_LIMIT_KEYS):
            measurements_with_limits += 1

    return limits_declared, measurements_with_limits, unrecognized


def _build_coverage(
    measurements: list[dict[str, Any]],
    corners: list[dict[str, Any]],
    *,
    capture_only: bool = False,
    grid_stats: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build checked-work coverage from rolled-up measurements and their corners.

    ``capture_only`` (issue #3158) is the request's declared intent to capture waveforms
    with no measurements: each corner that ran clean then counts its capture as the
    checked work, and ``capture_only: true`` is recorded on the block. A corner that did
    not pass stays a skip, so a run failure is never waived.
    """
    limits_declared, measurements_with_limits, unrecognized = _summarise_limits(
        measurements
    )

    grid_stats = grid_stats or []
    checked: list[str] = []
    skipped: list[dict[str, str]] = []
    sim_probit_sigma.coverage(grid_stats, checked, skipped)
    inapplicable = []
    for corner_index, corner in enumerate(corners):
        if not measurements:
            corner_checked, corner_skipped = _corner_without_measurements(
                corner, corner_index, capture_only
            )
            checked.extend(corner_checked)
            skipped.extend(corner_skipped)
        for entry in sim_derive.per_corner_measurements(measurements):
            actual, missing, irrelevant = _measurement_coverage(
                entry, corner, corner_index
            )
            checked.extend(actual)
            skipped.extend(missing)
            inapplicable.extend(irrelevant)
    sim_derive_rollup.attach_coverage(measurements, checked, skipped, inapplicable)
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
        "measurements_declared": len(measurements) + len(grid_stats),
        "measurements_with_limits": measurements_with_limits + len(grid_stats),
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

    A corner graded ``inconclusive`` contributes ``inconclusive`` to the aggregate
    regardless of what its own measurement-vs-``limits`` comparison said -- so one
    distrusted corner masks a concurrent clean ``fail`` at *this* aggregate, exactly as
    one errored corner already does. That is deliberate (the aggregate reports the
    strongest claim about trustworthiness, not the union of every corner's verdict);
    ``corners[]`` and the top-level ``failed``/``inconclusive`` counts retain the
    per-corner detail. ``worst_case`` still scans every corner, distrusted ones
    included: it is a pointer to the corner worth looking at, and hiding the worst
    margin would make an inconclusive rollup harder to debug, not easier.
    """
    rollup: list[dict[str, Any]] = []
    for spec in measurements_spec:
        name = spec["name"]
        entries = sim_corner_extremes.corner_entries(name, corners)

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
            **sim_corner_extremes.corner_extremes(entries),  # issue #5221
            # Additive/optional (issue #2482): `step`/`derived_from` for an
            # `analysis_steps[]` measurement; nothing for any other one.
            **sim_steps.measurement_provenance(spec),
            **sim_derive_rollup.rollup_fields(spec, corners),
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
                # Issue #6967: the declared dispersion bound, graded on the
                # same `by_corner[]` populations (a no-op when undeclared).
                sim_population_limits.grade(entry, mc_stats, spec, monte_carlo)
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
# The reader/writer moved to `sim_raw.py` (issue #6752), the noise-mode
# classifier of a parsed waveform to `sim_noise.py` (issue #7866).
# --------------------------------------------------------------------------- #

noise_representation = sim_noise.noise_representation
_parse_spice_seconds = spice_number.parse
