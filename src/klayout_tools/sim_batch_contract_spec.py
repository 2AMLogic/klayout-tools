"""Batch contract: job.json spec and runner feature gates.

Split from :mod:`klayout_tools.sim_batch_contract` (issue #4065), moved verbatim.
"""

from __future__ import annotations

import os
import re
import shutil
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any

from . import (
    remote_transport,
    sim_eye,
    sim_mismatch_scale,
    sim_op_check,
    sim_post_commands,
    sim_search,
    sim_settled,
    sim_slope_band_meas,
    sim_snm_meas,
    sim_step_resolution,
    sim_stress,
    sim_supply_relative,
    sim_transfer_meas,
    sim_twoport,
)
from .sim_batch_contract import (
    REQUEST_FEATURE_MIN_RUNNER_VERSION,
)

# --------------------------------------------------------------------------- #
# job.json (2am's shell-command contract)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class BatchJobInput:
    """One file uploaded to ``s3://<bucket>/<jobs>/<id>/inputs/<name>``.

    Exactly one of ``local_path`` (an existing file) or ``content`` (a
    string written to a temp file first) must be given. ``label`` is a short
    human-readable name used only in an upload failure's message.

    ``name`` must be a relative name that stays inside ``inputs/`` -- the
    same rule (and the same validator)
    ``remote_transport.JobInput.remote_name`` applies, since the same
    netlist-declared ``.include``/``.inc`` closure feeds both transports
    (issue #2485).
    """

    name: str
    label: str
    local_path: str | None = None
    content: str | None = None

    def __post_init__(self) -> None:
        if (self.local_path is None) == (self.content is None):
            raise ValueError(
                "BatchJobInput requires exactly one of local_path or content "
                f"(name={self.name!r})"
            )
        remote_transport._validate_job_relative_name(
            self.name, field="BatchJobInput.name"
        )

    def materialize(self, inputs_root: str) -> None:
        """Place this input at ``<inputs_root>/<name>``, creating the
        parents a ``name`` carrying a subdirectory needs -- the local
        ``inputs/`` tree ``sim_batch.submit_job`` transfers in one
        recursive copy (issue #5539).

        Two inputs that declare the same ``name`` resolve last-one-wins,
        exactly as the second of two transfers to one S3 key did. The
        staged tree is always a *copy*, never a hardlink to the caller's
        own file: a later input declaring the same ``name`` would then
        truncate the caller's file through that link.
        """
        dest = os.path.join(inputs_root, self.name)
        # Never empty: `dest` is always at least one segment under
        # `inputs_root`, and a `name` may carry subdirectories of its own.
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        if self.local_path is None:
            with open(dest, "w", encoding="utf-8") as handle:
                handle.write(self.content or "")
        else:
            shutil.copyfile(self.local_path, dest)


@dataclass(frozen=True)
class BatchJobSpec:
    """2am's ``job.json`` -- everything optional except ``cmd``.

    Field-for-field the contract ``batch-fleet-harness.sh`` parses:
    ``cmd`` runs under ``bash -c`` in the job directory; ``tool`` is echoed
    into ``status.json``; ``pdk_variant``/``pdk_root`` are exported (the
    latter is layer 1 of the image's own PDK resolution order -- 2am's FSx
    hook); ``cores_per_job`` divides the instance's *physical* core count to
    derive ``$EDA_JOB_CONCURRENCY``; ``timeout_seconds`` caps the job,
    clamped down by the instance watchdog.

    ``inputs`` is **not** part of that contract -- it is this module's own
    upload list (what lands under ``inputs/``), excluded from
    :meth:`to_job_json` for exactly that reason.
    """

    cmd: str
    tool: str | None = None
    pdk_variant: str | None = None
    pdk_root: str | None = None
    cores_per_job: int | None = None
    timeout_seconds: int | None = None
    inputs: tuple[BatchJobInput, ...] = ()
    image_profile: str | None = None

    def to_job_json(self) -> dict[str, Any]:
        """The ``job.json`` document, in ``batch-fleet.md``'s own field
        order, omitting every unset optional field (the harness treats an
        absent field and an empty one identically, and a smaller document
        is a smaller contract surface)."""
        document: dict[str, Any] = {}
        if self.tool is not None:
            document["tool"] = self.tool
        document["cmd"] = self.cmd
        for key in ("pdk_variant", "pdk_root", "cores_per_job", "timeout_seconds"):
            value = getattr(self, key)
            if value is not None:
                document[key] = value
        if self.image_profile not in (None, "sim"):
            document["image_profile"] = self.image_profile
        return document


def _release_segment(version: str) -> str | None:
    """Read the numeric release prefix of a klt version, stripping dev and local parts.

    Mirrors the worker shell parser. An empty or malformed prefix has no
    release segment. The general compatibility check uses its first two
    components; request features may also use the patch and dev distance.
    """
    head = version.strip().split("+", 1)[0]
    prefix = re.match(r"[0-9.]*", head).group(0)  # always matches
    if prefix.endswith("."):
        prefix = prefix[:-1]
    return prefix if re.fullmatch(r"\d+(?:\.\d+)*", prefix) else None


def _runner_gate_release(version: str) -> str | None:
    """Major.minor used by the general worker gate; a lone major is unknown.

    Patch and development builds of one minor match. Breaking the worker
    contract requires a minor release and a rebaked runner. The worker
    preflight repeats this release comparison in shell.
    """
    release = _release_segment(version)
    if release is None:
        return None
    parts = release.split(".")
    return ".".join(parts[:2]) if len(parts) >= 2 else None


#: The registry maps each feature to its first supporting release segment and
#: detector (#3249). The major.minor gate allows patch-level differences, so
#: feature checks compare the full release segment. Step-scoped on_unreached
#: needs a worker observation for development builds within the same segment.
#: Following v0.7.0, the first 0.7.1 features were analysis_steps (#2482),
#: measurements[].transfer (#2923/#2924), stimulus (#2977), dual_injection
#: (analysis.passes / measurements[].tian, #3159/#3160/#3161), complex_vectors
#: (#3337), twoport (#3338), twoport_noise (#3339), snm (#3895) and search
#: (#4121). Eye (#4292) and stress (#4592/#4858) first ship in 0.8.1.
#: ``search_param`` (``measurements[].search.param``, #5391) landed after
#: ``v0.9.1``, so its first release segment is ``0.9.2``; an older runner would
#: reject the key or silently search nothing, hence mandatory. ``slope_band``
#: (``measurements[].slope_band``, #5518) missed the ``v0.9.2`` tag (it was not
#: in that release), so ``0.9.3`` is its first release segment; an older runner
#: would reject the unknown form or drop the measurement, hence mandatory.
#: ``crossings_value_from`` (``measurements[].crossings.value_from``, #6913)
#: likewise first ships in ``0.9.3``; an older runner would reject the unknown
#: key (at best) or grade the raw crossing count, hence mandatory.
#: Scanned mismatch_params (#5486) and exclusive_bounds (#7645) require 0.9.3.
def _step_measurements(request: dict[str, Any]) -> Iterator[dict[str, Any]]:
    for step in request.get("analysis_steps") or []:
        if isinstance(step, dict):
            yield from (
                m for m in step.get("measurements") or [] if isinstance(m, dict)
            )


def _uses_extended_eye(request: dict[str, Any]) -> bool:
    if any(sim_eye.is_eye(m) for m in _step_measurements(request)):
        return True
    return any(
        isinstance(spec, dict)
        and isinstance(spec.get("eye"), dict)
        and spec["eye"].get("phase") is not None
        for spec in request.get("measurements") or []
    )


def _declared(finder: Callable[[list[Any]], Any]) -> Callable[[dict[str, Any]], bool]:
    """Detector: the request declares at least one measurement ``finder`` finds."""
    return lambda request: bool(finder(request.get("measurements") or []))


def _uses_pdk_relative_osdi(request: dict[str, Any]) -> bool:
    options = request.get("options") or {}
    entries = options.get("osdi_preload")
    return (
        isinstance(entries, list)
        and options.get("stage_model_inputs") is not True
        and any(
            isinstance(entry, dict) and "pdk_relative" in entry for entry in entries
        )
    )


_step_res_declared = _declared(sim_step_resolution.step_resolution_measurements)
_RUNNER_REQUEST_FEATURES: dict[str, tuple[tuple[int, int, int], Any]] = {
    "analysis_steps": ((0, 7, 1), lambda r: r.get("analysis_steps") is not None),
    "step_on_unreached": (
        (0, 9, 3),
        lambda r: any(m.get("on_unreached") == "null" for m in _step_measurements(r)),
    ),
    "_regrade": ((0, 9, 3), lambda r: r.get("_regrade") is not None),  # also CLI-probed
    "transfer": ((0, 7, 1), _declared(sim_transfer_meas.transfer_measurements)),
    "snm": ((0, 7, 1), _declared(sim_snm_meas.snm_measurements)),
    "slope_band": (
        (0, 9, 3),
        _declared(sim_slope_band_meas.slope_band_measurements),
    ),
    "eye": ((0, 8, 1), _declared(sim_eye.eye_measurements)),
    "eye_step_or_phase": ((0, 9, 3), _uses_extended_eye),
    "osdi_pdk_relative": ((0, 9, 3), _uses_pdk_relative_osdi),
    "exclusive_bounds": ((0, 9, 3), sim_supply_relative.request_uses_exclusive_bounds),
    "step_resolution": ((0, 8, 1), _step_res_declared),
    "settled": ((0, 8, 1), _declared(sim_settled.settled_measurements)),
    "stress": ((0, 8, 1), _declared(sim_stress.stress_measurements)),
    "search": ((0, 7, 1), _declared(sim_search.search_measurements)),
    "search_param": (
        (0, 9, 2),
        lambda request: any(
            isinstance(spec, dict)
            and isinstance(spec.get("search"), dict)
            and spec["search"].get("param") is not None
            for spec in request.get("measurements") or []
        ),
    ),
    "crossings_value_from": (
        (0, 9, 3),
        lambda request: any(
            isinstance(spec, dict)
            and isinstance(spec.get("crossings"), dict)
            and spec["crossings"].get("value_from") is not None
            for spec in request.get("measurements") or []
        ),
    ),
    "post_commands": (
        (0, 9, 3),
        lambda request: sim_post_commands.is_declared(request.get("analysis")),
    ),
    "stimulus": (
        (0, 7, 1),
        lambda request: request.get("stimulus") is not None,
    ),
    "dual_injection": (
        (0, 7, 1),
        lambda request: _uses_dual_injection(request),
    ),
    "complex_vectors": (
        (0, 7, 1),
        lambda request: (
            (request.get("options") or {}).get("complex_vectors") is not None
        ),
    ),
    "twoport": (
        (0, 7, 1),
        lambda request: sim_twoport.is_twoport_request(request.get("analysis")),
    ),
    "twoport_noise": (
        (0, 7, 1),
        lambda request: sim_twoport.has_noise_request(request.get("analysis")),
    ),
    "mismatch_params": ((0, 9, 3), sim_mismatch_scale.wire_uses_scanned_cards),
    # No entry for `options.measurement_cases` (issue #5731): the expansion is
    # resolved client-side -- `sim_measurement_template.expand_request` runs
    # in `load_request` (which `run_sim` calls, passing the loaded request
    # through to `_prepare_sim_request`) before any
    # dispatch -- so the forwarded request
    # document carries only the expanded plain `measurements[]` (the option
    # key itself is dropped). An older runner sees an ordinary request.
}

#: Registry features a runner must be *known* to support (issue #2924): a
#: runner that predates one, or whose version cannot be read, is refused
#: whatever ``runner_version_check`` says, because the alternative is a worker
#: that accepts the request and omits the metric. Every other feature keeps the
#: warn-and-run behaviour of #3249. ``stimulus`` is mandatory because an older
#: runner would simulate with undriven control lines, exiting 0 (#2977).
#: ``dual_injection`` is mandatory because an older runner would ignore
#: ``analysis.passes`` and run a single AC pass or drop ``tian`` (#3161).
#: ``complex_vectors`` is mandatory because an older runner would return no
#: sidecar (#3337). ``twoport`` is mandatory because an older runner would
#: reject the unknown analysis kind or run a plain AC (#3338).
#: ``twoport_noise`` is mandatory because an older runner would reject
#: ``analysis.noise`` or omit ``nf_db`` (#3339). ``corners.params`` is
#: mandatory because an older runner ignores the axis, running one corner per
#: process x temperature with ``status: pass`` (#4235). ``post_commands`` is
#: mandatory because an older runner ignores the field, composing the deck
#: without ``linearize``/``fft`` and evaluating ``expr`` measurements against
#: the transient plot with ``status: pass`` (#6080).
#: Older runners drop mismatch_params and run at nominal sigma even when the
#: report echoes the requested scale, invalidating the negative control (#5486).
#: The #5645 five (``corners.options``, ``options.nodeset``,
#: ``options.initial_conditions``, ``analysis_overrides``,
#: ``analysis.op_check``) are mandatory because each one's post-run
#: ``*_unsupported_worker`` echo check already hard-fails the whole request
#: -- mandatory membership mirrors that severity, moving the identical
#: refusal from after the grid ran to before anything is launched.
#: ``lossless supply_v corner_id spelling`` (#5870) hard-fails on a post-run
#: ``supply_precision_unsupported_worker`` echo. Its unreleased registry version
#: is ``None``, so only that echo applies until a release pins the pre-launch gate.
#: ``exclusive_bounds`` stops older workers from passing equality at a strict bound.
_MANDATORY_RUNNER_FEATURES = frozenset(
    {
        "mismatch_params",
        "_regrade",
        "transfer",
        "snm",
        "slope_band",
        "crossings_value_from",
        "exclusive_bounds",
        "osdi_pdk_relative",
        "search",
        "search_param",
        "eye",
        "eye_step_or_phase",
        "step_resolution",
        "step_on_unreached",
        "settled",
        "stress",
        "stimulus",
        "dual_injection",
        "complex_vectors",
        "twoport",
        "twoport_noise",
        "corners.params",
        "corners.options",
        "options.nodeset",
        "options.initial_conditions",
        "analysis_overrides",
        "analysis.op_check",
        "lossless supply_v corner_id spelling",
        "post_commands",
    }
)


def _uses_dual_injection(request: dict[str, Any]) -> bool:
    analysis = request.get("analysis")
    if isinstance(analysis, dict) and analysis.get("passes") is not None:
        return True
    return any(
        isinstance(spec, dict) and spec.get("tian") is not None
        for spec in request.get("measurements") or []
    )


def _uses_measurement_expr(request: dict[str, Any]) -> bool:
    return any(
        isinstance(spec, dict) and spec.get("expr") is not None
        for spec in request.get("measurements") or []
    )


def _uses_process_section_objects(request: dict[str, Any]) -> bool:
    bundles = (request.get("corners") or {}).get("process") or []
    return any(
        isinstance(section, dict)
        for bundle in bundles
        if isinstance(bundle, dict)
        for section in bundle.get("sections") or []
    )


def _uses_option(name: str) -> Any:
    return lambda request: bool((request.get("options") or {}).get(name))


#: Detectors for the :data:`REQUEST_FEATURE_MIN_RUNNER_VERSION` entries that
#: name a released version (issue #2901), keyed by the registry key's name
#: before its `` (`` note. A ``None`` (unreleased) entry has no detector on
#: purpose: it is never turned into "every runner is too old" -- the feature
#: it describes is already gated by ``analysis_steps`` above.
_RELEASED_FEATURE_DETECTORS: dict[str, Any] = {
    "measurements[].expr": _uses_measurement_expr,
    "corners.process[].sections[]": _uses_process_section_objects,
    "corners.params": lambda request: bool(
        (request.get("corners") or {}).get("params")
    ),
    "corners.units": lambda request: "units" in (request.get("corners") or {}),
    "corners.temperature_param": lambda request: (
        "temperature_param" in (request.get("corners") or {})
    ),
    "options.ngspice_init": _uses_option("ngspice_init"),
    "options.ngspice_options": _uses_option("ngspice_options"),
    "options.osdi_preload": _uses_option("osdi_preload"),
    "options.stage_model_inputs": _uses_option("stage_model_inputs"),
    # The five #5645 rows: fields whose only worker guard was a post-run
    # *_unsupported_worker echo check, so an old runner image ran the whole
    # grid and the result was then refused wholesale. Each detector mirrors
    # the predicate its echo check uses (sim_op_check.is_declared for
    # analysis.op_check), so "declared" means the same thing pre-launch and
    # post-run.
    "analysis.op_check": lambda request: sim_op_check.is_declared(
        request.get("analysis") or {}
    ),
    "analysis_overrides": lambda request: bool(request.get("analysis_overrides")),
    "options.nodeset": _uses_option("nodeset"),
    "options.initial_conditions": _uses_option("initial_conditions"),
    "corners.options": lambda request: bool(
        (request.get("corners") or {}).get("options")
    ),
}

# One registry, not two: the #3232 minimum versions feed the same table the
# pre-launch, runner-side and post-run checks already read.
for _key, _version in REQUEST_FEATURE_MIN_RUNNER_VERSION.items():
    _name = _key.split(" (")[0]
    if _version is not None:
        _RUNNER_REQUEST_FEATURES[_name] = (
            tuple(int(part) for part in _version.split(".")),  # type: ignore[assignment]
            _RELEASED_FEATURE_DETECTORS[_name],
        )
del _key, _version, _name


def _release_key(release: str) -> int | None:
    """A sortable integer for the first three components of a
    :func:`_release_segment` (missing ones are 0), or ``None``. The runner
    preflight computes the same number in shell."""
    if not re.fullmatch(r"\d+(?:\.\d+)*", release):
        return None
    parts = [int(part) for part in release.split(".")[:3]]
    parts += [0] * (3 - len(parts))
    return (parts[0] * 10**6 + parts[1]) * 10**6 + parts[2]


def _request_features(request: dict[str, Any]) -> list[str]:
    """The :data:`_RUNNER_REQUEST_FEATURES` names ``request`` uses."""
    return [
        name for name, (_, uses) in _RUNNER_REQUEST_FEATURES.items() if uses(request)
    ]


def _feature_release_key(name: str) -> int:
    """Comparable key of the runner release that introduced registry feature
    ``name``. Registry versions are literal tuples, so they always parse."""
    key = _release_key(".".join(map(str, _RUNNER_REQUEST_FEATURES[name][0])))
    assert key is not None, name
    return key


def _missing_runner_features(
    features: list[str], runner_version: str | None, step_probe: object = None
) -> list[str] | None:
    """Known missing features, or None when only unverified features remain."""
    release = _release_segment(runner_version) if runner_version else None
    key = _release_key(release) if release else None
    if key is None:
        return None
    missing = [name for name in features if key < _feature_release_key(name)]
    # SCM development distance counts commits on a branch, not feature ancestry.
    if (
        "step_on_unreached" in features
        and key == _feature_release_key("step_on_unreached")
        and release != (runner_version or "").strip().partition("+")[0]
    ):
        if step_probe == "unsupported":
            missing.append("step_on_unreached")
        elif step_probe != "supported" and not missing:
            return None
    return missing


#: Filename the worker capture writes under ``$EDA_OUTPUT_DIR`` (issue #2834):
#: what the *job instance* actually resolved -- simulator binary and banner,
#: PDK variant revision stamp, CPU model, ``OMP_NUM_THREADS``, a worker-local
#: image manifest when present. Written by the same runner preflight that
#: writes :data:`BATCH_RUNNER_IDENTITY_FILENAME`; absent on older workers.
