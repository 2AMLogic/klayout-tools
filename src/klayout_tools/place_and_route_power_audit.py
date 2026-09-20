"""Power-delivery audit for ``klt place-and-route`` (issue #2086).

``request.power`` is optional, and omitting it produces a run that
**completes** -- exit 0, a routed DEF, a merged GDS, real area/wirelength/
timing numbers -- while placing **no tapcells, no PDN and no filler cells**.
Before this module existed nothing in the response said so: the only
difference between a power-complete result and a power-less one was a
``power.pdn: false`` flag describing what was *configured*, never what was
actually *placed*. A downstream area or timing number taken from such a run
is meaningless, and the artifact carried no marker saying so (issue #2086,
reported from a real ``gf180mcu_fd_sc_mcu7t5v0`` run).

This module closes that gap without changing the request contract: it reads
the DEF the run just wrote and reports **measured** counts --
tapcell/endcap/filler instances, and the ``SPECIALNETS`` power-grid
structure (``FOLLOWPIN`` rail segments, ``STRIPE`` strap segments, PDN
vias) -- into the additive ``power.placed`` response field, and builds the
loud, human-readable strings the response's top-level ``warnings`` field
carries.

Design notes:

- **Measured zero vs. unavailable evidence are different answers.** A DEF
  that declares ``COMPONENTS n ;`` is parseable evidence: every count it
  yields is a *measurement*, and ``0 fillers`` means zero fillers were
  placed. A missing/unreadable/section-less file yields
  ``evidence: "unavailable"`` with every count ``null`` and a
  ``unavailable_reason`` -- never a fabricated ``0``. The whole point of the
  issue is that a tool answering when it cannot is worse than one that is
  absent, so this module never guesses.
- **Text parsing, not a DEF reader.** The checks are counts of section
  entries and wiring statements, which the DEF text states directly; this
  mirrors :func:`klayout_tools.place_and_route_sta._def_pin_net_names`'s
  existing "deliberately a small, targeted scan (not a general DEF parser)"
  posture rather than standing up ``klayout.db``'s LEF/DEF reader (which
  needs the tech LEF too) just to count instances.
- **The check list is the filer's own detector.** Issue #2086's reporter
  ships a ~70-line ``check-pdn.py`` against the same DEF text and offered it
  as the spec: no ``SPECIALNET`` for the power/ground net, no ``FOLLOWPIN``
  segments (no rails), no ``STRIPE`` segments (no straps), zero PDN vias,
  zero tap/endcap instances, zero fill instances. :data:`CHECK_LABELS` names
  exactly that set, so a *transcription* error in a supplied
  ``request.power`` block (a strap layer that draws nothing) is caught by
  the same pass that catches an absent block.
- **Warn, never refuse.** ``request.power`` stays optional -- a floorplan
  exploration run has no reason to build a PDN -- so an absent or partial
  power delivery is reported (response field + ``warnings`` + a stderr line
  from the CLI), not turned into a nonzero exit. The JSON contract's
  ``status``/exit-code semantics are unchanged.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Any

#: ``power.placed.evidence`` when the DEF was read and parsed: every count
#: in the block is a real measurement, and a ``0`` means zero.
EVIDENCE_DEF = "def"
#: ``power.placed.evidence`` when no DEF could be parsed: every count is
#: ``null`` and ``unavailable_reason`` says why. Never confuse with a
#: measured zero -- that distinction is the issue's entire subject.
EVIDENCE_UNAVAILABLE = "unavailable"

#: ``power.placed.status`` values.
STATUS_COMPLETE = "complete"
STATUS_PARTIAL = "partial"
STATUS_ABSENT = "absent"
STATUS_UNKNOWN = "unknown"

#: Human labels for the machine tokens ``power.placed.missing`` carries --
#: used only to render the ``warnings`` strings. The tokens themselves are
#: the contract (see ``docs/cli/place-and-route.md``).
CHECK_LABELS: dict[str, str] = {
    "tapcells": "tapcell instances",
    "endcaps": "endcap instances",
    "fillers": "filler-cell instances",
    "special_nets": "a SPECIALNETS section",
    "power_special_net": "a SPECIALNETS entry for the power net",
    "ground_special_net": "a SPECIALNETS entry for the ground net",
    "followpin_segments": "FOLLOWPIN rail segments",
    "stripe_segments": "STRIPE strap segments",
    "pdn_vias": "PDN vias",
}

_COMPONENTS_BEGIN_RE = re.compile(r"^\s*COMPONENTS\s+(\d+)\s*;\s*$")
_COMPONENTS_END_RE = re.compile(r"^\s*END\s+COMPONENTS\s*$")
#: One ``COMPONENTS`` record's opening line, ``- <instName> <macroName> …``.
#: Continuation lines inside a record always start with ``+`` (LEF/DEF 5.8
#: section 6.4), so a leading ``-`` unambiguously starts a new record.
_COMPONENT_START_RE = re.compile(r"^\s*-\s+(\S+)\s+(\S+)")
_SPECIALNETS_BEGIN_RE = re.compile(r"^\s*SPECIALNETS\s+(\d+)\s*;\s*$")
_SPECIALNETS_END_RE = re.compile(r"^\s*END\s+SPECIALNETS\s*$")
#: A line that opens another DEF section (``<SECTION> <n> ;``) or closes one
#: (``END <SECTION>``). Used as a hard stop while scanning a section whose own
#: ``END`` line never arrives, so a truncated ``COMPONENTS`` section cannot
#: swallow the ``- VPWR``/``- VGND`` records of the ``SPECIALNETS`` section
#: that follows it -- which would otherwise report those nets as placed
#: component instances.
_SECTION_BOUNDARY_RE = re.compile(r"^\s*(?:END\s+\S+|[A-Z][A-Z0-9]*\s+\d+\s*;)\s*$")

#: Wiring-statement keywords that open a special-net wiring attribute
#: (``+ ROUTED``/``+ FIXED``/``+ COVER``/``+ SHIELD``, LEF/DEF 5.8 section
#: 6.20). Each is followed by ``<layerName> <routeWidth>``.
_WIRING_KEYWORDS = frozenset({"ROUTED", "FIXED", "COVER", "SHIELD"})

#: DEF placement orientations -- a bare token inside a wiring statement is a
#: via name *unless* it is one of these (a via instance may be followed by
#: its orientation). Excluded so an orientation is never miscounted as a
#: second via.
_DEF_ORIENTATIONS = frozenset({"N", "S", "E", "W", "FN", "FS", "FE", "FW"})


def read_def_text(def_path: str) -> str | None:
    """Return ``def_path``'s text, or ``None`` if it cannot be read.

    ``errors="replace"`` matches the existing DEF scans in this package: a
    stray non-UTF-8 byte in a comment must not turn a readable file into
    "no evidence".
    """
    try:
        with open(def_path, encoding="utf-8", errors="replace") as handle:
            return handle.read()
    except OSError:
        return None


def parse_def_component_masters(text: str) -> dict[str, int] | None:
    """Count ``COMPONENTS`` records per macro (master) name.

    Returns ``None`` -- "cannot tell", never a fabricated zero -- for every
    shape whose record list this scan cannot trust:

    - no ``COMPONENTS <n> ;`` header at all (an unparseable file, or one
      truncated before the section began);
    - a header whose declared record count disagrees with the number of
      records actually found. A section cut off mid-way (or one whose
      records ran into the next section because ``END COMPONENTS`` never
      arrived) would otherwise grade as a complete measurement -- reporting
      e.g. a measured ``0 fillers`` for a design whose filler records were
      simply never read. That is exactly the "answering when it cannot"
      failure this module exists to prevent, so a declared/found mismatch
      is reported as unavailable evidence instead.

    A header that is present, agrees with what was found, and is followed by
    no records returns ``{}`` -- a genuine, measured "zero instances".
    ``END COMPONENTS`` itself is *not* required once the declared count is
    satisfied: a DEF whose every declared record was read is a complete
    measurement even if the file was cut off immediately after the last one.
    """
    counts: dict[str, int] = {}
    declared: int | None = None
    records = 0
    in_section = False
    for line in text.splitlines():
        if not in_section:
            header = _COMPONENTS_BEGIN_RE.match(line)
            if header:
                in_section = True
                declared = int(header.group(1))
            continue
        if _COMPONENTS_END_RE.match(line):
            break
        if _SECTION_BOUNDARY_RE.match(line):
            # Another section opened without `END COMPONENTS`: stop here so
            # its own `- <name> …` records are never counted as components.
            break
        match = _COMPONENT_START_RE.match(line)
        if match:
            records += 1
            counts[match.group(2)] = counts.get(match.group(2), 0) + 1
    if not in_section or records != declared:
        return None
    return counts


def parse_def_special_nets(text: str) -> list[dict[str, Any]] | None:
    """Parse the ``SPECIALNETS`` section into one record per special net.

    Each record is ``{"name", "use", "followpin_segments",
    "stripe_segments", "other_segments", "stripe_layers", "vias"}``:

    - ``followpin_segments`` -- wiring statements tagged ``+ SHAPE
      FOLLOWPIN``, i.e. the standard-cell row power rails ``pdngen`` draws
      from a ``-followpins`` strap.
    - ``stripe_segments``/``stripe_layers`` -- statements tagged ``+ SHAPE
      STRIPE`` and the layers they were drawn on, i.e. the PDN straps. A
      grid with rails but no straps (the ``request.power``-less row-rail
      fallback, issue #1442) reports ``followpin_segments > 0`` and
      ``stripe_segments == 0``, which is exactly the distinction a caller
      needs.
    - ``vias`` -- via instances inside those wiring statements: the PDN's
      actual layer-to-layer connections. A grid whose straps exist but
      never connect down to the rails reports stripes with zero vias.

    An absent section returns ``[]`` -- DEF omits empty sections entirely,
    so "no ``SPECIALNETS`` section" *is* "no special nets" for a file that
    otherwise parsed (the caller establishes that via
    :func:`parse_def_component_masters`).

    Returns ``None`` -- "cannot tell", handled by the caller as unavailable
    evidence -- when a section *is* present but its own structure does not
    hold up, on the same fail-closed rule
    :func:`parse_def_component_masters` applies:

    - the number of net records read disagrees with the ``SPECIALNETS
      <n> ;`` header's declared count, or
    - the last record was never terminated by its ``;`` (the file, or the
      section, stops mid-record).

    ``END SPECIALNETS`` itself is optional once both hold, and the scan
    stops at the next section boundary either way, so a missing terminator
    never lets ``NETS``' own ``- <net>`` records be read as special nets.
    A short read is **not** safe to report as a measurement: an unfinished
    ``NEW met4 10 + SHAPE STRIPE`` carries a shape but no geometry, and
    counting it would turn a truncated file into a ``complete`` grid.
    """
    section: list[str] = []
    in_section = False
    declared: int | None = None
    for line in text.splitlines():
        if not in_section:
            header = _SPECIALNETS_BEGIN_RE.match(line)
            if header:
                in_section = True
                declared = int(header.group(1))
            continue
        if _SPECIALNETS_END_RE.match(line) or _SECTION_BOUNDARY_RE.match(line):
            break
        section.append(line)
    if not in_section:
        return []

    tokens: list[str] = []
    for line in section:
        tokens.extend(line.replace("(", " ( ").replace(")", " ) ").split())
    scanner = _SpecialNetScanner()
    nets = scanner.scan(tokens)
    if scanner.truncated or len(nets) != declared:
        return None
    return nets


def _at(tokens: list[str], index: int) -> str | None:
    """``tokens[index]``, or ``None`` past the end -- the section may be
    truncated, and a scanner that indexes past it would turn a malformed
    DEF into a traceback instead of a partial measurement."""
    return tokens[index] if 0 <= index < len(tokens) else None


def _skip_group(tokens: list[str], index: int) -> int:
    """Index just past the parenthesised group starting at ``index``."""
    depth = 1
    index += 1
    while index < len(tokens) and depth:
        if tokens[index] == "(":
            depth += 1
        elif tokens[index] == ")":
            depth -= 1
        index += 1
    return index


class _SpecialNetScanner:
    """Token walker for one ``SPECIALNETS`` section.

    A class rather than a closure-heavy loop purely so each token shape is
    its own small method: the section's grammar (net records, wiring
    attributes, ``NEW`` continuations, parenthesised point groups, bare via
    names) is genuinely branchy, and splitting it keeps every piece
    individually readable -- and under this repo's own complexity gate.
    """

    def __init__(self) -> None:
        self.nets: list[dict[str, Any]] = []
        self.current: dict[str, Any] | None = None
        self.in_wiring = False
        self.layer: str | None = None
        self.shape: str | None = None
        #: Whether the wiring statement currently open has produced any
        #: actual geometry yet (a routing-point group or a via). A
        #: statement that stops at its ``+ SHAPE STRIPE`` header -- the
        #: shape of a file truncated mid-record -- has none, and must not
        #: be counted as a placed strap.
        self.geometry = False
        #: ``True`` once the token stream ends inside an unterminated net
        #: record (no closing ``;``). The caller reports that as
        #: unavailable evidence rather than as a measurement.
        self.truncated = False

    def scan(self, tokens: list[str]) -> list[dict[str, Any]]:
        index = 0
        while index < len(tokens):
            index = self._step(tokens, index)
        self._flush_segment()
        self.truncated = self.current is not None
        return self.nets

    def _step(self, tokens: list[str], index: int) -> int:
        token = tokens[index]
        if token == "-":
            return self._start_net(tokens, index)
        if token == ";":
            return self._end_net(index)
        if self.current is None:
            return index + 1
        if token == "+":
            return self._attribute(tokens, index)
        if token == "NEW" and self.in_wiring:
            return self._new_segment(tokens, index)
        if token == "(":
            self.geometry = self.geometry or self.in_wiring
            return _skip_group(tokens, index)
        return self._maybe_via(token, index)

    def _start_net(self, tokens: list[str], index: int) -> int:
        self._flush_segment()
        self.current = {
            "name": _at(tokens, index + 1) or "",
            "use": None,
            "followpin_segments": 0,
            "stripe_segments": 0,
            "other_segments": 0,
            "stripe_layers": [],
            "vias": 0,
        }
        self.nets.append(self.current)
        self.in_wiring = False
        return index + 2

    def _end_net(self, index: int) -> int:
        self._flush_segment()
        self.in_wiring = False
        self.current = None
        return index + 1

    def _attribute(self, tokens: list[str], index: int) -> int:
        keyword = _at(tokens, index + 1)
        if keyword in _WIRING_KEYWORDS:
            self._open_segment(_at(tokens, index + 2))
            return index + 4  # `+` KEYWORD layerName routeWidth
        if keyword == "SHAPE" and self.in_wiring:
            self.shape = _at(tokens, index + 2)
            return index + 3
        if keyword == "USE":
            if self.current is not None:
                self.current["use"] = _at(tokens, index + 2)
            self._close_wiring()
            return index + 3
        # Any other net-level attribute (`+ SOURCE`, `+ WEIGHT`, …) ends the
        # wiring attribute this net was in, if any.
        self._close_wiring()
        return index + 1

    def _new_segment(self, tokens: list[str], index: int) -> int:
        self._open_segment(_at(tokens, index + 1))
        return index + 3  # NEW layerName routeWidth

    def _maybe_via(self, token: str, index: int) -> int:
        """Count a bare token inside a wiring statement as a via instance.

        Scope note (see the module docstring): this is the ``pdngen``
        grammar only -- a via name optionally followed by its orientation.
        A ``+ MASK <maskNum>`` prefix on a routing point, or a net-level
        ``+ VIA viaName`` attribute, would be miscounted/uncounted; neither
        shape appears in the DEFs this audit grades, and ``vias`` is a
        count metric rather than geometry.
        """
        if self.in_wiring and self.current is not None:
            if token not in _DEF_ORIENTATIONS:
                self.current["vias"] += 1
                self.geometry = True
        return index + 1

    def _open_segment(self, layer: str | None) -> None:
        self._flush_segment()
        self.in_wiring = True
        self.layer = layer
        self.shape = None
        self.geometry = False

    def _close_wiring(self) -> None:
        self._flush_segment()
        self.in_wiring = False

    def _flush_segment(self) -> None:
        net, layer, shape = self.current, self.layer, self.shape
        geometry = self.geometry
        self.layer = None
        self.shape = None
        self.geometry = False
        # A statement that never got as far as a routing-point group or a
        # via placed nothing: counting its `+ SHAPE STRIPE` header alone
        # would report a truncated record as a placed strap.
        if net is None or layer is None or not geometry:
            return
        if shape == "FOLLOWPIN":
            net["followpin_segments"] += 1
            return
        if shape != "STRIPE":
            net["other_segments"] += 1
            return
        net["stripe_segments"] += 1
        if layer not in net["stripe_layers"]:
            net["stripe_layers"].append(layer)


def audit_power_delivery(
    *,
    def_path: str | None,
    unavailable_reason: str | None,
    tapcell_master: str | None,
    endcap_master: str | None,
    filler_masters: Sequence[str],
    power_net: str | None,
    ground_net: str | None,
    expect_fillers: bool,
) -> dict[str, Any]:
    """Measure what power delivery the run at ``def_path`` actually placed.

    ``tapcell_master``/``endcap_master``/``filler_masters`` are the *cell
    library's* masters (from ``place_and_route``'s own ``_TAPCELL_CELLS``/
    ``_FILLER_CELLS`` tables), passed regardless of whether ``request.power``
    was given -- a power-less run's whole problem is that those instances
    are absent, so the audit has to know what to look for. ``None``/empty
    means "this library has no known master of that kind", and the matching
    check is skipped rather than failed.

    ``power_net``/``ground_net`` name the nets whose ``SPECIALNETS`` entries
    are expected (``request.power``'s own, or the row-rail fallback's).
    ``None``/``None`` (neither known -- ``request.power`` omitted on a
    library with no row-rail fallback) degrades to the weaker "is there any
    special net at all" check, never to inventing a net name.

    ``expect_fillers`` is ``True`` only once the run reached the stage that
    places them (``"route"``); a ``"place"``-stage DEF legitimately has
    none, and reporting that as a hole would be a false alarm.

    Returns the ``power.placed`` response block -- see
    ``docs/cli/place-and-route.md`` for the field contract.
    """
    text, masters, reason = _read_placed_masters(def_path, unavailable_reason)
    if text is None or masters is None:
        return _unavailable_block(def_path, reason)

    special_nets = parse_def_special_nets(text)
    if special_nets is None:
        # A `SPECIALNETS` section that is present but structurally broken
        # (record count disagrees with its header, or the file stops
        # mid-record). Its grid cannot be measured, and a short read of it
        # would grade as a *smaller but complete-looking* grid -- so the
        # whole block is unavailable evidence, not a measurement.
        return _unavailable_block(
            def_path,
            f"DEF at {def_path} has a truncated or miscounted SPECIALNETS "
            "section (its power grid cannot be measured)",
        )
    tapcells = masters.get(tapcell_master, 0) if tapcell_master else 0
    endcaps = masters.get(endcap_master, 0) if endcap_master else 0
    fillers = sum(masters.get(master, 0) for master in filler_masters)

    checks = _power_checks(
        special_nets=special_nets,
        tapcell_master=tapcell_master,
        endcap_master=endcap_master,
        filler_masters=filler_masters,
        power_net=power_net,
        ground_net=ground_net,
        expect_fillers=expect_fillers,
        tapcells=tapcells,
        endcaps=endcaps,
        fillers=fillers,
    )
    missing = [token for token, ok in checks if not ok]

    return {
        "evidence": EVIDENCE_DEF,
        "def_path": def_path,
        "unavailable_reason": None,
        "status": _grade(checks, missing),
        "missing": missing,
        "components": sum(masters.values()),
        "tapcells": tapcells,
        "endcaps": endcaps,
        "fillers": fillers,
        "special_nets": special_nets,
    }


def _read_placed_masters(
    def_path: str | None, unavailable_reason: str | None
) -> tuple[str | None, dict[str, int] | None, str | None]:
    """``(def text, master -> count, unavailable reason)`` for ``def_path``.

    The masters are ``None`` -- with a reason -- for every "cannot tell"
    shape: no DEF written at this stage, an unreadable file, or a file with
    no ``COMPONENTS`` section. Each is reported as such rather than as an
    empty count map, which the grading below would read as a measured zero.
    """
    if def_path is None:
        return None, None, unavailable_reason or "no DEF was written by this run"
    text = read_def_text(def_path)
    if text is None:
        return None, None, f"DEF at {def_path} could not be read"
    masters = parse_def_component_masters(text)
    if masters is None:
        return (
            None,
            None,
            f"DEF at {def_path} declares no COMPONENTS section "
            "(truncated or not a DEF)",
        )
    return text, masters, None


def _unavailable_block(def_path: str | None, reason: str | None) -> dict[str, Any]:
    """The ``power.placed`` block for "no evidence": every count ``null``."""
    return {
        "evidence": EVIDENCE_UNAVAILABLE,
        "def_path": def_path,
        "unavailable_reason": reason or "no DEF was written by this run",
        "status": STATUS_UNKNOWN,
        "missing": [],
        "components": None,
        "tapcells": None,
        "endcaps": None,
        "fillers": None,
        "special_nets": None,
    }


def _power_checks(
    *,
    special_nets: list[dict[str, Any]],
    tapcell_master: str | None,
    endcap_master: str | None,
    filler_masters: Sequence[str],
    power_net: str | None,
    ground_net: str | None,
    expect_fillers: bool,
    tapcells: int,
    endcaps: int,
    fillers: int,
) -> list[tuple[str, bool]]:
    """``(token, passed)`` for every check that *applies* to this run.

    A check whose subject this run cannot have is omitted rather than
    failed: a library with no endcap master, or a pre-``"route"`` DEF that
    could not yet carry fillers. Grid structure is graded across the
    expected power/ground nets only when their names are known -- otherwise
    across every special net the DEF declares, which is the strongest
    honest statement available without inventing a net name.

    Each graded net must satisfy the structure checks **on its own**: a
    ``SPECIALNETS`` entry that exists but carries no rails, no straps and
    no vias (e.g. a bare ``- VSS + USE GROUND ;``) is a name, not a grid,
    and a power delivery that reaches only one supply is not complete. So
    these are ``all(... > 0)`` across the graded nets rather than a sum,
    which a single fully-routed net could otherwise satisfy for both.
    """
    expected = [net for net in (power_net, ground_net) if net]
    by_name = {net["name"]: net for net in special_nets}
    graded = (
        [by_name[name] for name in expected if name in by_name]
        if expected
        else special_nets
    )

    checks: list[tuple[str, bool]] = []
    if tapcell_master:
        checks.append(("tapcells", tapcells > 0))
    if endcap_master:
        checks.append(("endcaps", endcaps > 0))
    if expect_fillers and filler_masters:
        checks.append(("fillers", fillers > 0))
    if power_net:
        checks.append(("power_special_net", power_net in by_name))
    if ground_net:
        checks.append(("ground_special_net", ground_net in by_name))
    if not expected:
        checks.append(("special_nets", bool(special_nets)))
    checks.append(("followpin_segments", _every_net(graded, "followpin_segments")))
    checks.append(("stripe_segments", _every_net(graded, "stripe_segments")))
    checks.append(("pdn_vias", _every_net(graded, "vias")))
    return checks


def _every_net(graded: list[dict[str, Any]], key: str) -> bool:
    """``True`` when *every* graded net carries at least one ``key``.

    Empty is ``False``: no nets to grade means the structure is absent,
    never vacuously present.
    """
    return bool(graded) and all(net[key] > 0 for net in graded)


def _grade(checks: list[tuple[str, bool]], missing: list[str]) -> str:
    """``complete`` / ``partial`` / ``absent`` for a set of graded checks."""
    if not missing:
        return STATUS_COMPLETE
    return STATUS_ABSENT if len(missing) == len(checks) else STATUS_PARTIAL


def _counts_phrase(placed: dict[str, Any]) -> str:
    """Render the measured counts as the clause every warning quotes."""
    if placed["evidence"] != EVIDENCE_DEF:
        return f"placed counts unavailable ({placed['unavailable_reason']})"
    nets = placed["special_nets"] or []
    followpins = sum(net["followpin_segments"] for net in nets)
    stripes = sum(net["stripe_segments"] for net in nets)
    vias = sum(net["vias"] for net in nets)
    return (
        f"{placed['tapcells']} tapcell(s), {placed['endcaps']} endcap(s), "
        f"{placed['fillers']} filler cell(s), {len(nets)} power special net(s), "
        f"{followpins} FOLLOWPIN rail segment(s), {stripes} STRIPE strap "
        f"segment(s), {vias} PDN via(s)"
    )


def _missing_phrase(placed: dict[str, Any]) -> str:
    """The measured ``missing`` tokens as human labels, naming the special
    net responsible when only *some* of them are deficient.

    Each structure check is graded per net (see :func:`_power_checks`), so
    "no PDN vias" can mean a grid that is fully routed on one supply and a
    bare name on the other. When that asymmetry is the finding, the warning
    says which net, rather than leaving a caller to diff ``special_nets[]``
    by hand; when every net is equally deficient the labels already say it.
    """
    labels = ", ".join(CHECK_LABELS.get(token, token) for token in placed["missing"])
    structural = {"followpin_segments", "stripe_segments", "pdn_vias"}
    nets = placed["special_nets"] or []
    if not structural.intersection(placed["missing"]):
        return labels
    deficient = [
        net["name"]
        for net in nets
        if not (net["followpin_segments"] and net["stripe_segments"] and net["vias"])
    ]
    if not deficient or len(deficient) == len(nets):
        return labels
    return f"{labels} (on special net(s) {', '.join(deficient)})"


def _omitted_power_warning(placed: dict[str, Any]) -> str:
    """The warning for a run that supplied no ``request.power`` block.

    Every clause here is derived from what the DEF actually measured
    (issue #2086's own defect class is an *unmeasured* claim rendered as if
    it were a measurement). It deliberately does **not** assert that the
    layout has no rails or no fill: on ``sky130_fd_sc_hd`` at the
    ``"route"`` stage the row-rail fallback (issue #1442) draws real
    ``SPECIALNETS`` rails and runs ``filler_placement`` without any
    ``request.power`` at all, so a fixed "no fill" template would
    contradict its own measured counts. What is always true, and is what
    this says, is that no power-delivery block was *requested*.
    """
    counts = _counts_phrase(placed)
    tail = (
        "Supply request.power (see docs/cli/place-and-route.md, 'Power "
        "delivery') to build one."
    )
    # "requested", not "configured": the row-rail fallback *does* emit
    # `define_pdn_grid`/`pdngen` Tcl on this path, so only the request side
    # of the statement is unconditionally true.
    head = "request.power was omitted: no power delivery was requested for this run"
    if placed["evidence"] != EVIDENCE_DEF:
        return (
            f"{head}, and what it placed could not be measured ({counts}). "
            "Treat its area, timing and DRC numbers as unverified for "
            f"signoff. {tail}"
        )
    if placed["status"] == STATUS_COMPLETE:
        return (
            f"{head}. The produced DEF nonetheless carries every power "
            f"structure this audit checks for ({counts}) -- whatever drew "
            "them (the cell library's row-rail fallback, or a pre-existing "
            "grid) is not a requested PDN, so this run's area, timing and "
            f"DRC numbers are still not a signoff result. {tail}"
        )
    if placed["status"] == STATUS_ABSENT:
        return (
            f"{head}, and the produced DEF has no {_missing_phrase(placed)} "
            f"({counts}). The layout has no power delivery, no substrate/"
            "well taps and no fill -- its area, timing and DRC numbers are "
            f"not a signoff result. {tail}"
        )
    return (
        f"{head}, and the produced DEF has no {_missing_phrase(placed)} "
        f"({counts}). What it does carry (the cell library's row-rail "
        "fallback, issue #1442) is not a power grid on its own, so this "
        "run's area, timing and DRC numbers are not a signoff result. "
        f"{tail}"
    )


def power_delivery_warnings(
    placed: dict[str, Any], *, power_requested: bool
) -> list[str]:
    """Build the loud ``warnings`` strings for this run's power delivery.

    Two shapes, both quoting the measured counts so the *artifact* carries
    the evidence rather than only the absence of a complaint (issue #2086):

    - ``request.power`` omitted -- always warned about. Omitting it is a
      legal, supported request, but no power delivery was *asked for*, so
      the run's area/timing/DRC numbers are not a signoff result and
      nothing else in the response says so at a glance. The prose is built
      from the measured counts (see :func:`_omitted_power_warning`), never
      from a fixed template -- the row-rail fallback (issue #1442) really
      does place rails and fillers on this path, and a warning that
      asserted otherwise would be the very defect this module exists to
      report.
    - ``request.power`` supplied but the DEF is missing part of it -- the
      *transcription*-error case: a strap layer or pitch that draws nothing
      silently produces the same structurally-empty grid as omitting the
      block entirely.

    Returns ``[]`` for a supplied block whose every check passed, and for a
    supplied block whose evidence is unavailable **only** when there is
    genuinely nothing to say (never true here -- unavailable evidence on a
    supplied block is still reported, because "cannot tell" is not "fine").
    """
    if not power_requested:
        return [_omitted_power_warning(placed)]
    if placed["status"] == STATUS_COMPLETE:
        return []
    if placed["evidence"] != EVIDENCE_DEF:
        return [
            "request.power was supplied but what it placed could not be "
            f"verified ({placed['unavailable_reason']}). Treat this run's "
            "power delivery as unconfirmed."
        ]
    counts = _counts_phrase(placed)
    labels = _missing_phrase(placed)
    return [
        f"request.power was supplied but the produced DEF has no {labels} "
        f"({counts}). Power delivery is {placed['status']} -- check the "
        "request's strap layers/pitch/width against the platform's own PDN "
        "config before using this run's numbers."
    ]
