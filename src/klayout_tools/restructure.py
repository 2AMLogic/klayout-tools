"""Timing-driven restructuring: a bounded cell-resizing loop that closes
setup violations on ``klt synthesize``'s native gate-level critical path
(``klayout_tools.sta``, issue #925) -- Phase 3 of
[Epic #704](https://github.com/2AMLogic/klayout-tools/issues/704), issue
#926.

**Scope and technique.** The minimum-bar restructuring technique this
module implements is *cell resizing*: repeatedly finding the highest-
contribution cell on the reported ``worst_path``, swapping it for a
same-function, higher-drive-strength variant already present in the
resolved liberty (e.g. ``sky130_fd_sc_hd__buf_1`` -> ``__buf_2``), and
re-measuring the whole-netlist critical path via
:func:`klayout_tools.sta.compute_critical_path` (the same #925 engine
``klt synthesize``'s ``sta`` field already calls) until the target period is
met or the loop gives up. Buffer insertion and re-mapping (the acceptance
criteria's two named extensions) are out of scope for this increment -- see
"Known simplifications" below.

**Why cell resizing, why this shape.** A same-family, higher-drive variant
is *pin-compatible by construction* (same pin names/directions -- verified
against the liberty before ever proposing a swap, not assumed from the
naming convention alone) and *never changes the netlist's logic* (drive
strength is a NLDM/electrical property, not a functional one) -- swapping
the leading cell-type token on exactly one instantiation line is therefore
a purely electrical edit, never a semantic one. That is what makes the
technique amenable to the :func:`klt equiv <klayout_tools.equiv.run_equiv>`
acceptance gate this module's caller (``synthesize.py``) wires in after any
resize is actually applied (issue #926 acceptance criterion 3): a
restructuring pass that could change logic would make that gate meaningless.

**Drive-strength naming convention.** Both PDK families this repo has
proven (``sky130_fd_sc_hd``, ``gf180mcu_fd_sc_mcu9t5v0`` -- see
``synthesize.py``'s own :data:`~klayout_tools.synthesize._ABC_CONSTR_INPUTS`/
:data:`~klayout_tools.synthesize._TIE_CELLS` tables) name drive-strength
variants as ``<family>_<drive-strength-integer>`` (``buf_1``, ``buf_2``,
``buf_4``, ...). :func:`_cell_family_and_drive` reads exactly that trailing
``_<digits>`` suffix -- a cell name with no such suffix (e.g. a sequential
cell, a tie cell, or a library that names variants differently) simply has
no upsize candidates, never a guessed one.

**Bounded, greedy, single-technique-per-iteration.** Each iteration tries
*one* resize (the current worst path's highest-contribution resizable cell)
and keeps it only if it actually reduces the worst-path delay; the loop
stops -- reporting ``converged`` plus a ``gave_up_reason`` -- the moment it
either meets the target, runs out of resizable candidates, produces a
non-improving candidate, or exhausts ``max_iterations``
(:data:`DEFAULT_MAX_ITERATIONS`). It never loops unboundedly (issue #926
acceptance criterion 4).

**Known simplifications** (inherited from #925's own engine, see
``klayout_tools/sta.py`` and ``native/statime/README.md``): no wire delay,
no SDC/``create_clock``, a path *delay* is being driven toward a target
*period* with no separate setup/hold margin modeled -- "meets the target"
here means ``worst_path.delay_ns <= target_period_ns``, the same
period-as-budget framing ``constraints.clock_period_ns`` already uses for
ABC's own ``-D`` delay target (``synthesize.py``'s
:func:`~klayout_tools.synthesize._resolve_delay_target_ps`). Hold-time
closure (a *minimum*-delay violation) is not modeled at all -- the engine
reports only the worst (longest) path, never a short-path check -- so this
module addresses setup only, despite the issue title's "setup/hold"
framing; hold closure is a documented gap for a follow-up, not silently
claimed here.
"""

from __future__ import annotations

import re
from typing import Any

from .sta import (
    DEFAULT_INPUT_TRANSITION_NS,
    DEFAULT_OUTPUT_LOAD_PF,
    StaError,
    compute_critical_path,
)

#: The bounded iteration cap this module's docstring promises -- a caller
#: (``synthesize.py``'s ``restructure_max_iterations`` kwarg) may lower or
#: raise it, but every call has *some* cap; there is no "no limit" option.
DEFAULT_MAX_ITERATIONS = 8

#: ``<family>_<drive-strength-integer>`` -- see this module's docstring
#: "Drive-strength naming convention".
_DRIVE_SUFFIX_RE = re.compile(r"^(?P<family>.+)_(?P<drive>\d+)$")

#: Matches a liberty ``cell (name) {`` header; the block itself is read via
#: brace matching from the character right after this match (see
#: :func:`_match_brace`), not by this regex.
_CELL_HEADER_RE = re.compile(r"\bcell\s*\(\s*(?P<name>[A-Za-z0-9_]+)\s*\)\s*\{")

#: Matches a ``pin (name) {`` header *within* an already-isolated cell
#: block. Liberty ``pg_pin (...)`` entries (power/ground) use a different
#: keyword and are never matched here -- exactly the pins this module's
#: pin-compatibility check should ignore (see the module docstring).
_PIN_HEADER_RE = re.compile(r"\bpin\s*\(\s*(?P<name>[A-Za-z0-9_\[\]]+)\s*\)\s*\{")

_DIRECTION_RE = re.compile(r"\bdirection\s*:\s*(?P<direction>input|output)\s*;")


class RestructureError(Exception):
    """Raised when the restructuring loop cannot even be attempted: an
    unreadable liberty/netlist file, or a malformed liberty (unbalanced
    braces). Never raised for "no violation" or "gave up" outcomes -- those
    are reported in :func:`restructure_for_timing`'s own return value, per
    this module's "never fabricate, never silently fail" discipline mirrored
    from :mod:`klayout_tools.sta`.
    """


def _match_brace(text: str, open_index: int) -> int:
    """Return the index of the ``}`` matching the ``{`` at ``text[open_index]``.

    Raises :class:`RestructureError` for unbalanced braces -- a malformed
    liberty file, never silently truncated.
    """
    depth = 0
    for i in range(open_index, len(text)):
        char = text[i]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return i
    raise RestructureError("unbalanced braces while parsing liberty file")


def _parse_liberty_pins(liberty_text: str) -> dict[str, dict[str, str]]:
    """Parse ``cell (name) { ... pin (name) { direction : input|output; ... } ... }``
    blocks out of ``liberty_text``, returning ``{cell_name: {pin_name:
    "input"|"output"}}``.

    Deliberately minimal -- this is not a general liberty parser (that is
    ``native/statime/src/liberty.rs``'s job, for NLDM timing tables this
    module never needs): it extracts only what
    :func:`_cell_pin_compatible` needs to verify a proposed upsize is
    pin-compatible before it is ever proposed. Every other liberty
    construct (timing arcs, ``pg_pin`` power/ground pins, ``bus``/
    ``bundle`` groups) is ignored, not misparsed -- pins with no recognised
    ``direction :`` line are simply absent from the result, and a
    :class:`_cell_family_and_drive`-eligible cell with no pins at all
    resolves to an empty pin dict (which the equality check in
    :func:`_upsize_candidates` treats as pin-incompatible with anything
    that has real pins, never as a false match).
    """
    cells: dict[str, dict[str, str]] = {}
    for header in _CELL_HEADER_RE.finditer(liberty_text):
        cell_name = header.group("name")
        open_index = header.end() - 1  # the '{' this header matched
        close_index = _match_brace(liberty_text, open_index)
        block = liberty_text[open_index : close_index + 1]

        pins: dict[str, str] = {}
        for pin_header in _PIN_HEADER_RE.finditer(block):
            pin_open = pin_header.end() - 1
            try:
                pin_close = _match_brace(block, pin_open)
            except RestructureError:
                continue
            pin_block = block[pin_open : pin_close + 1]
            direction_match = _DIRECTION_RE.search(pin_block)
            if direction_match is not None:
                pins[pin_header.group("name")] = direction_match.group("direction")
        # A later block for the same cell name (should not happen in a
        # well-formed liberty) overwrites rather than merges -- "last one
        # wins" matches how a real liberty parser would treat a duplicate
        # definition.
        cells[cell_name] = pins
    return cells


def _cell_family_and_drive(cell_name: str) -> tuple[str, int] | None:
    """Split ``cell_name`` into ``(family, drive_strength)`` via the trailing
    ``_<digits>`` convention, or ``None`` if it does not match -- see this
    module's docstring "Drive-strength naming convention"."""
    match = _DRIVE_SUFFIX_RE.match(cell_name)
    if match is None:
        return None
    return match.group("family"), int(match.group("drive"))


def _upsize_candidates(
    cell_name: str, pins_by_cell: dict[str, dict[str, str]]
) -> list[str]:
    """Same-family, pin-compatible cells with a strictly higher drive
    strength than ``cell_name``, ascending by drive strength (the smallest
    -- cheapest -- upsize first). Empty when ``cell_name`` does not match
    the drive-strength naming convention, is not present in
    ``pins_by_cell``, or the liberty has no higher-drive, pin-compatible
    sibling.
    """
    parsed = _cell_family_and_drive(cell_name)
    if parsed is None:
        return []
    family, drive = parsed
    current_pins = pins_by_cell.get(cell_name)
    if not current_pins:
        return []

    candidates: list[tuple[int, str]] = []
    for other_name, other_pins in pins_by_cell.items():
        if other_name == cell_name or not other_pins:
            continue
        other_parsed = _cell_family_and_drive(other_name)
        if other_parsed is None:
            continue
        other_family, other_drive = other_parsed
        if other_family != family or other_drive <= drive:
            continue
        if other_pins != current_pins:
            continue
        candidates.append((other_drive, other_name))
    candidates.sort()
    return [name for _, name in candidates]


def _swap_instance_cell_type(
    netlist_text: str, instance_name: str, old_cell_type: str, new_cell_type: str
) -> str:
    """Rewrite exactly one cell instantiation's leading cell-type token in
    ``netlist_text``.

    Scoped to the precise shape ``write_verilog -noattr`` emits (and
    ``native/statime/src/netlist.rs`` parses) -- one instantiation per
    statement, its cell type and instance name together on their own
    line: ``  <cell_type> <instance_name> (``. Matching anchored to the
    start of a line (rather than a bare substring/word-boundary search)
    means this can never touch a same-named instance of a *different* cell
    type, an occurrence inside a comment, or any other instance of the same
    cell type elsewhere in the design -- only the one
    ``old_cell_type``+``instance_name`` pairing.

    Raises :class:`RestructureError` if no such line is found (zero
    matches) or the pairing is ambiguous (more than one -- ``write_verilog``
    never emits a duplicate instance name in one module, so more than one
    match means the caller passed a stale/mismatched cell type or the
    netlist was hand-edited).
    """
    pattern = re.compile(
        r"^([ \t]*)"
        + re.escape(old_cell_type)
        + r"(\s+)"
        + re.escape(instance_name)
        + r"(\s*\()",
        re.MULTILINE,
    )
    matches = list(pattern.finditer(netlist_text))
    if len(matches) != 1:
        raise RestructureError(
            f"expected exactly one instantiation of '{old_cell_type} "
            f"{instance_name}' in the netlist, found {len(matches)}"
        )
    match = matches[0]
    replacement = (
        f"{match.group(1)}{new_cell_type}{match.group(2)}"
        f"{instance_name}{match.group(3)}"
    )
    return netlist_text[: match.start()] + replacement + netlist_text[match.end() :]


def _pick_resize(
    worst_path: dict[str, Any], pins_by_cell: dict[str, dict[str, str]]
) -> tuple[str, str, str] | None:
    """Pick the resize to try next: among ``worst_path["hops"]`` (the
    per-cell breakdown :func:`klayout_tools.sta.compute_critical_path`
    already reports), rank the cell-driven hops by their *incremental*
    delay contribution (this hop's ``arrival_ns`` minus the previous hop's)
    -- the actual worst offender on the path, not just the first cell in
    it -- and return the first one (highest contribution first) that has at
    least one available upsize candidate.

    Returns ``(instance_name, old_cell_type, new_cell_type)``, or ``None``
    when no hop on the path has any upsize candidate at all (every cell is
    already at its library's largest drive strength, or none match the
    drive-strength naming convention) -- the "give up" signal
    :func:`restructure_for_timing` reports via ``gave_up_reason``.
    """
    hops = worst_path.get("hops") or []
    contributions: list[tuple[float, dict[str, Any]]] = []
    previous_arrival = 0.0
    for hop in hops:
        arrival = hop["arrival_ns"]
        if hop.get("cell") is not None:
            contributions.append((arrival - previous_arrival, hop))
        previous_arrival = arrival
    contributions.sort(key=lambda item: item[0], reverse=True)

    for _, hop in contributions:
        cell_type = hop["cell"]
        candidates = _upsize_candidates(cell_type, pins_by_cell)
        if candidates:
            instance_name = hop["point"].split("/", 1)[0]
            return instance_name, cell_type, candidates[0]
    return None


def restructure_for_timing(
    netlist_path: str,
    liberty_path: str,
    top: str,
    target_period_ns: float,
    *,
    output_netlist_path: str,
    max_iterations: int = DEFAULT_MAX_ITERATIONS,
    input_transition_ns: float = DEFAULT_INPUT_TRANSITION_NS,
    output_load_pf: float = DEFAULT_OUTPUT_LOAD_PF,
) -> dict[str, Any]:
    """Close (or reduce) a setup violation on ``netlist_path``'s worst path
    against ``target_period_ns`` (nanoseconds), via the bounded cell-resizing
    loop this module's docstring describes.

    Measures the starting critical path with
    :func:`klayout_tools.sta.compute_critical_path` (same call
    ``synthesize.py``'s own ``sta`` field already makes -- same boundary
    condition by default). If the initial ``worst_path.delay_ns`` already
    meets ``target_period_ns``, returns immediately with ``converged: True``
    and no file written at all -- restructuring a design with no violation
    is a no-op, not a forced pass. Otherwise repeats, up to
    ``max_iterations`` times: pick a resize (:func:`_pick_resize`), apply it
    to a copy of the netlist text, write that candidate to
    ``output_netlist_path``, and re-measure. A candidate is kept only if it
    strictly reduces the worst-path delay; a non-improving candidate, or no
    available candidate at all, ends the loop immediately (bounded,
    greedy -- see the module docstring).

    Returns a report dict:

    - ``target_period_ns``, ``max_iterations`` -- echoed inputs.
    - ``initial_worst_path_delay_ns``, ``final_worst_path_delay_ns``.
    - ``converged`` (bool) -- ``True`` iff the final delay meets the target.
    - ``iterations_used`` -- resizes actually *attempted* (including any
      non-improving one that ended the loop), never more than
      ``max_iterations``.
    - ``gave_up_reason`` -- ``None`` when ``converged`` is ``True``,
      otherwise a human-readable reason (no candidate available, a
      non-improving candidate, the netlist became unanalyzable after a
      resize, or the iteration cap was reached).
    - ``resizes_applied`` -- ``[{"instance", "from_cell", "to_cell"}, ...]``
      in the order they were kept; empty when nothing was ever applied.
    - ``restructured_netlist_path`` -- ``output_netlist_path`` when
      ``resizes_applied`` is non-empty, else ``None`` (nothing was written).

    Raises :class:`RestructureError` for a setup failure (unreadable
    netlist/liberty file) -- never for "gave up", which is a normal,
    reported outcome, not an exception.
    """
    try:
        with open(netlist_path, encoding="utf-8") as handle:
            current_text = handle.read()
    except OSError as exc:
        raise RestructureError(
            f"could not read netlist '{netlist_path}': {exc}"
        ) from exc

    try:
        with open(liberty_path, encoding="utf-8") as handle:
            liberty_text = handle.read()
    except OSError as exc:
        raise RestructureError(
            f"could not read liberty '{liberty_path}': {exc}"
        ) from exc
    pins_by_cell = _parse_liberty_pins(liberty_text)

    def _measure(path: str) -> dict[str, Any]:
        return compute_critical_path(
            path,
            liberty_path,
            top,
            input_transition_ns=input_transition_ns,
            output_load_pf=output_load_pf,
        )

    try:
        sta_result = _measure(netlist_path)
    except StaError as exc:
        raise RestructureError(f"could not analyze starting netlist: {exc}") from exc

    worst_path = sta_result["worst_path"]
    initial_delay = worst_path["delay_ns"]
    worst_delay = initial_delay

    resizes_applied: list[dict[str, str]] = []
    iterations_used = 0
    converged = worst_delay <= target_period_ns
    gave_up_reason: str | None = None

    while not converged and iterations_used < max_iterations:
        iterations_used += 1
        resize = _pick_resize(worst_path, pins_by_cell)
        if resize is None:
            gave_up_reason = (
                "no upsize candidate is available for any cell on the worst path"
            )
            break

        instance_name, old_cell_type, new_cell_type = resize
        try:
            candidate_text = _swap_instance_cell_type(
                current_text, instance_name, old_cell_type, new_cell_type
            )
        except RestructureError as exc:
            gave_up_reason = f"could not apply resize: {exc}"
            break

        try:
            with open(output_netlist_path, "w", encoding="utf-8") as handle:
                handle.write(candidate_text)
        except OSError as exc:
            raise RestructureError(
                f"could not write candidate netlist '{output_netlist_path}': {exc}"
            ) from exc

        try:
            candidate_sta = _measure(output_netlist_path)
        except StaError as exc:
            gave_up_reason = (
                f"resizing {instance_name} ({old_cell_type} -> {new_cell_type}) "
                f"produced a netlist the STA engine could not analyze: {exc}"
            )
            break

        candidate_delay = candidate_sta["worst_path"]["delay_ns"]
        if candidate_delay >= worst_delay:
            gave_up_reason = (
                f"resizing {instance_name} ({old_cell_type} -> {new_cell_type}) "
                f"did not reduce the worst-path delay "
                f"({candidate_delay:.4f}ns >= {worst_delay:.4f}ns)"
            )
            break

        current_text = candidate_text
        worst_path = candidate_sta["worst_path"]
        worst_delay = candidate_delay
        resizes_applied.append(
            {
                "instance": instance_name,
                "from_cell": old_cell_type,
                "to_cell": new_cell_type,
            }
        )
        converged = worst_delay <= target_period_ns

    if not converged and gave_up_reason is None:
        gave_up_reason = (
            f"reached max_iterations ({max_iterations}) without meeting the "
            "target period"
        )

    return {
        "target_period_ns": target_period_ns,
        "max_iterations": max_iterations,
        "initial_worst_path_delay_ns": initial_delay,
        "final_worst_path_delay_ns": worst_delay,
        "converged": converged,
        "iterations_used": iterations_used,
        "gave_up_reason": gave_up_reason,
        "resizes_applied": resizes_applied,
        "restructured_netlist_path": (output_netlist_path if resizes_applied else None),
    }
