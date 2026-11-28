"""Issue #6075's zero-delay ``INTERCONNECT`` drops for bit-selected
*internal* nets: the shared gate-level module lookup
(:func:`_find_gate_level_module`, from ``functional_verification_sdf_instance``),
the netlist-side pin map
(:func:`_collect_sdf_internal_bus_net_pins`), the per-entry predicate
(:func:`_sdf_interconnect_is_zero_delay_internal_bus_net`), the SDF-text
drop pass (:func:`_drop_sdf_zero_delay_internal_bus_net_interconnects`),
the pipeline step (:func:`_apply_sdf_internal_bus_net_drop`), and the
``environment.sdf.dropped`` class they count under
(:data:`SDF_INTERNAL_BUS_NET_DROPPED_CLASS`, whose reason is carried in
:data:`SDF_INTERNAL_BUS_NET_REASONS` and merged into
``functional_verification.SDF_BENIGN_DIAGNOSTIC_REASONS`` the same way
``functional_verification_sdf_steps`` merges its own).

Its own module because the per-file size ratchet
(``scripts/check_file_size_baseline.py``) lets
``functional_verification.py`` and ``functional_verification_sdf.py``
only shrink -- the same constraint ``functional_verification_sdf_steps``
(issue #5296) was split out under. The shared SDF-text helpers are
imported from ``functional_verification_sdf``; nothing here is imported
back by it, so the dependency stays one-directional.
"""

from __future__ import annotations

import re

from .functional_verification_sdf import (
    _SDF_INTERCONNECT_HEAD_RE,
    _drop_sdf_interconnect_entries,
    _read_sdf_text,
    _read_verilog_source_texts,
    _sdf_interconnect_is_zero_delay,
)
from .functional_verification_sdf_instance import _find_gate_level_module
from .functional_verification_sdf_rename import _sdf_divider
from .functional_verification_sdf_steps import _write_sdf_drop_step

# --------------------------------------------------------------------------- #
# Issue #6075: the one-modpath-per-bit-selected-net cap recorded for #1619
# shape (b) is NOT confined to top-level port nets. Measured live on raw
# iverilog/vvp 13.0 (2026-11-17, this repo's own fixture -- spike §3.7's
# dated addendum carries the full matrix): when the connecting net is a
# bit-select of an INTERNAL wire, *every* `INTERCONNECT` entry on that net
# fails -- `VPI error: Could not insert intermodpath!` per entry, then
# `SDF ERROR: ... Could not find intermodpath!` -- at fanout 1 as much as
# fanout 4, at non-zero delay as much as zero (0-of-N for every N and every
# delay population; a scalar internal net at the same fanout annotates
# cleanly, and a top-level port bit keeps #5705's last-declared-survivor
# N-1-of-N rule). The escape does not matter: a plain-name internal bus
# (`wire [3:0] nb; ... .Y(nb[2])`) fails identically to an escaped one
# (`wire [3:0] \soc_bus ; ... .Y(\soc_bus [3])`).
#
# The entries' SDF text carries no trace of this: an entry between two
# instance pins (`u_src.Y u_l1.A`) is byte-identical whether the connecting
# net is a bit-select (fails) or a scalar (annotates cleanly) -- so unlike
# every sibling class, the decision cannot be made from the SDF text
# alone. It is made structurally, from the same gate-level parse the run
# already compiles (the
# ``klayout_tools.verilog_netlist.parse_gate_level_verilog`` lookup
# :func:`functional_verification_sdf._find_gate_level_instances` performs):
# an entry is eligible only when BOTH endpoints are single-level
# `<instance><divider><pin>` pairs whose parsed connections resolve to the
# *same* bit-select expression of a wire that is not a top-level port.
# Every ambiguity (a bare port endpoint, a deeper hierarchical path,
# endpoints the parse cannot resolve, endpoints resolving to different
# nets) resolves toward *not* dropping -- the entry stays in place and
# fails loudly.
#
# The exemption carries the same zero-delay bound verbatim as #2285, #2363
# and #2984: an entry is dropped only when *every* delay member it carries
# is zero, so dropping it cannot change any simulated timing at any corner.
# A NON-zero entry on the identical net is left in place and still fails
# (measured: it does), keeping the #5769 boundary untouched.
# --------------------------------------------------------------------------- #

#: ``INTERCONNECT`` entries removed before annotation because both of their
#: endpoints are instance pins whose parsed netlist connections resolve to
#: the same bit-select of an *internal* wire (a wire that is not a
#: top-level port) and carry zero delay at every corner. Measured live on
#: Icarus 13.0 (issue #6075; spike §3.7's dated addendum): Icarus fails to
#: insert an intermodpath for *every* entry on such a net -- 0-of-N at any
#: fanout and any delay population, unlike a top-level port bit's
#: last-declared-survivor rule (#5705) -- and the SDF text cannot tell the
#: failing entries from clean ones (``inst.pin`` endpoints are identical),
#: so the decision is made from the same gate-level parse the run already
#: compiles. An all-zero entry models no delay, so dropping it cannot
#: change simulated timing; a non-zero entry on the identical net is left
#: in place and still fails loudly (the #5769 boundary). Pre-dropped from
#: the SDF text, not derived from a diagnostic substring.
SDF_INTERNAL_BUS_NET_DROPPED_CLASS = "zero_delay_internal_bus_net_interconnect"

#: ``SDF_BENIGN_DIAGNOSTIC_REASONS`` entry for the class above, merged into
#: that dict by ``functional_verification``.
SDF_INTERNAL_BUS_NET_REASONS: dict[str, str] = {
    SDF_INTERNAL_BUS_NET_DROPPED_CLASS: (
        "Icarus Verilog fails to insert an intermodpath for EVERY "
        "INTERCONNECT entry that runs across a bit-select of an internal "
        "wire -- both endpoints are ordinary instance pins, and the entry's "
        "text is byte-identical to an entry on a scalar net that annotates "
        "cleanly, so the decision is made from the netlist the run already "
        "compiles (measured live on Icarus 13.0, issue #6075: 0-of-N "
        "entries applied at any fanout, unlike a top-level port bit's "
        "last-declared-survivor rule). Entries whose every min:typ:max "
        "delay value is zero model no delay at any corner, so they are "
        "removed before annotation and counted here instead of failing the "
        "run -- a non-zero-delay entry on the same net is left in place and "
        "still fails loudly"
    ),
}

#: A connection value that is a single-bit select of a declared net --
#: ``soc_bus[3]`` -- the shape :func:`klayout_tools.verilog_netlist.
#: parse_gate_level_verilog` emits for an instance pin connected to one bit
#: of a bus (``.Y(\\soc_bus [3])`` and ``.Y(soc_bus[3])`` both resolve to
#: this spelling; the escaped-identifier whitespace is the parser's own
#: normalization).
_SDF_BIT_SELECT_CONN_RE = re.compile(r"^(.+)\[(\d+)\]$")


def _collect_sdf_internal_bus_net_pins(
    source_paths: list[str], hdl_toplevel: str
) -> dict[str, str]:
    """Map every ``<instance>.<port>`` pin of ``hdl_toplevel``'s own
    instances whose parsed connection is a bit-select of an *internal* wire
    (a wire whose base name is not one of ``hdl_toplevel``'s ports) to that
    bit-select expression -- ``{"u_src.Y": "soc_bus[3]", ...}`` (issue
    #6075).

    This is the netlist-side half of
    :func:`_drop_sdf_zero_delay_internal_bus_net_interconnects`'s decision:
    an ``INTERCONNECT`` entry between two such pins that resolve to the
    *same* expression runs across exactly the net fragment Icarus cannot
    insert an intermodpath for. The SDF text cannot carry this -- its
    ``inst.pin`` endpoints are byte-identical for a bit-selected and a
    scalar connecting net -- so the decision is structural, from the same
    non-ANSI gate-level parse
    :func:`functional_verification_sdf._collect_sdf_physical_only_instances`
    already reuses (:func:`_find_gate_level_module`).

    Returns an empty map -- never raises -- whenever the module description
    cannot be derived (see :func:`_find_gate_level_module`), which makes
    the drop pass a no-op: a *normalization* pass must never be worse than
    not having tried. A scalar connection, a whole-bus (list) connection,
    and a bit-select whose base name IS a port are all excluded -- the
    measured failing shape is a single bit of an internal wire, and every
    other shape either annotates cleanly (scalar) or is another class's
    territory (a port bit is #2984's source shape, #2285's destination
    shape).
    """
    from .verilog_netlist import parse_gate_level_verilog

    module = _find_gate_level_module(
        _read_verilog_source_texts(source_paths),
        hdl_toplevel,
        parse_module=parse_gate_level_verilog,
    )
    if module is None:
        return {}
    port_names = {re.sub(r"\[\d+\]$", "", str(name)) for name in module["ports"]}
    pins: dict[str, str] = {}
    for instance in module["instances"]:
        inst = str(instance["name"])
        for port, conn in instance["connections"].items():
            if not isinstance(conn, str):
                continue
            bit_match = _SDF_BIT_SELECT_CONN_RE.match(conn)
            if bit_match is None or bit_match.group(1) in port_names:
                continue
            pins[f"{inst}.{port}"] = conn
    return pins


def _sdf_interconnect_is_zero_delay_internal_bus_net(
    entry_text: str, internal_bus_net_pins: dict[str, str], divider: str
) -> bool:
    """``True`` when ``entry_text`` is an ``INTERCONNECT`` entry whose
    *source* and *destination* are both single-level
    ``<instance><divider><pin>`` pairs that
    :func:`_collect_sdf_internal_bus_net_pins` resolved to the *same*
    bit-select of an internal wire, and whose every delay member is zero --
    the condition
    :func:`_drop_sdf_zero_delay_internal_bus_net_interconnects` removes an
    entry on (issue #6075).

    Split endpoints on the SDF file's declared ``divider``, then use
    the netlist pin map's ``instance.pin`` keys. A bare top-level-port
    endpoint (no divider), a deeper hierarchical path, or endpoints outside
    the map (or mapping to different nets) leaves the entry in place,
    whatever its delays.
    """
    head = _SDF_INTERCONNECT_HEAD_RE.match(entry_text)
    if head is None:
        return False
    endpoints = []
    for token in (head.group(1), head.group(2)):
        instance, separator, pin = token.rpartition(divider)
        if not separator or divider in instance:
            return False
        endpoint = internal_bus_net_pins.get(f"{instance}.{pin}")
        if endpoint is None:
            return False
        endpoints.append(endpoint)
    return endpoints[0] == endpoints[1] and _sdf_interconnect_is_zero_delay(entry_text)


def _drop_sdf_zero_delay_internal_bus_net_interconnects(
    sdf_path: str, internal_bus_net_pins: dict[str, str]
) -> tuple[str, int] | None:
    """Remove every zero-delay ``INTERCONNECT`` entry in ``sdf_path`` whose
    source and destination pins :func:`_collect_sdf_internal_bus_net_pins`
    resolve to the same bit-select of an internal wire, returning ``(text
    with those entries removed, how many were removed)`` -- or ``None``
    when there is nothing to remove, in which case the caller keeps handing
    ``sdf_path`` itself to ``$sdf_annotate`` unchanged.

    Meant to run *after* the #2363 and #2984 passes, whose predicates key on
    a bit-selected top-level *port* source and can never claim an entry
    between two instance pins -- the classes stay attributable by
    construction, and no entry is counted twice.

    An empty map makes this a no-op that returns ``None`` without reading
    the file -- the overwhelmingly common case (no internal bus net in the
    netlist), and a real post-route SDF can be large enough that this
    pass's whole cost should be paid only when it can possibly matter.
    """
    if not internal_bus_net_pins:
        return None
    text = _read_sdf_text(sdf_path)
    if text is None:
        return None
    divider = _sdf_divider(text)

    return _drop_sdf_interconnect_entries(
        text,
        lambda entry_text: _sdf_interconnect_is_zero_delay_internal_bus_net(
            entry_text, internal_bus_net_pins, divider
        ),
    )


def _apply_sdf_internal_bus_net_drop(
    annotate_source_path: str,
    output_dir: str,
    sources: list[str],
    hdl_toplevel: str,
    sdf_pre_dropped_counts: dict[str, int],
) -> str:
    """Issue #6075's zero-delay internal-bit-select-net SDF-text drop,
    applied as one step of
    :func:`functional_verification.run_functional_verification`'s
    SDF-normalization pipeline (the sibling of the #2984 and #2363 steps).
    Runs after the physical-only and bus-port-source passes -- neither of
    whose predicates can ever claim an entry between two instance pins --
    so every entry is counted under exactly one class. Counts under
    :data:`SDF_INTERNAL_BUS_NET_DROPPED_CLASS`.

    The decision is structural (:func:`_collect_sdf_internal_bus_net_pins`,
    over the same ``sources``/``hdl_toplevel`` parse the physical-only pass
    already reads): Icarus fails to insert an intermodpath for *every*
    ``INTERCONNECT`` entry crossing a bit-select of an internal wire --
    measured 0-of-N at any fanout and any delay population -- and the SDF
    text alone cannot tell those entries from clean ones.

    Returns ``annotate_source_path`` unchanged when nothing was dropped, or
    a new normalized-copy path otherwise.
    """
    return _write_sdf_drop_step(
        _drop_sdf_zero_delay_internal_bus_net_interconnects(
            annotate_source_path,
            _collect_sdf_internal_bus_net_pins(sources, hdl_toplevel),
        ),
        annotate_source_path,
        output_dir,
        "klt_sdf_internal_bus_net_dropped.sdf",
        SDF_INTERNAL_BUS_NET_DROPPED_CLASS,
        sdf_pre_dropped_counts,
    )
