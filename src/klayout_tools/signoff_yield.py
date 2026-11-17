"""The `klt yield` campaign-discipline disclosure block (issue #2467).

The three envelope-reading helpers here quote a cited yield report's own
statements about whether its numbers can support a claim -- per-measurement
``sample_size`` verdicts and ``negative_control`` state, rolled up across
``measurements[]`` -- verbatim, never re-derived: `klt yield` computes both
(``docs/cli/yield.md``'s "Sample-size verdict" and "Negative control"), and
re-deriving either here would let this module and the producer disagree
about the same campaign. They lived in ``signoff.py`` until issue #5786
moved them out for that file's size-ratchet ceiling, unchanged.

``graded_entry_campaign`` (issue #5786) copies the block onto a
multi-citation item's ``citations_graded`` entry, so a multi-citation item
6 discloses each cited campaign the same way the single-citation form does.
"""

from __future__ import annotations

from typing import Any


def _yield_campaign_disclosure(
    kind: str, envelope: dict[str, Any]
) -> dict[str, Any] | None:
    """The cited `klt yield` campaign's own two statements about whether its
    numbers can support a claim at all (issue #2467) -- its per-measurement
    ``sample_size.verdict`` and its ``negative_control`` state, rolled up
    across ``measurements[]`` -- or ``None`` when there is nothing to quote.

    Quoted verbatim off the envelope, never re-derived: `klt yield` computes
    both (``docs/cli/yield.md``'s "Sample-size verdict" and "Negative
    control"), and re-deriving either here would let this module and the
    producer disagree about the same campaign.

    ``None`` in exactly two cases, and they mean the same thing to a reader
    ("this artifact makes no campaign-discipline statement"), never "the
    campaign was disciplined":

    - ``kind != "yield"``. No other envelope kind carries ``measurements[]``
      of this shape, and item 6 is the only item that accepts this kind.
    - The envelope reports no measurements at all (an empty or non-list
      ``measurements``). There is then nothing whose sizing or self-check
      could be stated.

    The rolled-up fields, and the aggregation rule each uses -- stated here
    because a campaign is a *set* of measurements and "the campaign's
    verdict" is not a field any of them carries:

    - ``sample_size`` -- ``"insufficient"`` when **any** measurement says so,
      ``"sufficient"`` when at least one says so and none says otherwise,
      and ``None`` when no measurement states a verdict at all (a report
      written before the block existed). "Any" rather than "all": the item is
      graded against the whole cited campaign, so one unsized measurement
      leaves part of the claim unsized, and a missing statement is never read
      as an insufficient one (the same back-compat rule
      :func:`_drc_coverage_disclosure` applies to a missing ``coverage``
      block).
    - ``undersized_measurements`` -- the names behind that verdict, in the
      report's own measurement order.
    - ``negative_control`` -- ``"not_detected"`` when **any** declared
      control failed to show the seeded degradation, ``"detected"`` when at
      least one was declared and every declared one detected, and
      ``"not_declared"`` when no measurement declared one. "Any failed" and
      "at least one declared" are deliberately asymmetric, and match the two
      run-level warnings `klt yield` itself emits (``native/yield/src/
      estimate.rs``): one working self-check demonstrates the campaign's
      statistics *can* discriminate, while one failed self-check
      demonstrates they cannot.
    - ``measurements_without_negative_control`` / ``undetected_negative_controls``
      -- the names behind that verdict, likewise in report order, so a reader
      who sees ``"detected"`` beside a non-empty first list can tell a
      fully-controlled campaign from a partly-controlled one.
    """
    if kind != "yield":
        return None
    raw = envelope.get("measurements")
    measurements = (
        [m for m in raw if isinstance(m, dict)] if isinstance(raw, list) else []
    )
    if not measurements:
        return None

    sample_size_verdict, undersized = _yield_sample_size_rollup(measurements)
    (
        control_verdict,
        without_control,
        undetected,
        not_applicable,
    ) = _yield_negative_control_rollup(measurements)
    return {
        "sample_size": sample_size_verdict,
        "undersized_measurements": undersized,
        "negative_control": control_verdict,
        "measurements_without_negative_control": without_control,
        "undetected_negative_controls": undetected,
        "not_applicable_negative_controls": not_applicable,
    }


def _yield_sample_size_rollup(
    measurements: list[dict[str, Any]],
) -> tuple[str | None, list[Any]]:
    """:func:`_yield_campaign_disclosure`'s ``sample_size`` /
    ``undersized_measurements`` pair (issue #2467) -- see that function's
    docstring for the aggregation rule this applies.

    Split out rather than inlined because the two axes a campaign is rolled
    up along (sizing, self-check) have *different* aggregation rules, and
    reading them in one loop obscured which "any"/"at least one" belonged to
    which axis.
    """
    undersized: list[Any] = []
    sized = False
    for measurement in measurements:
        sample_size = measurement.get("sample_size")
        if not isinstance(sample_size, dict):
            continue
        verdict = sample_size.get("verdict")
        if verdict == "insufficient":
            undersized.append(measurement.get("name"))
        elif verdict == "sufficient":
            sized = True
    if undersized:
        return "insufficient", undersized
    return ("sufficient" if sized else None), undersized


def _yield_negative_control_rollup(
    measurements: list[dict[str, Any]],
) -> tuple[str, list[Any], list[Any], list[dict[str, Any]]]:
    """:func:`_yield_campaign_disclosure`'s ``negative_control`` /
    ``measurements_without_negative_control`` /
    ``undetected_negative_controls`` triple (issue #2467) -- see that
    function's docstring for the aggregation rule this applies.

    A measurement whose ``negative_control`` is ``null``/absent declared
    none; a declared one whose ``verdict`` is anything other than
    ``"detected"`` (``"not_detected"`` today) did not show the seeded
    degradation. A declared-but-unrecognised verdict is counted with the
    latter deliberately: an unknown self-check result is not a passing one.

    A ``"not_applicable"`` verdict with a non-empty ``reason`` (issue #4523)
    is a declared disposition: it is listed in the fourth element, excluded
    from the other lists, and can never itself make the rollup ``detected``.
    Without a reason it fails closed, like an unrecognised verdict.
    """
    without_control: list[Any] = []
    undetected: list[Any] = []
    not_applicable: list[dict[str, Any]] = []
    detected = False
    for measurement in measurements:
        control = measurement.get("negative_control")
        if not isinstance(control, dict):
            without_control.append(measurement.get("name"))
        elif control.get("verdict") == "detected":
            detected = True
        elif (
            control.get("verdict") == "not_applicable"
            and isinstance(control.get("reason"), str)
            and control["reason"].strip()
        ):
            not_applicable.append(
                {"name": measurement.get("name"), "reason": control["reason"]}
            )
        else:
            undetected.append(measurement.get("name"))
    if undetected:
        return "not_detected", without_control, undetected, not_applicable
    if detected:
        return "detected", without_control, undetected, not_applicable
    return "not_declared", without_control, undetected, not_applicable


def graded_entry_campaign(citation: dict[str, Any] | None) -> dict[str, Any]:
    """The ``yield_campaign`` block a multi-citation item's
    ``citations_graded`` entry carries (issue #5786) -- the member citation's
    own block, or ``{}`` when there is none to carry.

    Copied off the citation :func:`signoff._citation` already built, never
    re-derived: re-reading the envelope here would let an entry and the
    citation it sits beside describe different states of the same report.
    ``{}`` -- key absent -- for a non-``yield`` member, a report with no
    measurements, and an unmet member (whose citation is not retained), so
    every pre-#5786 output is unchanged.
    """
    if citation and "yield_campaign" in citation:
        return {"yield_campaign": citation["yield_campaign"]}
    return {}
