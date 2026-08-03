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

import hashlib
import json
import os
import re
import subprocess
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from . import remote_transport
from ._provenance import build_provenance, sha256_file
from .pdk import PdkNotFoundError, find_pdk
from .pdk_models import _pdk_variant_family
from .remote_launcher import RemoteLauncher, RemoteLaunchError

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
#: request in, same report out, just faster on a multi-core box. ``remote``
#: (Epic #253 Phase 2, issue #265) provisions one right-sized EC2 instance
#: (``remote_launcher.RemoteLauncher``, #264) and runs the *same*
#: ``local-parallel`` worker-pool code on that box instead of the caller's
#: own cores, via the SSH/SCP push-then-pull transport in
#: :mod:`klayout_tools.remote_transport` -- see ``_run_remote`` and
#: ``docs/design/remote-sim-backend-spike.md`` decisions 2/5. Like
#: ``engine``, ``backend`` is a request *data* field, not part of the JSON
#: shape. Unknown names raise :class:`SimError` before any corner runs,
#: mirroring ``engine`` validation.
SUPPORTED_BACKENDS = ("local", "local-parallel", "remote")

#: Recognised values for ``request.monte_carlo.vary`` -- which axis (or
#: axes) of statistical variation the sample sequence is declared to
#: exercise. See :func:`_expand_monte_carlo` and this module's "Monte Carlo
#: sampling" section below for the seed-derivation contract this drives, and
#: docs/cli/sim.md's "Monte Carlo" section for the request/response shape.
SUPPORTED_MC_VARY = ("mismatch", "process", "both")

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
    ("res_", "resistor"),
    ("resistor", "resistor"),
    ("diode", "diode"),
)


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


def _mismatch_family_report(
    netlist_path: str, pdk_variant: str | None
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
    try:
        with open(netlist_path, encoding="utf-8") as handle:
            netlist_text = handle.read()
    except OSError:
        netlist_text = ""

    pdk_family = _pdk_variant_family(pdk_variant) if pdk_variant else None

    report: list[dict[str, Any]] = []
    for family in _detect_device_families(netlist_text):
        entry = _MISMATCH_ACTIVITY.get((pdk_family, family)) if pdk_family else None
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
    are omitted the backend defaults to ``local``. ``local``,
    ``local-parallel``, and ``remote`` are implemented; any other name raises
    :class:`SimError` (see :data:`SUPPORTED_BACKENDS`). ``remote`` requires
    ``request.remote`` and ``request.models.pdk`` -- see ``docs/cli/sim.md``'s
    "Remote backend" section.

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

    monte_carlo_spec = request.get("monte_carlo")
    monte_carlo_info: dict[str, Any] | None = None
    if monte_carlo_spec is not None:
        corner_points, monte_carlo_info = _expand_monte_carlo(
            corner_points, monte_carlo_spec
        )
        if monte_carlo_info["vary"] in ("mismatch", "both"):
            # Additive: per-device-family mismatch-activity report (#355) --
            # only meaningful when this run's sample sequence actually
            # varies mismatch. Never claims a hard-zero-mismatch family
            # (e.g. gf180mcu's poly resistor) was sampled just because the
            # request's global mismatch switch/section was engaged.
            monte_carlo_info = {
                **monte_carlo_info,
                "family_mismatch": _mismatch_family_report(
                    netlist_path, models.get("pdk")
                ),
            }

    corners, engine_version, remote_environment = _BACKENDS[backend](
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
        request=request,
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
    if remote_environment is not None:
        # Additive/optional: only present for the `remote` backend -- see
        # `_run_remote` and docs/cli/sim.md's "Remote backend" section.
        environment["remote"] = remote_environment
    if monte_carlo_info is not None:
        # Additive/optional: only present when the request declares
        # `monte_carlo` -- the seed contract's request-level echo, per
        # docs/cli/sim.md's "Monte Carlo" section. Per-sample seed values
        # live on each corner (`corners[].monte_carlo`), not here.
        environment["monte_carlo"] = monte_carlo_info

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
    """One expanded (process, supply, temperature) point, pre-run.

    ``sample_index``/``mc_seed`` are populated only when this point is one of
    a Monte Carlo sample sequence (see :func:`_expand_monte_carlo`) --
    ``None``/``None`` for an ordinary PVT corner point. Keeping them on
    :class:`CornerPoint` itself (rather than a parallel structure) is why
    sampling needs no new execution backend: every backend already consumes
    a plain ``list[CornerPoint]`` and calls :func:`_run_corner` per point,
    so a sampled point flows through exactly the same path as an
    unsampled one.

    ``process_sections`` is populated only when the request's
    ``corners.process[]`` entry for this point was the multi-section bundle
    object form (``{"name": str, "sections": list[str]}``, see
    :func:`_parse_process_entry`) rather than a bare string -- ``None`` for
    an ordinary single-section (or process-less) corner. ``process`` always
    holds the corner's display name either way (the bundle's ``name``, or
    the bare string itself), so `corner_id`/`slug` labeling, the response's
    ``process`` field, and `_matches_exclude` need no bundle-awareness of
    their own.
    """

    __slots__ = (
        "process",
        "process_sections",
        "supply_v",
        "temperature_c",
        "sample_index",
        "mc_seed",
    )

    def __init__(
        self,
        process: str | None,
        supply_v: dict[str, float],
        temperature_c: float,
        *,
        process_sections: list[str] | None = None,
        sample_index: int | None = None,
        mc_seed: dict[str, int] | None = None,
    ) -> None:
        self.process = process
        self.process_sections = process_sections
        self.supply_v = supply_v
        self.temperature_c = temperature_c
        self.sample_index = sample_index
        self.mc_seed = mc_seed

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
        if self.sample_index is None:
            return base
        return f"{base}/mc{self.sample_index}"

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


def _parse_process_entry(entry: Any) -> tuple[str | None, list[str] | None]:
    """Normalize one ``corners.process[]`` entry into ``(name, sections)``.

    A bare string is today's single-``.lib``-section corner (e.g. sky130's
    ``"tt"``): returns ``(entry, None)`` -- ``process_sections=None`` signals
    :func:`_write_corner_deck` to emit its historical single ``.lib`` line,
    byte-for-byte unchanged from before this function existed.

    An object ``{"name": str, "sections": list[str]}`` is a multi-section
    corner *bundle* -- e.g. gf180mcu's ``sm141064.ngspice``, which has no
    all-device corner sections and instead needs one ``.lib`` card per
    device family (MOS + ``bjt_*`` + ``diode_*`` + ``res_*`` + ``moscap_*`` +
    ``mimcap_*``) to fully select a named corner: returns ``(name,
    list(sections))``, and :func:`_write_corner_deck` emits one ``.lib`` line
    per section, in declaration order (ordering matters -- the gf180 section
    set has interdependent global switch params).
    """
    if isinstance(entry, str):
        return entry, None
    if isinstance(entry, dict):
        name = entry.get("name")
        sections = entry.get("sections")
        if not isinstance(name, str) or not name:
            raise SimError(
                "corners.process bundle entry requires a non-empty string 'name'"
            )
        if (
            not isinstance(sections, list)
            or not sections
            or not all(isinstance(s, str) and s for s in sections)
        ):
            raise SimError(
                "corners.process bundle entry requires a non-empty list of "
                "non-empty strings 'sections'"
            )
        return name, list(sections)
    raise SimError(
        "corners.process entries must be a string or an object "
        '{"name": str, "sections": list[str]}'
    )


def _expand_corners(
    corners_spec: dict[str, Any], exclude_spec: list[dict[str, Any]]
) -> list[CornerPoint]:
    """Expand the declared axes into the deterministic, odometer-style corner list.

    Axis order (outer to inner, per the spike): process, then the supply
    sweep, then temperature. A missing axis collapses to a single default
    point (``process=None``, ``supply_v={}``, ``temperature_c=27``) rather
    than an error -- a request that only cares about one axis need not
    populate the others.

    Each ``corners.process[]`` entry is either a bare string (one ``.lib``
    section, unchanged) or a ``{"name": str, "sections": list[str]}`` bundle
    object (multiple ``.lib`` sections under one named corner) -- see
    :func:`_parse_process_entry`.

    ``corners.supply_v``'s keys sweep together by index (same-length arrays;
    rails move as a set), matching the spike's documented semantics.
    """
    raw_processes: list[Any] = corners_spec.get("process") or [None]
    processes: list[tuple[str | None, list[str] | None]] = [
        (None, None) if raw is None else _parse_process_entry(raw)
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

    temperatures: list[float] = corners_spec.get("temperature_c") or [27]

    points: list[CornerPoint] = []
    for process, process_sections in processes:
        for supply_v in supply_points:
            for temperature_c in temperatures:
                point = CornerPoint(
                    process,
                    supply_v,
                    temperature_c,
                    process_sections=process_sections,
                )
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
# Monte Carlo sampling
# --------------------------------------------------------------------------- #
#
# Phase 1 of #344's decomposition: the sampling *orchestration* core --
# request schema, seed handling, fan-out mechanics. Statistics rollup
# (mean/sigma/quantiles) and limit-window pass/fail evaluation are phase 2
# (a separate sub-issue) -- this module only produces N reproducibly-seeded
# samples per corner point; it never aggregates them.
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

    return n, seed, vary


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


def _expand_monte_carlo(
    corner_points: list[CornerPoint], mc_spec: dict[str, Any]
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
    every sample's ``mismatch_seed`` -- and therefore anything downstream
    that depends only on it -- is identical across the N samples (sigma=0),
    proving the sampler isn't silently injecting variation nobody asked
    for. Requesting the *same* ``mc_spec["seed"]`` again (same process,
    different process, doesn't matter) reproduces the exact same sequence
    of derived seeds -- see :func:`_derive_mc_seed`.
    """
    n, seed, vary = _validate_monte_carlo_spec(mc_spec)
    vary_process = vary in ("process", "both")
    vary_mismatch = vary in ("mismatch", "both")

    sampled: list[CornerPoint] = []
    for corner_index, base in enumerate(corner_points):
        for sample_index in range(n):
            process_seed = _derive_mc_seed(
                seed,
                "process",
                corner_index,
                sample_index if vary_process else "fixed",
            )
            mismatch_seed = _derive_mc_seed(
                seed,
                "mismatch",
                corner_index,
                sample_index if vary_mismatch else "fixed",
            )
            rndseed = _derive_mc_seed(seed, "rndseed", process_seed, mismatch_seed)
            sampled.append(
                CornerPoint(
                    base.process,
                    base.supply_v,
                    base.temperature_c,
                    process_sections=base.process_sections,
                    sample_index=sample_index,
                    mc_seed={
                        "process_seed": process_seed,
                        "mismatch_seed": mismatch_seed,
                        "rndseed": rndseed,
                    },
                )
            )
    return sampled, {"n": n, "seed": seed, "vary": vary}


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
) -> tuple[list[dict[str, Any]], str | None, dict[str, Any] | None]:
    """The ``local`` backend: run each expanded corner sequentially in-process.

    This is the unit a backend implements -- given the expanded corner list
    plus the already-resolved netlist/model paths and run options, return
    ``(per_corner_reports, engine_version, remote_environment)``. It
    intentionally reproduces the original in-line loop byte-for-byte (same
    ordering, same ``engine_version`` last-writer-wins tracking) so the
    report JSON and on-disk artifacts are unchanged from before the backend
    seam existed. ``max_workers``/``request`` are accepted for signature
    parity with the other backends (every entry in :data:`_BACKENDS` is
    called with the same keyword set) but are meaningless for a sequential
    local runner, so both are ignored here; the third return value is always
    ``None`` (only ``remote`` populates it).
    """
    del max_workers, request  # unused: sequential local run, see docstring
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
    return corners, engine_version, None


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
    request: dict[str, Any],
) -> tuple[list[dict[str, Any]], str | None, dict[str, Any] | None]:
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

    ``request`` is accepted for signature parity with the other backends
    (only ``remote`` reads it) and ignored; the third return value is always
    ``None`` here.
    """
    del request  # unused: local-parallel needs no request-level context
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
    return corners, engine_version, None


#: Default idle-poll interval for :func:`remote_transport.wait_for_ssh` when
#: called from :func:`_run_remote` -- kept short so tests exercising a
#: timeout don't stall, while still reasonable against real EC2 boot times.
_REMOTE_SSH_POLL_INTERVAL_S = 5.0

#: Default overall SSH-readiness budget, per the design note's documented
#: 1-3 minute spin-up estimate plus slack (decision 3) -- generous, not
#: measured; see docs/design/remote-sim-backend-spike.md.
_REMOTE_SSH_READY_TIMEOUT_S = 240.0


def _run_remote(
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
) -> tuple[list[dict[str, Any]], str | None, dict[str, Any] | None]:
    """The ``remote`` backend: provision one EC2 instance sized for the whole
    corner matrix, push the netlist + a request-specific copy of ``request``
    to it over SSH/SCP, and run the *same* ``local-parallel`` worker-pool
    code (unmodified, on the provisioned box) via ``klt sim ... --backend
    local-parallel`` -- not a reimplementation of corner expansion, ordering,
    measurement extraction, or pass/fail classification (see
    ``docs/design/remote-sim-backend-spike.md`` decisions 2 and 5, and
    :mod:`klayout_tools.remote_transport`'s :func:`remote_transport.run_remote_job`).
    The generic push/run/collect job description built here
    (:func:`_build_remote_job_description`) is `klt sim`'s own instance of
    the contract ``docs/design/remote-job-description.md`` documents (issue
    #278, Epic #253 Phase 3).

    ``analysis``/``measurements_spec``/``models_lib`` are accepted for
    signature parity with the other backends but unused directly here --
    they are already embedded in ``request`` (the remote request document is
    built from ``request`` itself, see :func:`_build_remote_request`), and
    ``models_lib`` is a *local* path resolution only used for this run's
    ``environment.models_lib``/``models_lib_sha256`` provenance (computed by
    ``run_sim`` regardless of backend); the remote host resolves its own
    baked model library from ``request.models`` and its own ``$PDK_ROOT``
    (see decision 4).

    ``max_workers`` (the caller's *local* worker-pool override) is ignored --
    the provisioned box was already sized to fit
    ``corner_count * threads_per_corner`` with headroom
    (``remote_launcher.select_instance_type``), so its own
    ``local-parallel`` default (``_default_max_workers``, derived from that
    box's own CPU count) is already right-sized without an override.

    Teardown is guaranteed on every exit path -- normal completion, any
    exception raised in this function's body (including a transport
    failure), or a caught SIGINT/SIGTERM -- by ``RemoteLauncher``'s own
    context-manager guarantee (guardrail mechanics SS3(a)). A provisioning or
    transport failure is re-raised as :class:`SimError`: no corner ever ran,
    the same "the sweep never started" class as an unresolvable netlist or
    model library.
    """
    del analysis, measurements_spec, models_lib, max_workers  # see docstring

    remote_spec = request.get("remote") or {}
    region = remote_spec.get("region")
    pdk = (request.get("models") or {}).get("pdk")
    if not pdk:
        raise SimError(
            "backend 'remote' requires request.models.pdk (selects which "
            "baked-AMI PDK to provision -- see "
            "remote_launcher.SUPPORTED_PDKS and docs/cli/sim.md)"
        )
    ssh_key_path = remote_spec.get("ssh_key_path")
    if not ssh_key_path:
        raise SimError(
            "backend 'remote' requires request.remote.ssh_key_path (local "
            "private key matching request.remote.key_name, used to SSH/SCP "
            "into the provisioned instance)"
        )
    ssh_user = remote_spec.get("ssh_user") or remote_transport.DEFAULT_SSH_USER

    job_id = f"klt-sim-{uuid.uuid4().hex[:12]}"
    launcher = RemoteLauncher(
        region=region,
        pdk=pdk,
        corner_count=len(corner_points),
        job_id=job_id,
        spot=remote_spec.get("spot", True),
        max_hourly_cost_usd=remote_spec.get("max_hourly_cost_usd"),
        launcher_cidr=remote_spec.get("launcher_cidr"),
        security_group_id=remote_spec.get("security_group_id"),
        key_name=remote_spec.get("key_name"),
        subnet_id=remote_spec.get("subnet_id"),
    )

    started = time.monotonic()
    info: dict[str, Any] | None = None
    remote_report: dict[str, Any] | None = None
    try:
        with launcher:
            info = launcher.provision()
            public_ip = launcher.get_public_ip()
            remote_transport.wait_for_ssh(
                public_ip,
                user=ssh_user,
                identity_file=ssh_key_path,
                timeout_s=remote_spec.get(
                    "ssh_ready_timeout_s", _REMOTE_SSH_READY_TIMEOUT_S
                ),
                poll_interval_s=_REMOTE_SSH_POLL_INTERVAL_S,
            )
            spin_up_s = round(time.monotonic() - started, 3)

            remote_job_dir = remote_transport.job_dir(ssh_user, job_id)
            remote_request = _build_remote_request(
                request,
                timeout_s=timeout_s,
                keep_artifacts=keep_artifacts,
                want_waveforms=want_waveforms,
            )
            job = _build_remote_job_description(remote_request, netlist_path)
            remote_transport.push_job(
                host=public_ip,
                user=ssh_user,
                identity_file=ssh_key_path,
                remote_job_dir=remote_job_dir,
                job=job,
            )
            remote_report = remote_transport.run_remote_job(
                host=public_ip,
                user=ssh_user,
                identity_file=ssh_key_path,
                remote_job_dir=remote_job_dir,
                job=job,
                timeout_s=remote_spec.get(
                    "ssh_timeout_s",
                    _default_remote_run_timeout_s(len(corner_points), timeout_s),
                ),
            )
            if keep_artifacts:
                remote_transport.pull_artifacts(
                    host=public_ip,
                    user=ssh_user,
                    identity_file=ssh_key_path,
                    remote_job_dir=remote_job_dir,
                    local_artifacts_dir=artifacts_dir,
                    job=job,
                )
            remote_transport.cleanup_job(
                host=public_ip,
                user=ssh_user,
                identity_file=ssh_key_path,
                remote_job_dir=remote_job_dir,
            )
    except (RemoteLaunchError, remote_transport.RemoteTransportError) as exc:
        raise SimError(f"remote backend failed: {exc}") from exc

    assert info is not None and remote_report is not None  # provision()/run succeeded

    corners = remote_report.get("corners", [])
    if keep_artifacts:
        _rewrite_remote_artifact_paths(
            corners,
            remote_root=remote_transport.artifacts_root(
                remote_job_dir, job.artifacts_relative_dir
            ),
            local_root=artifacts_dir,
        )
    engine_version = (remote_report.get("environment") or {}).get("engine_version")

    remote_environment = {
        "provider": info["provider"],
        "region": info["region"],
        "instance_type": info["instance_type"],
        "instance_id": info["instance_id"],
        "spot": info["spot"],
        "estimated_hourly_cost_usd": info["estimated_hourly_cost_usd"],
        "ami_id": info["ami_id"],
        "pdk_snapshot": info["pdk_snapshot"],
        "spin_up_s": spin_up_s,
    }
    return corners, engine_version, remote_environment


def _build_remote_request(
    request: dict[str, Any],
    *,
    timeout_s: float,
    keep_artifacts: bool,
    want_waveforms: bool,
) -> dict[str, Any]:
    """Build the request document pushed to the remote host: a copy of the
    caller's own request with ``backend`` forced to ``local-parallel`` (per
    decision 2 -- the box runs #255's worker pool directly, never ``remote``
    recursively) and ``netlist`` repointed at the pushed file's job-relative
    path.

    ``models`` is forwarded unchanged -- per decision 4, the remote host
    resolves its own baked model library the same way ``_resolve_models_lib``
    resolves a local one (``models.pdk`` plus the AMI's own ``$PDK_ROOT``, so
    long as ``models.pdk_root`` is not itself an operator-local absolute path
    that only exists on the caller's own machine -- see docs/cli/sim.md's
    "Remote backend" section for this constraint). ``options.max_workers`` is
    dropped so the remote run resolves its own default from that box's own
    CPU count (see ``_run_remote``'s docstring).
    """
    remote_request = dict(request)
    remote_request.pop("remote", None)
    remote_request["backend"] = "local-parallel"
    remote_request["netlist"] = remote_transport.REMOTE_NETLIST_FILENAME

    options = dict(request.get("options") or {})
    options["timeout_s"] = timeout_s
    options["keep_artifacts"] = keep_artifacts
    options["waveforms"] = want_waveforms
    options.pop("max_workers", None)
    remote_request["options"] = options
    return remote_request


#: `klt sim`'s own remote-command exit codes that still mean "the sweep ran
#: and produced a report": 0 (pass)/3 (measurement failure)/4 (corner error)
#: -- see ``docs/cli/sim.md``'s exit-code table and
#: ``remote_transport.run_remote_job``'s docstring.
_REMOTE_SIM_SUCCESS_EXIT_CODES: tuple[int, ...] = (0, 3, 4)


def _build_remote_job_description(
    remote_request: dict[str, Any], netlist_path: str
) -> remote_transport.JobDescription:
    """Build the `klt sim` corner-fan-out job as a generic
    :class:`remote_transport.JobDescription` (issue #278, Epic #253 Phase
    3): the netlist and the generated ``remote_request`` document are the
    pushed inputs, ``klt sim ... --backend local-parallel --format json`` is
    the remote command, and ``.klt/sim`` (``sim.run_sim``'s own
    ``keep_artifacts`` default, since the remote invocation is never given
    an explicit ``--outdir``) is the collected artifacts directory.

    This is the one and only place `klt sim`'s remote job shape is
    constructed -- :mod:`klayout_tools.remote_transport`'s push/run/collect
    functions accept it as data and hard-code none of it, so a future
    `extract`/`lvs`/DRC remote backend builds its own
    :class:`remote_transport.JobDescription` here instead (see
    ``docs/design/remote-job-description.md``).
    """
    return remote_transport.JobDescription(
        label="klt sim",
        inputs=(
            remote_transport.JobInput(
                remote_name=remote_transport.REMOTE_NETLIST_FILENAME,
                label="netlist",
                local_path=netlist_path,
            ),
            remote_transport.JobInput(
                remote_name=remote_transport.REMOTE_REQUEST_FILENAME,
                label="request",
                content=json.dumps(remote_request),
            ),
        ),
        command=(
            f"klt sim {remote_transport.REMOTE_REQUEST_FILENAME} "
            "--backend local-parallel --format json"
        ),
        success_exit_codes=_REMOTE_SIM_SUCCESS_EXIT_CODES,
        artifacts_relative_dir=remote_transport.DEFAULT_ARTIFACTS_RELATIVE_DIR,
    )


def _default_remote_run_timeout_s(
    corner_count: int, per_corner_timeout_s: float
) -> float:
    """Conservative SSH-command timeout for the remote ``klt sim`` invocation:
    the fully-serial worst case (every corner hits its own timeout, one
    after another) plus slack for SSH/``klt`` startup.

    The provisioned box is right-sized to run every corner concurrently
    (``remote_launcher.select_instance_type``), so real runs are expected to
    finish far faster than this bound -- it exists only so a genuinely
    wedged remote run doesn't hang the SSH channel forever. Overridable via
    ``request.remote.ssh_timeout_s``.
    """
    return per_corner_timeout_s * corner_count + 120.0


def _rewrite_remote_artifact_paths(
    corners: list[dict[str, Any]], *, remote_root: str, local_root: str
) -> None:
    """Rewrite each corner's ``artifacts.*`` paths from the remote host's
    filesystem (where the pulled report JSON was generated) to the local
    path :func:`remote_transport.pull_artifacts` just copied them to.

    The response's ``artifacts`` block always describes paths on the machine
    the caller is running on -- ``local``/``local-parallel``/``remote``
    alike -- so a raw remote path would be meaningless (and unreadable) to a
    caller inspecting the returned report.
    """
    for corner in corners:
        artifacts = corner.get("artifacts") or {}
        for key, value in list(artifacts.items()):
            if not value:
                continue
            relative = os.path.relpath(value, remote_root)
            artifacts[key] = os.path.join(local_root, relative)


#: Backend registry: name -> implementation. Membership is validated against
#: :data:`SUPPORTED_BACKENDS` in :func:`run_sim` before dispatch, so this only
#: ever holds implemented backends.
_BACKENDS = {
    "local": _run_local,
    "local-parallel": _run_local_parallel,
    "remote": _run_remote,
}


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

    artifacts: dict[str, str | None] = {
        "log": None,
        "raw": None,
        "waveform": None,
        "deck": None,
    }
    if keep_artifacts:
        if os.path.isfile(log_path):
            artifacts["log"] = log_path
        if raw_path is not None and os.path.isfile(raw_path):
            artifacts["raw"] = raw_path
            artifacts["waveform"] = _parse_and_persist_waveform(raw_path)
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
        "monte_carlo": monte_carlo,
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
    """Generate the corner-specific ngspice deck: an optional Monte Carlo
    seed card, ``.lib``/``.include``/``.temp``, the request's verbatim
    ``.meas`` cards, and a ``.control`` block that ``alter``s the supply
    sources, optionally captures an ASCII rawfile, and runs the declared
    analysis.
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
    if point.process_sections is not None:
        # Multi-section corner bundle (e.g. gf180mcu's per-device-family
        # `.lib` cards) -- one line per declared section, in order (see
        # `_parse_process_entry`).
        for section in point.process_sections:
            lines.append(f".lib {models_lib} {section}")
    elif point.process is not None:
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
