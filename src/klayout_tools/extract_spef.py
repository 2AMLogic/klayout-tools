"""SPEF (Standard Parasitic Exchange Format) export for ``klt extract --spef``.

Split out of ``extract.py`` (issue #1195) as a self-contained subsystem: the
SPEF identifier grammar (:func:`_spef_name`, :func:`_spef_port_node_name`),
the targeted DEF ``NETS``-section scanner that supplies real cell-instance
connectivity (:func:`def_net_instance_pins`), the per-``*D_NET`` RC-star node
planner (:func:`_spef_net_topology_nodes`), and the writer itself
(:func:`_write_spef`). Every function here takes its inputs as explicit
parameters -- nothing closes over ``extract.py``'s module state -- so this is
a pure format translation of an already-computed ``_inject_parasitics``
summary (issue #948, Epic #700 Phase 3; survey doc:
``docs/design/post-route-sta-survey.md`` sections 3.1/4.1), not a second
extraction pass.

:class:`ExtractError` lives here rather than in ``extract.py`` purely to keep
the dependency one-directional: :func:`_write_spef` raises it, and
``extract.py`` re-exports it (``from klayout_tools.extract_spef import
ExtractError``) so every existing ``from klayout_tools.extract import
ExtractError`` / ``except ExtractError`` call site keeps working unchanged.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime, timezone
from typing import Any


class ExtractError(Exception):
    """Raised when a layout cannot be extracted: bad file, unknown deck,
    unresolvable PDK, missing/ambiguous top cell, or an engine error.

    The CLI turns this into a clean stderr message + exit code 1, never a
    traceback.
    """


#: Standard Parasitic Exchange Format version this writer declares in its
#: ``*SPEF`` header line (``klt extract --spef``, issue #948, Epic #700
#: Phase 3). IEEE 1481-1999 is the SPEF grammar version this writer's
#: ``*D_NET``/``*CONN``/``*CAP``/``*RES`` block shapes follow -- see "SPEF
#: export (`--spef`)" in docs/cli/extract.md.
SPEF_STANDARD = "IEEE 1481-1999"


def _spef_timestamp() -> str:
    """An ISO-8601 UTC timestamp for the SPEF ``*DATE`` header line --
    informational only (no downstream field parses it back), matching every
    other timestamp this repo's writers emit."""
    return datetime.now(timezone.utc).strftime("%a %b %d %H:%M:%S %Y")


#: Every character SPEF's own identifier grammar (IEEE 1481-1999) does *not*
#: admit bare -- anything outside ``[A-Za-z0-9_]``. Matched one character at a
#: time by :func:`_spef_name`, which backslash-escapes each hit.
_SPEF_ESCAPE_RE = re.compile(r"[^A-Za-z0-9_]")


def _spef_name(name: str) -> str:
    """``name`` rendered as a SPEF identifier: every character outside
    ``[A-Za-z0-9_]`` backslash-escaped, which is exactly what SPEF's own
    (IEEE 1481-1999) identifier grammar requires of a *special character*.

    **Not cosmetic -- verified live, and a hard parse error without it.**
    KLayout's extracted net names routinely contain characters SPEF reserves:
    an unlabelled net is named ``$<n>`` by KLayout itself, a net carrying
    several layout labels is named by joining them with ``|``, and a bussed
    pin label keeps its ``[``/``]``. Feeding those through unescaped makes a
    real OpenSTA ``read_spef`` abort on the *first* ``*D_NET`` line
    (``[ERROR STA-1670] ... syntax error``, reproduced on the routed `gcd`
    corpus fixture against ``openroad/orfs:latest`` while implementing issue
    #948); escaping them makes the identical file parse cleanly.

    The escape is applied to every identifier position the file has -- the
    ``*PORTS`` list, each ``*D_NET`` name, each ``*CONN``/``*P`` entry, and
    every ``*CAP``/``*RES`` node -- so a name and its references always agree
    on spelling. Reading tools strip the backslashes back off, so the name an
    STA session matches against its own netlist is the unescaped one.
    """
    return _SPEF_ESCAPE_RE.sub(lambda m: "\\" + m.group(0), name)


#: Same character class as :data:`_SPEF_ESCAPE_RE` *except* ``[``/``]`` --
#: SPEF's own declared bus-delimiter characters (``*BUS_DELIMITER [ ]``, this
#: writer's own header line). Matched by :func:`_spef_port_node_name`, used
#: only for a *bare* (colon-free) port-pin ``*RES``/``*CAP`` node reference
#: (issue #961's residual-annotation fix) -- see that function's docstring
#: for why a bus-indexed port needs its brackets left *un*-escaped there,
#: unlike every other identifier position this file writes.
_SPEF_PORT_NODE_ESCAPE_RE = re.compile(r"[^A-Za-z0-9_\[\]]")


def _spef_port_node_name(name: str) -> str:
    """``name`` (a *declared-port* net's own name) rendered as a bare,
    colon-free SPEF ``*RES``/``*CAP`` node identifier -- every character
    :func:`_spef_name` would escape *except* ``[``/``]``, which are left as
    literal, un-escaped bus-delimiter characters.

    **Root cause, live-verified against a real OpenSTA session (issue #961's
    residual, following PR #984's topology rework):** PR #984 gave every
    net -- port or not -- an internal, colon-scoped primary node
    (``<net>:1``, never the bare net/port name) after finding that a bare
    identifier is never a valid two-node ``*RES``/``*CAP`` endpoint. That
    finding was correct for *non-port* nets but incomplete for *port* nets:
    OpenSTA's own SPEF reader (``SpefReader::findParasiticNode``, the
    ``The-OpenROAD-Project/OpenSTA`` fork) resolves a colon-free ``*RES``/
    ``*CAP`` node through ``findPortPinRelative`` -- ``Network::findPin`` on
    the *design's own top-level pin*, not through any bare-net-name lookup.
    A bare, un-escaped **non-bus** port name (``clk``, ``done``, ``start``)
    resolves there cleanly (confirmed directly against OpenSTA's own shipped
    ``examples/gcd_sky130hd.spef`` test fixture, whose ``*RES 1 clk *198:13
    ...`` line is exactly this shape, and by giving this repo's own routed
    `gcd`'s ``done``/``clk`` nets an un-escaped bare-name primary node and
    confirming zero warnings and zero unannotated loads). A **bus-indexed**
    port name (``a_in[0]``, ``result[13]``) only resolves the *same* way
    when its brackets are left **un-escaped** -- this writer's own
    :func:`_spef_name` (used everywhere else in this file) backslash-escapes
    them, which tells the reader "this is a literal ``[``/``]`` character,
    not a bus index" per ``*BUS_DELIMITER``'s own declared meaning, so
    ``findPin`` looks for a pin literally named ``a_in\\[0\\]`` -- which does
    not exist, only the real, bus-expanded ``a_in[0]`` pin does -- and warns
    ``pin a_in\\[0\\] not found``, live-reproduced isolating this exact
    mechanism on `gcd`'s own routed SPEF. Every *other* identifier this file
    writes (the ``*D_NET``/``*PORTS``/``*P`` net-name text, ``*I
    <inst>:<pin>`` device-pin references, and every non-port
    ``<net>:<N>``-numbered internal node) is unaffected: those resolve
    through :func:`findNet`/instance-pin lookup, which issue #951 already
    live-verified tolerates (and correctly un-escapes) backslash-escaped
    brackets -- only the specific ``findPortPinRelative`` bare-token path
    this helper feeds does not.

    **Measured live on the routed `gcd` corpus fixture (2026-08-14,
    `openroad/orfs:latest`):** using this helper for every uniquely-named
    port net's primary node dropped ``report_parasitic_annotation``'s
    "partially unannotated drivers" count from 52 (PR #984's own residual)
    to **0**, with zero new parse warnings (the pre-existing, unrelated
    ``$``-prefixed intermediate-net-name warning count -- a `write_verilog`
    round-trip artifact of this reproduction's own harness, not this
    writer -- stayed exactly at its own baseline count).

    Callers must restrict this to net names :func:`_write_spef` already
    knows are unambiguous (``name_counts[net] == 1``) -- same guard as
    ``net_instance_pins``' own duplicate-name skip, for the same reason: a
    layout label shared by several un-strapped islands (e.g. `gcd`'s 105
    separate ``VGND`` islands) has one real design pin object per name, so
    collapsing every island onto that single shared node would assert
    electrical continuity none of them individually has.
    """
    return _SPEF_PORT_NODE_ESCAPE_RE.sub(lambda m: "\\" + m.group(0), name)


#: Matches a DEF ``NETS`` section's opening line (``NETS <numNets> ;``,
#: LEF/DEF 5.8 Language Reference section 6.9) -- anchored at line start so a
#: ``SPECIALNETS <n> ;`` line (a different section, same trailing shape)
#: never matches: after the leading whitespace the next literal characters
#: must be exactly ``NETS``, which ``SPECIALNETS`` does not start with.
_DEF_NETS_BEGIN_RE = re.compile(r"^\s*NETS\s+\d+\s*;\s*$")
#: Matches the section's closing ``END NETS`` line -- same anchoring
#: argument keeps it from matching ``END SPECIALNETS``.
_DEF_NETS_END_RE = re.compile(r"^\s*END\s+NETS\s*$")
#: Matches one net record's opening line, ``- <netName> ...`` -- a new
#: record always starts a fresh line inside the section (LEF/DEF grammar),
#: mirroring ``place_and_route.py``'s ``_DEF_PIN_START_RE``.
_DEF_NET_START_RE = re.compile(r"^\s*-\s+(\S+)")
#: Matches one ``( <ref> <pin> )`` connection tuple inside a net record's
#: body -- ``<ref>`` is either an instance name or the literal ``PIN`` (a
#: top-level design port connection, DEF's own convention, already covered
#: by ``place_and_route.py``'s ``_def_pin_net_names``/``declared_pins`` and
#: excluded by :func:`def_net_instance_pins` below).
_DEF_NET_CONN_RE = re.compile(r"\(\s*(\S+)\s+(\S+)\s*\)")
#: Matches the first whitespace-delimited ``+`` token in a net record's body
#: -- the start of the record's first ``+``-prefixed clause (``+ ROUTED``,
#: ``+ USE SIGNAL``, ``+ NONDEFAULTRULE ...``, LEF/DEF 5.8 section 6.9). The
#: DEF grammar puts the whole ``( <ref> <pin> )`` connection list *before*
#: any such clause, so everything from this token onward is routing/attribute
#: syntax, not connectivity. Splitting there is what keeps ``+ ROUTED
#: metal1 ( 27300 60900 ) ( * 63500 ) NEW metal2 ( 27300 63500 ) <via>``'s
#: coordinate and via tuples -- which have the identical ``( <a> <b> )``
#: shape as a connection -- from being scanned as fake ``(instance, pin)``
#: pairs. Anchored on whitespace (or line start) on the left and whitespace
#: (or line end) on the right so a ``+`` inside an escaped DEF identifier
#: (``\+foo``) or a coordinate never triggers the split.
_DEF_NET_CLAUSE_RE = re.compile(r"(?:^|\s)\+(?=\s|$)")


def def_net_instance_pins(def_path: str) -> dict[str, tuple[tuple[str, str], ...]]:
    """Parse ``def_path``'s own ``NETS`` section for each net's real
    ``(instance, pin)`` connectivity (issue #961's device-terminal
    ``*CONN`` correlation): a small, targeted scan in the same style as
    ``place_and_route.py``'s ``_def_pin_net_names`` (not a general DEF
    parser -- this repo already reads DEF geometry through
    ``klayout.db``'s own LEF/DEF reader for every other purpose).

    Each ``NETS`` record is ``- <netName> ( <ref> <pin> ) ... ;``, where
    ``<ref>`` is either a cell instance name or the literal ``PIN`` (a
    top-level design-port connection -- already covered by
    :func:`_write_spef`'s existing ``port_names``/``*P`` handling via
    ``place_and_route.py``'s ``declared_pins``, so ``PIN`` entries are
    dropped here rather than double-reported). A record may span several
    lines (a high-fanout net wraps in a real routed DEF), so this
    accumulates a record's own text across lines until the terminating
    ``;``, matching ``_def_pin_net_names``'s own multi-line handling.

    Only the record's *connection-list prefix* is scanned: DEF's grammar
    puts the whole ``( <ref> <pin> )`` list before any ``+``-prefixed
    clause, and a real routed net continues with ``+ ROUTED <layer> ( x y )
    ( * y ) NEW <layer> ( x y ) <via> ...`` whose coordinate and via tuples
    have the identical ``( <a> <b> )`` shape as a connection. Scanning the
    full record body would therefore turn wire points into fake
    ``(instance, pin)`` pairs on essentially every routed signal net, so
    the body is split at its first ``+`` token (:data:`_DEF_NET_CLAUSE_RE`)
    and only the part before it is matched.

    These are exactly the ``<inst>``/``<pin>`` identifiers OpenSTA's own
    linked gate-level design already uses (DEF preserves the flow's
    original instance/pin names verbatim), which is what makes
    ``*I <inst>:<pin>`` entries built from this data resolvable against a
    real OpenSTA session -- unlike this repo's own layout-driven ``Device``
    naming (``$1517:G``), which has no asserted correlation to any
    digital-flow instance name (see :func:`_write_spef`'s docstring).

    Returns ``{}`` (never raises) for a missing file, a DEF with no
    ``NETS`` section, or a section that parses to no connections at all --
    the caller's own "no restriction" default, matching
    :func:`place_and_route._def_pin_net_names`'s "absence is not proof of
    zero" posture but expressed as an empty mapping (every lookup is a
    ``.get(name, ())`` at the call site, so there is no behavioral
    difference between "absent" and "empty" here the way there is for
    ``declared_pins``, which must distinguish "no restriction" from "empty
    restriction").
    """
    try:
        with open(def_path, encoding="utf-8", errors="replace") as handle:
            text = handle.read()
    except OSError:
        return {}

    connections: dict[str, list[tuple[str, str]]] = {}
    in_nets = False
    current_net: str | None = None
    current_body: list[str] = []

    def _flush() -> None:
        nonlocal current_net, current_body
        if current_net is not None and current_body:
            # Only the record's connection-list prefix -- everything before
            # its first `+`-prefixed clause -- holds `( <ref> <pin> )`
            # connections. A routed net's `+ ROUTED`/`NEW` geometry uses the
            # identical `( <a> <b> )` shape for coordinates and vias, so
            # scanning the whole body would invent connections out of wire
            # points (see `_DEF_NET_CLAUSE_RE`).
            body = _DEF_NET_CLAUSE_RE.split(" ".join(current_body), maxsplit=1)[0]
            pairs = [
                (ref, pin)
                for ref, pin in _DEF_NET_CONN_RE.findall(body)
                if ref != "PIN"
            ]
            if pairs:
                connections.setdefault(current_net, []).extend(pairs)
        current_net = None
        current_body = []

    for line in text.splitlines():
        if not in_nets:
            if _DEF_NETS_BEGIN_RE.match(line):
                in_nets = True
            continue
        if _DEF_NETS_END_RE.match(line):
            _flush()
            break
        start_match = _DEF_NET_START_RE.match(line)
        if start_match:
            _flush()
            current_net = start_match.group(1)
        if current_net is not None:
            current_body.append(line)
        if line.rstrip().endswith(";"):
            _flush()

    return {name: tuple(pairs) for name, pairs in connections.items()}


def _spef_net_topology_nodes(
    spef_net: str, *, hub_differs: bool, port_node_name: str | None = None
) -> tuple[str, str, int]:
    """Plan the two SPEF node identifiers one ``*D_NET`` block's own RC star
    needs -- ``net_node`` (where device-terminal legs, DEF-instance-pin legs,
    and (when ``hub_differs``) the Gamma-shunt resistor all originate) and
    ``hub_node`` (where the net's self- and coupling-capacitance attach) --
    plus the next free internal-node index a caller should continue numbering
    from for any additional per-block nodes (device-terminal legs).

    **PR #984's root-cause fix (issue #961), live-verified against a real
    OpenSTA session:** for a *non-port* net, OpenSTA's SPEF reader only
    accepts a **colon-scoped, two-part identifier** -- ``*I <inst>:<pin>``
    or a properly-scoped internal node ``<net>:<N>`` (IEEE 1481-1999's own
    convention for a net's non-pin internal nodes) -- as either endpoint of
    a two-node ``*RES``/coupling ``*CAP`` entry. A *bare*, single-token
    non-port net name is never valid there. Given that, ``net_node`` is a
    fresh internal node ``<net>:1`` by default -- never the bare net name.

    **Issue #961's own residual, live-verified against a real OpenSTA
    session (`docs/cli/place-and-route.md`'s "``*CONN`` device-terminal pin
    correlation" section has the full log):** PR #984's own finding that a
    bare identifier is *never* a valid endpoint turned out to be incomplete
    for *port* nets specifically -- OpenSTA's ``findParasiticNode`` resolves
    a colon-free node through ``Network::findPin`` on the design's own
    top-level pin, not through any net-name lookup, so a bare **port** name
    genuinely does resolve there (confirmed directly against OpenSTA's own
    shipped ``examples/gcd_sky130hd.spef`` test fixture, whose ``*RES 1 clk
    *198:13 ...`` line is exactly this shape) -- see
    :func:`_spef_port_node_name`'s own docstring for the full mechanism,
    including the separate bus-bracket-escaping wrinkle a bus-indexed port
    (``a_in[0]``) needs on top of this. Pass ``port_node_name`` (that
    function's own output, restricted to unambiguous port names -- see its
    docstring) to use it as ``net_node`` instead of the ``<net>:1`` default.

    ``hub_node`` is a second, distinct internal node (``<net>:2``) only when
    the net's capacitance hub is not the net's own node (``hub_differs`` --
    the no-device-terminal Gamma-shunt fallback, ``docs/cli/extract.md``'s
    "The model: a star topology" section); when the hub *is* the net's own
    node (the common case, at least one device terminal), ``hub_node`` is
    simply ``net_node`` (the port's own bare name, when ``port_node_name``
    is given). The Gamma-shunt ``<net>:2`` hub itself is unaffected by
    ``port_node_name`` either way -- it is colon-scoped, resolved through
    ``findNet`` rather than ``findPin``, and issue #951 already
    live-verified that path tolerates a net's name regardless of port
    status.
    """
    net_node = port_node_name if port_node_name is not None else f"{spef_net}:1"
    if hub_differs:
        hub_node = f"{spef_net}:2"
        next_idx = 3
    else:
        hub_node = net_node
        next_idx = 2
    return net_node, hub_node, next_idx


def _unescape_spice_safe_net_name(name: str) -> str:
    """Reverse :func:`spice_safe_net_name`'s leading-``$`` backslash escape
    (issue #1162) for a net-name string already read out of a *built*
    JSON-shaped report (``parasitics_report``, the same dict ``klt
    extract``'s own JSON response returns).

    :func:`_write_spef` reads straight from ``parasitics_report``, which
    already carries :func:`spice_safe_net_name`'s escaped ``\\$N`` spelling
    for an anonymous net. SPEF's *own* grammar (:func:`_spef_name`)
    independently escapes every character outside ``[A-Za-z0-9_]`` --
    including a literal backslash -- so feeding it an already-escaped
    ``\\$N`` string double-escapes it into ``\\\\\\$N`` (confirmed directly
    against a live ``_spef_name`` call). Reversing this one, narrow,
    unambiguous transform first -- no ordinary net name otherwise starts
    with a literal backslash -- restores the raw KLayout identity spelling
    SPEF's own escaping expects, exactly as it worked before issue #1162
    introduced the JSON-level escape. A no-op for every net name that is not
    itself an escaped anonymous net.
    """
    if name.startswith("\\$"):
        return name[1:]
    return name


def _write_spef(
    path: str,
    *,
    design_name: str,
    klt_version: str | None,
    parasitics_report: Mapping[str, Any],
    port_names: Iterable[str],
    net_instance_pins: Mapping[str, Sequence[tuple[str, str]]] | None = None,
) -> None:
    """Write ``parasitics_report`` (an already-computed :func:`_inject_parasitics`
    summary -- see ``run_extract``'s ``spef_output``/``klt extract --spef``
    flag) as a Standard Parasitic Exchange Format file (SPEF,
    :data:`SPEF_STANDARD`), for ``read_spef``-style STA consumption (issue
    #948, Epic #700 Phase 3; survey doc: ``docs/design/
    post-route-sta-survey.md`` sections 3.1/4.1).

    **A format translation of already-computed data, not a new extraction
    pass** (the survey's own §4.1 scoping): every R/C value written here
    comes straight from ``parasitics_report["nets"]`` -- the exact
    star-topology model ``docs/cli/extract.md``'s "Parasitic (RC)
    extraction" section documents for the written SPICE ``R``/``C`` device
    cards -- re-expressed as SPEF's ``*D_NET``/``*CAP``/``*RES`` block
    syntax instead. Nothing here re-derives or re-measures a single R or C
    value.

    **Every two-terminal ``*RES``/``*CAP`` endpoint is a colon-scoped,
    ``*CONN``-declared instance-pin identifier, a properly-scoped internal
    node, or (for an unambiguously-named port net) the port's own bare name
    -- never a bare *non-port* net name (issue #961's root-cause fix and its
    own residual, both live-verified against a real OpenSTA session).** See
    :func:`_spef_net_topology_nodes`'s and :func:`_spef_port_node_name`'s own
    docstrings for the exact node-planning rule and the live reproductions
    that pinned it down: a bare (single-token, no ``:``) *non-port* net name
    is never a valid two-node endpoint, but a bare *port* name is -- through
    a different OpenSTA resolution path (pin lookup, not net lookup) that
    PR #984 did not originally distinguish from the non-port case, and that
    additionally requires a bus-indexed port's own brackets to stay
    un-escaped (``a_in[0]``, not ``a_in\\[0\\]``) to resolve, unlike every
    other identifier this function writes.

    **Net-name correlation, plus optional device-terminal ``*CONN``
    correlation (issue #948/#961 scope).** Each ``*D_NET`` block is keyed by
    ``entry["net"]`` -- the same layout-label-derived name ``docs/design/
    post-route-sta-survey.md`` §2.1 flags as the one identifier an STA tool
    reading this file needs to resolve against its own (Verilog-derived)
    flat net list; ``docs/cli/place-and-route.md``'s ``read_spef`` wiring
    checks exactly that correlation (an explicit "N of M nets annotated"
    count) after loading this file. ``*CONN`` entries always name this
    net's own **port** membership (``*P <name> B``, when ``port_names``
    marks it a top-level pin) -- a block-level "this net is also a port"
    association per the SPEF grammar, needing no separate resistor tying the
    bare port name into the internal RC network (live-verified: attempting
    such a tie is itself what produces a ``pin <port> not found`` warning).
    When ``net_instance_pins`` is given (issue #961's
    :func:`def_net_instance_pins`, sourced from the routed DEF's own
    ``NETS`` section rather than this repo's own layout-driven ``Device``
    naming, which has no asserted correlation to any digital-flow instance
    name -- the survey's own §4.1 "Risk" paragraph), it additionally emits
    one ``*I <inst>:<pin> B`` entry per real cell-instance connection **and**
    wires each into the RC network with a zero-ohm ``*RES`` leg from the
    net's own primary node (``net_node``, see :func:`_spef_net_topology_nodes`)
    -- the same node every device-terminal leg and Gamma-shunt fallback
    resistor already originates from. This does not distribute resistance
    *between* DEF-level pins (this model has no notion of which pin drives
    and which loads, nor of the physical wire path between them); it makes
    every real design pin on the net a genuine, resolvable node in the RC
    network at the net's own lumped potential, which is what lets OpenSTA
    actually attach this net's total capacitance to a real driver/load pin
    instead of discarding the whole ``*D_NET`` block as unconnected.
    ``net_instance_pins`` omitted (or a net absent from it) falls back to the
    pre-#961 port-only ``*CONN`` behavior. ``*CONN`` is optional per the SPEF
    grammar (IEEE 1481-1999), so a net with neither a port nor any
    instance-pin correlation is still a syntactically valid ``*D_NET`` block
    with no ``*CONN`` section at all.

    **Duplicate net names are a known, inherited limitation -- and
    ``net_instance_pins`` correlation is skipped entirely for them.** A
    layout label shared by several distinct, un-strapped net islands (e.g.
    the `gcd` corpus's 105 separate ``VGND`` islands, see
    docs/cli/extract.md's "Coverage" section) emits one independent
    ``*D_NET`` block per island under the identical name -- SPEF's own
    net-name-keyed grammar has no per-island qualifier to disambiguate them
    the way this response's own ``net_id`` field does, so a reader that
    folds same-named ``*D_NET`` blocks together (rather than accumulating
    them) will not see every island's own contribution. The DEF's own
    ``NETS`` section, by contrast, declares each logical net name **once**
    (with every one of its real connections), so blindly attaching that
    full connection list to *every* same-named ``*D_NET`` island would
    assert connectivity no single island actually has. This function
    therefore only emits ``*I`` entries for a net name that appears exactly
    once in ``parasitics_report["nets"]`` -- a duplicated name keeps its
    pre-#961 port-only (or empty) ``*CONN`` section, the same known
    limitation as before this feature existed. Immaterial for genuine
    signal nets (which are not un-strapped, so never collide this way in
    practice).

    **Internal (non-port) nodes are numbered ``<net>:<N>``, scoped to their
    own ``*D_NET`` block (issue #961's root-cause fix), not named after the
    model's own internal net names.** :func:`_spef_net_topology_nodes` plans
    each block's ``net_node`` (where device-terminal legs, DEF-instance-pin
    legs, and the Gamma-shunt resistor all originate) and ``hub_node`` (where
    self- and coupling-capacitance attach); every device terminal's own
    ``leg_net`` (a KLayout-internal net name, e.g. ``_031___t0``, that no
    ``*CONN`` entry anywhere ever declares) becomes one further ``<net>:<N>``
    node of its own, bridged to ``net_node`` by one ``*RES`` card -- the
    identical star topology ``docs/cli/extract.md``'s "The model: a star
    topology" section documents, expressed as SPEF resistor cards instead of
    SPICE ``R`` device cards, but with SPEF-legal node identifiers instead of
    the model's own (SPICE-legal, SPEF-illegal-as-a-two-node-endpoint)
    internal net names.

    **Coupling ``*CAP`` entries reference the coupled net's own ``hub_node``
    (issue #961 defect 2), which is the coupled port's own bare name when it
    qualifies for :func:`_spef_port_node_name` (issue #961's residual fix) --
    never a bare *non-port* net name.** A Gamma-shunt-fallback net's
    ``hub_node`` differs from its own ``net_node``; referencing the bare
    ``net`` name (or even the bare ``hub_net`` name) of a *non-port* net from
    a *different* ``*D_NET`` block's coupling ``*CAP`` line would both name
    an undeclared two-node endpoint (defect 2) and repeat the general
    bare-name-as-two-node-endpoint defect PR #984 fixed. A ``net -> hub_node``
    lookup built from every entry's own planned topology (first occurrence
    wins for a duplicated net name, the same tolerance this function's other
    duplicate-name handling already applies, and the same reason a
    duplicated *port* name never gets :func:`_spef_port_node_name`'s bare
    form) resolves each coupling partner to its real, SPEF-legal hub node
    before it is written -- live-verified this resolves correctly even when
    the coupling partner's own ``*D_NET`` block has not been written yet
    (SPEF pin/net lookups resolve against the already-fully-loaded design,
    not file position).

    Every identifier written -- ``*PORTS`` entries, ``*D_NET``/``*P`` names,
    ``*I <inst>:<pin>`` device-pin references, and every non-port
    ``*CAP``/``*RES`` node -- is rendered through :func:`_spef_name`, which
    backslash-escapes the characters SPEF's own grammar reserves. This is
    load-bearing, not cosmetic: KLayout's extracted names carry
    ``$``/``|``/``[``/``]`` routinely, and a real OpenSTA ``read_spef``
    aborts on the first such line unescaped. The one exception is a
    unique-named port net's own bare-name ``*RES``/``*CAP`` node (this
    function's ``net_node``/``hub_node`` when :func:`_spef_port_node_name`
    applies), which is rendered through *that* function instead -- it
    escapes the same reserved characters *except* ``[``/``]``, see its own
    docstring for why a bus-indexed port specifically needs its brackets
    left un-escaped there.

    Units are declared, not converted: ``*C_UNIT 1 FF``/``*R_UNIT 1 OHM``
    match ``parasitics_report``'s own units exactly, so every numeric value
    below is copied through unchanged -- no femtofarad/ohm rescaling, and
    therefore no unit-conversion error to introduce.

    Raises :class:`ExtractError` if ``path`` cannot be written.
    """
    # Reverse `spice_safe_net_name`'s leading-`$` backslash escape (issue
    # #1162) on every net-name-shaped value this function reads out of
    # `parasitics_report`/`port_names` *before* any of the SPEF-specific
    # logic below runs -- restores this function's pre-#1162 raw-identity
    # input shape unchanged, so `_spef_name`'s own escaping (applied further
    # down) is not handed an already-escaped string it would double-escape.
    # See `_unescape_spice_safe_net_name`'s docstring.
    parasitics_report = {
        **parasitics_report,
        "nets": [
            {
                **entry,
                "net": _unescape_spice_safe_net_name(entry["net"]),
                "hub_net": _unescape_spice_safe_net_name(entry["hub_net"]),
                "terminals": [
                    {
                        **terminal,
                        "leg_net": _unescape_spice_safe_net_name(terminal["leg_net"]),
                    }
                    for terminal in entry.get("terminals", [])
                ],
                "coupled": [
                    {**coupled, "net": _unescape_spice_safe_net_name(coupled["net"])}
                    for coupled in entry.get("coupled", [])
                ],
            }
            for entry in parasitics_report["nets"]
        ],
    }
    port_names = [_unescape_spice_safe_net_name(name) for name in port_names]

    port_name_set = frozenset(port_names)
    net_instance_pins_map: Mapping[str, Sequence[tuple[str, str]]] = (
        net_instance_pins or {}
    )

    # Name-occurrence count, computed in its own pass *before* hub_by_net_name
    # below -- issue #961's residual fix needs the *final* count for a name
    # (is it unique across the whole file?) to decide whether that port
    # qualifies for `_spef_port_node_name`'s bare form, which an
    # incrementally-updated count (correct only after the *last* occurrence)
    # cannot answer for a net's *first* occurrence.
    name_counts: dict[str, int] = {}
    for entry in parasitics_report["nets"]:
        name_counts[entry["net"]] = name_counts.get(entry["net"], 0) + 1

    # One two-terminal coupling `*CAP` card per distinct coupled net pair
    # (issue #760's `coupled[]`), emitted exactly once -- `coupled[]` reports
    # every pair from *both* endpoints (docs/cli/extract.md's "Vertical-
    # overlap coupling capacitance" section), so this dedupes by an
    # order-independent key before attaching each pair's card to whichever
    # net's own `*D_NET` block is written first (in `nets[]`'s existing
    # sort-by-name order).
    coupling_by_net: dict[str, list[dict[str, Any]]] = {}
    seen_pairs: set[tuple[str, str]] = set()
    # `net -> hub_node` lookup (first occurrence wins for a duplicated net
    # name) -- issue #961 defect 2, see this function's docstring. `hub_node`
    # is the SPEF-legal identifier `_spef_net_topology_nodes` would plan for
    # that entry's own block: a unique-named port net's own bare name (issue
    # #961's residual fix, via `_spef_port_node_name`), else the internal
    # `<net>:<N>` node PR #984 planned (never a bare *non-port* net name,
    # never a *duplicated* port's bare name either -- both remain invalid
    # two-node `*RES`/`*CAP` endpoints, live-verified).
    hub_by_net_name: dict[str, str] = {}
    for entry in parasitics_report["nets"]:
        this_net = entry["net"]
        if this_net not in hub_by_net_name:
            is_unique_port = this_net in port_name_set and name_counts[this_net] == 1
            _, this_hub_node, _ = _spef_net_topology_nodes(
                _spef_name(this_net),
                hub_differs=entry["hub_net"] != this_net,
                port_node_name=(
                    _spef_port_node_name(this_net) if is_unique_port else None
                ),
            )
            hub_by_net_name[this_net] = this_hub_node
        for coupled in entry.get("coupled", []):
            pair_key = tuple(sorted((this_net, coupled["net"])))
            if pair_key in seen_pairs:
                continue
            seen_pairs.add(pair_key)
            coupling_by_net.setdefault(this_net, []).append(coupled)

    lines: list[str] = [
        f'*SPEF "{SPEF_STANDARD}"',
        f'*DESIGN "{design_name}"',
        f'*DATE "{_spef_timestamp()}"',
        '*VENDOR "2AM Logic"',
        '*PROGRAM "klt extract"',
        f'*VERSION "{klt_version or "unknown"}"',
        '*DESIGN_FLOW "NAME_SCOPE LOCAL" "PIN_CAP NONE"',
        "*DIVIDER /",
        "*DELIMITER :",
        "*BUS_DELIMITER [ ]",
        "*T_UNIT 1 PS",
        "*C_UNIT 1 FF",
        "*R_UNIT 1 OHM",
        "*L_UNIT 1 HENRY",
    ]

    sorted_ports = sorted(port_name_set)
    if sorted_ports:
        lines.append("")
        lines.append("*PORTS")
        for name in sorted_ports:
            # Direction is unknown from layout-derived extraction alone (a
            # GDS text label carries no I/O-direction metadata) -- declared
            # `B` (bidirectional/unspecified, a real SPEF direction token),
            # never guessed as `I`/`O`.
            lines.append(f"{_spef_name(name)} B")

    for entry in parasitics_report["nets"]:
        net = entry["net"]
        hub = entry["hub_net"]
        terminals = entry.get("terminals", [])
        own_coupling = coupling_by_net.get(net, [])
        total_cap_ff = entry["capacitance_ff"] + sum(
            coupled["capacitance_ff"] for coupled in own_coupling
        )
        # Every identifier below goes through `_spef_name` -- SPEF's own
        # grammar rejects the bare `$`/`|`/`[`/`]` characters KLayout's
        # extracted net names routinely carry (see that helper's docstring
        # for the live `read_spef` parse error this prevents).
        spef_net = _spef_name(net)
        is_port = net in port_name_set
        hub_differs = hub != net

        # Issue #961's root-cause fix (PR #984) plus its own residual fix:
        # `net_node`/`hub_node` are SPEF-legal two-node-entry endpoints --
        # the internal `<net>:<N>` node PR #984 planned for a *non-port* net
        # (never the model's own bare internal net name), or -- for an
        # unambiguously-named *port* net -- the port's own bare name
        # (`_spef_port_node_name`, issue #961's residual fix; live-verified,
        # see this function's docstring and `_spef_net_topology_nodes`'s /
        # `_spef_port_node_name`'s). `next_node_idx` is where any further
        # per-block internal nodes (device-terminal legs, below) continue
        # numbering from.
        is_unique_port = is_port and name_counts.get(net, 0) == 1
        net_node, hub_node, next_node_idx = _spef_net_topology_nodes(
            spef_net,
            hub_differs=hub_differs,
            port_node_name=_spef_port_node_name(net) if is_unique_port else None,
        )

        # Issue #961: real cell-instance connectivity from the routed DEF's
        # own `NETS` section, restricted to net names that are unambiguous
        # (see this function's docstring, "Duplicate net names" paragraph)
        # -- a duplicated name (e.g. `VGND`) keeps the pre-#961 port-only
        # `*CONN` behavior rather than asserting connectivity no single
        # island actually has.
        instance_pins = (
            net_instance_pins_map.get(net, ()) if name_counts.get(net, 0) == 1 else ()
        )

        lines.append("")
        lines.append(f"*D_NET {spef_net} {total_cap_ff:.6f}")
        conn_lines: list[str] = []
        if is_port:
            conn_lines.append(f"*P {spef_net} B")
        for inst, pin in instance_pins:
            # Direction is unknown without a LEF pin-direction lookup (the
            # DEF `NETS` section alone does not carry it) -- declared `B`,
            # matching the same "never guessed" posture the `*PORTS`/`*P`
            # entries above already follow.
            conn_lines.append(f"*I {_spef_name(inst)}:{_spef_name(pin)} B")
        if conn_lines:
            lines.append("*CONN")
            lines += conn_lines

        cap_lines: list[str] = [f"1 {hub_node} {entry['capacitance_ff']:.6f}"]
        for i, coupled in enumerate(own_coupling, start=2):
            # Issue #961 defect 2: reference the coupled net's own hub
            # *node* (a SPEF-legal identifier), not its bare name -- see
            # this function's docstring, "Coupling `*CAP` entries"
            # paragraph.
            coupled_hub_node = hub_by_net_name.get(
                coupled["net"], _spef_name(coupled["net"])
            )
            cap_lines.append(
                f"{i} {hub_node} {coupled_hub_node} {coupled['capacitance_ff']:.6f}"
            )

        res_lines: list[str] = []
        next_res_idx = 1
        node_idx = next_node_idx
        if terminals:
            for terminal in terminals:
                # Each device terminal's own `leg_net` (a KLayout-internal
                # name, e.g. `_031___t0`, that no `*CONN` entry ever
                # declares) becomes a fresh `<net>:<N>` internal node rather
                # than being referenced by its own bare name -- issue #961's
                # root-cause fix, see this function's docstring.
                leg_node = f"{spef_net}:{node_idx}"
                node_idx += 1
                res_lines.append(
                    f"{next_res_idx} {net_node} {leg_node} "
                    f"{terminal['resistance_ohm']:.6f}"
                )
                next_res_idx += 1
        elif hub_differs:
            # No-device-terminal Gamma-shunt fallback (docs/cli/extract.md's
            # "The model: a star topology" section): a single resistor from
            # the net's own node to its (now internal-node-numbered) hub --
            # the only case where `hub_node` differs from `net_node`.
            res_lines.append(
                f"{next_res_idx} {net_node} {hub_node} {entry['resistance_ohm']:.6f}"
            )
            next_res_idx += 1
        for inst, pin in instance_pins:
            # Zero-ohm connectivity leg from the net's own primary node to
            # this real design pin -- see this function's docstring,
            # "device-terminal `*CONN` correlation" paragraph, for why this
            # does not attempt to apportion resistance between DEF-level
            # pins.
            conn_node = f"{_spef_name(inst)}:{_spef_name(pin)}"
            res_lines.append(f"{next_res_idx} {net_node} {conn_node} 0.000000")
            next_res_idx += 1

        lines.append("*CAP")
        lines += cap_lines
        if res_lines:
            lines.append("*RES")
            lines += res_lines
        lines.append("*END")

    lines.append("")

    try:
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")
    except OSError as exc:
        raise ExtractError(f"could not write SPEF '{path}': {exc}") from exc
