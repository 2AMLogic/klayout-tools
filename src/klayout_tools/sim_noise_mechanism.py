"""Per-mechanism noise measurements for ``klt sim`` (issue #5553).

``measurements[].noise`` (issue #2938, :mod:`klayout_tools.sim_noise`) reduces
a ``noise`` analysis to a *total*: thermal plus flicker plus gate-leakage/shot
noise lumped together. A spec that budgets a thermal share separately could
not be graded against a simulated number at all. An optional ``mechanism`` key
names the share instead::

    {"name": "flicker_rms", "noise": {"quantity": "onoise",
                                      "band_hz": [1, 1e5],
                                      "mechanism": "flicker"}}

This module owns that form: its deck emission, its post-run reduction and its
response entry. The vector namespace it reduces (which vectors are
contributors, which are leaves, what mechanism each leaf belongs to) is
:mod:`klayout_tools.sim_noise_leaves`; the spot/band reductions are
:mod:`klayout_tools.sim_noise`'s, shared with the total form so the two stay
comparable.

Why this form is a post-run reduction, not a lowered ``let``
------------------------------------------------------------
The total form lowers to ngspice ``let`` lines over ``noise1.onoise_spectrum``.
A mechanism total cannot: the per-contributor vector names are hierarchical
flattened instance paths that are unknown until the run, and ngspice's ``expr``
has no vector glob, so the only lowered form would be a request that
hand-enumerates every device instance -- which under-reports silently the
moment a device is missed, the worst failure mode for an evidence record. So
the whole ``noise1`` plot is dumped to an ASCII rawfile next to the corner's
other artifacts and reduced here, in Python, *inside* the corner run. Only the
reduced scalar leaves the corner, so ``remote``/``batch`` workers need no
artifact retention -- the contract :mod:`klayout_tools.sim_cycle_mean` and
:mod:`klayout_tools.sim_twoport` already use.

``pts_per_summary``
-------------------
Only with a trailing ``pts_per_summary`` argument on the analysis line
(``noise v(out) V1 dec 20 10 1e7 1``) does ``noise1`` carry per-contributor
spectra at all; without it the plot holds exactly ``frequency``,
``inoise_spectrum`` and ``onoise_spectrum`` (measured, ngspice-47). A
``mechanism`` measurement without it is therefore refused up front rather than
returning a null value. The argument is deliberately *not* injected: rewriting
the caller's analysis line also multiplies the vector count by the device
count, which is the caller's cost to accept.

Noise amplitudes add in **power**, never linearly; verified against a real
sky130 plot, ``sqrt(sum(leaf**2))`` reproduces ``onoise_spectrum`` to machine
precision at every frequency.

Completeness, asserted
----------------------
A mechanism family the suffix table does not know must not be able to vanish
from the total silently, so every mechanism measurement also reduces *all*
leaves and the plot's own ``onoise_spectrum`` over the same frequency or band.
When the two disagree by more than :data:`COMPLETENESS_TOLERANCE`, or any
unclassified leaf carries power at all, the corner gets a warning diagnostic
(:data:`INCOMPLETE_DIAGNOSTIC_CODE`) naming the unclassified suffixes and
stating the residual. The same numbers ride the response's additive ``noise``
sibling object, so the assertion is machine-checkable and not only readable.
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass
from typing import Any

from . import sim_noise_leaves

#: The ``noise.mechanism`` values.
NOISE_MECHANISMS = sim_noise_leaves.NOISE_MECHANISMS

#: Largest relative disagreement between the power sum of every leaf and the
#: plot's own ``onoise_spectrum`` that still counts as complete. The measured
#: agreement on a real sky130 plot is ~1e-16 (floating-point noise), so this
#: is loose by many orders of magnitude on purpose: it guards against a
#: *structural* miss (a whole generator family absent from the leaf set), not
#: against numerical error.
COMPLETENESS_TOLERANCE = 1e-3

#: Reason tokens a failed ``mechanism`` entry's diagnostic message leads with.
ERROR_CODES = (
    "noise_mechanism_rawfile_missing",
    "noise_mechanism_unreadable",
    "noise_mechanism_no_contributors",
    "noise_mechanism_no_value",
)

#: ``code`` of the warning diagnostic the completeness assertion raises.
INCOMPLETE_DIAGNOSTIC_CODE = "noise_mechanism_incomplete"


def is_mechanism(spec: dict[str, Any]) -> bool:
    """Whether a ``measurements[]`` entry is a ``noise`` entry that names a
    ``mechanism`` (and so is reduced here rather than lowered to ``let``)."""
    body = spec.get("noise")
    return isinstance(body, dict) and body.get("mechanism") is not None


def mechanism_measurements(
    measurements_spec: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """The ``mechanism`` entries, in declaration order."""
    return [spec for spec in measurements_spec if is_mechanism(spec)]


def has_pts_per_summary(analysis: dict[str, Any]) -> bool:
    """Whether the noise analysis' ``args`` carry the trailing
    ``pts_per_summary`` token (``v(out) V1 dec 20 10 1e7 **1**``). False when
    ``args`` cannot be read as a noise sweep -- the caller refuses on the
    swept-range check first."""
    tokens = str(analysis.get("args", "")).split()
    for index, token in enumerate(tokens):
        if token.lower() in ("dec", "oct", "lin") and len(tokens) >= index + 4:
            return len(tokens) > index + 4
    return False


# --------------------------------------------------------------------------- #
# Deck emission
# --------------------------------------------------------------------------- #


def rawfile_path(corner_dir: str) -> str:
    """Where the ``noise1`` (spectra) plot is dumped for this corner. One file
    for every ``mechanism`` measurement -- they all reduce the same plot."""
    return os.path.join(corner_dir, "noise_mechanism.raw")


def control_lines(
    measurements_spec: list[dict[str, Any]], corner_dir: str
) -> list[str]:
    """The ``.control`` lines that dump the per-contributor spectra plot to an
    ASCII rawfile. Empty when no ``mechanism`` entry is declared, so every
    request without one emits a byte-identical deck.

    ngspice's current plot after ``noise`` is the *integrated* one (``noise2``);
    the spectra live in ``noise1`` (issue #2893). The dump therefore has to
    move the current plot, and it puts it back afterwards via ``$curplot`` --
    captured rather than hard-coded, so this group cannot leave the engine in a
    state a later command (the ``options.waveforms`` rawfile, an ``expr``
    ``let``) would read differently. Verified on ngspice-47: ``onoise_total``
    still resolves after the restore.
    """
    if not mechanism_measurements(measurements_spec):
        return []
    return [
        "set __klt_nzm_plot = $curplot",
        "setplot noise1",
        f"write {rawfile_path(corner_dir)}",
        "setplot $__klt_nzm_plot",
    ]


# --------------------------------------------------------------------------- #
# Outcome
# --------------------------------------------------------------------------- #


@dataclass
class NoiseMechanismOutcome:
    """One ``mechanism`` measurement's result, or why there is none."""

    value: float | None = None
    mechanism: str | None = None
    #: The plot's own ``onoise_spectrum`` reduced the same way -- what the
    #: mechanism share is a share *of*.
    total: float | None = None
    #: The unclassified leaves reduced the same way.
    unclassified: float | None = None
    #: ``|all_leaves - total| / total`` of the reduced values.
    residual_relative: float | None = None
    #: Suffixes (or vector names) of unclassified leaves carrying power.
    unclassified_suffixes: tuple[str, ...] = ()
    error: str | None = None
    code: str | None = None

    def report(self) -> dict[str, Any]:
        """The response's additive ``noise`` object for this measurement."""
        return {
            "mechanism": self.mechanism,
            "total": self.total,
            "unclassified": self.unclassified,
            "residual_relative": self.residual_relative,
            "unclassified_suffixes": list(self.unclassified_suffixes),
        }

    @property
    def incomplete(self) -> bool:
        """Whether the completeness assertion failed: the leaf set does not
        reproduce the plot's own total, or an unclassified generator carries
        power."""
        if self.value is None:
            return False
        if self.unclassified_suffixes:
            return True
        return (
            self.residual_relative is not None
            and self.residual_relative > COMPLETENESS_TOLERANCE
        )

    def incomplete_message(self, name: str) -> str:
        """The completeness warning's message."""
        residual = (
            "unknown"
            if self.residual_relative is None
            else f"{self.residual_relative:.3e} relative"
        )
        carried = (
            f"unclassified generator(s) [{', '.join(self.unclassified_suffixes)}] "
            "carry power"
            if self.unclassified_suffixes
            else "no unclassified generator carries power"
        )
        return (
            f"measurement '{name}': the {self.mechanism} noise share is not "
            f"backed by a complete decomposition -- {carried}, and the power "
            "sum of every leaf differs from onoise_spectrum by "
            f"{residual} (tolerance {COMPLETENESS_TOLERANCE:g}). The reported "
            "value counts only the generators klt recognises, so treat it as "
            "a lower bound on that mechanism until they are classified"
        )


# --------------------------------------------------------------------------- #
# Extraction
# --------------------------------------------------------------------------- #


def extract_corner(
    measurements_spec: list[dict[str, Any]], corner_dir: str, analysis: dict[str, Any]
) -> dict[int, NoiseMechanismOutcome]:
    """``{position in measurements_spec: outcome}`` for every ``mechanism``
    entry of one finished corner, read from the rawfile :func:`control_lines`
    asked ngspice to write. Never raises: any rawfile problem becomes each
    measurement's own ``error``."""
    from . import sim_noise

    if not mechanism_measurements(measurements_spec):
        return {}
    sweep = (sim_noise.swept_range(analysis) or ("lin", 0.0, 0.0))[0]
    plot, failure = _read_plot(rawfile_path(corner_dir))
    outcomes: dict[int, NoiseMechanismOutcome] = {}
    for position, spec in enumerate(measurements_spec):
        if not is_mechanism(spec):
            continue
        if failure is not None:
            outcomes[position] = NoiseMechanismOutcome(
                mechanism=spec["noise"]["mechanism"],
                code=failure[0],
                error=f"measurement '{spec['name']}': {failure[1]}",
            )
            continue
        assert plot is not None
        outcomes[position] = _reduce_one(spec, plot, sweep)
    return outcomes


def _read_plot(raw_path: str) -> tuple[dict[str, Any] | None, tuple[str, str] | None]:
    """``(waveform, None)`` or ``(None, (code, message))``."""
    from .sim import SimError, parse_ascii_rawfile

    if not os.path.isfile(raw_path):
        return None, (
            "noise_mechanism_rawfile_missing",
            "the per-contributor noise spectra were not written (no rawfile) "
            "-- check that the noise analysis ran and completed",
        )
    try:
        waveform = parse_ascii_rawfile(raw_path)
    except (SimError, OSError) as exc:
        return None, (
            "noise_mechanism_unreadable",
            f"could not read the per-contributor noise spectra rawfile: {exc}",
        )
    if not waveform["points"]:
        return None, (
            "noise_mechanism_unreadable",
            "the per-contributor noise spectra rawfile holds no samples",
        )
    return waveform, None


def _reduce_one(
    spec: dict[str, Any], plot: dict[str, Any], sweep: str
) -> NoiseMechanismOutcome:
    from . import sim_noise

    body = spec["noise"]
    mechanism = body["mechanism"]
    names = [variable["name"] for variable in plot["variables"]]
    columns = {
        name: [point[index] for point in plot["points"]]
        for index, name in enumerate(names)
    }
    missing = _refuse_plot(spec, columns, names)
    if missing is not None:
        return missing
    leaves = sim_noise_leaves.classify_leaves(names)
    points = len(plot["points"])

    def reduce_leaves(subset: list[sim_noise_leaves.Leaf]) -> float | None:
        return sim_noise.reduce_density(
            body,
            sweep,
            columns["frequency"],
            _power_density(columns, [leaf.name for leaf in subset], points),
        )

    unclassified_leaves = [leaf for leaf in leaves if leaf.mechanism is None]
    value = reduce_leaves([leaf for leaf in leaves if leaf.mechanism == mechanism])
    total = sim_noise.reduce_density(
        body, sweep, columns["frequency"], columns["onoise_spectrum"]
    )
    every = reduce_leaves(leaves)
    if value is None or total is None:
        return NoiseMechanismOutcome(
            mechanism=mechanism,
            code="noise_mechanism_no_value",
            error=(
                f"measurement '{spec['name']}': the noise spectra grid does "
                "not bracket the requested frequency, so no value could be "
                "reduced"
            ),
        )
    return NoiseMechanismOutcome(
        value=value,
        mechanism=mechanism,
        total=total,
        unclassified=reduce_leaves(unclassified_leaves),
        residual_relative=(
            abs(every - total) / total if every is not None and total > 0 else None
        ),
        unclassified_suffixes=_powered_labels(unclassified_leaves, columns),
    )


def _refuse_plot(
    spec: dict[str, Any], columns: dict[str, list[float]], names: list[str]
) -> NoiseMechanismOutcome | None:
    """The rawfile parsed but cannot answer the question -- or None if it can."""
    mechanism = spec["noise"]["mechanism"]
    if "onoise_spectrum" not in columns or "frequency" not in columns:
        return NoiseMechanismOutcome(
            mechanism=mechanism,
            code="noise_mechanism_no_contributors",
            error=(
                f"measurement '{spec['name']}': the rawfile carries no "
                "'frequency'/'onoise_spectrum' vector -- it is not a noise "
                "spectral-density plot"
            ),
        )
    if not sim_noise_leaves.classify_leaves(names):
        return NoiseMechanismOutcome(
            mechanism=mechanism,
            code="noise_mechanism_no_contributors",
            error=(
                f"measurement '{spec['name']}': the noise spectra plot carries "
                "no per-contributor vectors, so no mechanism can be separated "
                "out. ngspice only emits them when the noise analysis line "
                "ends with a pts_per_summary argument (e.g. "
                "`v(out) V1 dec 20 10 1e7 1`)"
            ),
        )
    return None


def _power_density(
    columns: dict[str, list[float]], names: list[str], points: int
) -> list[float]:
    """The amplitude density of ``names`` combined: ``sqrt(sum(a**2))`` per
    frequency point. Noise amplitudes add in power, never linearly."""
    return [
        math.sqrt(sum(columns[name][index] ** 2 for name in names))
        for index in range(points)
    ]


def _powered_labels(
    leaves: list[sim_noise_leaves.Leaf], columns: dict[str, list[float]]
) -> tuple[str, ...]:
    """The distinct labels of ``leaves`` that carry *any* power, sorted.

    Power rather than mere presence: sky130's BSIM4 gate-leakage leaves
    (``igb``/``igd``/``igs``) are emitted for every MOSFET but are identically
    ``0.0`` at ordinary biases, so naming them would make the completeness
    diagnostic fire on every sky130 noise run and stop meaning anything.
    """
    return tuple(
        sorted(
            {
                leaf.label
                for leaf in leaves
                if any(value != 0.0 for value in columns[leaf.name])
            }
        )
    )


# --------------------------------------------------------------------------- #
# Response entry
# --------------------------------------------------------------------------- #


def measurement_result(
    spec: dict[str, Any],
    outcome: NoiseMechanismOutcome | None,
    *,
    save_mode: str,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """One ``mechanism`` measurement's response entry.

    ``value`` is that mechanism's share of the output-referred noise, graded
    through ``sim._grade_measurement_value`` like any scalar; the completeness
    evidence rides the additive ``noise`` sibling.

    A failure is ``status: "error"``, ``value: null`` with a
    ``code: "measurement"`` diagnostic whose message starts with a
    ``[<reason>]`` token from :data:`ERROR_CODES`. A value whose decomposition
    is *incomplete* is still reported -- a sound lower bound beats no number --
    but carries a warning naming what was not classified, so the gap cannot
    pass silently into an evidence record."""
    from . import sim as _sim

    name, unit = spec["name"], spec.get("unit")
    if outcome is None or outcome.value is None:
        message = (
            outcome.error
            if outcome is not None and outcome.error is not None
            else f"measurement '{name}' produced no noise mechanism value"
        )
        reason = (
            outcome.code
            if outcome is not None and outcome.code is not None
            else "noise_mechanism_rawfile_missing"
        )
        if save_mode == "netlist" and reason == "noise_mechanism_rawfile_missing":
            message += _sim.SAVE_MODE_NETLIST_NO_VALUE_HINT
        return {
            "name": name,
            "value": None,
            "unit": unit,
            "status": "error",
            "margin": None,
        }, [
            {
                "severity": "error",
                "code": "measurement",
                "message": f"[{reason}] {message}",
            }
        ]
    status, margin, diagnostics = _sim._grade_measurement_value(spec, outcome.value)
    if outcome.incomplete:
        diagnostics = [
            *diagnostics,
            {
                "severity": "warning",
                "code": INCOMPLETE_DIAGNOSTIC_CODE,
                "message": outcome.incomplete_message(name),
            },
        ]
    return {
        "name": name,
        "value": outcome.value,
        "unit": unit,
        "status": status,
        "margin": margin,
        "noise": outcome.report(),
    }, diagnostics
