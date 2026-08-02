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
not a code path -- but only ``"ngspice"`` is implemented in this version.
``ngspice -b`` is invoked as a subprocess per corner (never ``libngspice``):
process-corner selection needs a fresh ``.lib`` parse anyway, a hung engine
must be killable without taking down our own process, and process fan-out is
the whole parallelism story for a future ``max_parallel`` (see the spike's
"Invocation strategy").

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

import json
import os
import re
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from ._provenance import build_provenance, sha256_file
from .pdk import PdkNotFoundError, find_pdk

#: Bumped only on a non-additive (breaking) change to this command's own
#: JSON shape -- see docs/json-contract.md.
SCHEMA_VERSION = 1

#: Per-corner wall-clock budget applied when the request omits
#: ``options.timeout_s``.
DEFAULT_TIMEOUT_S = 120

#: ``ngspice`` is the only implemented engine in v1; see this module's
#: docstring and the spike's engine survey for why.
SUPPORTED_ENGINES = ("ngspice",)

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
#: request in, same report out, just faster on a multi-core box. Like
#: ``engine``, ``backend`` is a request *data* field, not part of the JSON
#: shape -- ``remote`` (Epic #253 later phases) is reserved but unimplemented
#: here. Unknown names raise :class:`SimError` before any corner runs,
#: mirroring ``engine`` validation.
SUPPORTED_BACKENDS = ("local", "local-parallel")

#: Assumed ngspice worker-thread count used to derive the ``local-parallel``
#: backend's conservative default ``max_workers`` -- see :func:`_default_max_workers`.
#: Each ``ngspice -b`` process is itself internally multi-threaded (BLAS/matrix
#: solve), so a naive one-worker-per-corner pool oversubscribes a small box
#: immediately (see #253/#168's design note); this factor is a deliberately
#: conservative estimate, not a measured value.
_ASSUMED_THREADS_PER_NGSPICE = 8

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

#: A `.meas` card's declared analysis type (the token right after
#: ``.meas``/``.measure``), e.g. ``tran`` in ``.meas tran vout find v(out)``.
_MEAS_CARD_TYPE_RE = re.compile(r"^\s*\.meas(?:ure)?\s+(\S+)", re.IGNORECASE)

#: ngspice's own ``.MEASURE`` statement only implements these analysis types
#: (verified empirically against ngspice 46 -- see issue #205). Notably,
#: there is no ``.MEASURE OP``: an operating-point analysis has no sweep
#: variable for a measurement to search over, unlike DC/AC/TRAN/SP.
_SUPPORTED_MEAS_TYPES = frozenset({"dc", "ac", "tran", "sp"})

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
        "netlist",
        re.compile(
            r"^Error:\s*.*\b(syntax|unknown|undefined|parse|subckt)\b",
            re.IGNORECASE | re.MULTILINE,
        ),
    ),
)


class SimError(Exception):
    """Raised when a corner sweep cannot even be attempted: a missing/malformed
    request file, an unresolvable netlist or model library, or an unsupported
    engine.

    The CLI turns this into a clean stderr message + exit code 1, never a
    traceback. Distinct from a *corner* reporting ``status: "error"`` inside
    an otherwise-successful response -- that is a documented outcome, not an
    exception (see this module's docstring and docs/cli/sim.md).
    """


def load_request(request_path: str) -> dict[str, Any]:
    """Read and minimally validate a ``klt sim`` request JSON file.

    Raises :class:`SimError` if the file is missing/unreadable, not valid
    JSON, or missing a required top-level field (``netlist``, ``analysis``).
    ``models`` is validated separately, in :func:`run_sim` -- it is only
    required when the request declares a ``corners.process`` axis (see
    ``_resolve_models_lib``). Does not require the request to carry a
    ``schema`` field (see this module's docstring).
    """
    if not os.path.exists(request_path):
        raise SimError(f"file not found: {request_path}")
    if os.path.isdir(request_path):
        raise SimError(f"not a file: {request_path}")

    try:
        with open(request_path, encoding="utf-8") as handle:
            request = json.load(handle)
    except (OSError, UnicodeDecodeError) as exc:
        raise SimError(f"could not read request file: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise SimError(f"request file is not valid JSON: {exc}") from exc

    if not isinstance(request, dict):
        raise SimError("request file must contain a JSON object")

    for field in ("netlist", "analysis"):
        if field not in request:
            raise SimError(f"request is missing required field: {field}")

    return request


def _validate_meas_card(name: str, spice: str) -> None:
    """Reject a ``.meas`` card declaring an analysis type ngspice's own
    ``.MEASURE`` statement does not implement.

    Most notably ``"op"``: it is a legitimate ``analysis.kind`` (ngspice's
    operating-point analysis runs fine as a plain ``op`` control-block
    command), but there is no such thing as ``.MEASURE OP`` -- an operating
    point has no sweep variable for a measurement to search over the way
    DC/AC/TRAN/SP do. Pairing ``analysis.kind: "op"`` with a ``.meas op``
    card fails with ngspice's own
    ``Error: unrecognized analysis type 'op' for the following .meas
    statement`` (verified against ngspice 46, see issue #205) -- this
    catches it before the request ever reaches ngspice, with an actionable
    message instead of a raw parser error. Applies to every ``.meas`` card
    regardless of the request's own ``analysis.kind``: a ``.meas`` type
    ngspice does not implement is invalid on its own terms, not just when
    paired with a mismatched analysis.

    A card this function does not recognise as a ``.meas``/``.measure``
    line (unexpected shape) is left alone -- validating hand-written SPICE
    syntax in general is out of scope here.
    """
    match = _MEAS_CARD_TYPE_RE.match(spice)
    if match is None:
        return
    meas_type = match.group(1).lower()
    if meas_type not in _SUPPORTED_MEAS_TYPES:
        raise SimError(
            f"measurement '{name}' declares an unsupported .meas analysis "
            f"type '{meas_type}': ngspice has no `.MEASURE {meas_type.upper()}` "
            'analysis type; use analysis.kind: "tran" with a short '
            "single-step transient and `.meas tran ... at=<t>` instead"
        )


def run_sim(
    request_path: str,
    *,
    artifacts_dir: str | None = None,
    backend: str | None = None,
    max_workers: int | None = None,
) -> dict[str, Any]:
    """Run the PVT corner matrix declared by the request at ``request_path``.

    ``artifacts_dir`` overrides where per-corner logs/rawfiles are written
    when ``options.keep_artifacts`` is true; it defaults to a ``.klt/sim/``
    directory next to the request file (the same "next to the input"
    convention as ``klt render``'s default output directory).

    ``backend`` selects the execution backend, overriding the request's own
    ``backend`` field when given (the ``--backend`` CLI flag path). When both
    are omitted the backend defaults to ``local``. ``local`` and
    ``local-parallel`` are implemented; any other name raises
    :class:`SimError` (see :data:`SUPPORTED_BACKENDS`).

    ``max_workers`` bounds the ``local-parallel`` backend's worker pool,
    overriding the request's own ``options.max_workers`` when given (the
    ``--max-workers`` CLI flag path). Ignored by ``local``. When both are
    omitted, defaults to a conservative estimate derived from
    ``os.cpu_count()`` (see :func:`_default_max_workers`) -- each ``ngspice``
    process is itself internally multi-threaded, so one worker per CPU
    oversubscribes a small box immediately. Must be a positive integer when
    given explicitly; a non-positive value raises :class:`SimError`.

    Returns a dict matching the documented JSON schema (see
    ``docs/cli/sim.md``). Raises :class:`SimError` for anything that prevents
    the sweep from starting at all (bad request, unresolvable netlist/model
    library, unsupported engine, unknown backend) -- once the sweep starts,
    every corner is reported in the response, including ones that error.
    """
    request = load_request(request_path)
    request_dir = os.path.dirname(os.path.abspath(request_path))

    engine = request.get("engine", "ngspice")
    if engine not in SUPPORTED_ENGINES:
        raise SimError(
            f"unsupported engine '{engine}' (supported: {', '.join(SUPPORTED_ENGINES)})"
        )

    backend = backend if backend is not None else request.get("backend", "local")
    if backend not in SUPPORTED_BACKENDS:
        raise SimError(
            f"unsupported backend '{backend}' "
            f"(supported: {', '.join(SUPPORTED_BACKENDS)})"
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

    corners_spec = request.get("corners") or {}
    models = request.get("models") or {}
    models_lib: str | None = None
    if corners_spec.get("process"):
        # Only the process axis needs a model library -- supply/temperature
        # are plain netlist/control-block mutations (see this module's
        # docstring and the spike's "Native PVT sweeping" survey row).
        models_lib = _resolve_models_lib(models, request_dir)

    # Best-effort PDK resolution for the shared `provenance` block: `models`
    # may name a PDK variant (`models.pdk`) whose release stamps the run's
    # model library. Never fatal -- the sweep already resolved `models_lib`
    # above; if the variant can't be found here, provenance.pdk is null
    # rather than fabricated (see `_provenance.build_provenance`).
    provenance_pdk: dict[str, Any] | None = None
    if models.get("pdk") or models.get("pdk_root"):
        try:
            provenance_pdk = find_pdk(
                variant=models.get("pdk"), root=models.get("pdk_root")
            )
        except PdkNotFoundError:
            provenance_pdk = None

    analysis = request.get("analysis") or {}
    if "kind" not in analysis or "args" not in analysis:
        raise SimError("request.analysis requires 'kind' and 'args'")

    measurements_spec = request.get("measurements", [])
    for spec in measurements_spec:
        if "name" not in spec or "spice" not in spec:
            raise SimError(
                "each request.measurements[] entry requires 'name' and 'spice'"
            )
        _validate_meas_card(spec["name"], spec["spice"])

    options = request.get("options") or {}
    timeout_s = options.get("timeout_s", DEFAULT_TIMEOUT_S)
    keep_artifacts = bool(options.get("keep_artifacts", False))
    want_waveforms = bool(options.get("waveforms", False))

    max_workers = max_workers if max_workers is not None else options.get("max_workers")
    if max_workers is not None:
        if (
            not isinstance(max_workers, int)
            or isinstance(max_workers, bool)
            or max_workers < 1
        ):
            raise SimError("options.max_workers must be a positive integer")

    if artifacts_dir is None:
        artifacts_dir = os.path.join(request_dir, ".klt", "sim")

    corner_points = _expand_corners(corners_spec, request.get("exclude") or [])

    corners, engine_version = _BACKENDS[backend](
        corner_points=corner_points,
        netlist_path=netlist_path,
        models_lib=models_lib,
        analysis=analysis,
        measurements_spec=measurements_spec,
        timeout_s=timeout_s,
        keep_artifacts=keep_artifacts,
        want_waveforms=want_waveforms,
        artifacts_dir=artifacts_dir,
        max_workers=max_workers,
    )

    measurements_rollup = _rollup_measurements(measurements_spec, corners)

    passed = sum(1 for c in corners if c["status"] == "pass")
    failed = sum(1 for c in corners if c["status"] == "fail")
    errored = sum(1 for c in corners if c["status"] == "error")
    if errored:
        status = "error"
    elif failed:
        status = "fail"
    else:
        status = "pass"

    environment: dict[str, Any] = {
        "engine": engine,
        "engine_version": engine_version,
        "models_lib": models_lib,
        "models_lib_sha256": sha256_file(models_lib),
        "netlist_sha256": sha256_file(netlist_path),
    }
    if netlist_source is not None:
        # Additive/optional: only present when the request declares it, so
        # existing consumers of a request that omits netlist_source see an
        # unchanged environment block (see docs/cli/sim.md).
        environment["netlist_source"] = netlist_source

    return {
        "schema_version": SCHEMA_VERSION,
        "netlist": netlist_ref,
        "status": status,
        "corner_count": len(corners),
        "passed": passed,
        "failed": failed,
        "errored": errored,
        "environment": environment,
        "provenance": build_provenance(
            deck_name=(os.path.basename(models_lib) if models_lib else None),
            deck_path=models_lib,
            pdk=provenance_pdk,
        ),
        "measurements": measurements_rollup,
        "corners": corners,
    }


# --------------------------------------------------------------------------- #
# Model/netlist path resolution
# --------------------------------------------------------------------------- #


def _resolve_relative(path: str, base_dir: str) -> str:
    """Expand env vars/``~`` in ``path``; join relative paths against ``base_dir``."""
    expanded = os.path.expanduser(os.path.expandvars(path))
    if os.path.isabs(expanded):
        return expanded
    return os.path.join(base_dir, expanded)


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

    pdk_variant = models.get("pdk")
    pdk_root = models.get("pdk_root")
    if pdk_variant is not None or pdk_root is not None:
        try:
            resolution = find_pdk(variant=pdk_variant, root=pdk_root)
        except PdkNotFoundError as exc:
            raise SimError(str(exc)) from exc
        variant_dir = os.path.join(resolution["root"], resolution["variant"])
        resolved = lib if os.path.isabs(lib) else os.path.join(variant_dir, lib)
    else:
        resolved = _resolve_relative(lib, request_dir)

    if not os.path.isfile(resolved):
        raise SimError(f"model library not found: {resolved}")
    return resolved


# --------------------------------------------------------------------------- #
# Corner-matrix expansion
# --------------------------------------------------------------------------- #


class CornerPoint:
    """One expanded (process, supply, temperature) point, pre-run."""

    __slots__ = ("process", "supply_v", "temperature_c")

    def __init__(
        self,
        process: str | None,
        supply_v: dict[str, float],
        temperature_c: float,
    ) -> None:
        self.process = process
        self.supply_v = supply_v
        self.temperature_c = temperature_c

    @property
    def corner_id(self) -> str:
        """``<process>/<supply>V/<temp>C``, per the spike's response schema.

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
        return f"{process_label}/{supply_label}/{temp_label}C"

    @property
    def slug(self) -> str:
        """Filesystem-safe artifact-directory name for this corner, derived
        from :attr:`corner_id` (e.g. ``ss/1.620V/125C`` -> ``ss_1p620V_125C``)."""
        label = self.corner_id.replace("/", "_")
        return label.replace(".", "p").replace("=", "").replace("-", "n")


def _format_number(value: float) -> str:
    """Render an int-valued float without a trailing ``.0`` (e.g. temperatures)."""
    if float(value).is_integer():
        return str(int(value))
    return str(value)


def _expand_corners(
    corners_spec: dict[str, Any], exclude_spec: list[dict[str, Any]]
) -> list[CornerPoint]:
    """Expand the declared axes into the deterministic, odometer-style corner list.

    Axis order (outer to inner, per the spike): process, then the supply
    sweep, then temperature. A missing axis collapses to a single default
    point (``process=None``, ``supply_v={}``, ``temperature_c=27``) rather
    than an error -- a request that only cares about one axis need not
    populate the others.

    ``corners.supply_v``'s keys sweep together by index (same-length arrays;
    rails move as a set), matching the spike's documented semantics.
    """
    processes: list[str | None] = corners_spec.get("process") or [None]

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

    temperatures: list[float] = corners_spec.get("temperature_c") or [27]

    points: list[CornerPoint] = []
    for process in processes:
        for supply_v in supply_points:
            for temperature_c in temperatures:
                point = CornerPoint(process, supply_v, temperature_c)
                if not _is_excluded(point, exclude_spec):
                    points.append(point)
    return points


def _is_excluded(point: CornerPoint, exclude_spec: list[dict[str, Any]]) -> bool:
    """A point is excluded if it matches every key of any one exclude entry."""
    for entry in exclude_spec:
        if _matches_exclude(point, entry):
            return True
    return False


def _matches_exclude(point: CornerPoint, entry: dict[str, Any]) -> bool:
    for key, value in entry.items():
        if key == "process":
            if point.process != value:
                return False
        elif key == "temperature_c":
            if point.temperature_c != value:
                return False
        elif key == "supply_v":
            if not isinstance(value, dict):
                return False
            for supply_key, supply_value in value.items():
                if point.supply_v.get(supply_key) != supply_value:
                    return False
        else:
            return False
    return True


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
) -> tuple[list[dict[str, Any]], str | None]:
    """The ``local`` backend: run each expanded corner sequentially in-process.

    This is the unit a backend implements -- given the expanded corner list
    plus the already-resolved netlist/model paths and run options, return
    ``(per_corner_reports, engine_version)``. It intentionally reproduces the
    original in-line loop byte-for-byte (same ordering, same
    ``engine_version`` last-writer-wins tracking) so the report JSON and
    on-disk artifacts are unchanged from before the backend seam existed.
    ``max_workers`` is accepted for signature parity with the other backends
    (every entry in :data:`_BACKENDS` is called with the same keyword set)
    but is meaningless for a sequential runner, so it is ignored here.
    """
    del max_workers  # unused: sequential by definition, see docstring
    engine_version: str | None = None
    corners: list[dict[str, Any]] = []
    for point in corner_points:
        result, version = _run_corner(
            point=point,
            netlist_path=netlist_path,
            models_lib=models_lib,
            analysis=analysis,
            measurements_spec=measurements_spec,
            timeout_s=timeout_s,
            keep_artifacts=keep_artifacts,
            want_waveforms=want_waveforms,
            artifacts_dir=artifacts_dir,
        )
        corners.append(result)
        if version is not None:
            engine_version = version
    return corners, engine_version


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
    """
    cpu_count = os.cpu_count() or 1
    return max(1, cpu_count // _ASSUMED_THREADS_PER_NGSPICE)


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
) -> tuple[list[dict[str, Any]], str | None]:
    """The ``local-parallel`` backend: fan the expanded corner list across a
    bounded local worker pool.

    Corners share nothing -- each is a pure function of netlist + models +
    corner (see this module's docstring and #253/#168) -- so this is a
    :class:`~concurrent.futures.ThreadPoolExecutor` pool over :func:`_run_corner`
    (a thread, not a process, pool: the actual work is the ``ngspice``
    subprocess call, which releases the GIL for the duration of the wait, so
    threads give the same concurrency as processes here without the
    pickling/import overhead of ``ProcessPoolExecutor``). Futures are indexed
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
    """
    resolved_workers = (
        max_workers if max_workers is not None else _default_max_workers()
    )

    results: list[tuple[dict[str, Any], str | None] | None] = [None] * len(
        corner_points
    )
    with ThreadPoolExecutor(max_workers=resolved_workers) as pool:
        future_to_index = {
            pool.submit(
                _run_corner,
                point=point,
                netlist_path=netlist_path,
                models_lib=models_lib,
                analysis=analysis,
                measurements_spec=measurements_spec,
                timeout_s=timeout_s,
                keep_artifacts=keep_artifacts,
                want_waveforms=want_waveforms,
                artifacts_dir=artifacts_dir,
            ): index
            for index, point in enumerate(corner_points)
        }
        for future in as_completed(future_to_index):
            results[future_to_index[future]] = future.result()

    engine_version: str | None = None
    corners: list[dict[str, Any]] = []
    for result in results:
        assert result is not None  # every index was submitted exactly once
        corner, version = result
        corners.append(corner)
        if version is not None:
            engine_version = version
    return corners, engine_version


#: Backend registry: name -> implementation. Membership is validated against
#: :data:`SUPPORTED_BACKENDS` in :func:`run_sim` before dispatch, so this only
#: ever holds implemented backends.
_BACKENDS = {"local": _run_local, "local-parallel": _run_local_parallel}


# --------------------------------------------------------------------------- #
# Per-corner ngspice invocation
# --------------------------------------------------------------------------- #

_ENGINE_VERSION_RE = re.compile(r"ngspice-([\w.]+)")


def _run_corner(
    *,
    point: CornerPoint,
    netlist_path: str,
    models_lib: str | None,
    analysis: dict[str, Any],
    measurements_spec: list[dict[str, Any]],
    timeout_s: float,
    keep_artifacts: bool,
    want_waveforms: bool,
    artifacts_dir: str,
) -> tuple[dict[str, Any], str | None]:
    """Run one corner point through ``ngspice -b`` and classify the result.

    Returns ``(corner_report, engine_version_or_none)``. Never raises: any
    failure to run (bad spawn, timeout, nonzero-but-uninformative exit) is
    folded into the corner's own ``status: "error"`` + ``diagnostics``,
    per the contract's "every corner is reported" guarantee.
    """
    if keep_artifacts:
        corner_dir = os.path.join(artifacts_dir, point.slug)
        os.makedirs(corner_dir, exist_ok=True)
    else:
        corner_dir = _tmp_work_dir()

    deck_path = os.path.join(corner_dir, "corner.cir")
    log_path = os.path.join(corner_dir, "ngspice.log")
    raw_path = os.path.join(corner_dir, "waveform.raw") if want_waveforms else None

    _write_corner_deck(
        deck_path=deck_path,
        netlist_path=netlist_path,
        models_lib=models_lib,
        point=point,
        analysis=analysis,
        measurements_spec=measurements_spec,
        raw_path=raw_path,
    )

    diagnostics: list[dict[str, str]] = []
    started = time.monotonic()
    timed_out = False
    engine_version: str | None = None
    try:
        completed = subprocess.run(
            ["ngspice", "-b", deck_path, "-o", log_path],
            capture_output=True,
            text=True,
            timeout=timeout_s,
        )
        engine_version = _extract_engine_version(completed.stdout)
    except subprocess.TimeoutExpired:
        timed_out = True
    except FileNotFoundError as exc:
        diagnostics.append(
            {
                "severity": "error",
                "code": "unknown",
                "message": f"could not launch ngspice: {exc}",
            }
        )
    runtime_s = round(time.monotonic() - started, 3)

    log_text = ""
    if os.path.isfile(log_path):
        try:
            with open(log_path, encoding="utf-8", errors="replace") as handle:
                log_text = handle.read()
        except OSError:
            log_text = ""

    if timed_out:
        diagnostics.append(
            {
                "severity": "error",
                "code": "timeout",
                "message": f"ngspice did not complete within {timeout_s}s, killed",
            }
        )
    else:
        diagnostics.extend(_classify_diagnostics(log_text))

    measurement_values = _parse_measurements(log_text)
    measurement_results: list[dict[str, Any]] = []
    for spec in measurements_spec:
        name = spec["name"]
        value = measurement_values.get(name)
        unit = spec.get("unit")
        limits = spec.get("limits")
        if value is None:
            measurement_results.append(
                {
                    "name": name,
                    "value": None,
                    "unit": unit,
                    "status": "error",
                    "margin": None,
                }
            )
            diagnostics.append(
                {
                    "severity": "error",
                    "code": "measurement",
                    "message": f"measurement '{name}' produced no value",
                }
            )
        else:
            m_status, margin = _evaluate_limits(value, limits)
            measurement_results.append(
                {
                    "name": name,
                    "value": value,
                    "unit": unit,
                    "status": m_status,
                    "margin": margin,
                }
            )

    # A recovered `singular_matrix`/`nonconvergence` classification (ngspice's
    # own gmin/source-stepping recovery narration -- routine noise on the way
    # to a *successful* analysis, not evidence the run failed) does not count
    # as fatal on its own: downgrade its severity in place before computing
    # the corner's aggregate status. See `_recovered_from_stepping` and issue
    # #205; `timeout`/`netlist`/`measurement`/`unknown` are never downgraded.
    if _recovered_from_stepping(measurements_spec, measurement_results, log_text):
        for diagnostic in diagnostics:
            if diagnostic["code"] in ("singular_matrix", "nonconvergence"):
                diagnostic["severity"] = "warning"

    # error > fail > pass, mirroring the response's own aggregate precedence:
    # any engine-level diagnostic still at severity "error" (timeout, an
    # unrecovered singular matrix/nonconvergence, netlist, unresolvable
    # measurement, unknown) means no trustworthy result exists for this
    # corner, which always outranks a clean limit violation.
    if any(d["severity"] == "error" for d in diagnostics) or any(
        m["status"] == "error" for m in measurement_results
    ):
        status = "error"
    elif any(m["status"] == "fail" for m in measurement_results):
        status = "fail"
    else:
        status = "pass"

    artifacts: dict[str, str | None] = {"log": None, "raw": None, "waveform": None}
    if keep_artifacts:
        if os.path.isfile(log_path):
            artifacts["log"] = log_path
        if raw_path is not None and os.path.isfile(raw_path):
            artifacts["raw"] = raw_path
            artifacts["waveform"] = _parse_and_persist_waveform(raw_path)
    else:
        _cleanup_dir(corner_dir)

    corner = {
        "corner_id": point.corner_id,
        "process": point.process,
        "supply_v": point.supply_v,
        "temperature_c": point.temperature_c,
        "status": status,
        "runtime_s": runtime_s,
        "measurements": measurement_results,
        "diagnostics": diagnostics,
        "artifacts": artifacts,
    }
    return corner, engine_version


def _tmp_work_dir() -> str:
    import tempfile

    return tempfile.mkdtemp(prefix="klt-sim-")


def _cleanup_dir(path: str) -> None:
    import shutil

    shutil.rmtree(path, ignore_errors=True)


def _write_corner_deck(
    *,
    deck_path: str,
    netlist_path: str,
    models_lib: str | None,
    point: CornerPoint,
    analysis: dict[str, Any],
    measurements_spec: list[dict[str, Any]],
    raw_path: str | None,
) -> None:
    """Generate the corner-specific ngspice deck: ``.lib``/``.include``/``.temp``,
    the request's verbatim ``.meas`` cards, and a ``.control`` block that
    ``alter``s the supply sources, optionally captures an ASCII rawfile, and
    runs the declared analysis.
    """
    lines = ["* klt sim -- generated corner deck, do not edit"]
    if point.process is not None:
        lines.append(f".lib {models_lib} {point.process}")
    lines.append(f".include {netlist_path}")
    lines.append(f".temp {point.temperature_c}")

    for spec in measurements_spec:
        lines.append(spec["spice"])

    lines.append(".control")
    for key, value in sorted(point.supply_v.items()):
        lines.append(f"alter {key}={value}")
    if raw_path is not None:
        lines.append("set filetype=ascii")
    lines.append(f"{analysis['kind']} {analysis['args']}")
    if raw_path is not None:
        lines.append(f"write {raw_path}")
    lines.append("quit")
    lines.append(".endc")
    lines.append(".end")

    with open(deck_path, "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")


def _extract_engine_version(stdout: str) -> str | None:
    match = _ENGINE_VERSION_RE.search(stdout or "")
    return match.group(1) if match else None


def _classify_diagnostics(log_text: str) -> list[dict[str, str]]:
    """Classify structured diagnostics from ``log_text``, per this module's
    ordered ``_DIAGNOSTIC_PATTERNS`` (most specific first, one match per code)."""
    diagnostics: list[dict[str, str]] = []
    for code, pattern in _DIAGNOSTIC_PATTERNS:
        match = pattern.search(log_text)
        if match:
            line = _line_containing(log_text, match.start())
            diagnostics.append({"severity": "error", "code": code, "message": line})
    return diagnostics


def _line_containing(text: str, offset: int) -> str:
    start = text.rfind("\n", 0, offset) + 1
    end = text.find("\n", offset)
    if end == -1:
        end = len(text)
    return text[start:end].strip()


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

    Conservative by construction: recovery requires *every* requested
    measurement to have actually come back with a value (a corner with no
    ``measurements[]`` at all has no independent signal to check against, so
    it is never considered recovered), and ngspice's own
    ``simulation(s) aborted`` trailer must be absent -- either one failing
    means the analysis never produced a trustworthy result and the
    classification stays fatal.
    """
    if not measurements_spec:
        return False
    if any(m["status"] == "error" for m in measurement_results):
        return False
    if _SIMULATION_ABORTED_RE.search(log_text):
        return False
    return True


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


def _rollup_measurements(
    measurements_spec: list[dict[str, Any]], corners: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Per-measurement rollup across all corners: aggregate status and the
    worst-case corner (smallest/most-negative margin; ``None`` margins --
    unextractable values -- are treated as worst of all)."""
    rollup: list[dict[str, Any]] = []
    for spec in measurements_spec:
        name = spec["name"]
        entries = []
        for corner in corners:
            for m in corner["measurements"]:
                if m["name"] == name:
                    entries.append((corner, m))
                    break

        statuses = {m["status"] for _, m in entries}
        if "error" in statuses:
            agg_status = "error"
        elif "fail" in statuses:
            agg_status = "fail"
        else:
            agg_status = "pass"

        worst_case = None
        worst_margin = None
        for corner, m in entries:
            margin = m["margin"]
            sort_key = margin if margin is not None else float("-inf")
            if worst_case is None or sort_key < worst_margin:
                worst_margin = sort_key
                worst_case = {
                    "corner_id": corner["corner_id"],
                    "value": m["value"],
                    "margin": margin,
                }

        rollup.append(
            {
                "name": name,
                "unit": spec.get("unit"),
                "limits": spec.get("limits"),
                "status": agg_status,
                "worst_case": worst_case,
            }
        )
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
    """
    try:
        with open(raw_path, encoding="utf-8", errors="replace") as handle:
            text = handle.read()
    except OSError as exc:
        raise SimError(f"could not read rawfile: {exc}") from exc

    header_match = re.search(r"^Plotname:\s*(.*)$", text, re.MULTILINE)
    if header_match is None:
        raise SimError(f"not a recognisable ngspice rawfile: {raw_path}")
    plotname = header_match.group(1).strip()

    var_count_match = re.search(r"^No\. Variables:\s*(\d+)$", text, re.MULTILINE)
    if var_count_match is None:
        raise SimError(f"malformed rawfile (no variable count): {raw_path}")
    var_count = int(var_count_match.group(1))

    variables_block_match = re.search(
        r"^Variables:\n(.*?)^Values:\n", text, re.MULTILINE | re.DOTALL
    )
    if variables_block_match is None:
        raise SimError(f"malformed rawfile (no Variables/Values section): {raw_path}")
    variables: list[dict[str, Any]] = []
    for line in variables_block_match.group(1).splitlines():
        parts = line.split()
        if len(parts) < 3:
            continue
        variables.append({"index": int(parts[0]), "name": parts[1], "type": parts[2]})
    if len(variables) != var_count:
        raise SimError(
            f"malformed rawfile: declared {var_count} variables, "
            f"parsed {len(variables)}"
        )

    values_text = text[variables_block_match.end() :]
    points: list[list[float]] = []
    current: list[float] = []
    for raw_line in values_text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        tokens = line.split()
        if len(tokens) == 2 and current == []:
            # "<point_index> <value0>" -- start of a new point.
            current = [float(tokens[1])]
        elif len(tokens) == 1:
            current.append(float(tokens[0]))
        else:
            continue
        if len(current) == var_count:
            points.append(current)
            current = []

    return {"plotname": plotname, "variables": variables, "points": points}


def _parse_and_persist_waveform(raw_path: str) -> str:
    """Parse the rawfile at ``raw_path`` to the waveform JSON shape and write
    it alongside as ``<raw_path>.json``, returning that path.

    Kept as its own step (rather than inlining waveform data into the
    response) so the JSON contract's own response never grows to
    waveform-shaped size -- per the spike, waveform data is an optional
    *artifact*, referenced by path, not inlined.
    """
    waveform = parse_ascii_rawfile(raw_path)
    json_path = raw_path + ".json"
    with open(json_path, "w", encoding="utf-8") as handle:
        json.dump(waveform, handle)
    return json_path
