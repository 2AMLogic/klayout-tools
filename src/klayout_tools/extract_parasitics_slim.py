"""Slim the finished ``parasitics`` report block (issue #5816).

``klt extract --report-parasitics slim --spef ...``: when the SPEF written
beside the JSON already carries the per-net RC network and every coupling
capacitor value, most of ``parasitics.nets[]`` is either a less-compact
re-statement of the SPEF (the per-net star/ladder topology, ``coupled[]``'s
per-side copy of each coupling capacitor) or one constant repeated per net
(the lumped model's vacuous ``integrity`` sub-keys, the empty
``terminals``/``segments``/``pin_order``, ``hub_net``'s node spelling the
SPEF's own ``*D_NET`` blocks already encode). The slim shape drops exactly
that, keeps everything the SPEF lacks (``by_layer``, per-net
``integrity.capacitance``, the scalars, any populated distributed-ladder
field) and adds one global ``coupling_by_layer_pair`` histogram so the
``coupled[].levels`` layer-pair distribution -- the one ``coupled`` datum
the SPEF does not carry -- survives in a few hundred bytes.

Pure function over the already-built report: the SPEF writer and
``_build_metrics`` both consume the **full** block before this runs
(``extract_assemble.py`` applies it last), so slimming corrupts neither.
The default (``full``) never calls into this module and stays byte-identical
to the pre-#5816 output.
"""

from __future__ import annotations

from typing import Any

#: The value ``parasitics.report_shape`` carries on a slimmed report. A
#: ``full`` report carries no ``report_shape`` key at all -- its shape is
#: the documented default, unchanged since before this flag existed.
SLIM_REPORT_SHAPE = "slim"

#: The per-net ``integrity`` sub-keys slim mode drops when they carry no
#: finding. ``capacitance`` is never in this set: the net's own
#: declared-vs-emitted reconciliation is per-net data the SPEF lacks.
_CONSTANT_INTEGRITY_SUBKEYS = (
    "open",
    "islands",
    "orphan_nodes",
    "suspicious_resistors",
    "short_candidates",
)

#: ``parasitics.nets[]`` fields the SPEF beside the report carries verbatim
#: (its ``*D_NET``/``*CONN``/``*CAP``/``*RES`` rows are a format translation
#: of the same per-net model), or the SPEF's own node naming covers
#: (``hub_net``). Dropped on every net entry in slim mode.
_SPEF_CARRIED_NET_FIELDS = (
    "coupled",
    "hub_net",
)


def _integrity_subkey_is_constant(key: str, block: Any) -> bool:
    """Whether one per-net ``integrity`` sub-key carries no finding and no
    per-net detail worth keeping: the vacuous lumped-model constants.

    - ``open``: the star model's ``not_applicable`` verdict (a distributed
      net's real ``pass``/``open`` check stays).
    - every other key here: the zero-finding ``{count: 0, details: []}``
      shape (a net with an actual island/orphan/suspicious/short finding
      keeps its block, details included -- the top-level summary counts the
      finding but carries no detail of its own).
    """
    if not isinstance(block, dict):
        return False
    if key == "open":
        return block.get("status") == "not_applicable"
    return block.get("count", 1) == 0 and block.get("details", None) == []


def _slim_integrity(integrity: dict[str, Any]) -> dict[str, Any]:
    """Drop the constant per-net ``integrity`` sub-keys, keep the rest."""
    slimmed = dict(integrity)
    for key in _CONSTANT_INTEGRITY_SUBKEYS:
        if key in slimmed and _integrity_subkey_is_constant(key, slimmed[key]):
            del slimmed[key]
    return slimmed


def coupling_by_layer_pair_histogram(nets: list[dict[str, Any]]) -> dict[str, Any]:
    """The global ``coupling_by_layer_pair`` histogram over ``nets[]``'
    ``coupled[].levels`` -- layer pair -> ``{count, capacitance_ff}``.

    Each ``parasitics.nets[]`` entry's ``coupled[]`` lists every coupling
    capacitor **from that net's side**, so a pair appears twice across
    ``nets[]`` with identical values. Deduplicating on the unordered net
    pair (plus value) counts each capacitor exactly once -- the histogram's
    counts sum back to ``cc_count`` and its sums to
    ``total_coupling_capacitance_ff``. A capacitor spanning several layer
    pairs contributes to each pair it spans (a capacitor's ``levels`` is the
    set of ``[lower_metal_index, upper_metal_index]`` deck-``metals`` pairs
    whose overlap built it), so per-pair counts count capacitors, not fF.
    """
    histogram: dict[str, dict[str, Any]] = {}
    seen_pairs: set[tuple[Any, ...]] = set()
    for entry in nets:
        for coupled in entry.get("coupled") or ():
            pair_key = (
                tuple(sorted((entry["net"], coupled["net"]))),
                coupled["capacitance_ff"],
            )
            if pair_key in seen_pairs:
                continue
            seen_pairs.add(pair_key)
            for lo, hi in coupled.get("levels") or ():
                bucket = histogram.setdefault(
                    f"{lo}/{hi}", {"count": 0, "capacitance_ff": 0.0}
                )
                bucket["count"] += 1
                bucket["capacitance_ff"] += coupled["capacitance_ff"]
    for bucket in histogram.values():
        bucket["capacitance_ff"] = round(bucket["capacitance_ff"], 6)
    return histogram


def _slim_net_entry(entry: dict[str, Any]) -> dict[str, Any]:
    """One ``parasitics.nets[]`` entry, slimmed.

    Copy-then-drop: unknown/additive keys (e.g. ``--resistance-graph``'s
    per-net ``resistance_graph`` block, which no SPEF carries) survive by
    default -- slim drops only the named SPEF-carried and empty/constant
    fields, never the catch-all rest.
    """
    slimmed = dict(entry)
    for key in _SPEF_CARRIED_NET_FIELDS:
        slimmed.pop(key, None)
    # Empty/constant model-shape fields: the distributed ladder's populated
    # ``terminals``/``segments``/``pin_order`` are per-net data no SPEF row
    # spells out, so only the empty lumped-model forms go.
    if not slimmed.get("terminals"):
        slimmed.pop("terminals", None)
    if not slimmed.get("segments"):
        slimmed.pop("segments", None)
    if slimmed.get("pin_order") is None:
        slimmed.pop("pin_order", None)
    integrity = slimmed.get("integrity")
    if isinstance(integrity, dict):
        slimmed["integrity"] = _slim_integrity(integrity)
    return slimmed


def slim_parasitics_report(parasitics: dict[str, Any]) -> dict[str, Any]:
    """The slim shape of a finished ``parasitics`` block (issue #5816).

    Returns a new top-level dict; the input (and every nested object on it
    that survives verbatim) is shared, not copied -- the caller drops its
    own reference to the full block when it swaps this one in. See the
    module docstring for what is dropped and why.
    """
    slimmed = dict(parasitics)
    slimmed["nets"] = [_slim_net_entry(entry) for entry in slimmed.get("nets") or ()]
    slimmed["coupling_by_layer_pair"] = coupling_by_layer_pair_histogram(
        parasitics.get("nets") or []
    )
    slimmed["report_shape"] = SLIM_REPORT_SHAPE
    return slimmed
