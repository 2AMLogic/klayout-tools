"""Request-key and SPEF-diagnostic helpers split out of ``post_route_sta``.

``post_route_sta.py`` is under the per-file size ratchet (issue #2766), so
the ``klt sta`` known-request-key list (issue #5065), the SPEF net-name
mismatch hint, and the SPEF missing-nets sample parser live here.
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


def parse_spef_missing_nets(stdout: str) -> list[str]:
    """A capped sample of design-side net names that failed to correlate
    against the SPEF's own declared name set, parsed from
    ``_spef_net_check_lines``'s own
    ``===KLT_STA_SPEF_MISSING_NETS_BEGIN===``/``===END===``-delimited stdout
    block (one net name per line, already the design's real spelling --
    ``get_full_name``'s own output). Returns ``[]`` when the markers aren't
    found (defensive; should not happen for a successful run) or the block
    is empty (nothing missing)."""
    try:
        start_idx = stdout.index(SPEF_MISSING_NETS_BEGIN) + len(SPEF_MISSING_NETS_BEGIN)
        stop_idx = stdout.index(SPEF_MISSING_NETS_END, start_idx)
    except ValueError:
        return []
    block = stdout[start_idx:stop_idx]
    return [line.strip() for line in block.splitlines() if line.strip()]
