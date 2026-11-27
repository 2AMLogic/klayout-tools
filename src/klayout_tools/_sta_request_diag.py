"""Request-key and SPEF-diagnostic helpers split out of ``post_route_sta``.

``post_route_sta.py`` is under the per-file size ratchet (issue #2766), so
the ``klt sta`` known-request-key list (issue #5065), the SPEF net-name
mismatch hint, the SPEF missing-nets sample parser, and the marker-block
extractor every ``klt sta`` stdout parser shares (issue #6767) live here,
alongside the connectivity test shared by both SPEF correlation loops.
"""

from __future__ import annotations

from typing import Literal

from .post_route_sta_errors import PostRouteStaError

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

#: The flag that attaches a ``klt extract --spef`` SPEF's parasitics to pins:
#: without it the SPEF carries no instance ``*CONN`` and ``read_spef`` attaches
#: nothing to any pin (issue #7808). Shared by both hints below.
SPEF_CONNECTIONS_FLAG = "`--def-net-connections <routed.def>`"

#: The ``klt extract`` re-run recipe, valid on every deck (issue #7808). The
#: declared-pin source differs by deck: ``--def-pins`` is refused by name on a
#: FinFET deck (docs/cli/extract.md, "ASAP7 FinFET extraction"), where the same
#: DEF ``PINS`` names go through ``--pins``. ``klt sta`` links a DEF + SPEF and
#: cannot tell which deck produced the SPEF, so the text names both spellings
#: with their applicability rather than guessing one.
SPEF_EXTRACT_RECIPE = (
    f"`--def-net-names`, {SPEF_CONNECTIONS_FLAG} and the declared pins "
    "(`--def-pins <routed.def>` on a planar deck, or on a FinFET deck such as "
    "asap7, where `--def-pins` is refused, `--pins <A,B,...>` listing the "
    "routed DEF `PINS` section's names)"
)

#: Appended to the "only N of M nets are named by this SPEF" reason.
SPEF_NAME_MISMATCH_HINT = (
    " (if this SPEF came from `klt extract`, re-run it with "
    f"{SPEF_EXTRACT_RECIPE} so net names match the linked design and "
    "parasitics attach to pins)"
)

#: Appended to the "every timing path ... is byte-identical" reason: names
#: that never correlated are covered by the hint above; this one covers a SPEF
#: whose names all match but which has no instance ``*CONN``.
SPEF_NO_PIN_ATTACH_HINT = (
    " (a SPEF with no instance `*CONN` entries attaches nothing to any pin; "
    f"if this SPEF came from `klt extract`, re-run it with {SPEF_CONNECTIONS_FLAG})"
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


def validate_name_list(
    constraints: dict[str, object],
    key: str,
    *,
    list_kind: Literal["port", "input port", "instance"] = "port",
    clock_port: str | None = None,
    clock_reason: str = "",
) -> list[str] | None:
    """Validate literal STA names in request order and return a fresh list."""
    field = f"request.constraints.{key}"
    value = constraints.get(key)
    if value is None:
        return None
    if not isinstance(value, list):
        raise PostRouteStaError(f"{field} must be a list of {list_kind} names")
    name_kind, repeat_noun = (
        ("instance", "an instance") if list_kind == "instance" else ("port", "a port")
    )
    seen: set[str] = set()
    for name in value:
        if not isinstance(name, str) or not name or name != name.strip():
            raise PostRouteStaError(
                f"{field} entries must be non-empty {name_kind}-name strings "
                f"without leading/trailing whitespace (got {name!r})"
            )
        if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in name):
            raise PostRouteStaError(
                f"{field} entry {name!r} contains a control character"
            )
        if name == clock_port:
            raise PostRouteStaError(
                f"{field} must not name the clock port {name!r} -- {clock_reason}"
            )
        if name in seen:
            raise PostRouteStaError(f"{field} must not repeat {repeat_noun} ({name!r})")
        seen.add(name)
    return list(value) or None
