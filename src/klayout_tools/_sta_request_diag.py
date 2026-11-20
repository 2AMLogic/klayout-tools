"""Request-key and SPEF-diagnostic helpers split out of ``post_route_sta``.

``post_route_sta.py`` is under the per-file size ratchet (issue #2766), so
the ``klt sta`` known-request-key list (issue #5065), the SPEF net-name
mismatch hint, the SPEF missing-nets sample parser, and the marker-block
extractor every ``klt sta`` stdout parser shares (issue #6767) live here.
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
