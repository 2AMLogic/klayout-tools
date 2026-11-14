"""Batch contract: job.json spec and runner feature gates.

Split from :mod:`klayout_tools.sim_batch_contract` (issue #4065), moved verbatim.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from . import (
    remote_transport,
    sim_eye,
    sim_search,
    sim_settled,
    sim_snm_meas,
    sim_step_resolution,
    sim_stress,
    sim_transfer_meas,
    sim_twoport,
)
from .sim_batch_contract import (
    REQUEST_FEATURE_MIN_RUNNER_VERSION,
)

if TYPE_CHECKING:
    pass


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
        return document


def _release_segment(version: str) -> str | None:
    """The PEP 440 release segment of a ``klt --version`` build string, or
    ``None`` when it has none (issue #3184).

    Steps: drop the PEP 440 local part (``+...``), keep the leading
    ``[0-9.]`` run, drop one trailing ``.``, and accept the result only if it
    is ``N(.N)*``. This equals ``packaging.version.Version(v).release`` for
    the strings klt produces, without importing ``packaging`` (not a runtime
    dependency). The strings klt produces (``_version_scheme.render``) are
    ``X.Y.Z`` for a clean build on a release tag, ``X.Y.(Z+1).devN+g<sha>``
    for any commit after the released base's tag (so ``0.7.1.dev212+g<sha>``
    on a checkout past ``v0.7.0``), ``X.Y.Z.devN+g<sha>`` before the base is
    tagged, either with ``.dirty`` appended to the local part, and
    ``X.Y.Z.dev0+unknown`` with no git facts. Their release segments are
    ``X.Y.Z``, ``X.Y.(Z+1)``, ``X.Y.Z`` and ``X.Y.Z``.

    The gate does not compare this whole segment: it compares
    :func:`_runner_gate_release`, its first two components.
    """
    head = version.strip().split("+", 1)[0]
    prefix = re.match(r"[0-9.]*", head).group(0)  # always matches
    if prefix.endswith("."):
        prefix = prefix[:-1]
    return prefix if re.fullmatch(r"\d+(?:\.\d+)*", prefix) else None


def _runner_gate_release(version: str) -> str | None:
    """The ``major.minor`` the runner version gate compares, or ``None``
    (issue #3184).

    The gate compares major.minor only: patch releases, ``.devN`` builds and
    ``+g<sha>`` local builds count as the same version as their major.minor.
    So a post-tag git client ``0.7.1.dev212+g<sha>`` matches a ``0.7.0``
    runner, ``0.7.3`` matches ``0.7.0`` in either direction, and
    ``0.8.0.devN`` or ``0.8.0`` against any ``0.7.x`` is a mismatch. A
    version whose release segment (:func:`_release_segment`) has fewer than
    two components (``7``) has no gate release and counts as unknown.

    This rests on a release discipline: a change that breaks the runner
    contract ships in a minor release, never a patch, and every minor
    release is published to PyPI and gets a rebaked runner image.
    :func:`_runner_preflight_script` repeats the same steps in POSIX shell on
    the runner, so both sides of the gate use one rule.
    """
    release = _release_segment(version)
    if release is None:
        return None
    parts = release.split(".")
    return ".".join(parts[:2]) if len(parts) >= 2 else None


#: Request features a client can send that a runner built before the feature
#: landed cannot parse (issue #3249): ``name -> (first runner release that
#: supports it, detector)``. The major.minor gate (:func:`_runner_gate_release`)
#: deliberately lets a post-tag client talk to a patch-level older runner, so
#: it cannot see these; this registry compares the *full* release segment
#: instead. ``analysis_steps[]`` (#2482) landed after ``v0.7.0``, so the first
#: release segment that can carry it is ``0.7.1`` (post-tag builds render as
#: ``0.7.1.devN``). A ``0.7.1.devN`` runner built before the feature landed is
#: indistinguishable from one built after, so it counts as supporting it.
#: ``transfer`` (``measurements[].transfer``, #2923/#2924) was merged after
#: ``v0.7.0`` too, so ``0.7.1`` is likewise its first release segment, as is
#: ``stimulus`` (the declarative timed stimulus block, #2977), and
#: ``dual_injection`` (``analysis.passes`` / ``measurements[].tian``,
#: #3159/#3160/#3161), ``complex_vectors`` (``options.complex_vectors``,
#: #3337), ``twoport`` (``analysis.kind: "twoport"``, #3338), and
#: ``twoport_noise`` (``analysis.noise``, #3339), and ``snm``
#: (``measurements[].snm``, #3895), and ``search`` (``measurements[].search``,
#: #4121). ``eye`` (``measurements[].eye``, #4292) landed after ``v0.8.0``, so
#: its first release segment is ``0.8.1``, as is ``stress`` (#4592/#4858).
#: ``search_param`` (``measurements[].search.param``, #5391) landed after
#: ``v0.9.1``, so its first release segment is ``0.9.2``; an older runner would
#: reject the key or silently search nothing, hence mandatory.
def _declared(finder: Callable[[list[Any]], Any]) -> Callable[[dict[str, Any]], bool]:
    """Detector: the request declares at least one measurement ``finder`` finds."""
    return lambda request: bool(finder(request.get("measurements") or []))


_step_res_declared = _declared(sim_step_resolution.step_resolution_measurements)
_RUNNER_REQUEST_FEATURES: dict[str, tuple[tuple[int, int, int], Any]] = {
    "analysis_steps": ((0, 7, 1), lambda r: r.get("analysis_steps") is not None),
    "transfer": ((0, 7, 1), _declared(sim_transfer_meas.transfer_measurements)),
    "snm": ((0, 7, 1), _declared(sim_snm_meas.snm_measurements)),
    "eye": ((0, 8, 1), _declared(sim_eye.eye_measurements)),
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
#: process x temperature with ``status: pass`` (#4235).
_MANDATORY_RUNNER_FEATURES = frozenset(
    {
        "transfer",
        "snm",
        "search",
        "search_param",
        "eye",
        "step_resolution",
        "settled",
        "stress",
        "stimulus",
        "dual_injection",
        "complex_vectors",
        "twoport",
        "twoport_noise",
        "corners.params",
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
    features: list[str], runner_version: str | None
) -> list[str] | None:
    """The ``features`` a runner of ``runner_version`` predates, or ``None``
    when its version is unknown (unknown is never "missing")."""
    release = _release_segment(runner_version) if runner_version else None
    key = _release_key(release) if release else None
    if key is None:
        return None
    return [name for name in features if key < _feature_release_key(name)]


#: Filename the worker capture writes under ``$EDA_OUTPUT_DIR`` (issue #2834):
#: what the *job instance* actually resolved -- simulator binary and banner,
#: PDK variant revision stamp, CPU model, ``OMP_NUM_THREADS``, a worker-local
#: image manifest when present. Written by the same runner preflight that
#: writes :data:`BATCH_RUNNER_IDENTITY_FILENAME`; absent on older workers.
