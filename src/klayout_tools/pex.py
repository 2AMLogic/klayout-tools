"""``klt pex``: extract a parasitic-annotated netlist from a routed layout,
re-run the schematic testbenches against it per corner, and report a
per-corner, per-spec-row schematic-vs-extracted delta -- Phase 1a of Epic
#709 ("PEX-aware post-layout sim flow for klt").

Pure library: :func:`run_pex` returns plain Python data (a ``dict`` of
JSON-serialisable primitives) and never prints, mirroring ``extract.py`` /
``sim.py``. Serialisation and human-readable formatting live in the CLI
command module (``cli/pex_cmd.py``).

## What this productizes

`.claude/skills/design-extraction/SKILL.md`'s "Post-layout re-verification
workflow" and ``docs/cli/sim.md``'s "Post-layout verification
(``netlist_source``)" section already document a *manual* two-step version
of this workflow: run ``klt extract`` on the layout, then re-run the same
``klt sim`` testbench request against the extracted netlist with
``netlist_source: "extracted"``, and diff the two runs' ``measurements[]``
by hand. ``klt pex`` is that workflow productized into one command: given a
routed layout and one or more of those existing testbench requests, it
drives extraction itself (:func:`~klayout_tools.extract.run_extract` with
``parasitics=True``), re-runs each testbench twice (schematic netlist, then
extracted netlist) via :func:`~klayout_tools.sim.run_sim`, and emits a
per-corner, per-spec-row delta report -- no new testbench format, no
hand-diffing.

## Reusing the existing testbench convention -- the DUT ``.include`` swap

A ``klt sim`` request's ``netlist`` field is a single file (see
``sim.py``'s "Netlist convention" docstring section): it does not separate
"the DUT" from "the stimulus". ``docs/cli/extract.md``'s "Verified
compatible with ``klt sim``'s netlist convention" section documents the
resolution already in use: an extracted netlist is a bare DUT with no
stimulus, so *"a thin testbench ``.include``s the extracted file,
instantiates the ``.subckt``, and adds the sources, and that testbench is
the ``klt sim`` ``netlist``"* -- verified end to end by
``tests/test_extract.py``'s ``test_extracted_netlist_feeds_klt_sim_
unmodified``. This module leans on that same convention for the
*schematic* side too: a testbench request's ``netlist`` file is expected to
``.include``/``.inc`` a separate schematic-equivalent DUT file (hand-written
or generator-produced, sharing the extracted netlist's
``.SUBCKT <top> <pins...> ... .ENDS <top>`` interface) rather than inlining
the DUT's own devices directly. ``klt pex`` re-runs each testbench schematic-
side completely unmodified (reusing the existing testbench file exactly as
authored -- the literal AC #2 requirement: "reusing the existing testbench
conventions, no parallel testbench format"), and for the extracted side,
rewrites *only* that one ``.include``/``.inc`` line to point at the
extraction's own written netlist -- every source, load, and measurement
card in the testbench is untouched. See :func:`_find_dut_include` /
:func:`_rewrite_dut_include`. A testbench with no ``.include``/``.inc``
directive at all cannot be re-pointed this way and is refused up front
(:class:`PexError`); so is a testbench with *more than one* such directive,
since which of them is "the DUT" is not inferable and swapping the first
one silently re-points the wrong file (issue #1030) -- see those functions'
docstrings for the extraction mechanics.

## The other half of that convention: one shared pin list

Because the testbench's own ``X...`` instantiation line is reused
byte-identically on both sides, the schematic DUT's
``.SUBCKT <top> <pins...>`` interface and the extracted netlist's must
declare the *same number of pins*. When they do not -- e.g. a deck whose
extraction promotes a device-body/substrate-tap net to a top-level pin that
a hand-written schematic subcircuit never declares -- a strict-enough
ngspice refuses the extracted-side deck with
``Too few parameters for subcircuit type "<name>"`` (or ``Too many ...``)
and no measurement comes back; a laxer one (e.g. ngspice 42) can silently
accept the mismatched deck instead and simulate to completion with the
extra/missing terminals left dangling. :func:`run_pex` detects this
statically and pre-flight, comparing the two ``.SUBCKT`` headers for the
specific subcircuit ``klt extract`` wrapped its output in *before* either
side ever simulates (issue #1041) -- engine-independent, so behaviour is
identical on every ngspice version -- and reports it as a named, structured
``pin_count_mismatch`` block in its own JSON report (both sides' pin lists
and counts, plus ngspice's own line when the reactive fallback below found
it instead). A reactive fallback (issue #1030) -- inferring the mismatch
from ngspice's own refusal once an extracted-side run already came back
with nothing measured -- remains for mismatches the static header
comparison cannot see, e.g. a subcircuit name only one side declares, or an
unreadable header, rather than leaving those buried in a per-corner
``ngspice.log`` artifact -- see :func:`_pin_count_mismatch` and
``docs/cli/pex.md``. Bridging a genuine interface mismatch (a
caller-supplied pin map, a wrapper subcircuit) is deliberately *not* in
scope here: this reports the mismatch, it does not paper over it.

## Envelope shape: matches the provisional shape #871 already wired into `klt signoff`

Issue #871 (Phase 2b of epic #706, merged before this issue) taught ``klt
signoff``'s ``_classify``/``_check_passed``/``_detail``
(``signoff.py``) to recognise a *provisional*, Curator-proposed ``pex``
envelope shape -- a top-level ``delta`` list (per-corner, per-spec-row
schematic-vs-extracted comparisons) plus a ``reference_netlist`` field --
before this module existed, because Epic #706's own post-layout T1 item
(item 7) needed something to grade against. This module's real
:func:`run_pex` output matches that shape exactly (``delta[]`` entries
carry ``spec_row``/``corner_id``/``schematic_value``/``extracted_value``/
``delta_pct``/``status``; ``status`` is ``"pass"`` only when every ``delta``
row passed), so ``signoff.py``'s existing recognition logic needs no
change -- see ``docs/cli/pex.md`` for the full, ratified contract and
``docs/cli/signoff.md``'s "Item 7 is kind-restricted" section for the
now-reconciled note.

**Scope-mismatch note (Curator-flagged, issue #801):** the provisional
shape's own worked examples (``docs/design-evidence-tiers.md``,
``docs/cli/signoff.md``) invoke ``klt pex extracted.spice schematic.spice
--format json`` -- i.e. *two already-produced netlists* as input. This
issue's own Acceptance Criteria (and Epic #709's Phase 1a goal) instead
take a **routed layout plus a testbench set** as input, since ``klt pex``
must run extraction itself as part of the command, not merely diff two
netlists someone else already produced and simulated. This module follows
the AC (layout + testbenches in), per the issue's explicit resolution of
that discrepancy -- see the issue's PR description for the full note.

## What ``--parasitics`` does and does not model, inherited unchanged

``klt pex`` always extracts with ``parasitics=True`` (first-order lumped
RC -- one series R per net terminal plus one ground C per net, from the
deck's curated sheet-resistance/capacitance table; see
``extract.py``'s ``PARASITIC_MODEL_SCOPE``). It inherits that model's scope
and limits unchanged: quasi-static only, and by default a single lumped
element per net (issue #760's vertical-overlap coupling is always included;
issue #976's ``--critical-net`` lateral coupling and issue #977's
``--distributed-rc`` multi-segment ladder are both opt-in, forwarded
straight through to ``klt extract`` -- see this function's own
``critical_nets``/``distributed_rc`` docstring paragraphs). Neither opt-in
is on by default, so an unmodified caller sees the exact same model as
before either existed. ``result["extraction"]["model"]`` echoes
``PARASITIC_MODEL_SCOPE`` verbatim so a reader of the delta report does not
have to cross-reference ``extract.py`` to know what the "extracted" side of
each row's comparison does and does not account for.
"""

from __future__ import annotations

import copy
import json
import os
import re
from collections.abc import Sequence
from typing import Any

from . import env_provenance
from ._paths import _resolve_relative
from .extract import ExtractError, run_extract
from .sim import SimError, load_request, run_sim

__all__ = ["PexError", "run_pex"]

#: Bumped only on a non-additive (breaking) change to this command's own
#: JSON shape -- see docs/json-contract.md.
#:
#: Bumped 1 -> 2 (issue #1261): `layout`/`netlist`/`reference_netlist` and
#: each `testbenches[]` entry's `request`/`schematic_netlist` changed from a
#: raw (often absolute) path string to the `{path, scope}` shape
#: `env_provenance.repo_relative_path` already defines -- a committed
#: evidence record wraps this response unmodified
#: (`docs/design/sim-evidence-discipline-spike.md`), so an absolute path
#: here used to leak the author's home directory/worktree layout into every
#: such record. See `_report_path` below.
SCHEMA_VERSION = 2

#: A `.include`/`.inc` directive line, ngspice's two spellings, either
#: quoted or bare -- matches `_find_dut_include`'s "thin testbench" swap
#: point. Case-insensitive: ngspice itself accepts either case.
_INCLUDE_RE = re.compile(
    r"^(?P<prefix>\s*\.(?:include|inc)\s+)(?P<target>.+?)\s*$", re.IGNORECASE
)

#: ngspice's own text when a subcircuit is instantiated with the wrong number
#: of terminals -- verified against ngspice 46:
#: ``Too many parameters for subcircuit type "res" (instance: xxres)``,
#: followed by ``Simulation interrupted due to error!``. This is exactly what
#: a schematic/extracted top-level pin-list mismatch produces on the
#: extracted-side run (issue #1030).
_PIN_COUNT_MISMATCH_RE = re.compile(
    r"too\s+(?P<direction>few|many)\s+parameters\s+for\s+subcircuit\s+type\s+"
    r"[\"']?(?P<subckt>[^\"'\s]+)[\"']?",
    re.IGNORECASE,
)

#: A ``.subckt`` header line -- ``group("body")`` is everything after the
#: directive (the subcircuit name, then its pins, then optional
#: ``name=value`` parameters). See :func:`_subckt_interfaces`.
_SUBCKT_RE = re.compile(r"^\s*\.subckt\s+(?P<body>\S.*?)\s*$", re.IGNORECASE)


class PexError(Exception):
    """Raised when a `klt pex` run cannot be completed: a bad testbench
    request, a testbench with no `.include`/`.inc` DUT reference, testbenches
    that reference different schematic netlists, or a failure in the
    extraction/simulation steps this command drives (wrapping the original
    :class:`~klayout_tools.extract.ExtractError` /
    :class:`~klayout_tools.sim.SimError`).

    The CLI turns this into a clean stderr message + exit code 1, never a
    traceback -- matching every other `klt` verb's error contract.
    """


def _report_path(path: str | None, *, repo_root: str | None) -> dict[str, Any]:
    """Normalise one input-path field for this module's own JSON response
    (issue #1261) -- a thin, module-local wrapper over
    :func:`~klayout_tools.env_provenance.repo_relative_path` rather than a
    second normalizer, so `klt pex`'s reports and `klt env-provenance`'s own
    emitter agree on exactly one `{path, scope}` shape.

    Deliberately **not** applied to the nested `pin_count_mismatch`/
    `flat_dut_mismatch` diagnostic blocks' own `netlist` fields -- those are
    out of this issue's confirmed scope (tracked separately if it turns out
    to matter) and stay raw strings, unchanged.
    """
    return env_provenance.repo_relative_path(path, repo_root=repo_root)


def _strip_quotes(token: str) -> str:
    if len(token) >= 2 and token[0] == token[-1] and token[0] in ("'", '"'):
        return token[1:-1]
    return token


def _find_dut_include(body_text: str, body_dir: str) -> tuple[int, str]:
    """Locate *the* `.include`/`.inc` directive in a testbench body
    (``body_text``, read from the file at ``body_dir``'s sibling) -- the
    "thin testbench `.include`s the DUT" convention this module's docstring
    documents.

    Returns ``(line_index, resolved_dut_path)``: the zero-based line index
    of the directive (for :func:`_rewrite_dut_include`) and the absolute
    path of the file it names (relative targets resolve against
    ``body_dir``, the testbench body file's own directory -- the same
    convention ngspice itself uses for `.include`).

    Raises :class:`PexError` if the body contains no such directive -- a
    flat testbench with the DUT's own devices inlined directly has no
    single swap point this module can re-point at the extracted netlist
    without also parsing/rewriting device cards, which is out of scope (see
    this module's docstring).

    Also raises :class:`PexError` if the body contains **more than one**
    such directive (issue #1030). Which of several `.include`d files is
    "the DUT" is not inferable from the directive alone -- a testbench that
    also `.include`s, say, a PDK's global switch-parameter file ahead of its
    DUT is entirely legitimate ngspice, and the earlier first-match-wins
    behaviour silently re-pointed *that* file at the extracted netlist,
    leaving the real DUT in place and reporting the wrong
    ``reference_netlist``. A hard error naming every matched line is the
    documented behaviour instead; the fix is to leave exactly one
    `.include`/`.inc` line in the testbench body (fold the others into the
    DUT file, or inline them).
    """
    matches: list[tuple[int, str, str]] = []
    for index, line in enumerate(body_text.splitlines()):
        match = _INCLUDE_RE.match(line)
        if match:
            target = _strip_quotes(match.group("target").strip())
            matches.append((index, line.strip(), target))

    if len(matches) > 1:
        listed = "; ".join(f"line {index + 1}: `{text}`" for index, text, _ in matches)
        raise PexError(
            f"testbench netlist has {len(matches)} `.include`/`.inc` "
            f"directives ({listed}) -- klt pex swaps exactly one of them for "
            "the extracted netlist and cannot tell which one is the DUT "
            "(before this was an error it silently swapped the first one, "
            "which re-pointed the wrong file and reported the wrong "
            "reference_netlist -- issue #1030). Leave exactly one "
            "`.include`/`.inc` line in the testbench body: fold any other "
            "included file (a PDK switch-parameter file, shared `.param` "
            "cards) into the DUT netlist itself or inline it into the "
            "testbench"
        )
    if matches:
        index, _text, target = matches[0]
        return index, _resolve_relative(target, body_dir)

    raise PexError(
        "testbench netlist has no `.include`/`.inc` directive -- klt pex "
        "requires the schematic-side testbench to `.include` its DUT "
        "netlist file (the \"thin testbench .include's the extracted "
        'file" convention -- see docs/cli/extract.md\'s "Verified '
        "compatible with klt sim's netlist convention\" and docs/cli/"
        "pex.md), so the DUT can be swapped for the extracted netlist "
        "without touching the rest of the testbench"
    )


def _rewrite_dut_include(body_text: str, line_index: int, new_dut_path: str) -> str:
    """Return ``body_text`` with the `.include`/`.inc` directive at
    ``line_index`` re-pointed at ``new_dut_path`` (always re-quoted) --
    every other line (sources, loads, `.model` cards, measurements) is
    byte-identical to the input.
    """
    lines = body_text.splitlines(keepends=True)
    line = lines[line_index]
    match = _INCLUDE_RE.match(line.rstrip("\n"))
    assert match is not None, "line_index must name a line _find_dut_include matched"
    newline = "\n" if line.endswith("\n") else ""
    lines[line_index] = f'{match.group("prefix")}"{new_dut_path}"{newline}'
    return "".join(lines)


def _check_dut_declares_a_circuit(testbench_path: str, dut_path: str) -> None:
    """Raise :class:`PexError` if ``dut_path`` -- the sole `.include`/`.inc`
    target :func:`_find_dut_include` resolved for ``testbench_path`` -- does
    not plausibly *be* a DUT at all (Gap 1, issue #1255).

    A testbench whose only `.include` happens to name a shared parameter/
    model/corner file (declaring no `.SUBCKT` and no circuit element of its
    own -- e.g. a PDK's global switch-parameter file, or a corner-selection
    `.param` file, `.include`d because the testbench structurally can't
    `.include` the real DUT at all, e.g. its own devices are decomposed and
    written directly in the testbench body instead) used to be silently
    swapped in as if it were the DUT: the run would complete with
    `status: "pass"`, `reference_netlist` naming a file that is not the DUT
    at all, and an empty (or spurious) `delta[]` -- a vacuous pass, not an
    error. This is a hard, up-front error instead (before either side ever
    simulates), matching :func:`_find_dut_include`'s own two sibling error
    cases (no `.include` at all, or more than one).

    Deliberately permissive about *how* the DUT is written -- see
    :func:`_dut_declares_a_circuit`'s own docstring for what does and does
    not count as "a circuit".
    """
    text = _read_text(dut_path)
    if text is None:
        raise PexError(
            f"testbench '{testbench_path}': `.include`/`.inc` target "
            f"could not be read: {dut_path}"
        )
    if _dut_declares_a_circuit(text):
        return
    raise PexError(
        f"testbench '{testbench_path}': its single `.include`/`.inc` "
        f"directive names {dut_path!r}, but that file declares no "
        "`.SUBCKT` and no circuit element of its own -- it parses as "
        "parameter/model/corner-file content only, so it cannot be the "
        "DUT klt pex is meant to swap for the extracted netlist (issue "
        "#1255). If the DUT's own devices are written directly in the "
        "testbench body instead of `.include`d as a separate file (e.g. "
        "to decompose it into discrete primitives, to break a feedback "
        "loop a black-box DUT doesn't expose a port for), klt pex cannot "
        "re-point at the extracted netlist without also rewriting those "
        "device cards, which is out of scope -- rewrite the testbench so "
        "its single `.include`/`.inc` line names the schematic DUT file "
        "itself"
    )


def _read_text(path: str) -> str | None:
    """``path``'s text, or ``None`` if it cannot be read -- every caller here
    is building a *diagnostic*, so an unreadable file degrades the diagnostic
    rather than raising over it."""
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            return handle.read()
    except OSError:
        return None


def _logical_lines(netlist_text: str) -> list[str]:
    """``netlist_text``'s lines with SPICE ``+`` continuation lines folded
    into the line they continue, and inline ``$``/``;`` comments stripped --
    so :func:`_subckt_interfaces` sees each `.subckt` header whole."""
    lines: list[str] = []
    for raw in netlist_text.splitlines():
        line = re.split(r"\s\$|;", raw, maxsplit=1)[0].rstrip()
        stripped = line.lstrip()
        if stripped.startswith("+") and lines:
            lines[-1] = f"{lines[-1]} {stripped[1:].strip()}"
        else:
            lines.append(line)
    return lines


def _subckt_interfaces(netlist_text: str) -> dict[str, tuple[str, list[str]]]:
    """``{lowercased subckt name: (name as written, [pin names])}`` for every
    ``.subckt`` header in ``netlist_text``, in file order.

    Pins stop at the first ``name=value`` token (or a ``params:`` keyword) --
    a `.subckt` header's trailing default-parameter assignments are not part
    of its terminal list. Lowercased keys because ngspice itself is
    case-insensitive about subcircuit names, and `klt extract` writes an
    upper-case ``.SUBCKT`` header where a hand-written schematic netlist
    often does not.
    """
    interfaces: dict[str, tuple[str, list[str]]] = {}
    for line in _logical_lines(netlist_text):
        match = _SUBCKT_RE.match(line)
        if not match:
            continue
        tokens = match.group("body").split()
        name = tokens[0]
        pins: list[str] = []
        for token in tokens[1:]:
            if "=" in token or token.lower() in ("params:", "param:"):
                break
            pins.append(token)
        interfaces.setdefault(name.lower(), (name, pins))
    return interfaces


def _dut_declares_a_circuit(text: str) -> bool:
    """``True`` if ``text`` -- a ``.include``/``.inc`` target's own file
    contents -- declares at least one ``.subckt`` header or circuit element
    card of its own, i.e. plausibly *is* a DUT netlist rather than
    parameter/model/corner-file content with no circuit of its own (Gap 1,
    issue #1255: a testbench whose only `.include` happens to name a shared
    corner/parameter file used to be silently swapped in as if it were the
    DUT).

    Deliberately permissive about *how* the DUT is written: both a
    ``.SUBCKT``-wrapped file (any header at all, even an empty body --
    ``_subckt_interfaces`` only parses headers) and a flat file (bare device
    cards, no wrapper) pass this check; only a file with *no* circuit
    content at all -- pure ``.param``/``.model``/``.lib``/comment/`.` control
    cards -- fails it. A flat schematic DUT against a `.SUBCKT`-wrapped
    extraction is a *different*, separately-diagnosed condition -- see
    :func:`_flat_dut_mismatch`.

    Every non-blank line that survives comment-stripping and is not itself
    a `.`-prefixed control card is treated as a circuit element -- SPICE's
    own convention is that such a line names a device/subcircuit instance
    (``R``/``C``/``L``/``M``/``Q``/``D``/``X``/``V``/``I``/...), so no fixed
    prefix list is needed.
    """
    if _subckt_interfaces(text):
        return True
    for line in _logical_lines(text):
        stripped = line.strip()
        if not stripped or stripped.startswith(("*", ".")):
            continue
        return True
    return False


def _mismatch_side(
    netlist_path: str, interface: tuple[str, list[str]] | None
) -> dict[str, Any]:
    return {
        "netlist": netlist_path,
        "subcircuit": interface[0] if interface is not None else None,
        "pin_count": len(interface[1]) if interface is not None else None,
        "pins": list(interface[1]) if interface is not None else None,
    }


def _pin_count_mismatch(
    *,
    reference_netlist: str,
    extracted_netlist: str,
    subcircuit: str | None = None,
    ngspice_message: str | None = None,
) -> dict[str, Any] | None:
    """The structured ``pin_count_mismatch`` diagnostic (issue #1030), or
    ``None`` when no mismatch can be established.

    Compares the ``.SUBCKT`` interfaces the schematic DUT
    (``reference_netlist``) and the extraction (``extracted_netlist``)
    declare for the same subcircuit name and reports the first one whose pin
    *counts* differ -- the exact condition ngspice rejects with ``Too
    few/many parameters for subcircuit type``, since `klt pex` reuses the
    testbench's own unmodified ``X...`` instantiation line on both sides.

    ``subcircuit`` (when ngspice itself named one) restricts the comparison
    to that subcircuit. ``ngspice_message`` is ngspice's own line, carried
    through verbatim; when it is present but the two netlists' headers
    cannot be compared (unreadable file, a name only one side declares) the
    diagnostic is still returned, with ``null`` pin data -- ngspice's own
    verdict is authoritative on its own.
    """
    schematic_text = _read_text(reference_netlist)
    extracted_text = _read_text(extracted_netlist)
    schematic_interfaces = _subckt_interfaces(schematic_text or "")
    extracted_interfaces = _subckt_interfaces(extracted_text or "")

    candidates = (
        [subcircuit.lower()] if subcircuit is not None else list(extracted_interfaces)
    )
    for key in candidates:
        schematic_interface = schematic_interfaces.get(key)
        extracted_interface = extracted_interfaces.get(key)
        if schematic_interface is None or extracted_interface is None:
            continue
        if len(schematic_interface[1]) == len(extracted_interface[1]):
            continue
        return {
            "subcircuit": extracted_interface[0],
            "schematic": _mismatch_side(reference_netlist, schematic_interface),
            "extracted": _mismatch_side(extracted_netlist, extracted_interface),
            "ngspice_message": ngspice_message,
            "detail": (
                f"the extracted netlist declares `.SUBCKT "
                f"{extracted_interface[0]}` with {len(extracted_interface[1])} "
                f"pins ({', '.join(extracted_interface[1])}) but the schematic "
                f"DUT declares {len(schematic_interface[1])} "
                f"({', '.join(schematic_interface[1])}) -- klt pex re-runs the "
                "testbench's own unmodified subcircuit instantiation line "
                "against both, so ngspice refuses the extracted-side deck. "
                "Give the schematic subcircuit the same pin list as the "
                "extracted one (extraction promotes physical nets a schematic "
                "often does not model, e.g. a device-body/substrate-tap net), "
                "or extract with a `--top` cell whose interface matches"
            ),
        }

    if ngspice_message is None:
        return None
    return {
        "subcircuit": subcircuit,
        "schematic": _mismatch_side(
            reference_netlist,
            schematic_interfaces.get(subcircuit.lower()) if subcircuit else None,
        ),
        "extracted": _mismatch_side(
            extracted_netlist,
            extracted_interfaces.get(subcircuit.lower()) if subcircuit else None,
        ),
        "ngspice_message": ngspice_message,
        "detail": (
            "ngspice refused the extracted-side deck because the testbench's "
            "subcircuit instantiation line does not match the extracted "
            "netlist's `.SUBCKT` pin list -- klt pex re-runs that line "
            "unmodified against both the schematic DUT and the extracted "
            "netlist, so the two must declare the same number of pins"
        ),
    }


def _flat_dut_mismatch(
    *,
    reference_netlist: str,
    extracted_netlist: str,
    top_cell: str,
) -> dict[str, Any] | None:
    """The structured ``flat_dut_mismatch`` diagnostic (Gap 2, issue #1255):
    the schematic DUT (``reference_netlist``) declares **no** `.SUBCKT`
    header at all -- i.e. it is netlisted flat, with the testbench relying
    on the DUT's own internal nodes being ordinary top-level nets -- while
    the extraction (``extracted_netlist``) always writes a
    `.SUBCKT <top_cell>` wrapper (`klt extract`'s own convention, `top_cell`
    being its own top-cell name -- ``run_extract``'s ``"top"`` report
    field). Swapping the flat schematic `.include` for the `.SUBCKT`-wrapped
    extracted one silently disconnects every testbench source/probe from
    the DUT: `klt pex` only re-points the `.include`/`.inc` line, it never
    adds an `X...` instantiation of the newly-wrapped subcircuit.

    Unlike :func:`_pin_count_mismatch` -- which is *reactive*, only ever
    consulted once an extracted-side simulation attempt has already come
    back with nothing measured -- this check is *proactive*: it runs once,
    up front, from the two netlists' text alone, before either side
    simulates. That matters because a flat-vs-wrapped mismatch does not
    reliably fail loudly: a testbench with no `.ic`/similar card referencing
    the now-disconnected nodes may simply simulate the disconnected fixture
    to completion and report a plausible-looking but physically meaningless
    number for a measurement that happens to be well-defined on the
    stimulus side alone -- exactly the failure mode
    :func:`_pin_count_mismatch`'s "nothing measured came back" gate cannot
    catch.

    Returns ``None`` when the schematic DUT declares *any* `.SUBCKT` at all
    (not flat -- a real interface mismatch there, if any, is
    :func:`_pin_count_mismatch`'s job) or the extracted netlist does not
    itself declare ``top_cell`` as a `.SUBCKT` (should not happen -- `klt
    extract` always wraps its own output -- but degrades safely to "no
    diagnostic" rather than raising if it ever does).
    """
    schematic_text = _read_text(reference_netlist)
    if _subckt_interfaces(schematic_text or ""):
        return None  # not flat -- `_pin_count_mismatch`'s job, if any

    extracted_text = _read_text(extracted_netlist)
    extracted_interfaces = _subckt_interfaces(extracted_text or "")
    extracted_interface = extracted_interfaces.get(top_cell.lower())
    if extracted_interface is None:
        return None

    return {
        "subcircuit": extracted_interface[0],
        "schematic": _mismatch_side(reference_netlist, None),
        "extracted": _mismatch_side(extracted_netlist, extracted_interface),
        "ngspice_message": None,
        "detail": (
            f"the schematic DUT ({reference_netlist}) declares no `.SUBCKT` "
            "at all -- it is netlisted flat, with the testbench referencing "
            "its internal nodes directly as top-level nets -- but the "
            f"extracted netlist always wraps its output in `.SUBCKT "
            f"{extracted_interface[0]}` ({len(extracted_interface[1])} pins: "
            f"{', '.join(extracted_interface[1])}). klt pex only re-points "
            "the testbench's `.include`/`.inc` line at the extracted "
            "netlist -- it does not add an `X...` instantiation -- so every "
            "source/probe the testbench wires to the flat DUT's own nodes "
            "is left disconnected from the extracted netlist's `.SUBCKT` "
            "body on the extracted-side run, which may simulate to "
            "completion and report a spurious value rather than error. Wrap "
            "the schematic DUT in a matching `.SUBCKT ... .ENDS` (with the "
            "same top-level pin list `klt extract` produces) so the "
            "testbench's own instantiation line can be reused unmodified "
            "on both sides, the same convention `pin_count_mismatch` "
            "already requires"
        ),
    }


def _match_line(text: str, match: re.Match[str]) -> str:
    """The whole (stripped) line of ``text`` containing ``match``."""
    start = text.rfind("\n", 0, match.start()) + 1
    end = text.find("\n", match.start())
    return text[start : end if end != -1 else len(text)].strip()


def _pin_count_mismatch_from_message(
    message: str,
    *,
    reference_netlist: str,
    extracted_netlist: str,
) -> dict[str, Any] | None:
    """The ``pin_count_mismatch`` diagnostic for a chunk of engine output
    (an ngspice log, or a :class:`~klayout_tools.sim.SimError`'s text) that
    carries ngspice's own pin-count-mismatch line -- ``None`` when it does
    not."""
    match = _PIN_COUNT_MISMATCH_RE.search(message)
    if match is None:
        return None
    return _pin_count_mismatch(
        reference_netlist=reference_netlist,
        extracted_netlist=extracted_netlist,
        subcircuit=match.group("subckt"),
        ngspice_message=_match_line(message, match),
    )


def _pin_count_mismatch_from_report(
    sim_report: dict[str, Any],
    *,
    reference_netlist: str,
    extracted_netlist: str,
) -> dict[str, Any] | None:
    """The ``pin_count_mismatch`` diagnostic for an extracted-side `klt sim`
    response that came back with *no* measured value anywhere -- ``None``
    otherwise.

    A pin-count mismatch does not raise: `klt sim` classifies engine failures
    from the ngspice log and reports them as a corner-level ``status:
    "error"`` (every measurement "produced no value"), which is exactly what
    ngspice's ``Too few parameters for subcircuit type`` + ``Simulation
    interrupted due to error!`` produces. So the detection runs here, on the
    returned response: ngspice's own line when the corner kept its
    ``ngspice.log`` artifact, falling back to a direct comparison of the two
    netlists' ``.SUBCKT`` headers when it did not.

    Deliberately gated on "nothing at all came back": a run that produced any
    value cannot have been rejected for a pin-count mismatch, so a successful
    (or merely limit-failing) run can never pick up a false-positive
    diagnostic here.
    """
    corners = sim_report.get("corners") or []
    measurements = [m for corner in corners for m in corner["measurements"]]
    if not measurements or any(m["value"] is not None for m in measurements):
        return None

    for corner in corners:
        log_path = (corner.get("artifacts") or {}).get("log")
        if not log_path or not os.path.isfile(log_path):
            continue
        mismatch = _pin_count_mismatch_from_message(
            _read_text(log_path) or "",
            reference_netlist=reference_netlist,
            extracted_netlist=extracted_netlist,
        )
        if mismatch is not None:
            return mismatch

    return _pin_count_mismatch(
        reference_netlist=reference_netlist,
        extracted_netlist=extracted_netlist,
    )


def _index_corner_measurements(
    sim_report: dict[str, Any],
) -> dict[str, dict[str, dict[str, Any]]]:
    """``{corner_id: {measurement_name: measurement_dict}}`` from a `klt sim`
    response's ``corners[]`` -- the per-(corner, spec-row) lookup
    :func:`_build_delta_rows` diffs schematic against extracted with."""
    index: dict[str, dict[str, dict[str, Any]]] = {}
    for corner in sim_report["corners"]:
        index[corner["corner_id"]] = {m["name"]: m for m in corner["measurements"]}
    return index


def _delta_pct(schematic_value: Any, extracted_value: Any) -> float | None:
    if (
        schematic_value is None
        or extracted_value is None
        or not isinstance(schematic_value, (int, float))
        or not isinstance(extracted_value, (int, float))
        or schematic_value == 0
    ):
        return None
    return round(100.0 * (extracted_value - schematic_value) / abs(schematic_value), 3)


def _row_status(
    schematic_measurement: dict[str, Any] | None, extracted_measurement: dict[str, Any]
) -> str:
    """A delta row's own ``status`` -- mirrors `klt sim`'s own per-
    measurement limit evaluation on the *extracted* side (the side item 7
    actually grades): ``"pass"`` unless the extracted measurement itself
    failed or errored, or the schematic side is missing/errored (in which
    case no trustworthy delta exists to report at all -- ``"error"``, never
    a silently-fabricated ``"pass"``)."""
    extracted_status = extracted_measurement["status"]
    schematic_status = (
        schematic_measurement["status"]
        if schematic_measurement is not None
        else "error"
    )
    if (
        schematic_measurement is None
        or schematic_status == "error"
        or extracted_status == "error"
    ):
        return "error"
    if extracted_status == "fail":
        return "fail"
    return "pass"


def _build_delta_rows(
    *,
    spec_row_prefix: str | None,
    schematic_report: dict[str, Any],
    extracted_report: dict[str, Any],
) -> list[dict[str, Any]]:
    schematic_index = _index_corner_measurements(schematic_report)
    rows: list[dict[str, Any]] = []
    for corner in extracted_report["corners"]:
        corner_id = corner["corner_id"]
        schematic_measurements = schematic_index.get(corner_id, {})
        for extracted_measurement in corner["measurements"]:
            name = extracted_measurement["name"]
            spec_row = name if spec_row_prefix is None else f"{spec_row_prefix}.{name}"
            schematic_measurement = schematic_measurements.get(name)
            rows.append(
                {
                    "spec_row": spec_row,
                    "corner_id": corner_id,
                    "schematic_value": (
                        schematic_measurement["value"]
                        if schematic_measurement is not None
                        else None
                    ),
                    "extracted_value": extracted_measurement["value"],
                    "delta_pct": _delta_pct(
                        schematic_measurement["value"]
                        if schematic_measurement is not None
                        else None,
                        extracted_measurement["value"],
                    ),
                    "status": _row_status(schematic_measurement, extracted_measurement),
                }
            )
    return rows


def _unextracted_delta_rows(
    *,
    spec_row_prefix: str | None,
    schematic_report: dict[str, Any],
) -> list[dict[str, Any]]:
    """Delta rows for a testbench whose *extracted* side never produced a
    response at all (issue #1030's pin-count mismatch, surfaced as a
    :class:`~klayout_tools.sim.SimError`): one row per schematic-side
    ``(corner, measurement)`` with a ``null`` extracted value and ``status:
    "error"``.

    Same row shape as :func:`_build_delta_rows` -- a caller still gets a
    per-corner JSON report naming exactly which rows have no trustworthy
    extracted value, instead of the whole run aborting with only a stderr
    message.
    """
    rows: list[dict[str, Any]] = []
    for corner in schematic_report["corners"]:
        for measurement in corner["measurements"]:
            name = measurement["name"]
            rows.append(
                {
                    "spec_row": name
                    if spec_row_prefix is None
                    else f"{spec_row_prefix}.{name}",
                    "corner_id": corner["corner_id"],
                    "schematic_value": measurement["value"],
                    "extracted_value": None,
                    "delta_pct": None,
                    "status": "error",
                }
            )
    return rows


def _prepare_extracted_request(
    *,
    testbench_path: str,
    extracted_netlist_path: str,
    work_dir: str,
    label: str,
) -> str:
    """Build (and write, under ``work_dir``) an "extracted-side" copy of the
    testbench request at ``testbench_path``: identical in every field except
    ``netlist`` (re-pointed at a new DUT-swapped body file, see
    :func:`_rewrite_dut_include`) and ``netlist_source`` (forced to
    ``"extracted"``). Returns the written request file's path, suitable for
    :func:`~klayout_tools.sim.run_sim`.

    Any relative ``models.lib`` the original request declares is resolved
    to an absolute path first -- the written copy lives in ``work_dir``,
    not next to the original testbench file, so a relative reference would
    otherwise resolve against the wrong directory.
    """
    request = load_request(testbench_path)
    request_dir = os.path.dirname(os.path.abspath(testbench_path))

    schematic_body_path = _resolve_relative(request["netlist"], request_dir)
    with open(schematic_body_path, encoding="utf-8") as handle:
        body_text = handle.read()
    body_dir = os.path.dirname(schematic_body_path)
    include_line, _dut_path = _find_dut_include(body_text, body_dir)
    extracted_body_text = _rewrite_dut_include(
        body_text, include_line, extracted_netlist_path
    )

    extracted_body_path = os.path.join(
        work_dir, f"{label}.extracted-dut-testbench.spice"
    )
    with open(extracted_body_path, "w", encoding="utf-8") as handle:
        handle.write(extracted_body_text)

    extracted_request = copy.deepcopy(request)
    models = extracted_request.get("models")
    if isinstance(models, dict) and isinstance(models.get("lib"), str):
        models = dict(models)
        models["lib"] = _resolve_relative(models["lib"], request_dir)
        extracted_request["models"] = models
    extracted_request["netlist"] = extracted_body_path
    extracted_request["netlist_source"] = "extracted"

    extracted_request_path = os.path.join(work_dir, f"{label}.extracted-request.json")
    with open(extracted_request_path, "w", encoding="utf-8") as handle:
        json.dump(extracted_request, handle, indent=2)
    return extracted_request_path


def run_pex(
    layout_path: str,
    testbench_paths: Sequence[str],
    deck_name: str,
    *,
    output: str | None = None,
    top: str | None = None,
    pdk_variant: str | None = None,
    pdk_root: str | None = None,
    artifacts_dir: str | None = None,
    backend: str | None = None,
    critical_nets: Sequence[str] | None = None,
    distributed_rc: bool = False,
    mom_rlc_net: str | None = None,
    mom_rlc_resistance_ohm: float | None = None,
    mom_rlc_capacitance_ff: float | None = None,
    mom_rlc_inductance_nh: float | None = None,
) -> dict[str, Any]:
    """Extract ``layout_path`` (with lumped-RC parasitics) and re-run every
    testbench in ``testbench_paths`` against both the schematic DUT it
    already references and the newly-extracted one, producing a per-corner,
    per-spec-row delta report.

    ``deck_name``/``top``/``pdk_variant``/``pdk_root``/``output`` are passed
    through to :func:`~klayout_tools.extract.run_extract` (``parasitics``
    is always ``True`` here -- ``klt pex`` has no schematic-equivalent-only
    mode; use `klt extract` directly for that). ``critical_nets`` (``klt
    pex --critical-net``, repeatable, issue #976, Epic #709 Phase 2a) is
    also passed straight through to :func:`~klayout_tools.extract.run_extract`
    -- extracts lateral (same-layer, sidewall) coupling capacitance for any
    same-layer net pair naming one of these nets, in addition to Phase 1's
    always-on vertical-overlap coupling (issue #760), so the re-simulated
    extracted-side testbench (and the resulting `delta[]` rows) reflect it.
    ``None``/empty (the default) skips it entirely -- byte-identical to
    before this feature existed. ``distributed_rc`` (``klt pex
    --distributed-rc``, issue #977, Epic #709 Phase 2b) is also passed
    straight through to :func:`~klayout_tools.extract.run_extract` --
    replaces the single-lumped-element star model with a distributed,
    multi-segment RC ladder for every ``critical_nets``-named net (requires
    ``critical_nets`` to be non-empty), so the re-simulated extracted-side
    testbench (and the resulting `delta[]` rows) reflect the finer-grained
    model. ``False`` (the default) skips it entirely -- byte-identical to
    before this feature existed. ``mom_rlc_net``/``mom_rlc_resistance_ohm``/
    ``mom_rlc_capacitance_ff``/``mom_rlc_inductance_nh`` (``klt pex
    --mom-rlc-net <net> --mom-rlc-resistance-ohm <r>
    --mom-rlc-capacitance-ff <c> [--mom-rlc-inductance-nh <l>]``, issue
    #988, Epic #709 Phase 3a) are also passed straight through to
    :func:`~klayout_tools.extract.run_extract` -- substitutes a
    caller-supplied, directly-solved R/L/C for one named net (e.g. from a
    separate ``klt mom`` run against that net's real geometry) in place of
    this extraction's own Phase 1/2 lumped-RC/coupling-C value for that net,
    so the re-simulated extracted-side testbench (and the resulting
    `delta[]` rows) reflect a MoM-grade parasitic on exactly the net a
    caller has singled out as critical. See ``run_extract``'s own
    ``mom_rlc_net`` docstring paragraph for the full substitution/validation
    rules. ``None`` (the default) for all four skips this entirely --
    byte-identical to before this feature existed. ``backend`` is passed
    through to every :func:`~klayout_tools.sim.run_sim` call (schematic and
    extracted side, every testbench) -- see ``docs/cli/sim.md``'s
    "Execution backends". ``artifacts_dir`` overrides where this command
    writes its own generated artifacts (the extracted-side testbench body/
    request copies, see :func:`_prepare_extracted_request`, and each `klt
    sim` call's own ``options.keep_artifacts`` output, namespaced per
    testbench and per side as ``<artifacts_dir>/<testbench label>/
    schematic|extracted/``); it defaults to a ``.klt/pex/`` directory next
    to ``layout_path`` (the same "next to the input" convention `klt
    render`/`klt sim` already use).

    Each testbench's schematic-side run reuses the testbench file
    *completely unmodified* -- `run_sim(testbench_path, ...)`, exactly the
    request a caller already runs today (AC #2's "reusing the existing
    testbench conventions, no parallel testbench format"). The extracted-
    side run re-points only the testbench's `.include`/`.inc` DUT reference
    at the freshly-extracted netlist (see :func:`_prepare_extracted_request`)
    and tags `netlist_source: "extracted"`, reusing `klt sim`'s existing
    field (`docs/cli/sim.md`'s "Post-layout verification") rather than
    inventing a parallel mechanism.

    Every testbench must `.include`/`.inc` the *same* resolved schematic DUT
    file -- `klt pex` reports one `reference_netlist` for the whole run, not
    per testbench; testbenches for the same block conventionally all
    `.include` the same schematic netlist. Raises :class:`PexError` if they
    disagree.

    Each testbench body must carry **exactly one** `.include`/`.inc`
    directive -- none (nothing to swap) and more than one (no way to tell
    which one is the DUT) are both refused up front with a
    :class:`PexError`, before any simulation runs (issue #1030; see
    :func:`_find_dut_include`). That single `.include`/`.inc` target must
    also itself plausibly *be* a DUT -- a shared corner/parameter file with
    no `.SUBCKT` and no circuit element of its own is likewise refused up
    front with a :class:`PexError`, rather than silently swapped in for a
    vacuous `status: "pass"` (issue #1255, Gap 1; see
    :func:`_check_dut_declares_a_circuit`).

    A schematic/extracted top-level *pin-list* mismatch -- the extracted
    netlist declaring a different number of `.SUBCKT` pins than the
    schematic DUT for the specific subcircuit `klt extract` wrapped its
    output in -- is **not** an abort: the report is still emitted, with
    per-corner `delta[]` rows carrying a `null` `extracted_value` and the
    named `pin_count_mismatch` block naming both sides' pin lists. Detected
    proactively by default, from the two netlists' own `.SUBCKT` headers
    alone, before either side simulates (issue #1041) -- engine-independent,
    so it fires identically on every ngspice version rather than only on the
    versions strict enough to reject the mismatched deck outright with `Too
    few/many parameters for subcircuit type` (older ngspice, e.g. 42, can
    silently accept a mismatched deck instead). The reactive check ngspice's
    own refusal drives (issue #1030) remains as a fallback for mismatches
    the static comparison cannot see -- e.g. a subcircuit name only one side
    declares, or an unreadable header (see :func:`_pin_count_mismatch`,
    :func:`_pin_count_mismatch_from_report`,
    :func:`_pin_count_mismatch_from_message`).

    A schematic DUT netlisted **flat** (no `.SUBCKT`/`.ENDS` wrapper at
    all) against `klt extract`'s always-`.SUBCKT`-wrapped output is also
    not an abort, and is detected proactively -- before either side
    simulates, rather than reactively from a failed/empty simulation
    attempt -- since a flat-vs-wrapped mismatch does not reliably fail
    loudly. The report still emits, with per-corner `delta[]` rows carrying
    a `null` `extracted_value` (the extracted side is never even attempted)
    and the named `flat_dut_mismatch` block (issue #1255, Gap 2; see
    :func:`_flat_dut_mismatch`).

    Returns a dict matching the documented JSON schema (see
    ``docs/cli/pex.md``) -- notably a `delta[]` array plus a
    `reference_netlist` field, the shape issue #871 (Phase 2b of epic #706)
    already taught `klt signoff`'s `_classify()` to recognise as kind
    `"pex"` (see this module's docstring). Raises :class:`PexError` for
    anything that prevents the run from completing at all (bad testbench, a
    missing or ambiguous DUT reference, disagreeing schematic references, an
    extraction or simulation failure).
    """
    if not testbench_paths:
        raise PexError("at least one testbench request is required")

    # Issue #1261: every input-path field this run's own JSON response
    # echoes (`layout`/`netlist`/`reference_netlist`, each `testbenches[]`
    # entry's `request`/`schematic_netlist`) is normalised against this one
    # repo root, resolved once from `layout_path`'s own location -- the same
    # "walk up from the input" default `env_provenance.find_repo_root` uses
    # for its own caller.
    repo_root = env_provenance.find_repo_root(
        os.path.dirname(os.path.abspath(layout_path))
    )

    try:
        extract_report = run_extract(
            layout_path,
            deck_name,
            output=output,
            top=top,
            pdk_variant=pdk_variant,
            pdk_root=pdk_root,
            parasitics=True,
            critical_nets=critical_nets,
            distributed_rc=distributed_rc,
            mom_rlc_net=mom_rlc_net,
            mom_rlc_resistance_ohm=mom_rlc_resistance_ohm,
            mom_rlc_capacitance_ff=mom_rlc_capacitance_ff,
            mom_rlc_inductance_nh=mom_rlc_inductance_nh,
        )
    except ExtractError as exc:
        raise PexError(f"extraction failed: {exc}") from exc

    extracted_netlist_path = extract_report["netlist_path"]

    work_dir = (
        os.path.abspath(artifacts_dir)
        if artifacts_dir
        else os.path.join(os.path.dirname(os.path.abspath(layout_path)), ".klt", "pex")
    )
    os.makedirs(work_dir, exist_ok=True)

    # Every testbench's schematic- and extracted-side `klt sim` call gets its
    # own `keep_artifacts` output directory, explicitly namespaced under
    # `work_dir` -- `run_sim`'s own default (next to its request file) would
    # otherwise collide: every extracted-side request this module writes
    # lives directly in `work_dir` (see `_prepare_extracted_request`), so two
    # testbenches' extracted-side runs would share one default artifacts
    # directory, and corner-slug subdirectories would overwrite each other's
    # logs/rawfiles whenever a testbench sets `options.keep_artifacts`.

    single_testbench = len(testbench_paths) == 1
    labels: list[str] = []
    dut_paths: list[str] = []

    # First pass: resolve every testbench's `.include`d DUT reference and
    # validate them *before* running any simulation at all (extraction has
    # already run above, but every `klt sim` call is comparatively
    # expensive) -- a testbench with no `.include` directive, or a set of
    # testbenches that disagree on which schematic DUT they reference, is
    # reported as a fast, cheap error rather than only surfacing after every
    # testbench has already been simulated on both sides.
    for index, testbench_path in enumerate(testbench_paths):
        label = f"{index:02d}-{os.path.splitext(os.path.basename(testbench_path))[0]}"
        labels.append(label)

        try:
            request = load_request(testbench_path)
        except SimError as exc:
            raise PexError(f"testbench '{testbench_path}': {exc}") from exc

        request_dir = os.path.dirname(os.path.abspath(testbench_path))
        schematic_body_path = _resolve_relative(request["netlist"], request_dir)
        if not os.path.isfile(schematic_body_path):
            raise PexError(
                f"testbench '{testbench_path}': netlist not found: "
                f"{schematic_body_path}"
            )
        with open(schematic_body_path, encoding="utf-8") as handle:
            body_text = handle.read()
        _include_line, dut_path = _find_dut_include(
            body_text, os.path.dirname(schematic_body_path)
        )
        # Gap 1 (issue #1255): the single `.include`/`.inc` target must
        # itself plausibly be a DUT -- a shared corner/parameter file with
        # no circuit content of its own used to be silently swapped in as
        # if it were the DUT.
        _check_dut_declares_a_circuit(testbench_path, dut_path)
        dut_paths.append(dut_path)

    if len(set(dut_paths)) > 1:
        raise PexError(
            "testbenches reference different schematic DUT netlists "
            f"({sorted(set(dut_paths))!r}) -- klt pex expects every "
            "testbench in one run to `.include` the same block's schematic "
            "netlist, so a single reference_netlist can be reported"
        )
    reference_netlist = dut_paths[0]

    # Gap 2 (issue #1255): a schematic DUT netlisted flat (no `.SUBCKT`
    # wrapper at all) against `klt extract`'s always-`.SUBCKT`-wrapped
    # output is diagnosed once, up front, for the whole run -- unlike
    # `_pin_count_mismatch` below (reactive: only ever consulted once a
    # simulation attempt has already come back with nothing measured), this
    # must run *before* either side simulates, since a testbench with no
    # `.ic` card might otherwise just simulate a disconnected fixture and
    # report a spurious passing number rather than an error at all (see
    # `_flat_dut_mismatch`'s own docstring).
    flat_dut_mismatch = _flat_dut_mismatch(
        reference_netlist=reference_netlist,
        extracted_netlist=extracted_netlist_path,
        top_cell=extract_report["top"],
    )

    testbenches_summary: list[dict[str, Any]] = []
    delta: list[dict[str, Any]] = []
    # Issue #1030 / #1041: populated (once) when the schematic/extracted
    # top-level pin-list mismatch is detected -- reported as a named,
    # structured field in this run's own JSON report rather than left buried
    # in a per-corner ngspice.log. One field for the whole run: every
    # testbench shares one reference_netlist and one extracted netlist, so
    # the mismatch is a property of the pair, not of a testbench.
    #
    # Issue #1041: detection is now *proactive* by default, from the two
    # netlists' own `.SUBCKT` headers -- `extract_report["top"]` names
    # exactly the subcircuit `klt extract` wrapped its output in (the same
    # anchor `_flat_dut_mismatch` above already uses), so this pre-flight
    # `_pin_count_mismatch` call is scoped to that one instantiated
    # subcircuit and cannot false-positive on an unrelated helper `.SUBCKT`
    # elsewhere in either netlist (the false-positive guard the issue's own
    # Notes section calls out). Doing this before any extracted-side
    # simulation makes detection identical on every ngspice version --
    # previously (#1030) it only fired *reactively*, after an extracted-side
    # `klt sim` call already came back with nothing measured, which depended
    # on the installed ngspice actually refusing the mismatched deck (older
    # ngspice, e.g. 42, silently accepts many pin-count mismatches instead,
    # simulating to completion with dangling terminals and a plausible but
    # meaningless "pass" -- see docs/cli/pex.md). Skipped when
    # `flat_dut_mismatch` already fired: the schematic DUT declares no
    # `.SUBCKT` at all there, so this check could not find a same-named
    # interface to compare anyway, and the two diagnostics stay mutually
    # exclusive as before.
    #
    # `_pin_count_mismatch_from_report`/`_pin_count_mismatch_from_message`
    # below remain as the *reactive* fallback for mismatches this static
    # header comparison cannot see -- e.g. a subcircuit name only one side
    # declares, or an unreadable header -- and are only ever consulted when
    # this pre-flight check did not already find one.
    pin_count_mismatch: dict[str, Any] | None = None
    if flat_dut_mismatch is None:
        pin_count_mismatch = _pin_count_mismatch(
            reference_netlist=reference_netlist,
            extracted_netlist=extracted_netlist_path,
            subcircuit=extract_report["top"],
        )

    for index, testbench_path in enumerate(testbench_paths):
        label = labels[index]
        dut_path = dut_paths[index]

        try:
            schematic_report = run_sim(
                testbench_path,
                artifacts_dir=os.path.join(work_dir, label, "schematic"),
                backend=backend,
            )
        except SimError as exc:
            raise PexError(
                f"testbench '{testbench_path}': schematic-side simulation failed: {exc}"
            ) from exc

        extracted_report: dict[str, Any] | None = None
        # Gap 2 (issue #1255) / issue #1041: already know (statically, from
        # both netlists' own `.SUBCKT` headers, via `flat_dut_mismatch` or
        # the pre-flight `pin_count_mismatch` check above) that the
        # extracted-side deck would be meaningless to run -- either every
        # testbench source/probe wired to the flat DUT's own nodes is
        # disconnected from the extracted netlist's `.SUBCKT` body, or
        # ngspice would refuse (on a version that actually enforces it) or
        # silently mis-simulate (one that does not, e.g. ngspice 42) the
        # mismatched pin count -- so there is nothing trustworthy an actual
        # simulation attempt could add either way. Skip it entirely and fall
        # through to the `extracted_report is None` branch below, exactly as
        # an unrunnable extracted side already does.
        if flat_dut_mismatch is None and pin_count_mismatch is None:
            try:
                extracted_request_path = _prepare_extracted_request(
                    testbench_path=testbench_path,
                    extracted_netlist_path=extracted_netlist_path,
                    work_dir=work_dir,
                    label=label,
                )
                extracted_report = run_sim(
                    extracted_request_path,
                    artifacts_dir=os.path.join(work_dir, label, "extracted"),
                    backend=backend,
                )
            except SimError as exc:
                # Issue #1030: a pin-count mismatch reported through the
                # engine's own text (a backend that raises rather than
                # classifying, e.g. `remote`) is a *diagnosable* failure,
                # not an unexplained one -- report it as a named field plus
                # per-corner error rows and keep going, instead of aborting
                # the whole run with only a stderr message. Any other
                # SimError still aborts, unchanged.
                mismatch = _pin_count_mismatch_from_message(
                    str(exc),
                    reference_netlist=reference_netlist,
                    extracted_netlist=extracted_netlist_path,
                )
                if mismatch is None:
                    raise PexError(
                        f"testbench '{testbench_path}': extracted-side simulation "
                        f"failed: {exc}"
                    ) from exc
                pin_count_mismatch = pin_count_mismatch or mismatch

        spec_row_prefix = (
            None
            if single_testbench
            else os.path.splitext(os.path.basename(testbench_path))[0]
        )
        if extracted_report is None:
            delta.extend(
                _unextracted_delta_rows(
                    spec_row_prefix=spec_row_prefix,
                    schematic_report=schematic_report,
                )
            )
            testbenches_summary.append(
                {
                    "request": _report_path(testbench_path, repo_root=repo_root),
                    "schematic_netlist": _report_path(dut_path, repo_root=repo_root),
                    "corner_count": schematic_report["corner_count"],
                    "measurement_names": [
                        m["name"] for m in schematic_report["measurements"]
                    ],
                }
            )
            continue

        # Issue #1030 / #1041: reactive fallback -- only reached when the
        # pre-flight check above did not already find a mismatch (this
        # testbench's extracted side actually ran), for a mismatch the
        # static header comparison cannot see, e.g. a subcircuit name only
        # one side declares, or an unreadable header. `klt sim` classifies
        # ngspice's failure from its log rather than raising, so the
        # response comes back with every measurement errored. Only ever
        # consulted when nothing at all was measured (see
        # `_pin_count_mismatch_from_report`), so a run that produced values
        # cannot pick up a false-positive diagnostic.
        if pin_count_mismatch is None:
            pin_count_mismatch = _pin_count_mismatch_from_report(
                extracted_report,
                reference_netlist=reference_netlist,
                extracted_netlist=extracted_netlist_path,
            )

        rows = _build_delta_rows(
            spec_row_prefix=spec_row_prefix,
            schematic_report=schematic_report,
            extracted_report=extracted_report,
        )
        delta.extend(rows)
        testbenches_summary.append(
            {
                "request": _report_path(testbench_path, repo_root=repo_root),
                "schematic_netlist": _report_path(dut_path, repo_root=repo_root),
                "corner_count": extracted_report["corner_count"],
                "measurement_names": [
                    m["name"] for m in extracted_report["measurements"]
                ],
            }
        )

    passed = sum(1 for row in delta if row["status"] == "pass")
    failed = sum(1 for row in delta if row["status"] == "fail")
    errored = sum(1 for row in delta if row["status"] == "error")
    if errored:
        status = "error"
    elif failed:
        status = "fail"
    else:
        status = "pass"

    corner_count = len({row["corner_id"] for row in delta})

    parasitics = extract_report.get("parasitics") or {}
    result: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "status": status,
        "layout": _report_path(layout_path, repo_root=repo_root),
        "netlist": _report_path(extracted_netlist_path, repo_root=repo_root),
        "reference_netlist": _report_path(reference_netlist, repo_root=repo_root),
        "extraction": {
            "deck": deck_name,
            "device_count": extract_report["device_count"],
            "net_count": extract_report["net_count"],
            "netlist_sha256": extract_report["netlist_sha256"],
            "model": parasitics.get("model"),
            # Additive field (issue #976): echoes `--critical-net` back --
            # `[]` when the flag was never given, byte-identical to before
            # this feature existed.
            "critical_nets": parasitics.get("critical_nets") or [],
            # Additive field (issue #977): echoes `--distributed-rc` back --
            # `False` when the flag was never given, byte-identical to
            # before this feature existed.
            "distributed_rc": bool(parasitics.get("distributed_rc")),
            # Additive field (issue #988, Epic #709 Phase 3a): `None`
            # unless `--mom-rlc-net` was given, in which case it is `klt
            # extract`'s own substitution report (see
            # `run_extract`'s `mom_rlc_net` docstring paragraph) --
            # byte-identical to before this feature existed otherwise.
            "mom_rlc_override": parasitics.get("mom_rlc_override"),
        },
        "testbenches": testbenches_summary,
        "corner_count": corner_count,
        "delta": delta,
        "passed": passed,
        "failed": failed,
        "errored": errored,
        # Additive field (issue #1030): `None` on every run whose extracted
        # side actually simulated -- byte-identical to before this feature
        # existed. Non-`None` names the schematic/extracted top-level pin-list
        # mismatch that made the extracted-side deck unrunnable, with both
        # sides' pin lists (see `_pin_count_mismatch`).
        "pin_count_mismatch": pin_count_mismatch,
        # Additive field (issue #1255, Gap 2): `None` on every run whose
        # schematic DUT is `.SUBCKT`-wrapped (the normal case) --
        # byte-identical to before this feature existed. Non-`None` names
        # the flat-schematic-DUT-vs-`.SUBCKT`-wrapped-extraction mismatch
        # that made the extracted-side deck unrunnable (see
        # `_flat_dut_mismatch`); when this fires, no extracted-side
        # simulation is even attempted, so `pin_count_mismatch` above stays
        # `None`.
        "flat_dut_mismatch": flat_dut_mismatch,
        "provenance": extract_report["provenance"],
    }
    return result
