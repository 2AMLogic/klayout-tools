"""Declared metric namespace + registry (issue #247).

`klt` verbs each report their own numeric fields under ad-hoc names
(`violation_count`, `device_count`, `instance_count`, ...) with no declared
semantics: nothing states how a metric rolls up across blocks (sum? max?
mean?), whether a larger value is better or worse, or which metrics are
critical enough to gate signoff. That makes any cross-block rollup or
better/worse comparison hard-code that knowledge ad hoc, one call site at a
time.

This module is a **data-only registry** — name -> `MetricDef` — modeled on
LibreLane/OpenROAD's METRICS2.1 convention (hierarchical, double-underscore
names such as `design__core__area`, `timing__setup__ws`,
`drc__error__count`), reusing an ecosystem name wherever a concept matches so
klayout-tools output stays interoperable with OpenROAD/LibreLane tooling for
free. See `docs/design/metric-namespace.md` for the full design rationale
(additive-vs-rename decision, naming grammar, registry shape) and
`docs/json-contract.md` for how a verb's JSON payload carries this registry's
names.

**Scope of this module**: the registry and a small `aggregate()` dispatcher.
It does not itself decide *which* verb fields are declared metrics, or wire
a `metrics` block into any verb's JSON output — that is each verb's own
integration (see `layout_metrics.py` for the first, pilot integration).

## Naming grammar

A declared metric name is a sequence of two or more `__`-separated segments,
e.g. `design__instance__count`, `drc__error__count`,
`sim__corner__passed_count`. The convention (matching METRICS2.1): the first
segment names the owning domain/verb family (`design`, `drc`, `extract`,
`sim`, `timing`, `route`, ...), the last segment is usually a value-kind
suffix (`count`, `area`, `ws`, ...), and any segments in between narrow the
concept. Each segment is itself lowercase-with-underscores
(`[a-z0-9]+(_[a-z0-9]+)*`, e.g. `passed_count`) — a single underscore may
appear *within* a segment, but `__` (double underscore) is reserved as the
segment separator, so a name can never contain three or more consecutive
underscores. This module does not enforce the grammar beyond the
double-underscore convention checked by :func:`register` — see the design
doc for the full rationale on why enforcement stays loose in this pass.

## `aggregator`

How independent per-block values of this metric combine into a parent
(chip-level) rollup:

- `"sum"` — total across blocks (e.g. total instance count).
- `"max"` — worst/largest across blocks (e.g. a per-corner timing metric
  where the block-level critical path bounds the chip-level one).
- `"min"` — best/smallest across blocks.
- `"mean"` — arithmetic mean across blocks.

## `higher_is_better`

`True` when a larger value is a better outcome, `False` when a smaller value
is better, and `None` when the metric has no declared quality polarity at
all — a purely descriptive/structural count (e.g. `design__instance__count`)
that is not, by itself, a "better or worse" axis. `None` is a real,
supported state, not a placeholder for "not yet decided": forcing a polarity
onto every metric would fabricate semantics the metric itself does not
carry.

## `critical`

`True` marks a metric whose failure should gate signoff mechanically (e.g.
`drc__error__count`, where any nonzero value is a signoff blocker). Defaults
to `False`.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

__all__ = [
    "Aggregator",
    "MetricDef",
    "MetricNamespaceError",
    "REGISTRY",
    "register",
    "get_metric",
    "is_registered",
    "all_metrics",
    "aggregate",
]

#: The closed set of declared aggregator kinds. Deliberately data (a string
#: literal), not a Python callable, so the registry itself stays
#: JSON-serializable and exposable through docs/json-contract.md rather than
#: being importable-code-only.
Aggregator = Literal["sum", "min", "max", "mean"]

#: A segment is lowercase-with-underscores (e.g. `passed_count`); the full
#: name is two or more segments joined by `__`, which stays reserved as the
#: separator (a segment itself may contain single underscores, but never a
#: `__` pair -- see the module docstring's "Naming grammar" section, issue
#: #1849's `sim__corner__passed_count`-style names being the motivating
#: case for allowing an underscore within a segment).
_SEGMENT_RE = r"[a-z0-9]+(?:_[a-z0-9]+)*"
_NAME_RE = re.compile(rf"^{_SEGMENT_RE}(?:__{_SEGMENT_RE})+$")


class MetricNamespaceError(ValueError):
    """Raised for a malformed registration or an unknown metric lookup."""


@dataclass(frozen=True)
class MetricDef:
    """One declared metric's aggregation and polarity semantics.

    Attributes:
        name: The declared, METRICS2.1-style hierarchical name (e.g.
            `"design__instance__count"`).
        aggregator: How per-block values of this metric combine into a
            parent rollup. See the module docstring's "`aggregator`"
            section.
        higher_is_better: `True`/`False` when the metric has a declared
            quality polarity, `None` when it is purely descriptive/
            structural (see module docstring's "`higher_is_better`"
            section).
        critical: Whether this metric should mechanically gate signoff.
            Defaults to `False`.
        description: A short, human-readable description of the metric.
    """

    name: str
    aggregator: Aggregator
    higher_is_better: bool | None
    critical: bool = False
    description: str = ""


#: The module-level registry. Populated only by :func:`register` (never
#: mutated directly by callers), so every entry is guaranteed to have passed
#: the same validation.
REGISTRY: dict[str, MetricDef] = {}

_VALID_AGGREGATORS = ("sum", "min", "max", "mean")


def register(
    name: str,
    *,
    aggregator: Aggregator,
    higher_is_better: bool | None,
    critical: bool = False,
    description: str = "",
) -> MetricDef:
    """Declare a metric in the module-level :data:`REGISTRY`.

    Raises:
        MetricNamespaceError: `name` does not match the `a__b__c`-style
            naming grammar, `aggregator` is not one of the declared
            aggregator kinds, or `name` is already registered (a
            registration collision is a bug in the caller, never silently
            overwritten).
    """
    if not _NAME_RE.match(name):
        raise MetricNamespaceError(
            f"metric name {name!r} must be lowercase, double-underscore-"
            "separated segments (e.g. 'design__instance__count')"
        )
    if aggregator not in _VALID_AGGREGATORS:
        raise MetricNamespaceError(
            f"metric {name!r}: unknown aggregator {aggregator!r} "
            f"(expected one of {_VALID_AGGREGATORS})"
        )
    if name in REGISTRY:
        raise MetricNamespaceError(f"metric {name!r} is already registered")

    metric_def = MetricDef(
        name=name,
        aggregator=aggregator,
        higher_is_better=higher_is_better,
        critical=critical,
        description=description,
    )
    REGISTRY[name] = metric_def
    return metric_def


def get_metric(name: str) -> MetricDef:
    """Look up a declared metric by name.

    Raises:
        MetricNamespaceError: `name` is not registered.
    """
    try:
        return REGISTRY[name]
    except KeyError:
        raise MetricNamespaceError(f"unknown metric: {name!r}") from None


def is_registered(name: str) -> bool:
    """Whether `name` is a declared metric."""
    return name in REGISTRY


def all_metrics() -> tuple[MetricDef, ...]:
    """Every declared metric, sorted by name (deterministic order)."""
    return tuple(REGISTRY[name] for name in sorted(REGISTRY))


def aggregate(name: str, values: Sequence[float | int]) -> float | int:
    """Combine per-block `values` for metric `name` per its declared
    aggregator.

    Raises:
        MetricNamespaceError: `name` is not registered.
        ValueError: `values` is empty (no declared aggregator has a
            sensible answer for zero inputs).
    """
    metric_def = get_metric(name)
    if not values:
        raise ValueError(f"aggregate({name!r}, ...): values must be non-empty")

    if metric_def.aggregator == "sum":
        return sum(values)
    if metric_def.aggregator == "min":
        return min(values)
    if metric_def.aggregator == "max":
        return max(values)
    # "mean" is the only remaining declared aggregator (validated at
    # registration time), but stay defensive rather than assume.
    if metric_def.aggregator == "mean":
        return sum(values) / len(values)
    raise MetricNamespaceError(  # pragma: no cover - guarded at register()
        f"metric {name!r}: unhandled aggregator {metric_def.aggregator!r}"
    )


# --------------------------------------------------------------------------
# Pilot registrations: `klt layout-metrics` (issue #247, phase 1)
#
# Only `layout-metrics`' own fixed, non-caller-supplied numeric fields are
# declared here for this pilot. `layout-metrics`' `signals` block is
# attached *verbatim* from a separate `klt sim` run (see
# `layout_metrics.py`'s `_attach_signals`) -- registering it is deferred to
# `klt sim`'s own follow-on issue, matching how `sim`'s caller-supplied
# `measurements[]` names are out of scope for this registry in general (see
# the design doc's "Follow-on work" section).
# --------------------------------------------------------------------------

register(
    "design__layer__count",
    aggregator="max",
    higher_is_better=None,
    description=(
        "Distinct mask layers drawn in a block's layout stream (`klt "
        "layers`' layer_count). Rolls up via max, not sum: layers are "
        "typically shared across blocks in a hierarchy, so summing would "
        "double-count -- max is the closest of the four declared "
        "aggregators to 'the union across blocks is at least this large', "
        "until a dedicated union aggregator is worth adding."
    ),
)
register(
    "design__cell__count",
    aggregator="sum",
    higher_is_better=None,
    description=(
        "Distinct cell definitions in a block's layout stream (`klt "
        "cells`' cell_count)."
    ),
)
register(
    "design__instance__count",
    aggregator="sum",
    higher_is_better=None,
    description=(
        "Total placement records across every cell in a block's layout "
        "stream (`klt cells`' summed `instances`). Name and aggregator "
        "reuse LibreLane's own `design__instance__count` metric verbatim, "
        "per METRICS2.1 precedent."
    ),
)
register(
    "drc__error__count",
    aggregator="sum",
    higher_is_better=False,
    critical=True,
    description=(
        "Total DRC violation count for a block (`klt drc`'s "
        "violation_count, as surfaced through `klt layout-metrics`' "
        "`drc.violation_count`). Name follows METRICS2.1/LibreLane's "
        "`magic__drc_error__count`/`route__drc_errors` family. Critical: "
        "any nonzero value is a signoff blocker."
    ),
)

# --------------------------------------------------------------------------
# `klt sim` adoption (issue #1849, following #1847's `klt drc` adoption)
#
# Only the fixed, non-caller-supplied corner-sweep rollup fields
# (`corner_count`/`passed`/`failed`/`errored`) are declared here.
# `measurements[].name` stays permanently out of scope for this registry --
# those names are defined by the caller's own request spec
# (`docs/cli/sim.md`), not by `klt`, so `klt` cannot declare them ahead of
# time. See the design doc's "Follow-on work" section.
# --------------------------------------------------------------------------

register(
    "sim__corner__count",
    aggregator="sum",
    higher_is_better=None,
    description=(
        "Total number of corners in a `klt sim` sweep (`run_sim()`'s "
        "corner_count). Purely structural/descriptive -- no declared "
        "quality polarity."
    ),
)
register(
    "sim__corner__passed_count",
    aggregator="sum",
    higher_is_better=True,
    description=(
        "Number of corners that passed in a `klt sim` sweep (`run_sim()`'s "
        "passed). More passing corners is a better outcome."
    ),
)
register(
    "sim__corner__failed_count",
    aggregator="sum",
    higher_is_better=False,
    critical=True,
    description=(
        "Number of corners that failed (a measurement outside its limits) "
        "in a `klt sim` sweep (`run_sim()`'s failed). Critical: any nonzero "
        "value is a signoff blocker."
    ),
)
register(
    "sim__corner__errored_count",
    aggregator="sum",
    higher_is_better=False,
    critical=True,
    description=(
        "Number of corners that errored (the simulator itself failed to "
        "produce a usable result) in a `klt sim` sweep (`run_sim()`'s "
        "errored). Critical: any nonzero value is a signoff blocker."
    ),
)

# --------------------------------------------------------------------------
# `klt extract` adoption (issue #1848, extending #247's registry beyond its
# `layout-metrics`/`drc` (#1847) adopters).
#
# `device_count`/`net_count`/`pin_count` are always present in `run_extract`'s
# JSON payload; the eight `extract__*` names below (r_count, c_count,
# total_resistance_ohm, total_capacitance_ff, cc_count, l_count,
# total_coupling_capacitance_ff, total_inductance_nh) are declared only for
# `run_extract`'s `--parasitics` fields -- see `extract.py`'s own
# `_METRIC_NAME_BY_PARASITICS_FIELD` for the field -> declared-name mapping
# actually wired into the `metrics` block.
#
# Naming note: `register()`'s naming grammar (`_NAME_RE` above) requires
# every `__`-separated segment to be plain alphanumeric -- no single
# underscores inside a segment. That rules out a literal
# "extract__resistance__total_ohm"-style name (a single underscore inside
# the last segment); the unit ("ohm"/"ff"/"nh") is used as its own final
# segment instead (`extract__resistance__ohm`), with the "total" implied by
# the declared `sum` aggregator rather than spelled out in the name.
#
# All eleven are purely structural/physical counts and sums with no declared
# quality polarity -- the same `higher_is_better=None` reasoning
# `docs/design/metric-namespace.md` applies to `layout-metrics`' structural
# counts (`design__layer__count` et al.): a bigger device/net/pin count, or a
# bigger total parasitic R/C/L, is not by itself a "better or worse" outcome
# -- it is a sizing/parasitics fact other decisions act on.
# --------------------------------------------------------------------------

register(
    "extract__device__count",
    aggregator="sum",
    higher_is_better=None,
    description=(
        "Total extracted device count for a block (`klt extract`'s device_count)."
    ),
)
register(
    "extract__net__count",
    aggregator="sum",
    higher_is_better=None,
    description="Total extracted net count for a block (`klt extract`'s net_count).",
)
register(
    "extract__pin__count",
    aggregator="sum",
    higher_is_better=None,
    description="Total promoted-pin net count for a block (`klt extract`'s pin_count).",
)
register(
    "extract__resistor__count",
    aggregator="sum",
    higher_is_better=None,
    description=(
        "Total parasitic resistor device count injected by `--parasitics` "
        "(`klt extract`'s parasitics.r_count)."
    ),
)
register(
    "extract__capacitor__count",
    aggregator="sum",
    higher_is_better=None,
    description=(
        "Total parasitic ground-capacitor device count injected by "
        "`--parasitics` (`klt extract`'s parasitics.c_count)."
    ),
)
register(
    "extract__coupling__capacitor__count",
    aggregator="sum",
    higher_is_better=None,
    description=(
        "Total net-to-net coupling-capacitor device count injected by "
        "`--parasitics` (`klt extract`'s parasitics.cc_count)."
    ),
)
register(
    "extract__inductor__count",
    aggregator="sum",
    higher_is_better=None,
    description=(
        "Total series-inductor device count injected by `--parasitics` "
        "`--mom-rlc-net`/`--mom-rlc-inductance-nh` (`klt extract`'s "
        "parasitics.l_count)."
    ),
)
register(
    "extract__resistance__ohm",
    aggregator="sum",
    higher_is_better=None,
    description=(
        "Total parasitic resistance across every net, in ohms, injected by "
        "`--parasitics` (`klt extract`'s parasitics.total_resistance_ohm)."
    ),
)
register(
    "extract__capacitance__ff",
    aggregator="sum",
    higher_is_better=None,
    description=(
        "Total parasitic ground capacitance across every net, in "
        "femtofarads, injected by `--parasitics` (`klt extract`'s "
        "parasitics.total_capacitance_ff)."
    ),
)
register(
    "extract__coupling__capacitance__ff",
    aggregator="sum",
    higher_is_better=None,
    description=(
        "Total net-to-net coupling capacitance across every coupled pair, "
        "in femtofarads, injected by `--parasitics` (`klt extract`'s "
        "parasitics.total_coupling_capacitance_ff)."
    ),
)
register(
    "extract__inductance__nh",
    aggregator="sum",
    higher_is_better=None,
    description=(
        "Total series inductance across every `--mom-rlc-net`-substituted "
        "net, in nanohenries (`klt extract`'s "
        "parasitics.total_inductance_nh)."
    ),
)
