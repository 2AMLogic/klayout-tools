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
from collections import defaultdict
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from ._provenance import sha256_file
from .extract import ExtractError, run_extract
from .extract_netlist_target import resolve_netlist_target
from .extract_preflight import preflight_layout
from .pex_errors import PexError
from .pex_merged_pins import bind_declared_ports, replace_port_header
from .pex_net_correspondence import (
    build_probes,
    correspondence,
    preferred_renames,
)

#: The leg header line the merged writer replaces when it attaches cards.
_HEADER_NO_PARASITICS = (
    "* no parasitics: this engine extracts devices and connectivity only\n"
)


def _format_number(value: float) -> str:
    """SPICE number formatting in the curated writer's own style
    (``0.055``, ``8.738e-17``)."""
    return f"{value:.6g}"


def _fresh_node(preferred: str, used: set[str]) -> str:
    """Reserve a node in SPICE's case-insensitive namespace."""
    node = preferred
    index = 1
    while node.casefold() in used:
        node = f"{preferred}_{index}"
        index += 1
    used.add(node.casefold())
    return node


def _substrate_return(
    substrate: str,
    curated_nets: Sequence[Mapping[str, Any]],
    parasitics: Mapping[str, Any],
    matched: Sequence[Mapping[str, Any]],
    merged_name: Mapping[int, str],
    used: set[str],
) -> dict[str, Any] | None:
    tie = parasitics.get("substrate_dc_tie") or {}
    if not any(entry["net"] == substrate for entry in tie.get("nets", [])):
        return None
    clusters = [entry["net_id"] for entry in curated_nets if entry["name"] == substrate]
    matches = {m["cluster_id"]: m["runset"] for m in matched}
    cluster = clusters[0] if len(clusters) == 1 else None
    runset = matches.get(cluster)
    return {
        "curated": substrate,
        "runset": runset,
        "node": merged_name[cluster]
        if cluster is not None and runset is not None
        else _fresh_node(substrate, used),
        "tie_resistance_ohm": tie["resistance_ohm"],
    }


def _coupling_cards(
    parasitic_nets: Sequence[Mapping[str, Any]],
    hubs: Mapping[int, str],
    nodes: Mapping[int, str],
) -> tuple[list[str], list[dict[str, Any]]]:
    """Coupling rows name labels only. Both ends must resolve uniquely across
    the full curated report, including islands outside the matched set."""
    ids: dict[str, list[int]] = defaultdict(list)
    pairs: dict[tuple[str, str], float] = {}
    for entry in parasitic_nets:
        name = entry["net"]
        ids[name].append(entry["net_id"])
        for other in entry.get("coupled", []):
            a, b = sorted((name, other["net"]))
            pairs[a, b] = float(other["capacitance_ff"])

    cards = []
    omitted = []
    for (a, b), capacitance in sorted(pairs.items()):
        if len(ids[a]) != 1 or len(ids[b]) != 1:
            reason = "ambiguous_label"
        elif ids[a][0] not in hubs or ids[b][0] not in hubs:
            reason = "unmatched_endpoint"
        else:
            i, j = ids[a][0], ids[b][0]
            cards.append(
                f"C{nodes[i]}_{nodes[j]}_plcc {hubs[i]} {hubs[j]} "
                f"{_format_number(capacitance * 1e-15)}"
            )
            continue
        omitted.append({"curated_nets": [a, b], "reason": reason})
    return cards, omitted


def merge_parasitics(
    netlist_path: str,
    *,
    top: str,
    all_runset_nets: Sequence[str],
    curated_nets: Sequence[Mapping[str, Any]],
    substrate: str,
    parasitics: Mapping[str, Any],
    deck_name: str,
    report_block: dict[str, Any],
    renames: dict[str, str] | None = None,
) -> int:
    """Attach the per-cluster lumped network to the selected runset circuit."""
    matched = report_block["matched"]
    if renames is None:
        renames = preferred_renames(matched, all_runset_nets)
    path = Path(netlist_path)
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    nodes = {m["cluster_id"]: renames.get(m["runset"], m["runset"]) for m in matched}
    # A fresh global return must not globalize an existing node in any circuit.
    used = {
        token.casefold()
        for line in lines
        if not line.lstrip().startswith("*")
        for token in line.split()
    }
    used.update(net.casefold() for net in (*all_runset_nets, *nodes.values()))
    ground = _substrate_return(
        substrate, curated_nets, parasitics, matched, nodes, used
    )
    cards: list[str] = []
    hubs: dict[int, str] = {}
    wrote_ground_cap = False
    parasitic_nets = parasitics.get("nets") or []
    for entry in parasitic_nets:
        cluster = entry["net_id"]
        if cluster not in nodes:
            continue
        node = nodes[cluster]
        hub = node
        resistance = float(entry["resistance_ohm"])
        capacitance = float(entry["capacitance_ff"])
        if resistance > 0.0:
            hub = _fresh_node(f"{node}__kltph", used)
            cards.append(f"R{node}_plr {node} {hub} {_format_number(resistance)}")
        hubs[cluster] = hub
        if capacitance > 0.0 and ground:
            wrote_ground_cap = True
            cards.append(
                f"C{node}_plc {hub} {ground['node']} "
                f"{_format_number(capacitance * 1e-15)}"
            )
    coupled, omitted = _coupling_cards(parasitic_nets, hubs, nodes)
    cards.extend(coupled)
    report_block["omitted_couplings"] = omitted
    global_node = None
    if ground and wrote_ground_cap:
        node = ground["node"]
        if ground["runset"] is None:
            global_node = node
        cards.append(
            f"R{node}_dctie {node} 0 {_format_number(ground['tie_resistance_ohm'])}"
        )
        report_block["substrate_return"] = ground
    if cards or renames or "declared_ports" in report_block:
        _rewrite_netlist(
            path, lines, top, renames, cards, global_node, deck_name, report_block
        )
    return len(cards)


def _circuit_bounds(lines: Sequence[str], top: str) -> tuple[int, int]:
    start = None
    for i, line in enumerate(lines):
        words = line.split()
        if not words:
            continue
        if words[0].upper() == ".SUBCKT":
            if start is not None:
                break
            if len(words) > 1 and words[1].casefold() == top.casefold():
                start = i
        elif words[0].upper() == ".ENDS" and start is not None:
            return start, i
    raise PexError(f"merged leg: missing complete .SUBCKT block for '{top}'")


def _rename_nodes(card: str, renames: Mapping[str, str]) -> str:
    """Rewrite node tokens on a KLayout header or device card, across continuations."""
    tokens = [m for m in re.finditer(r"\S+", card) if m[0] != "+"]
    if not tokens or tokens[0][0].startswith("*"):
        return card
    start = 1
    end = next((i for i, token in enumerate(tokens) if "=" in token[0]), len(tokens))
    if tokens[0][0].upper() == ".SUBCKT":
        start = 2  # skip the circuit name
    elif tokens[0][0][0].upper() == "X":
        end -= 1  # the final positional argument is the called circuit
    elif tokens[0][0].startswith("."):
        return card
    chunks: list[str] = []
    offset = 0
    for token in tokens[start:end]:
        chunks.extend((card[offset : token.start()], renames.get(token[0], token[0])))
        offset = token.end()
    return "".join(chunks) + card[offset:]


def _rewrite_netlist(
    path: Path,
    lines: list[str],
    top: str,
    renames: Mapping[str, str],
    cards: Sequence[str],
    global_node: str | None,
    deck_name: str,
    report: Mapping[str, Any],
) -> None:
    """Preserve other circuits byte-for-byte and insert cards at this circuit's end."""
    start, end = _circuit_bounds(lines, top)
    i = start
    while i < end:
        following = i + 1
        while following < end and lines[following].lstrip().startswith("+"):
            following += 1
        lines[i:following] = _rename_nodes(
            "".join(lines[i:following]), renames
        ).splitlines(keepends=True)
        i = following
    lines[end:end] = [f"{card}\n" for card in cards]
    if "declared_ports" in report:
        replace_port_header(lines, start, top, report["declared_ports"])
    if global_node:
        lines.insert(start, f".GLOBAL {global_node}\n")
    if cards:
        header = (
            f"* parasitics attached from curated deck '{deck_name}' via "
            f"geometric net correspondence (issue #6606): {len(report['matched'])} "
            f"net(s) matched, {len(report['unmatched_runset_nets'])} unmatched "
            "runset net(s), "
            f"{len(report['disagreements'])} disagreement(s) -- see the report's "
            "extraction.net_correspondence\n"
        )
        if _HEADER_NO_PARASITICS in lines:
            lines[lines.index(_HEADER_NO_PARASITICS)] = header
        else:
            lines.insert(0, header)
    path.write_text("".join(lines), encoding="utf-8")


# --------------------------------------------------------------------------- #
# The merged-route driver
# --------------------------------------------------------------------------- #


@contextmanager
def _staged_netlist(target: str) -> Iterator[Path]:
    """Publish the finished leg by atomic rename after the merge."""
    try:
        with tempfile.TemporaryDirectory(
            prefix=".klt-pex-", dir=Path(target).parent
        ) as work:
            staged = Path(work) / "extracted.spice"
            yield staged
            os.replace(staged, target)
    except OSError as exc:
        raise ExtractError(f"cannot write merged netlist '{target}': {exc}") from exc


def merged_runset_extract(
    *,
    layout_path: str,
    output: str | None,
    top: str | None,
    runset: Any,
    deck_name: str,
    pdk_variant: str | None,
    pdk_root: str | None,
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
    resistance_graph_caps: Mapping[str, int] | None,
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
    ``net_correspondence`` block, the curated model name, and the final netlist hash.
    Options requiring a richer parasitic model are refused by name here,
    from the same table as ``klt extract``, apart from the options
    the merged route supports. The curated leg selects port names and the
    geometric correspondence binds their physical clusters to runset nodes."""
    from . import extract_runset
    from .extract_preflight import require_declared_set_for_rename

    require_declared_set_for_rename(rename_declared_pins, declared_pins, None)
    extract_runset.refuse_unsupported(
        {
            "pdk_variant": pdk_variant,
            "pdk_root": pdk_root,
            "deck_options": deck_options,
            "tie_declared_pins": tie_declared_pins,
            "critical_nets": critical_nets,
            "critical_net_patterns": critical_net_patterns,
            "critical_net_all": critical_net_all,
            "critical_net_excludes": critical_net_excludes,
            "distributed_rc": distributed_rc,
            "resistance_graph": resistance_graph,
            "resistance_graph_r_cut_ohm": resistance_graph_r_cut_ohm,
            "resistance_graph_caps": resistance_graph_caps,
            "parasitic_variants": parasitic_variants,
            "substrate_tie_ohms": substrate_tie_ohms,
            "mom_net": mom_rlc_net,
            "mom_rlc_resistance_ohm": mom_rlc_resistance_ohm,
            "mom_rlc_capacitance_ff": mom_rlc_capacitance_ff,
            "mom_rlc_inductance_nh": mom_rlc_inductance_nh,
        }
    )

    source = preflight_layout(
        layout_path, deck_name, top=top, label_layers=label_layers
    )
    target, _ = resolve_netlist_target(layout_path, output, False)
    with _staged_netlist(target) as staged:
        lvsdb_out = str(staged.with_name("runset.lvsdb"))
        report = extract_runset.run_extract_klayout_runset(
            layout_path,
            deck_file=runset.deck_file,
            output=str(staged),
            top=top,
            deck_vars=runset.deck_vars,
            device_models=device_models,
            device_params=runset.device_params,
            runset_timeout_s=runset.runset_timeout,
            lvsdb_out=lvsdb_out,
        )
        probes, incomplete = build_probes(
            lvsdb_out,
            source.top_cell,
            metals=source.deck.metals,
            contact=source.deck.contact,
        )
        curated = run_extract(
            layout_path,
            deck_name,
            top=str(report["top"]),
            parasitics=True,
            probes=probes,
            label_layers=label_layers,
            declared_pins=declared_pins,
            rename_declared_pins=rename_declared_pins,
            no_netlist=True,
        )

        parasitics = curated.get("parasitics") or {}
        corr = correspondence(
            report["nets"],
            curated.get("probes") or [],
            parasitics.get("nets") or [],
            incomplete,
        )
        corr["deck"] = deck_name
        if "runset_node_renames" in report:
            corr["runset_node_renames"] = report.pop("runset_node_renames")
        runset_nets = [entry["name"] for entry in report["nets"]]
        renames = None
        if declared_pins is not None:
            report["rename_declared_pins"] = curated["rename_declared_pins"]
            renames = bind_declared_ports(
                staged, declared_pins, curated["nets"], corr, report, runset_nets
            )
        corr["attached_card_count"] = merge_parasitics(
            str(staged),
            top=str(report["top"]),
            curated_nets=curated["nets"],
            substrate=source.deck.substrate_net,
            all_runset_nets=runset_nets,
            parasitics=parasitics,
            deck_name=deck_name,
            report_block=corr,
            renames=renames,
        )
        report["label_layers"] = curated["label_layers"]
        report["net_correspondence"] = corr
        # The curated integrity rows describe its own star, while this writer
        # emits a lumped network over just the matched clusters.
        report["parasitics"] = {"model": parasitics.get("model")}
        report["netlist_sha256"] = sha256_file(str(staged))
        report["netlist_path"] = target
    return report
