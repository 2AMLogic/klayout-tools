"""Design centering: turn a ``klt yield-sensitivity`` parameter ranking into
re-centering candidates against a ``klt size`` sized device's own geometry
(``klt design-centering``, issue #924, Phase 3 of the statistical/yield epic
#710).

Closes the loop epic #710's "Why" section describes: #923/``klt
yield-sensitivity`` answers *which* device/process mismatch terms drive a
Monte Carlo campaign's output spread; this module answers the next question
-- *what does that mean for a sized design* -- by mapping each ranked
mismatch parameter onto the sized device instance it belongs to and
proposing which instances are worth re-centering (growing) first.

Producer-side reference contract, not a wired #705 sizing loop
-----------------------------------------------------------------
Epic #705 (the analog-sizing engine) is still Phase 1 as of this issue and
has no design-centering stage to wire into (verified against
``src/klayout_tools/size.py`` while building this module: ``klt size``
reports a geometric sizing result, no re-centering/iteration loop). This
module therefore ships the **contract** #923's own docs already reserve for
it (``docs/cli/yield-sensitivity.md``'s "Downstream consumers" section:
``ranking[].parameter``/``ranking[].contribution``, read verbatim, no new
field added to that contract) plus a **reference consumer**
(:func:`build_recentering_proposal`, and the ``klt design-centering`` CLI
verb) that exercises it end-to-end. If #705 grows a real design-centering
stage later, that stage should wire into ``ranking[].parameter``/
``ranking[].contribution`` directly -- this module's proposal shape is a
worked example of *one* way to consume it, not the only one.

The mismatch-parameter vs. sizing-geometry key mismatch
----------------------------------------------------------
``klt yield-sensitivity``'s ranking is keyed by device/process *mismatch*
parameter names (e.g. ``vth_mismatch_m1``, ``beta_mismatch_m2`` -- whatever
names the sensitivity sample document's author chose). ``klt size``'s sized
device output is keyed by *geometry* -- either a single top-level
``operating_point`` (single-device mode) or a ``devices`` map keyed by
*instance* name (e.g. ``input_a``, ``mirror_b``, ``tail`` -- coupled
multi-device topology mode, see ``size.py``'s ``TOPOLOGY_INSTANCES``). There
is no naming convention shared between the two: a sensitivity sample
document's parameter names come from whatever harness generated the
campaign, and a topology's instance names come from ``klt size``'s own
topology definition. This module does not guess a mapping -- the caller
supplies one explicitly via ``request.parameter_map`` (``{mismatch_parameter:
instance_name}``), the bridge the issue's own curation note called out as
necessary. A ranked parameter absent from the map is not an error (not every
mismatch/process term corresponds to a device this sizing request touched --
e.g. a pure-process term with no per-instance representation); it is
reported separately under ``unmapped_parameters`` rather than silently
dropped.

Re-centering heuristic (Pelgrom's law, a stated simplification)
--------------------------------------------------------------------
For each mapped candidate, this module suggests an **area multiplier**: the
factor by which that instance's area (``W * L * NF * MULT``) would need to
grow to bring its mismatch term's contribution down to parity with the next
-highest-ranked *mapped* contributor. This follows the standard Pelgrom
mismatch model (Pelgrom, Duinmaijer & Welbers, 1989): a device mismatch
term's standard deviation scales as ``sigma ~ 1 / sqrt(area)``, so (holding
the ranking's own linear-regression coefficient fixed) its *contribution* to
the output scales the same way, and its squared contribution scales as
``1 / area``. Reducing a contribution by a factor ``k`` therefore needs
``area * k**2``. This is a **first-pass, order-of-magnitude heuristic** for
a reference consumer, not a rigorous re-optimization: it holds the
regression coefficient fixed (a real re-sized device's coefficient would
itself shift), ignores every other coupled effect a real re-centering pass
would need to re-verify (current density, gm/Id, the topology's own
coupling -- see ``size.py``'s own coupled-topology docstring), and is
explicitly the kind of unchecked-precision claim epic #710 already refuses
to ship for its own yield numbers -- callers should treat the multiplier as
"this is the candidate worth re-exploring, and roughly how much headroom it
needs", not a literal edit to apply unverified.

Pure library: :func:`build_recentering_proposal` returns plain Python data
(a JSON-serialisable ``dict``) and never prints -- serialisation and
human-readable formatting live in ``cli/design_centering_cmd.py``, matching
every other ``klt`` verb.

A real consumer, at last (issue #1326, epic #705 Phase 2)
------------------------------------------------------------
The "producer-side reference contract" section above is now half true:
``klt size`` gained a design-centering/yield-aware objective mode
(``request.design_centering``, additive to the worst-corner-margin
objective #769 shipped) that consumes exactly this module's
``ranking[].parameter``/``ranking[].contribution`` bridge -- not by calling
:func:`build_recentering_proposal` itself (that function needs an
*already-produced* ``klt size`` payload to resolve geometry from, which does
not exist yet when a sizing run is starting), but by importing this
module's two pure, sized-device-agnostic helpers,
:func:`rank_mapped_parameters` and :func:`suggested_area_multiplier`, and
applying them against the geometry its own run just solved. See
``klayout_tools/size.py``'s ``_apply_design_centering`` docstring for the
grow-then-re-verify method. This module's own CLI verb and
:func:`build_recentering_proposal` are unchanged and remain useful on their
own (e.g. against a sizing result produced by an older ``klt size`` that
predates this wiring, or by another tool entirely).
"""

from __future__ import annotations

from typing import Any

from ._paths import _load_request_json, validate_request_shape

#: Independently versioned from every other ``klt`` command's own
#: ``schema_version``, per docs/json-contract.md's "per command, not
#: globally" convention.
SCHEMA_VERSION = 1

#: Geometry fields read off a `klt size` operating point (single-device
#: mode's top-level `operating_point`, or one topology instance's own
#: `operating_point`) -- the four factors Pelgrom's law's `area` term is the
#: product of.
_GEOMETRY_FIELDS = ("w_um", "l_um", "nf", "mult")


class DesignCenteringError(Exception):
    """Raised when a design-centering request cannot be built: a missing/
    malformed request file, a sensitivity report with no usable ranking, a
    ``parameter_map`` entry naming a sized-device instance that does not
    exist, or an ambiguous measurement selection.

    The CLI turns this into a clean stderr message + exit code 1, never a
    traceback -- see ``docs/json-contract.md``.
    """


def load_request(request_path: str) -> dict[str, Any]:
    """Read and minimally validate a ``klt design-centering`` request JSON
    file: ``sensitivity`` (a ``klt yield-sensitivity`` JSON payload, or a
    bare ``measurement`` object out of one), ``sized_device`` (a ``klt
    size`` JSON payload, single-device or topology mode), and
    ``parameter_map`` (``{mismatch_parameter: instance_name}``).
    """
    request = _load_request_json(request_path, DesignCenteringError)
    return validate_request_shape(
        request,
        "request file",
        error_cls=DesignCenteringError,
        required_fields=("sensitivity", "sized_device", "parameter_map"),
    )


def _select_measurement(
    sensitivity: Any, measurement_name: str | None
) -> dict[str, Any]:
    """Resolve ``sensitivity`` (a full ``klt yield-sensitivity`` payload, or
    a bare ``measurement`` object already carrying its own ``ranking``) to
    the one measurement this proposal is built from.

    Mirrors ``yield_sensitivity.py``'s own selection rule: an explicitly
    named measurement absent from the document is an error, not a silent
    skip. When the document holds more than one measurement and none was
    named, the ambiguity itself is the error -- this module never guesses
    which measurement a caller meant.
    """
    if not isinstance(sensitivity, dict):
        raise DesignCenteringError("request.sensitivity must be a JSON object")

    if "ranking" in sensitivity:
        # A bare measurement object, not a full `measurements` envelope.
        name = sensitivity.get("name")
        if (
            measurement_name is not None
            and name is not None
            and name != measurement_name
        ):
            raise DesignCenteringError(
                f"request.measurement is '{measurement_name}' but "
                f"request.sensitivity's own name is '{name}'"
            )
        return sensitivity

    measurements = sensitivity.get("measurements")
    if not isinstance(measurements, list) or not measurements:
        raise DesignCenteringError(
            "request.sensitivity has no non-empty 'measurements' array and "
            "no 'ranking' of its own -- pass a klt yield-sensitivity JSON "
            "payload, or one of its 'measurements' entries directly"
        )
    by_name = {m.get("name"): m for m in measurements if isinstance(m, dict)}

    if measurement_name is not None:
        if measurement_name not in by_name:
            raise DesignCenteringError(
                f"no such measurement in request.sensitivity: "
                f"{measurement_name} (available: "
                f"{', '.join(sorted(n for n in by_name if n)) or 'none'})"
            )
        return by_name[measurement_name]

    if len(measurements) > 1:
        raise DesignCenteringError(
            "request.sensitivity has more than one measurement -- set "
            "request.measurement (or pass --measurement) to select one "
            f"(available: {', '.join(sorted(n for n in by_name if n)) or 'none'})"
        )
    return measurements[0]


def _geometry_for_instance(
    sized_device: dict[str, Any], instance: str
) -> dict[str, float | None] | None:
    """Resolve one ``instance`` name to its geometry (``w_um``/``l_um``/
    ``nf``/``mult``, plus a derived ``area_um2``) from a ``klt size`` JSON
    payload.

    Topology mode (``sized_device['devices']`` present): ``instance`` must
    name one of that topology's own instance keys (e.g. ``input_a``,
    ``mirror_b``) -- an unknown instance is a caller error (the
    ``parameter_map`` names something that does not exist in this sized
    device), raised rather than silently skipped. Returns ``None`` (no
    geometry, not an error) when that instance's own ``operating_point`` is
    ``None`` -- an evaluator error at that instance left nothing to measure
    (see ``size.py``'s per-instance ``status``); the candidate is still
    built, just without a numeric area multiplier.

    Single-device mode (no ``devices`` map): there is exactly one device, so
    every mapped parameter resolves to its top-level ``operating_point``
    regardless of the instance name given -- the name is carried through as
    a display label only, per this module's docstring.
    """
    devices = sized_device.get("devices")
    if isinstance(devices, dict):
        if instance not in devices:
            raise DesignCenteringError(
                f"parameter_map names sized-device instance '{instance}', "
                f"which is not one of request.sized_device's own instances "
                f"(available: {', '.join(sorted(devices)) or 'none'})"
            )
        op = devices[instance].get("operating_point")
    else:
        op = sized_device.get("operating_point")

    if not isinstance(op, dict):
        return None

    geometry: dict[str, float | None] = {}
    for field in _GEOMETRY_FIELDS:
        value = op.get(field)
        geometry[field] = float(value) if isinstance(value, (int, float)) else None

    if all(geometry[f] is not None for f in _GEOMETRY_FIELDS):
        geometry["area_um2"] = (
            geometry["w_um"] * geometry["l_um"] * geometry["nf"] * geometry["mult"]
        )
    else:
        geometry["area_um2"] = None
    return geometry


def _recommendation(
    *,
    parameter: str,
    instance: str,
    geometry: dict[str, float | None] | None,
    area_multiplier: float | None,
    is_weakest_mapped: bool,
) -> str:
    if is_weakest_mapped:
        return (
            f"'{parameter}' (-> instance '{instance}') is already the "
            "smallest-contributing mapped parameter in this ranking -- no "
            "re-centering action suggested."
        )
    if area_multiplier is None:
        return (
            f"'{parameter}' (-> instance '{instance}') outranks a "
            "mapped parameter with ~zero contribution, so no finite area "
            "multiplier is defined -- re-centering this instance would "
            "still be the first candidate to explore."
        )
    if geometry is None or geometry.get("area_um2") is None:
        return (
            f"'{parameter}' (-> instance '{instance}') is a re-centering "
            f"candidate (~{area_multiplier:.3g}x area headroom by Pelgrom "
            "scaling to reach parity with the next-ranked mapped "
            "parameter), but this instance has no usable operating-point "
            "geometry to compute an absolute target from -- see "
            "request.sized_device's own status for this instance."
        )
    area = geometry["area_um2"]
    return (
        f"'{parameter}' (-> instance '{instance}', current area "
        f"~{area:.4g}um^2 = W{geometry['w_um']:g}*L{geometry['l_um']:g}*"
        f"NF{geometry['nf']:g}*MULT{geometry['mult']:g}) is a re-centering "
        f"candidate: by Pelgrom's law (sigma ~ 1/sqrt(area)), growing this "
        f"instance's area by roughly {area_multiplier:.3g}x would bring "
        "this parameter's contribution down to parity with the next-ranked "
        "mapped parameter. A first-pass heuristic (see this module's "
        "docstring) -- re-verify with a fresh sizing/campaign pass, not an "
        "automatic edit."
    )


def rank_mapped_parameters(
    ranking: list[dict[str, Any]], parameter_map: dict[str, str]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Split a ``klt yield-sensitivity``-shaped ``ranking`` array
    (``ranking[].parameter``/``ranking[].contribution``, already sorted
    descending by |contribution| -- the producer's own contract) into
    ``(mapped, unmapped)`` against ``parameter_map``
    (``{mismatch_parameter: instance_name}``), preserving the ranking's own
    order in both lists.

    This is the *sized-device-agnostic* half of this module's contract --
    the geometry lookup and area-multiplier math a caller that already has
    a ``klt size`` payload handy layers on top (this module's own
    :func:`build_recentering_proposal`, or ``klt size``'s own
    design-centering objective mode, issue #1326 -- see this module's
    docstring's "A real consumer, at last" section) are kept out of this
    function so a caller without one (or one mid-solve, still producing
    its own) can reuse this bridge directly.

    Each ``mapped`` entry is ``{"rank", "parameter", "contribution",
    "instance"}``; each ``unmapped`` entry is ``{"rank", "parameter",
    "contribution"}`` (no ``instance`` -- nothing in ``parameter_map``
    named it). Raises :class:`DesignCenteringError` on a malformed ranking
    entry (missing/mistyped ``parameter``/``contribution``).
    """
    mapped: list[dict[str, Any]] = []
    unmapped: list[dict[str, Any]] = []
    for entry in ranking:
        parameter = entry.get("parameter")
        contribution = entry.get("contribution")
        if not isinstance(parameter, str) or not isinstance(contribution, (int, float)):
            raise DesignCenteringError(
                "the 'ranking' array has a malformed entry (missing "
                "'parameter'/'contribution')"
            )
        if parameter not in parameter_map:
            unmapped.append(
                {
                    "rank": entry.get("rank"),
                    "parameter": parameter,
                    "contribution": contribution,
                }
            )
            continue
        mapped.append(
            {
                "rank": entry.get("rank"),
                "parameter": parameter,
                "contribution": contribution,
                "instance": parameter_map[parameter],
            }
        )
    return mapped, unmapped


def suggested_area_multiplier(mapped: list[dict[str, Any]], index: int) -> float | None:
    """Pelgrom-law area multiplier for ``mapped[index]`` against the
    next-lower-ranked mapped contributor ``mapped[index + 1]`` -- see this
    module's docstring's "Re-centering heuristic" section for the
    derivation.

    Returns ``1.0`` when ``mapped[index]`` is already the weakest mapped
    candidate (nothing lower-ranked to reach parity with -- no re-centering
    action needed), or ``None`` when the next contributor's own
    contribution is exactly zero (no finite multiplier reaches parity with
    a zero target).
    """
    if index == len(mapped) - 1:
        return 1.0
    this_c = abs(mapped[index]["contribution"])
    next_c = abs(mapped[index + 1]["contribution"])
    return (this_c / next_c) ** 2 if next_c > 0 else None


def build_recentering_proposal(
    request: dict[str, Any], *, measurement: str | None = None
) -> dict[str, Any]:
    """Build a design-centering proposal from a parsed request (see
    :func:`load_request`).

    ``measurement`` overrides ``request.get("measurement")`` (the CLI's
    ``--measurement`` flag path) -- same override precedence every other
    ``klt`` verb uses for a request field a flag can also set.

    Returns this command's JSON payload (see ``docs/cli/design-centering.md``);
    raises :class:`DesignCenteringError` for anything that makes the
    proposal unbuildable.
    """
    sensitivity = request.get("sensitivity")
    sized_device = request.get("sized_device")
    parameter_map = request.get("parameter_map")

    if not isinstance(sized_device, dict):
        raise DesignCenteringError("request.sized_device must be a JSON object")
    if not isinstance(parameter_map, dict) or not parameter_map:
        raise DesignCenteringError(
            "request.parameter_map must be a non-empty JSON object mapping "
            "sensitivity parameter names to sized-device instance names"
        )
    for key, value in parameter_map.items():
        if not isinstance(value, str) or not value:
            raise DesignCenteringError(
                f"request.parameter_map['{key}'] must be a non-empty instance "
                "name string"
            )

    measurement_name = (
        measurement if measurement is not None else request.get("measurement")
    )
    if measurement_name is not None and not isinstance(measurement_name, str):
        raise DesignCenteringError("request.measurement must be a string")

    selected = _select_measurement(sensitivity, measurement_name)
    ranking = selected.get("ranking")
    if not isinstance(ranking, list) or not ranking:
        raise DesignCenteringError(
            "the selected measurement has no non-empty 'ranking' array -- "
            "pass the JSON payload klt yield-sensitivity produces"
        )

    mapped, unmapped = rank_mapped_parameters(ranking, parameter_map)
    for item in mapped:
        item["geometry"] = _geometry_for_instance(sized_device, item["instance"])

    # `ranking` is already sorted descending by |contribution| (the
    # producer's own contract) -- filtering to mapped entries preserves that
    # order, so `mapped[i+1]` is always the next-lower mapped contributor
    # (see `rank_mapped_parameters`).
    candidates: list[dict[str, Any]] = []
    for i, item in enumerate(mapped):
        is_weakest = i == len(mapped) - 1
        area_multiplier = suggested_area_multiplier(mapped, i)

        candidates.append(
            {
                "rank": item["rank"],
                "parameter": item["parameter"],
                "contribution": item["contribution"],
                "instance": item["instance"],
                "geometry": item["geometry"],
                "suggested_area_multiplier": area_multiplier,
                "recommendation": _recommendation(
                    parameter=item["parameter"],
                    instance=item["instance"],
                    geometry=item["geometry"],
                    area_multiplier=area_multiplier,
                    is_weakest_mapped=is_weakest,
                ),
            }
        )

    warnings: list[str] = []
    if unmapped:
        names = ", ".join(u["parameter"] for u in unmapped)
        warnings.append(
            f"{len(unmapped)} ranked parameter(s) have no entry in "
            f"request.parameter_map and were not converted into "
            f"re-centering candidates: {names}"
        )

    return {
        "schema_version": SCHEMA_VERSION,
        "measurement": selected.get("name"),
        "unit": selected.get("unit"),
        "candidate_count": len(candidates),
        "candidates": candidates,
        "unmapped_parameters": unmapped,
        "warnings": warnings,
    }


def run_design_centering(
    request_path: str, *, measurement: str | None = None
) -> dict[str, Any]:
    """Load a ``klt design-centering`` request file and build its proposal.

    Thin composition of :func:`load_request` + :func:`build_recentering_proposal`
    -- split so both a request *file* (this function, the CLI's own path)
    and an in-process caller already holding a parsed request dict (tests,
    a future #705 integration) can reuse the same core logic.
    """
    request = load_request(request_path)
    return build_recentering_proposal(request, measurement=measurement)
