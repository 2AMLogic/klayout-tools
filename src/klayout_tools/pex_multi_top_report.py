"""``klt pex`` multi-top mode: shaping the report (issue #5494).

The companion to :mod:`klayout_tools.pex_multi_top`, which owns the mode's
*mechanics* (normalising ``--top``, refusing what the mode does not define,
extracting each top and concatenating the results). This module owns what
the run then *reports*:

- :func:`_extraction_block` -- the ``extraction`` block for one
  ``run_extract`` report, built once from the combined report (the top-level
  field) and once per ``--top`` (each ``tops[]`` entry's own).
- :func:`_combine_extract_reports` / :func:`_combine_parasitics` -- one
  synthetic ``run_extract`` report describing the *combined* extracted
  netlist, reduced so it never reads cleaner than the worst top.
- :func:`_tops_block` -- the additive ``tops`` field, where per-top
  attribution lives.

Like its companion it imports nothing from :mod:`klayout_tools.pex` at
module scope (``pex`` imports *it*); the two functions that need a ``pex``
helper import it inside the call.
"""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from typing import Any

from . import env_provenance

#: ``parasitics.integrity`` counters that are sums across extractions
#: (issue #5494). The remaining keys (``short_threshold_ohm``,
#: ``cap_tolerance``, ``max_detail_items``) are thresholds the caller's own
#: flags fix, identical for every top of one run, so the first top's are the
#: run's.
_INTEGRITY_SUM_KEYS = (
    "nets_checked",
    "nets_with_findings",
    "open_nets",
    "island_components",
    "orphan_nodes",
    "suspicious_resistors",
    "short_candidates",
    "capacitance_mismatches",
)


def _union_in_top_order(values: Sequence[Sequence[str]]) -> list[str]:
    """The union of per-top name lists, first occurrence in ``--top`` order
    (issue #5523).

    When every top's list is identical -- the case for a plain
    ``--critical-net`` echo, which is this run's own flag repeated back by
    each extraction -- the first top's list is returned **verbatim**, so a
    deliberately repeated name (``--critical-net a --critical-net a``,
    echoed "as given, not sorted/deduplicated") survives unchanged and the
    field stays byte-identical to a single-top run's.

    The lists differ only when the names were *resolved* per top rather than
    echoed (``--critical-net-pattern`` / ``--critical-net-all``, which
    :func:`klayout_tools.extract._adopt_resolved_net_selectors` replaces with
    the set that matched *that* cell). Then the union is taken, so the
    combined field names every net scoped as critical anywhere in the
    combined netlist instead of the first top's resolution only -- the same
    "never cleaner than the worst top" rule the rest of this module follows.
    """
    first = list(values[0]) if values else []
    if all(list(value) == first for value in values[1:]):
        return first
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        for name in value:
            if name not in seen:
                seen.add(name)
                out.append(name)
    return out


def _combine_parasitics(blocks: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """The ``parasitics`` block describing the *combined* netlist (issue
    #5494): the first top's, with ``nets[]`` concatenated in ``--top`` order
    and every ``integrity`` counter summed.

    The echo fields (``model``, ``distributed_rc``, ``mom_rlc_override``,
    ``parasitic_variant``, the resistance-graph data) are the caller's own
    flags, identical across tops, so the first top's are the run's.

    ``critical_nets`` is only *usually* such an echo (issue #5523): with
    ``--critical-net-pattern`` / ``--critical-net-all`` it is the set the
    pattern resolved to against *that top's* nets, which differs per top, so
    it goes through :func:`_union_in_top_order` -- verbatim when every top
    agrees, their union when they do not.

    The two *diagnostic* parts are not echoes either: an integrity finding on
    the second top must not be invisible in a report whose ``netlist`` names
    the file that top's devices are in. So the counters are summed and the
    per-net entries concatenated -- the top-level block then reads as badly
    as the worst top, never cleaner.

    Two tops can each own a net of the same name (``vsubs``, a supply), so
    the concatenated ``nets[]`` can carry two same-named entries. They are
    carried verbatim and in ``--top`` order rather than merged: ``tops[]``
    is where a reader attributes a net to the extraction it came from.
    """
    combined = dict(blocks[0])
    nets: list[Any] = []
    for block in blocks:
        nets.extend(block.get("nets") or [])
    if any("nets" in block for block in blocks):
        combined["nets"] = nets
    if any("critical_nets" in block for block in blocks):
        combined["critical_nets"] = _union_in_top_order(
            [block.get("critical_nets") or [] for block in blocks]
        )
    summaries = [
        block["integrity"]
        for block in blocks
        if isinstance(block.get("integrity"), dict)
    ]
    if summaries:
        combined["integrity"] = {
            **summaries[0],
            **{
                key: sum(summary.get(key) or 0 for summary in summaries)
                for key in _INTEGRITY_SUM_KEYS
                if any(key in summary for summary in summaries)
            },
        }
    return combined


def _combine_extract_reports(
    reports: Sequence[Mapping[str, Any]],
    *,
    netlist_path: str,
    netlist_sha256: str | None,
) -> dict[str, Any]:
    """One synthetic ``run_extract`` report describing the *combined*
    extracted netlist a multi-top run simulated (issue #5494).

    Built so every downstream reader in this module -- :func:`_body_bias_
    report`, the ``extraction`` block, ``provenance`` -- keeps reading one
    report, exactly as it does for a single-top run, while the values it
    reads describe the file the extracted leg actually ran.

    The rule the issue settles, and the one this function implements: the
    combined report must **never read cleaner than the worst top**.

    - ``netlist_path``/``netlist_sha256`` identify the combined file.
    - ``device_count``/``net_count`` are sums -- the combined netlist really
      does contain all of them.
    - ``unbiased_pmos_body_nets`` is the concatenation in ``--top`` order, so
      :func:`_body_bias_report` reports ``unbiased`` when *any* top is and
      carries every top's entries. A reader of ``klt signoff``'s item-7
      citation sees the disclosure, which is the whole point of the field.
    - ``unsupported_devices`` (issue #5477, forwarded here since #5523) is
      the concatenation in ``--top`` order, for the same reason. It is
      **not** a flag echo:
      :func:`klayout_tools.extract._detect_unsupported_devices` computes it
      per extracted cell from the marker geometry *that cell* draws, so a
      clean first top and a flagged second top must not combine to a clean
      top-level field -- the combined netlist really is missing the second
      top's devices, and that limit disclosure is the field's whole job.
    - ``parasitics`` is :func:`_combine_parasitics`.
    - Everything else -- the flag echoes (``label_layers``,
      ``device_models``, ``rename_declared_pins``, ``pin_ties``, ``pdk``) and
      ``provenance`` -- is the first top's. Those are this run's own inputs,
      identical for every top because every top is extracted with the same
      flags, the same deck and the same PDK, from the same layout stream.
    - ``top`` stays the first top's name and is deliberately *unused* in
      multi-top mode: every diagnostic that keyed on it is run once per top
      instead (see :func:`_per_top_preflight`).

    Per-top detail is never lost -- it is reported under ``tops[]``, built
    from these same per-top reports by :func:`_tops_block`.
    """
    combined = dict(reports[0])
    combined["netlist_path"] = netlist_path
    combined["netlist_sha256"] = netlist_sha256
    combined["device_count"] = sum(report["device_count"] for report in reports)
    combined["net_count"] = sum(report["net_count"] for report in reports)
    unbiased: list[Any] = []
    for report in reports:
        unbiased.extend(report.get("unbiased_pmos_body_nets") or [])
    if any("unbiased_pmos_body_nets" in report for report in reports):
        combined["unbiased_pmos_body_nets"] = unbiased
    unsupported: list[Any] = []
    for report in reports:
        unsupported.extend(report.get("unsupported_devices") or [])
    if any("unsupported_devices" in report for report in reports):
        combined["unsupported_devices"] = unsupported
    combined["parasitics"] = _combine_parasitics(
        [report.get("parasitics") or {} for report in reports]
    )
    return combined


def _extraction_block(
    extract_report: Mapping[str, Any],
    *,
    deck_name: str,
    netlist_sha256: str | None,
    resistance_graph: bool,
    resistance_graph_r_cut_ohm: float | None,
) -> dict[str, Any]:
    """The report's ``extraction`` block for one ``run_extract`` report.

    Factored out of :func:`run_pex`'s response literal (issue #5494) with no
    change to a single field, so a multi-top run can build the same block
    twice: once from the combined report (the top-level ``extraction``) and
    once per ``--top`` (each ``tops[]`` entry's own ``extraction``), which is
    what makes a ``tops[]`` sub-block readable by anything that can already
    read the top-level one.

    ``netlist_sha256`` is passed in rather than read from ``extract_report``
    because a ``--tie-net`` rewrite re-hashes the file after extraction (see
    :func:`run_pex`), so the extract-time hash no longer identifies the file
    the report points at.
    """
    from .pex import _parasitic_integrity_report, _resistance_graph_echo
    from .pex_asap7 import netlist_adaptation_for_deck

    parasitics = extract_report.get("parasitics") or {}
    return {
        "deck": deck_name,
        "netlist_adaptation": netlist_adaptation_for_deck(deck_name),
        "device_count": extract_report["device_count"],
        "net_count": extract_report["net_count"],
        "netlist_sha256": netlist_sha256,
        "model": parasitics.get("model"),
        # Additive field (issue #976): echoes `--critical-net` back --
        # `[]` when the flag was never given, byte-identical to before
        # this feature existed.
        "critical_nets": parasitics.get("critical_nets") or [],
        # Additive field (issue #977): echoes `--distributed-rc` back --
        # `False` when the flag was never given, byte-identical to
        # before this feature existed.
        "distributed_rc": bool(parasitics.get("distributed_rc")),
        # Additive field (issue #988, Epic #709 Phase 3a): `None`
        # unless `--mom-rlc-net` was given, in which case it is `klt
        # extract`'s own substitution report (see
        # `run_extract`'s `mom_rlc_net` docstring paragraph) --
        # byte-identical to before this feature existed otherwise.
        "mom_rlc_override": parasitics.get("mom_rlc_override"),
        # Additive field (issue #2686): `klt extract`'s own
        # `label_layers` echo for this run's extraction -- the applied
        # `--label-layer` mapping as `{role: [layer, datatype] | null}`,
        # or `None` when the flag was never given.
        "label_layers": extract_report.get("label_layers"),
        # Additive field (issue #4234): `klt extract`'s `device_models`
        # echo -- the applied `--device-model` mapping, or `None`.
        "device_models": extract_report.get("device_models"),
        # Additive fields (issue #5477): `klt extract`'s own device
        # coverage disclosure -- `device_classes` (issue #221, what the
        # resolved deck can recognise) and `unsupported_devices` (issue
        # #2919, the deck-declared "cannot extract this yet" marker
        # geometry the layout draws) -- forwarded verbatim in every mode,
        # so a committed envelope carries the same limit statement the
        # extraction it cites published, instead of making a reader
        # re-run `klt extract`. Neither sees a family the deck declares
        # no class for *and* that draws no marker layer (e.g. `pnpMPA` on
        # sg13cmos5l); see `run_pex` on `model_mismatch` for that gap.
        "device_classes": extract_report.get("device_classes"),
        "unsupported_devices": extract_report.get("unsupported_devices"),
        # Additive field (issue #2854): echoes the effective
        # `--rename-declared-pins` value back -- `False` when the flag
        # was never given, byte-identical to before this feature
        # existed. The renames themselves are `klt extract`'s own
        # `pin_renames` report, visible here through the extraction's
        # written netlist (`.SUBCKT` header) rather than re-embedded.
        "rename_declared_pins": bool(extract_report.get("rename_declared_pins")),
        "tie_declared_pins": "pin_ties" in extract_report,
        # Additive field (issue #4383): the PDK install this extraction
        # actually read -- `klt extract`'s own `pdk` block (`variant`,
        # `root` as `{path, scope}`, `version`), `None` without `--pdk`.
        "pdk": extract_report.get("pdk"),
        # Additive field (issue #2764): the extraction's own
        # per-net parasitic RC network integrity diagnostics --
        # threshold/tolerances, aggregate counts, and one entry per net
        # (verbatim per-net payloads, `parasitics.nets[]` order). See
        # `_parasitic_integrity_report`.
        "parasitic_integrity": _parasitic_integrity_report(parasitics),
        # Additive field (issue #3056): echoes `--parasitic-variant`
        # back so a variant run is attributable -- present only when
        # the flag was given, byte-identical to before otherwise.
        **(
            {"parasitic_variant": parasitics["parasitic_variant"]}
            if "parasitic_variant" in parasitics
            else {}
        ),
        # Additive field (issue #4037): present only with
        # `--resistance-graph`.
        **(
            {
                "resistance_graph": _resistance_graph_echo(
                    parasitics, resistance_graph_r_cut_ohm
                )
            }
            if resistance_graph
            else {}
        ),
    }


def _tops_block(
    per_top_reports: Sequence[Mapping[str, Any]],
    per_top_preflight: Sequence[Mapping[str, Any]],
    *,
    multi_top: bool,
    deck_name: str,
    resistance_graph: bool,
    resistance_graph_r_cut_ohm: float | None,
    repo_root: str | None,
) -> list[dict[str, Any]] | None:
    """The additive ``tops`` field (issue #5494).

    ``None`` on a zero- or one-``--top`` run -- the whole envelope is then
    exactly what it was before this mode existed, with this one extra key
    whose ``null`` says "single-top run, every singular field below describes
    the one extraction". That is what keeps the change additive under
    ``docs/json-contract.md``: no existing field changes type or meaning, and
    a reader that does not know about ``tops`` is unaffected.

    On a multi-top run, one entry per ``--top`` **in ``--top`` order**, each
    carrying that top's own ``top``, ``netlist`` (the per-top staged
    extraction, not the combined file), ``extraction``, ``body_bias``, the
    three pre-flight mismatch diagnostics and ``provenance`` -- each in the
    same shape as the top-level field of that name, built by the same
    helper, so anything that can read the top level can read one of these.

    This is where per-top detail lives. The top-level fields describe the
    *combined* netlist the extracted leg actually simulated and are reduced
    so they never read cleaner than the worst top (see
    :func:`_combine_extract_reports` and :func:`_first_firing`); when a
    reader needs to know *which* top contributed a count, a floating body or
    a mismatch, this is the field that answers.
    """
    from .pex import _body_bias_report

    if not multi_top:
        return None
    entries: list[dict[str, Any]] = []
    for report, preflight in zip(per_top_reports, per_top_preflight, strict=True):
        netlist_path = os.path.abspath(report["netlist_path"])
        entries.append(
            {
                "top": preflight["top"],
                "netlist": env_provenance.repo_relative_path(
                    netlist_path or None, repo_root=repo_root
                ),
                "extraction": _extraction_block(
                    report,
                    deck_name=deck_name,
                    netlist_sha256=report["netlist_sha256"],
                    resistance_graph=resistance_graph,
                    resistance_graph_r_cut_ohm=resistance_graph_r_cut_ohm,
                ),
                # `--tie-net` is refused with two or more tops, so no tie
                # can apply to a per-top block: `_body_bias_report`'s second
                # argument is always `None` here.
                "body_bias": _body_bias_report(report),
                "pin_count_mismatch": preflight["pin_count_mismatch"],
                "flat_dut_mismatch": preflight["flat_dut_mismatch"],
                "pin_order_mismatch": preflight["pin_order_mismatch"],
                "provenance": report["provenance"],
            }
        )
    return entries
