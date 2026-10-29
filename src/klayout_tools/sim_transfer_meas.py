"""``measurements[].transfer`` for ``klt sim`` (issue #2923).

A ``measurements[]`` entry declared with ``transfer`` grades one scalar
stability metric of the AC ratio ``num / den``::

    {"name": "pm",
     "transfer": {"num": "v(out)", "den": "v(in)", "metric": "phase_margin_deg"},
     "limits": {"min": 45}}

The numerics (and the sign convention, interpolation and invalid-sample rules)
live in :mod:`klayout_tools.sim_transfer`; this module only validates the
request, asks ngspice to dump each unique ``num``/``den`` pair of the finished
``ac`` analysis to a small ASCII rawfile, reads it back **with its phase**
(:mod:`klayout_tools.sim_raw`) and hands the arrays to ``analyze_transfer``::

    let __klt_xfer_<k>_num = v(out)
    let __klt_xfer_<k>_den = v(in)
    write <corner_dir>/transfer_<k>.raw __klt_xfer_<k>_num __klt_xfer_<k>_den

The rawfile is independent of ``options.waveforms``. Only vectors klt (or the
netlist, under ``options.save_mode: "netlist"``) saved can be dumped; a vector
that does not resolve is that measurement's own error, never silently
substituted.

``value`` is the metric (graded like any scalar); an additive ``transfer``
sibling ``{num, den, metric}`` rides the entry. Three further tokens
(:data:`MARGIN_METRICS`, issue #3088) reduce the same ratio with
:func:`klayout_tools.sim_transfer.analyze_margins` -- the minimum margin over
**every** crossing and the Nyquist encirclement count -- and extend the
sibling with the full ``margins`` report and its ``verdict``. Every failure is
``status: "error"``, ``value: null`` with a ``code: "measurement"`` diagnostic
whose message starts with a ``[<reason>]`` token from :data:`ERROR_CODES`.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Any

from . import sim_raw, sim_transfer
from .sim_errors import SimError

#: The six first-crossing metric tokens, exactly
#: :mod:`klayout_tools.sim_transfer`'s names. Unchanged by issue #3088.
METRICS = sim_transfer._METRIC_NAMES

#: All-crossing tokens (issue #3088), computed by ``analyze_margins``.
MARGIN_METRICS = (
    "phase_margin_min_deg",
    "gain_margin_min_db",
    "encirclement_count",
)

#: Every accepted ``transfer.metric`` token.
ALL_METRICS = METRICS + MARGIN_METRICS

#: ``metric -> MarginReport attribute`` for :data:`MARGIN_METRICS`.
_MARGIN_ATTR = {
    "phase_margin_min_deg": "phase_margin_min_deg",
    "gain_margin_min_db": "gain_margin_min_db",
    "encirclement_count": "encirclements",
}

#: ``MarginReport.reasons`` values meaning "no such crossing exists": on a
#: clean band the limit is vacuously satisfied (see ``grade_margins``).
_NO_CROSSING_REASONS = (
    sim_transfer.REASON_NO_ZERO_DB_CROSSING,
    sim_transfer.REASON_NO_PHASE_CROSSING_ANY,
)

#: Reason tokens a failed ``transfer`` entry's diagnostic message leads with.
ERROR_CODES = (
    "transfer_vector_missing",
    "transfer_raw_invalid",
    "transfer_metric_unavailable",
)

_ALLOWED_KEYS = frozenset({"num", "den", "metric"})

#: Single-ended ``v(node)``: one non-empty token without whitespace, control
#: characters, commas or parentheses.
_SELECTOR_RE = re.compile(r"^[vV]\(([^\s\x00-\x1f\x7f,()]+)\)$")

_VECTOR_PREFIX = "__klt_xfer_"

_FORMS = ("spice", "expr", "crossings", "cycle_mean", "energy", "stress")


def is_transfer(spec: dict[str, Any]) -> bool:
    return spec.get("transfer") is not None


def transfer_measurements(
    measurements_spec: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    return [spec for spec in measurements_spec if is_transfer(spec)]


def normalize_selector(selector: Any) -> str | None:
    """``"v(node)"`` lower-cased, or ``None`` when not an accepted selector."""
    if not isinstance(selector, str):
        return None
    match = _SELECTOR_RE.match(selector)
    return None if match is None else f"v({match.group(1).lower()})"


def _pair(spec: dict[str, Any]) -> tuple[str, str]:
    body = spec["transfer"]
    return normalize_selector(body["num"]) or "", normalize_selector(body["den"]) or ""


def unique_pairs(measurements_spec: list[dict[str, Any]]) -> list[tuple[str, str]]:
    """Distinct normalized ``(num, den)`` pairs, in first-declared order --
    also the order their deck lines and rawfiles are numbered in."""
    pairs: list[tuple[str, str]] = []
    for spec in transfer_measurements(measurements_spec):
        pair = _pair(spec)
        if pair not in pairs:
            pairs.append(pair)
    return pairs


# --------------------------------------------------------------------------- #
# Request validation
# --------------------------------------------------------------------------- #


def validate_form_exclusive(spec: dict[str, Any]) -> bool:
    """For an entry declaring ``transfer``: refuse any other form beside it
    and validate the body. Returns ``True`` when the entry is a transfer
    entry (so the caller has nothing more to validate)."""
    if not is_transfer(spec):
        return False
    name = spec.get("name")
    if name is None:
        raise SimError("each request.measurements[] entry requires 'name'")
    others = [form for form in _FORMS if spec.get(form) is not None]
    if others:
        raise SimError(
            f"measurement '{name}' declares 'transfer' together with "
            f"'{others[0]}': they are alternative forms of a measurement -- "
            "declare exactly one (use a separate measurements[] entry for "
            "the other)"
        )
    validate_transfer_spec(name, spec["transfer"])
    return True


def validate_transfer_spec(name: Any, body: Any) -> None:
    if not isinstance(body, dict):
        raise SimError(
            f"measurement '{name}': 'transfer' must be an object, e.g. "
            '{"num": "v(out)", "den": "v(in)", "metric": "phase_margin_deg"}'
        )
    unknown = sorted(set(body) - _ALLOWED_KEYS)
    if unknown:
        raise SimError(
            f"measurement '{name}': unknown transfer key(s) {unknown}; "
            f"allowed: {sorted(_ALLOWED_KEYS)}"
        )
    for key in ("num", "den"):
        if normalize_selector(body.get(key)) is None:
            raise SimError(
                f"measurement '{name}': transfer.{key} must be a single-ended "
                f'"v(node)" selector with one node token (no whitespace, '
                f"comma or parenthesis); got {body.get(key)!r}"
            )
    if body.get("metric") not in ALL_METRICS:
        raise SimError(
            f"measurement '{name}': transfer.metric must be one of "
            f"{list(ALL_METRICS)} (got {body.get('metric')!r})"
        )


def validate_transfer_context(
    measurements_spec: list[dict[str, Any]], analysis: dict[str, Any]
) -> None:
    """A transfer metric is an AC quantity, and measurement names must be
    unique so each response entry is unambiguous."""
    declared = transfer_measurements(measurements_spec)
    if not declared:
        return
    kind = str(analysis.get("kind", "")).lower()
    if kind != "ac":
        raise SimError(
            f"measurement '{declared[0]['name']}' declares 'transfer', which "
            f'needs an AC sweep and so requires analysis.kind "ac" (got '
            f"{analysis.get('kind')!r})"
        )
    seen: set[str] = set()
    for spec in measurements_spec:
        key = str(spec.get("name")).lower()
        if key in seen:
            raise SimError(
                f"duplicate measurement name '{spec.get('name')}': names must "
                "be unique when measurements[] declares a transfer entry"
            )
        seen.add(key)


# --------------------------------------------------------------------------- #
# Deck emission
# --------------------------------------------------------------------------- #


def rawfile_path(corner_dir: str, index: int) -> str:
    return os.path.join(corner_dir, f"transfer_{index}.raw")


def control_lines(
    measurements_spec: list[dict[str, Any]], corner_dir: str
) -> list[str]:
    """Per unique pair: two ``let`` lines and one ``write`` of both vectors.
    Empty without a transfer entry (deck byte-identical to before)."""
    lines: list[str] = []
    for index, (num, den) in enumerate(unique_pairs(measurements_spec)):
        n_vec = f"{_VECTOR_PREFIX}{index}_num"
        d_vec = f"{_VECTOR_PREFIX}{index}_den"
        lines.append(f"let {n_vec} = {num}")
        lines.append(f"let {d_vec} = {den}")
        lines.append(f"write {rawfile_path(corner_dir, index)} {n_vec} {d_vec}")
    return lines


# --------------------------------------------------------------------------- #
# Extraction
# --------------------------------------------------------------------------- #


def is_margin_metric(spec: dict[str, Any]) -> bool:
    return spec["transfer"]["metric"] in MARGIN_METRICS


def margin_verdict(spec: dict[str, Any], report: sim_transfer.MarginReport) -> str:
    """``pass`` / ``fail`` / ``ambiguous`` for one margin-metric entry.

    ``limits.min`` maps onto ``min_phase_margin_deg`` / ``min_gain_margin_db``;
    ``encirclement_count`` with ``limits.max < 1`` requires no encirclement.
    Only the entry's own metric is requested, so another metric's state can
    never fail it."""
    metric = spec["transfer"]["metric"]
    limits = spec.get("limits") or {}
    min_limit, max_limit = limits.get("min"), limits.get("max")
    return sim_transfer.grade_margins(
        report,
        min_phase_margin_deg=min_limit if metric == "phase_margin_min_deg" else None,
        min_gain_margin_db=min_limit if metric == "gain_margin_min_db" else None,
        require_no_encirclement=(
            metric == "encirclement_count" and max_limit is not None and max_limit < 1
        ),
    )


@dataclass
class TransferOutcome:
    """One transfer measurement's result, or why there is none.

    A margin-metric entry (issue #3088) always carries ``margins`` and
    ``verdict`` once the rawfile was analysed, even when ``value`` is ``None``:
    "no such crossing" and "the band cannot support a verdict" are gradeable
    outcomes there, not extraction failures."""

    value: float | None = None
    error: str | None = None
    code: str | None = None
    margins: sim_transfer.MarginReport | None = None
    verdict: str | None = None

    def report(self, spec: dict[str, Any]) -> dict[str, Any]:
        body = spec["transfer"]
        out: dict[str, Any] = {
            "num": body["num"],
            "den": body["den"],
            "metric": body["metric"],
        }
        if self.margins is not None:
            out["margins"] = self.margins.to_dict()
            out["verdict"] = self.verdict
        return out


@dataclass
class _PairAnalysis:
    metrics: sim_transfer.TransferMetrics
    margins: sim_transfer.MarginReport | None


def _analyze_pair(raw_path: str, want_margins: bool) -> _PairAnalysis | TransferOutcome:
    """Metrics for one pair's rawfile, or a pair-level failure outcome (the
    caller prefixes the measurement name). ``analyze_margins`` runs only when
    an entry on the pair asks for a margin metric."""
    if not os.path.isfile(raw_path):
        return TransferOutcome(
            code="transfer_vector_missing",
            error=(
                "its num or den vector did not resolve against this AC "
                "analysis's saved vectors (no rawfile was written) -- check "
                "the node names, and that options.save_mode does not omit them"
            ),
        )
    try:
        raw = sim_raw.read_complex_ascii_rawfile(raw_path)
    except sim_raw.RawFileError as exc:
        return TransferOutcome(
            code="transfer_raw_invalid", error=f"could not read its rawfile: {exc}"
        )
    if len(raw.names) != 3 or not raw.points:
        return TransferOutcome(
            code="transfer_raw_invalid",
            error="its rawfile must hold a frequency axis, num and den with samples",
        )
    freqs = [z.real for z in raw.column(0)]
    try:
        metrics = sim_transfer.analyze_transfer(freqs, raw.column(1), raw.column(2))
        margins = (
            sim_transfer.analyze_margins(freqs, raw.column(1), raw.column(2))
            if want_margins
            else None
        )
    except sim_transfer.TransferInputError as exc:
        return TransferOutcome(code="transfer_raw_invalid", error=f"bad sweep: {exc}")
    return _PairAnalysis(metrics, margins)


def _margin_outcome(
    spec: dict[str, Any], margins: sim_transfer.MarginReport
) -> TransferOutcome:
    metric = spec["transfer"]["metric"]
    raw_value = getattr(margins, _MARGIN_ATTR[metric])
    return TransferOutcome(
        value=None if raw_value is None else float(raw_value),
        margins=margins,
        verdict=margin_verdict(spec, margins),
    )


def _outcome_for(
    spec: dict[str, Any], analysed: _PairAnalysis | TransferOutcome
) -> TransferOutcome:
    name = spec["name"]
    if isinstance(analysed, TransferOutcome):
        return TransferOutcome(
            code=analysed.code, error=f"measurement '{name}': {analysed.error}"
        )
    metric = spec["transfer"]["metric"]
    if metric in MARGIN_METRICS:
        assert analysed.margins is not None
        return _margin_outcome(spec, analysed.margins)
    value = getattr(analysed.metrics, metric)
    if value is None:
        reason = analysed.metrics.reasons.get(metric, "unavailable")
        return TransferOutcome(
            code="transfer_metric_unavailable",
            error=f"measurement '{name}': {metric} is unavailable ({reason})",
        )
    return TransferOutcome(value=float(value))


_REASON_KEY = {
    "phase_margin_min_deg": "phase_margin_min_deg",
    "gain_margin_min_db": "gain_margin_min_db",
    "encirclement_count": "encirclement",
}


def unavailable_reason(outcome: TransferOutcome, metric: str) -> str:
    """``MarginReport.reasons`` entry for a valueless margin metric."""
    assert outcome.margins is not None
    return outcome.margins.reasons.get(_REASON_KEY[metric], "unavailable")


def is_vacuous(outcome: TransferOutcome, spec: dict[str, Any]) -> bool:
    """A valueless margin metric whose only cause is "no such crossing" on a
    clean band: the limit is vacuously satisfied (``grade_margins``)."""
    if outcome.margins is None or outcome.value is not None:
        return False
    reason = unavailable_reason(outcome, spec["transfer"]["metric"])
    return reason in _NO_CROSSING_REASONS and not outcome.margins.ambiguity


def extract_corner_transfers(
    measurements_spec: list[dict[str, Any]], corner_dir: str
) -> dict[int, TransferOutcome]:
    """``{position in measurements_spec: outcome}`` for every transfer entry
    of one finished corner. One ratio computation per unique pair. Never
    raises: any rawfile problem becomes that measurement's own ``error``."""
    pairs = unique_pairs(measurements_spec)
    wants = [
        any(
            is_transfer(spec) and _pair(spec) == pair and is_margin_metric(spec)
            for spec in measurements_spec
        )
        for pair in pairs
    ]
    analysed = [
        _analyze_pair(rawfile_path(corner_dir, i), wants[i]) for i in range(len(pairs))
    ]
    return {
        position: _outcome_for(spec, analysed[pairs.index(_pair(spec))])
        for position, spec in enumerate(measurements_spec)
        if is_transfer(spec)
    }
