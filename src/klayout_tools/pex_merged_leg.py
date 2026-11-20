"""The merged ``klt pex --engine klayout-runset --deck <curated>`` leg
(issue #6606): the runset leg's own netlist with the curated deck's
parasitic network attached through the geometric net correspondence of
:mod:`klayout_tools.pex_net_correspondence`.

The merged leg is the runset leg rewritten in place: matched nodes renamed
to their correspondence-preferred name (a named runset net keeps its own
name -- device-side truth; an anonymous runset net adopts its curated
counterpart's label when it has one, never onto a colliding name), plus the
curated deck's lumped parasitic network appended for the matched nets --
one series ``R`` per net (node -> hub), one ground ``C`` per hub, coupling
capacitors between matched hubs, and the deck's substrate DC tie. This is
**coarser than the curated engine's own star**: the curated netlist
distributes a net's series R across its *curated* device terminals,
terminals that do not exist in a runset leg, so the merged writer collapses
to the single-lumped-R form the ``parasitics.nets[]`` report already
publishes (``rc_model: "lumped"``). Unmatched nets get no parasitics --
listed in the envelope, never silently dropped or silently merged.

:func:`merged_runset_extract` drives the whole route: the runset leg
exactly as the parasitic-free one runs it, a second curated-deck extraction
**with** parasitics, the join, the merge, and the report enrichment. It
runs under ``klt pex`` only when the caller gave ``--deck`` under
``--engine klayout-runset``; without it the runset route is byte-identical
to before this module existed.
"""

from __future__ import annotations

import os
import re
import tempfile
from collections.abc import Mapping, Sequence
from typing import Any

from .extract import run_extract
from .pex_net_correspondence import (
    build_probes,
    correspondence,
    preferred_renames,
)

#: The leg header line the merged writer replaces when it attaches cards.
_HEADER_NO_PARASITICS = (
    "* no parasitics: this engine extracts devices and connectivity only\n"
)

_CARD_LINE = re.compile(r"^\s*[XRCMLDQ]")


# --------------------------------------------------------------------------- #
# The merged leg writer
# --------------------------------------------------------------------------- #


def _ground_net(parasitics: Mapping[str, Any]) -> str | None:
    """The curated parasitic network's ground/substrate node -- the DC-tie
    block names it, and every ground capacitor lands on it."""
    nets = (parasitics.get("substrate_dc_tie") or {}).get("nets") or []
    return str(nets[0]["net"]) if nets else None


def _format_number(value: float) -> str:
    """SPICE number formatting in the curated writer's own style
    (``0.055``, ``8.738e-17``)."""
    return f"{value:.6g}"


def merge_parasitics(
    netlist_path: str,
    *,
    matched: Sequence[Mapping[str, Any]],
    all_runset_nets: Sequence[str],
    parasitics: Mapping[str, Any],
    deck_name: str,
    report_block: Mapping[str, Any],
) -> int:
    """Rewrite the runset leg at ``netlist_path`` in place -- rename matched
    nodes, append the curated lumped parasitic network for the matched nets,
    replace the leg's own "no parasitics" header line -- and return the
    number of parasitic cards written (series R, ground C, coupling C, tie).

    Never touches unmatched nets: a node no card references stays exactly as
    the runset wrote it; the correspondence block in the envelope, not a
    guessed attachment, is where unmatched nets are stated."""
    renames = preferred_renames(matched, all_runset_nets)
    merged_name = {
        entry["curated"]: renames.get(entry["runset"], entry["runset"])
        for entry in matched
    }
    matched_curated = set(merged_name)
    parasitic_nets = {
        str(entry["net"]): entry for entry in parasitics.get("nets") or []
    }
    coupled: dict[tuple[str, str], float] = {}
    for entry in parasitic_nets.values():
        for other in entry.get("coupled") or []:
            net_a, net_b = sorted((str(entry["net"]), str(other["net"])))
            coupled[(net_a, net_b)] = max(
                coupled.get((net_a, net_b), 0.0), float(other["capacitance_ff"])
            )

    ground_net = _ground_net(parasitics)
    if ground_net is not None:
        ground_net = merged_name.get(ground_net, ground_net)
    cards: list[str] = []
    hubs: dict[str, str] = {}
    for curated_net in sorted(matched_curated & set(parasitic_nets)):
        entry = parasitic_nets[curated_net]
        node = merged_name[curated_net]
        hub = node
        resistance = float(entry.get("resistance_ohm") or 0.0)
        capacitance = float(entry.get("capacitance_ff") or 0.0)
        if resistance > 0.0:
            hub = f"{node}__kltph"
            cards.append(f"R{node}_plr {node} {hub} {_format_number(resistance)}")
        hubs[curated_net] = hub
        if capacitance > 0.0 and ground_net is not None:
            cards.append(
                f"C{node}_plc {hub} {ground_net} {_format_number(capacitance * 1e-15)}"
            )
    for (net_a, net_b), capacitance_ff in sorted(coupled.items()):
        if net_a in hubs and net_b in hubs:
            cards.append(
                f"C{merged_name[net_a]}_{merged_name[net_b]}_plcc "
                f"{hubs[net_a]} {hubs[net_b]} "
                f"{_format_number(capacitance_ff * 1e-15)}"
            )
    tie = parasitics.get("substrate_dc_tie") or {}
    if (
        ground_net is not None
        and tie.get("nets")
        and any(ground_net in card.split(" ")[1:] for card in cards)
    ):
        # The DC tie rides along only with parasitics that need the ground
        # node: a leg nothing matched keeps no stray global + resistor.
        cards.append(
            f"R{ground_net}_dctie {ground_net} 0 "
            f"{_format_number(float(tie.get('resistance_ohm') or 0.0))}"
        )

    _rewrite_netlist(
        netlist_path,
        renames,
        cards,
        ground_net,
        deck_name,
        len(matched),
        report_block,
    )
    return len(cards)


def _rename_tokens(line: str, renames: Mapping[str, str]) -> str:
    """Replace whole whitespace-delimited tokens that name a renamed node,
    preserving the original whitespace (comments untouched)."""
    parts = re.split(r"(\s+)", line)
    return "".join(renames.get(part, part) for part in parts)


def _rewrite_netlist(
    path: str,
    renames: Mapping[str, str],
    cards: Sequence[str],
    ground_net: str | None,
    deck_name: str,
    matched_count: int,
    report_block: Mapping[str, Any],
) -> None:
    """Apply the node renames to every card/pin token, emit the ``.GLOBAL``
    the parasitic network needs, insert the parasitic cards before
    ``.ENDS``, and replace the "no parasitics" header line -- in place,
    preserving line order and pin order."""
    with open(path, encoding="utf-8") as handle:
        lines = handle.readlines()

    out: list[str] = []
    rewrote_header = False
    pending = list(cards)
    for line in lines:
        if line == _HEADER_NO_PARASITICS and cards:
            rewrote_header = True
            unmatched = report_block.get("unmatched_runset_nets") or []
            disagreements = report_block.get("disagreements") or []
            out.append(
                f"* parasitics attached from curated deck '{deck_name}' via "
                f"geometric net correspondence (issue #6606): {matched_count} "
                f"net(s) matched, {len(unmatched)} unmatched runset net(s), "
                f"{len(disagreements)} disagreement(s) -- see the report's "
                "extraction.net_correspondence\n"
            )
            continue
        if pending and line.strip().upper().startswith(".ENDS"):
            out.extend(f"{card}\n" for card in pending)
            pending = []
        out.append(
            _rename_tokens(line, renames)
            if not line.lstrip().startswith("*") and line.strip()
            else line
        )
    out.extend(f"{card}\n" for card in pending)
    if not rewrote_header and cards:
        out.insert(
            0, f"* parasitics attached from curated deck '{deck_name}' (issue #6606)\n"
        )
    if ground_net is not None and not any(
        line.strip().upper().startswith(".GLOBAL") and ground_net in line.split()
        for line in out
    ):
        first = next(
            (
                i
                for i, line in enumerate(out)
                if _CARD_LINE.match(line) or line.strip().upper().startswith(".SUBCKT")
            ),
            len(out),
        )
        out.insert(first, f".GLOBAL {ground_net}\n")
    with open(path, "w", encoding="utf-8") as handle:
        handle.writelines(out)


# --------------------------------------------------------------------------- #
# The merged-route driver
# --------------------------------------------------------------------------- #


def merged_runset_extract(
    *,
    layout_path: str,
    output: str | None,
    top: str | None,
    runset: Any,
    deck_name: str,
    label_layers: Mapping[str, tuple[int, int] | None] | None,
    device_models: Mapping[str, str] | None,
    deck_options: Mapping[str, str] | None,
    declared_pins: frozenset[str] | None,
    rename_declared_pins: bool,
    tie_declared_pins: bool,
    critical_nets: Sequence[str] | None,
    critical_net_patterns: Sequence[str] | None,
    critical_net_all: bool,
    critical_net_excludes: Sequence[str] | None,
    distributed_rc: bool,
    resistance_graph: bool,
    resistance_graph_r_cut_ohm: float | None,
    parasitic_variants: Sequence[str] | None,
    substrate_tie_ohms: float | None,
    mom_rlc_net: str | None,
    mom_rlc_resistance_ohm: float | None,
    mom_rlc_capacitance_ff: float | None,
    mom_rlc_inductance_nh: float | None,
) -> dict[str, Any]:
    """``klt pex --engine klayout-runset --deck <curated>`` (issue #6606):
    run the runset leg exactly as the parasitic-free route does, run a
    second, curated-deck extraction **with** parasitics, join the two by
    geometry, and merge the curated parasitic network into the runset leg.

    Returns the runset extract report, enriched in memory with the
    ``net_correspondence`` block, the curated ``parasitics`` (so the pex
    envelope's model/integrity disclosures describe the parasitics actually
    attached), and re-hashed netlist digests. Every pex option only a
    richer curated parasitic model could serve is refused by name here,
    from the same table ``klt extract`` refuses them by -- minus the two
    this route honours (``--deck`` selects the curated deck;
    ``--label-layer`` its label layers)."""
    from . import extract_runset
    from .decks import get_extraction_deck

    extract_runset.refuse_unsupported(
        {
            key: value
            for key, value in {
                "deck_options": deck_options,
                "declared_pins": declared_pins,
                "rename_declared_pins": rename_declared_pins or None,
                "tie_declared_pins": tie_declared_pins or None,
                "critical_nets": critical_nets,
                "critical_net_patterns": critical_net_patterns,
                "critical_net_all": critical_net_all or None,
                "critical_net_excludes": critical_net_excludes,
                "distributed_rc": distributed_rc or None,
                "resistance_graph": resistance_graph or None,
                "resistance_graph_r_cut_ohm": resistance_graph_r_cut_ohm,
                "parasitic_variants": parasitic_variants,
                "substrate_tie_ohms": substrate_tie_ohms,
                "mom_net": mom_rlc_net,
                "mom_rlc_resistance_ohm": mom_rlc_resistance_ohm,
                "mom_rlc_capacitance_ff": mom_rlc_capacitance_ff,
                "mom_rlc_inductance_nh": mom_rlc_inductance_nh,
            }.items()
            if value
        }
    )

    deck = get_extraction_deck(deck_name)
    with tempfile.TemporaryDirectory(prefix="klt-pex-correspondence-") as work:
        lvsdb_out = os.path.join(work, "runset.lvsdb")
        report = extract_runset.run_extract_klayout_runset(
            layout_path,
            deck_file=runset.deck_file,
            output=output,
            top=top,
            deck_vars=runset.deck_vars,
            device_models=device_models,
            device_params=runset.device_params,
            runset_timeout_s=runset.runset_timeout,
            lvsdb_out=lvsdb_out,
        )
        probes, positions = build_probes(
            lvsdb_out,
            layout_path,
            top=str(report["top"]),
            metals=list(deck.metals),
            dbu_um=float(report["dbu_um"]),
        )
        curated = run_extract(
            layout_path,
            deck_name,
            top=str(report["top"]),
            parasitics=True,
            probes=probes,
            label_layers=label_layers,
            no_netlist=True,
        )

    corr = correspondence(
        [str(entry["name"]) for entry in report["nets"]],
        curated.get("probes") or [],
        (curated.get("parasitics") or {}).get("nets") or [],
    )
    corr["deck"] = deck_name
    corr["attached_card_count"] = (
        merge_parasitics(
            report["netlist_path"],
            matched=corr["matched"],
            all_runset_nets=[str(entry["name"]) for entry in report["nets"]],
            parasitics=curated.get("parasitics") or {},
            deck_name=deck_name,
            report_block=corr,
        )
        if report.get("netlist_path")
        else 0
    )
    report["net_correspondence"] = corr
    if curated.get("parasitics"):
        report["parasitics"] = curated["parasitics"]
    if report.get("netlist_path"):
        from ._provenance import sha256_file

        report["netlist_sha256"] = sha256_file(report["netlist_path"])
    return report
