"""Top-N noise contributors for ``measurements[].noise`` (issue #5558).

A ``noise`` measurement (issue #2938) says *how much* noise a block has, and
its ``mechanism`` form (issue #5553) says how much of it is thermal or
flicker. Neither says *which device*, and a sizing decision needs the name:
"this block is flicker-limited" has to become "this device's 1/f generator
dominates". An optional ``top_n`` returns the largest contributors by name::

    {"name": "vn_band", "noise": {"quantity": "onoise", "band_hz": [1, 1e5],
                                  "top_n": 3}}

Where the list lives
--------------------
Not in ``value``, which stays exactly what the entry would return without
``top_n`` (so limits, Monte Carlo statistics and ``klt yield`` are unaffected),
and not in a per-corner artifact. It rides ``noise.contributors``, the one
additive member ``crossings`` and ``cycle_mean`` already established the shape
for: the structured part is a sibling on the measurement entry, absent unless
asked for, with no ``schema_version`` bump.

What is ranked
--------------
*Leaf* generators only (:func:`klayout_tools.sim_noise_leaves.classify_leaves`):
a device-total vector would hide which of its generators dominates. Each leaf
is reduced with the entry's own ``at_hz``/``band_hz`` reduction
(:func:`klayout_tools.sim_noise.reduce_density`), so a contributor is directly
comparable with ``value`` and the amplitudes add in power. A leaf whose
reduced value is exactly ``0.0`` is omitted: sky130's gate-leakage leaves
(``igb``/``igd``/``igs``) are emitted for every MOSFET and are identically
zero at ordinary biases, and listing them would only pad the list.

The reduction runs *inside* the corner run (the call site of
:func:`klayout_tools.sim_noise_mechanism.extract_corner`), so on a ``batch``
backend only the reduced list travels in the report and
``options.keep_artifacts: false`` still works. This module is a leaf: it holds
the pure pieces; the rawfile read and the outcome type stay in
:mod:`klayout_tools.sim_noise_mechanism`, whose per-contributor spectra dump it
shares.
"""

from __future__ import annotations

from typing import Any

from . import sim_noise_leaves
from .sim_errors import SimError


def requested(spec: dict[str, Any]) -> bool:
    """Whether a ``measurements[]`` entry is a ``noise`` entry asking for its
    ``top_n`` contributors."""
    body = spec.get("noise")
    return isinstance(body, dict) and body.get("top_n") is not None


def validate_top_n(name: Any, body: dict[str, Any]) -> None:
    """Validate the optional ``noise.top_n`` key (called from
    ``sim_noise.validate_noise_spec`` once ``quantity`` is known to be valid).

    A positive integer -- not a bool, not a float. And, like ``mechanism``,
    ``onoise`` only: ngspice emits per-contributor spectra for the
    output-referred quantity alone, so an ``inoise`` ranking cannot be built
    (refused by name rather than silently returning no list).
    """
    if "top_n" not in body:
        return
    top_n = body["top_n"]
    if isinstance(top_n, bool) or not isinstance(top_n, int) or top_n < 1:
        raise SimError(
            f"measurement '{name}': noise.top_n must be a positive integer "
            f"(got {top_n!r})"
        )
    if body.get("quantity") != "onoise":
        raise SimError(
            f"measurement '{name}': noise.top_n needs "
            'quantity "onoise" -- ngspice emits no per-contributor '
            "input-referred spectra, so the largest inoise contributors cannot "
            "be ranked. Rank them output-referred, or use the total form for "
            "inoise"
        )


def rank(
    body: dict[str, Any],
    sweep: str,
    frequency: list[float],
    columns: list[list[float]],
    leaves: list[sim_noise_leaves.Leaf],
) -> list[dict[str, Any]] | None:
    """The ``noise.contributors`` list of one entry, or None when there is
    nothing to report: no ``top_n`` was asked for, or the swept grid does not
    bracket the requested frequency (so no leaf reduces to a value).

    ``columns`` is keyed by rawfile column **index** (``leaf.index``), never
    by name: distinct columns may share a vector name and each is a separate
    contributor (#6978). The ``top_n`` largest leaves by reduced ``value``,
    descending, ties broken by ``name`` ascending so the order never depends
    on rawfile order. Fewer leaves than ``top_n`` returns them all. With a
    ``mechanism`` on the entry only that mechanism's leaves compete; without
    one every leaf does, an unclassified one reporting ``mechanism: None``.

    ``name`` is the leaf's vector name minus its ``onoise`` prefix and the one
    separator after it -- never parsed further, because the separator ngspice
    picks varies with the device name (see :mod:`sim_noise_leaves`).
    """
    top_n = body.get("top_n")
    if top_n is None:
        return None
    from . import sim_noise

    mechanism = body.get("mechanism")
    ranked: list[dict[str, Any]] = []
    for leaf in leaves:
        if mechanism is not None and leaf.mechanism != mechanism:
            continue
        value = sim_noise.reduce_density(body, sweep, frequency, columns[leaf.index])
        if value is None:
            return None
        if value != 0.0:
            ranked.append(
                {
                    "name": leaf.name[len(sim_noise_leaves.PREFIX) + 1 :],
                    "value": value,
                    "mechanism": leaf.mechanism,
                }
            )
    ranked.sort(key=lambda entry: (-entry["value"], entry["name"]))
    return ranked[:top_n]


def attach(
    measurements_spec: list[dict[str, Any]],
    measurement_results: list[dict[str, Any]],
    outcomes: dict[int, Any] | None,
    diagnostics: list[dict[str, Any]],
    save_mode: str,
) -> None:
    """Add ``noise.contributors`` to every *total* entry (``top_n`` without
    ``mechanism``) in ``measurement_results``, keyed by position.

    A total entry's ``value`` comes from the lowered ``let`` path through the
    generic branch of ``_corner_measurement_results``, which is what keeps it
    identical to the same entry without ``top_n``; the list is attached
    afterwards and never touches ``value``/``status``/``margin``. (A
    ``mechanism`` entry is a sidecar form and carries its list through
    :meth:`sim_noise_mechanism.NoiseMechanismOutcome.report` instead.)

    A list that could not be computed -- the rawfile is missing or unreadable,
    or holds no contributors -- is a ``warning`` diagnostic leading with the
    outcome's ``[<reason>]`` token, never ``status: "error"``: the total was
    fine. An entry that already has no value carries its own error and gets
    no second one. Results gain a ``noise`` member only when the list exists.
    """
    from . import sim as _sim

    for position, spec in enumerate(measurements_spec):
        if not requested(spec) or spec["noise"].get("mechanism") is not None:
            continue
        result = measurement_results[position]
        if result.get("value") is None:
            continue
        outcome = (outcomes or {}).get(position)
        if outcome is not None and outcome.contributors is not None:
            result["noise"] = {"contributors": outcome.contributors}
            continue
        name = spec["name"]
        reason = (
            outcome.code
            if outcome is not None and outcome.code is not None
            else "noise_mechanism_rawfile_missing"
        )
        message = (
            outcome.error
            if outcome is not None and outcome.error is not None
            else f"measurement '{name}': no per-contributor noise spectra were read"
        )
        if save_mode == "netlist" and reason == "noise_mechanism_rawfile_missing":
            message += _sim.SAVE_MODE_NETLIST_NO_VALUE_HINT
        diagnostics.append(
            {
                "severity": "warning",
                "code": "measurement",
                "message": (
                    f"[{reason}] {message}; the value is unaffected, "
                    "noise.contributors is omitted"
                ),
            }
        )
