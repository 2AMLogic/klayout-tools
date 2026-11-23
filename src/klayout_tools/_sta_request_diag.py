"""Request-key and SPEF-diagnostic helpers split out of ``post_route_sta``.

``post_route_sta.py`` is under the per-file size ratchet (issue #2766), so
the ``klt sta`` known-request-key list (issue #5065), the SPEF net-name
mismatch hint, the SPEF missing-nets sample parser, and the marker-block
extractor every ``klt sta`` stdout parser shares (issue #6767) live here,
alongside the connectivity test shared by both SPEF correlation loops.
"""

from __future__ import annotations

#: Top-level keys ``run_sta`` reads (``schema`` is always allowed). Anything
#: else is rejected (issue #5065) rather than silently dropped.
KNOWN_REQUEST_FIELDS = (
    "def",
    "verilog",
    "hdl_toplevel",
    "pdk",
    "constraints",
    "spef",
    "sdf",
    "geometry_source",
    "macros",
    "sdc_check",
)

#: Appended to the "only N of M nets are named by this SPEF" reason.
SPEF_NAME_MISMATCH_HINT = (
    " (if this SPEF came from `klt extract`, re-run it with "
    "`--def-net-names` and `--def-pins <routed.def>` so net names "
    "match the linked design)"
)

#: Delimits the (capped) sample of design-side net names that failed to
#: correlate against the SPEF's own declared name set -- see
#: ``_spef_net_check_lines`` and :func:`parse_spef_missing_nets`.
SPEF_MISSING_NETS_BEGIN = "===KLT_STA_SPEF_MISSING_NETS_BEGIN==="
SPEF_MISSING_NETS_END = "===KLT_STA_SPEF_MISSING_NETS_END==="

#: Both STA flows exclude internal nets with no pins or just one output
#: pin from design-side SPEF coverage (#6970, #7367). OpenSTA omits some
#: physical terminals, including supply pins, from get_pins. Confirm empty
#: connectivity against OpenDB before dropping a zero-pin net. A port
#: keeps the net eligible. If connectivity cannot be queried, keep the net
#: in the counts and missing-name sample.
SPEF_SKIP_DRIVER_ONLY = (
    "    if {![catch {",
    "        set klt_pins [get_pins -quiet -of_objects $klt_design_net]",
    "        expr {(([llength $klt_pins] == 0",
    "                && [[sta::sta_to_db_net $klt_design_net] getTermCount] == 0)",
    "            || ([llength $klt_pins] == 1",
    '            && [get_property [lindex $klt_pins 0] direction] eq "output"))',
    "            && [llength [get_ports -quiet -of_objects $klt_design_net]] == 0}",
    "    } klt_no_load] && $klt_no_load} {continue}",
)


def extract_block(text: str, begin: str, end: str) -> str | None:
    """The text between the ``begin``/``end`` markers, or ``None`` when
    either marker is absent (an OpenROAD build that never reached that part
    of the script, a truncated run, or a caller parsing a stream the markers
    aren't in). Adjacent markers give ``""`` -- a present, empty block, which
    callers must keep distinct from a missing one. The first ``begin`` and
    the first ``end`` after it win when markers repeat."""
    try:
        start_idx = text.index(begin) + len(begin)
        stop_idx = text.index(end, start_idx)
    except ValueError:
        return None
    return text[start_idx:stop_idx]


def parse_spef_missing_nets(stdout: str) -> list[str]:
    """A capped sample of design-side net names that failed to correlate
    against the SPEF's own declared name set, parsed from
    ``_spef_net_check_lines``'s own
    ``===KLT_STA_SPEF_MISSING_NETS_BEGIN===``/``===END===``-delimited stdout
    block (one net name per line, already the design's real spelling --
    ``get_full_name``'s own output). Returns ``[]`` when the markers aren't
    found (defensive; should not happen for a successful run) or the block
    is empty (nothing missing)."""
    block = extract_block(stdout, SPEF_MISSING_NETS_BEGIN, SPEF_MISSING_NETS_END)
    if block is None:
        return []
    return [line.strip() for line in block.splitlines() if line.strip()]
