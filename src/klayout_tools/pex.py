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

## Measuring with a caller-supplied command instead of a `klt sim` request

Everything above describes this module's original (and still default) input
contract: a routed layout plus one or more `klt sim` request JSON
testbenches. That contract composes badly with one specific, real class of
block (issue #2478). A `klt sim` request's ``measurements[]`` entries are
*verbatim `.meas` cards* (``docs/cli/sim.md``), so a spec row whose figure
is not expressible as a single `.meas` card cannot be driven through
:func:`run_pex` at all. Measurement shapes that are generic to analog
blocks, not to any one design, and that `.meas` cannot express:

- a threshold crossing that has to be *located* by a rule the caller
  applies across sample points or across separate runs (first crossing
  after a named edge, sign-corrected per sweep point, "unresolved" as a
  first-class outcome) rather than by one `.meas ... WHEN` card;
- a statistic computed **across** a Monte Carlo family and then rescaled by
  a gain fitted from a calibration sweep run alongside it;
- a measurement taken on a **derived** netlist -- e.g. a loop-broken
  sub-model assembled from a subset of the DUT's own device lines to make a
  small-signal analysis well-posed. This one is doubly blocked by the
  testbench contract: the derived file does not exist until the harness
  builds it, so no `.include` swap can re-point at it;
- an injection-based measurement whose stimulus sources are placed at nodes
  derived from the DUT's own device lines, then inverted by an estimator
  the caller owns.

Because ``docs/cli/signoff.md``'s T1 item 7 ("Post-layout verification")
accepts only a ``pex``-kind envelope for an analog partition (issue #871),
such a block could not cite item 7 *at all* -- no matter how much real,
committed, reproducible post-layout evidence it had. The invariant issue
#871 established is "item 7 requires a real, disclosed
schematic-vs-extracted comparison", not "item 7 requires that the
comparison was measured by `klt sim`".

``measure_command`` (``klt pex --measure-command``) closes that without
weakening the invariant. Instead of a testbench set, the caller supplies
**one command**, which this module runs **twice** -- once against the
schematic reference netlist (``reference_netlist``, then required
explicitly, since there is no testbench `.include` to derive it from) and
once against the netlist this command extracted itself. Each invocation
receives the netlist path as its final argument, plus ``KLT_PEX_SIDE`` /
``KLT_PEX_NETLIST`` / ``KLT_PEX_ARTIFACTS_DIR`` in its environment, and
prints a *measurement document* on stdout (see
:func:`_normalize_measurement_document` for the shape and its validation
rules). Everything else is unchanged: this command still drives extraction
itself, still owns both legs of the comparison, still computes every
``delta[]`` row by the same :func:`_build_delta_rows`, and still emits the
same envelope with the ``extraction.model``/``body_bias`` disclosures that
give item 7 its teeth.

What is deliberately **not** weakened:

- The caller supplies the *measurement*, never the comparison. `klt pex`
  picks both netlists, runs the command against each, and diffs the two
  results itself -- a caller cannot hand it a finished ``delta[]``.
- Extraction is still this command's own (:func:`~klayout_tools.extract.run_extract`
  with ``parasitics=True``), so ``extraction``/``body_bias``/``provenance``
  are derived from a real extraction of the caller's real layout, exactly as
  in testbench mode.
- The mode is **disclosed** in the envelope: ``measurement.mode`` is
  ``"command"``, ``measurement.command`` echoes the argv verbatim, and
  ``measurement.sides`` records each leg's netlist, exit status and
  measured rows -- so a reader of the evidence can tell a `klt sim`-measured
  record from an externally-measured one, and reproduce the latter.

Three testbench-swap diagnostics are inapplicable here and are reported as
``null``: ``pin_count_mismatch``, ``flat_dut_mismatch`` and
``pin_order_mismatch`` (issue #2890) all exist
because testbench mode reuses the testbench's own unmodified ``X...``
instantiation line against both netlists, which a caller-supplied command
does not do (it builds each side's deck itself). ``model_mismatch`` is
computed exactly as in testbench mode -- it compares the two netlists' own
device cards and is independent of how either side was measured.

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

import collections
import contextlib
import copy
import json
import math
import os
import re
import shlex
import subprocess
from collections.abc import Mapping, Sequence
from typing import Any

from . import env_provenance, output_lock
from ._paths import _fold_spice_continuations, _resolve_relative
from ._provenance import sha256_file
from .coverage import (
    REASON_NO_DELTA_ROWS,
    build_check_coverage,
    coverage_rollup,
    rollup_status,
    work_id,
)
from .extract import ExtractError, run_extract
from .sim import SimError, load_request, run_sim
from .spice_flatten import SpiceFlattenError, flatten_spice

__all__ = [
    "MEASURE_COMMAND_TIMEOUT_S",
    "MEASUREMENT_MODE_COMMAND",
    "MEASUREMENT_MODE_EXTRACT_ONLY",
    "MEASUREMENT_MODE_TESTBENCH",
    "PexError",
    "run_pex",
]

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

#: Issue #1983: ``body_bias.status`` values -- whether the extracted netlist
#: this run re-simulated has a DC bias path for every device body.
#:
#: - ``"biased"``: every PMOS body terminal resolved to a real, named net, so
#:   both legs' operating points are comparable.
#: - ``"unbiased"``: at least one PMOS body landed on an anonymous,
#:   KLayout-synthesized net with **no DC bias path at all** (``klt
#:   extract``'s own ``unbiased_pmos_body_nets[]``, issue #555). Per
#:   ``docs/cli/extract.md`` -> "Coverage", that makes a full-circuit
#:   resimulation of the extracted netlist "physically wrong, not merely
#:   imprecise" -- the run converges and produces numbers, and those numbers
#:   are not comparable to the schematic leg's.
#:
#: Deliberately a *separate* verdict from this command's top-level
#: ``status``, which stays exactly what it has always been (every graded
#: ``delta[]`` row met its tolerance). See :func:`_body_bias_report`.
BODY_BIAS_BIASED = "biased"
BODY_BIAS_UNBIASED = "unbiased"

#: Issue #2478: ``measurement.mode`` values -- how this run's two legs were
#: measured.
#:
#: - ``"testbench"``: the original (and default) contract -- one or more `klt
#:   sim` request JSON testbenches, re-run schematic-side unmodified and
#:   extracted-side with only their `.include`/`.inc` DUT line re-pointed.
#: - ``"command"``: a caller-supplied command, run once per side with that
#:   side's netlist path, printing a measurement document on stdout. See this
#:   module's "Measuring with a caller-supplied command" docstring section.
MEASUREMENT_MODE_TESTBENCH = "testbench"
MEASUREMENT_MODE_COMMAND = "command"
MEASUREMENT_MODE_EXTRACT_ONLY = "extract-only"

#: Default wall-clock cap on **one** ``--measure-command`` invocation (there
#: are exactly two per run, one per side), in seconds. Deliberately the same
#: generous-but-finite 30 minutes `klt signoff` caps a command-backed
#: evidence entry at (``signoff.py``'s ``_COMMAND_EVIDENCE_TIMEOUT_S``): a
#: caller-side harness that drives a Monte Carlo family or a multi-corner
#: sweep is genuinely slow, but a hung one must not hang `klt pex` forever.
#: Override per run with ``measure_timeout_s`` (``klt pex
#: --measure-timeout-s``).
MEASURE_COMMAND_TIMEOUT_S = 1800.0

#: The two sides a ``--measure-command`` is invoked for, in invocation order
#: -- also the two keys of the report's ``measurement.sides`` object.
_MEASURE_SIDES = ("schematic", "extracted")

#: A measurement document row's permitted ``status`` values -- the same three
#: a ``delta[]`` row itself uses, and the same three `klt sim` reports per
#: measurement, so :func:`_row_status` needs no command-mode special case.
_MEASUREMENT_STATUSES = ("pass", "fail", "error")


class PexError(Exception):
    """Raised when a `klt pex` run cannot be completed: a bad testbench
    request, a testbench with no `.include`/`.inc` DUT reference, testbenches
    that reference different schematic netlists, or a failure in the
    extraction/simulation steps this command drives (wrapping the original
    :class:`~klayout_tools.extract.ExtractError` /
    :class:`~klayout_tools.sim.SimError`).

    The CLI turns this into a clean stderr message + exit code 1, never a
    traceback -- matching every other `klt` verb's error contract.

    ``code`` is an optional machine-readable classification emitted as the
    error envelope's ``error.code`` (``docs/json-contract.md``); ``None``
    (every pre-#2889 error) keeps the envelope byte-identical.
    """

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        self.code = code


def _body_bias_report(extract_report: Mapping[str, Any]) -> dict[str, Any]:
    """The ``body_bias`` block (issue #1983): whether the extracted netlist
    this run re-simulated actually has a DC bias path for every device body.

    Built from the extraction this command drove itself -- ``klt extract``
    already detects the condition and reports it as
    ``unbiased_pmos_body_nets[]`` (issue #555), so nothing here re-derives
    it; this reduces that array to a verdict plus its counts and carries the
    entries through into `klt pex`'s own envelope.

    **Why this belongs in the `klt pex` report and not only in the
    extraction's.** ``docs/cli/extract.md`` states that a PMOS body on an
    anonymous, synthesized net has no DC bias path at all, which makes a
    full-circuit resimulation of that netlist *physically wrong, not merely
    imprecise*. `klt pex`'s entire output is such a resimulation, compared
    row-by-row against a schematic leg -- so this condition does not merely
    reduce the precision of a ``delta[]`` row, it invalidates the comparison
    the row reports. A `klt pex` record is also the artifact
    ``docs/design-evidence-tiers.md`` item 7 is cited from, and that record
    never carried the extraction's own JSON: a reader of the evidence had no
    way to tell a post-layout number measured on a properly-biased netlist
    from one measured on a floating-body netlist. That is the gap issue
    #1983 reported.

    **Reported, not enforced.** ``status`` is untouched: a run whose deltas
    all met tolerance still reports ``status: "pass"`` with
    ``body_bias.status: "unbiased"``. Whether an unbiased body should
    invalidate the verdict is a policy question for the consumer of the
    evidence (see ``docs/cli/signoff.md`` and
    ``docs/design-evidence-tiers.md`` item 7), not one this command answers
    on the caller's behalf -- and answering it here would retroactively fail
    every design on a PDK whose deck has no well-tap mechanism, which is a
    separate decision from making the condition visible.

    ``unbiased_pmos_body_nets`` is carried verbatim (same field name, same
    ``{"device", "net"}`` entry shape as ``klt extract``'s own) rather than
    re-spelled, so a reader who knows one artifact already knows the other.
    """
    entries = list(extract_report.get("unbiased_pmos_body_nets") or [])
    return {
        "status": BODY_BIAS_UNBIASED if entries else BODY_BIAS_BIASED,
        "unbiased_device_count": len(entries),
        "unbiased_nets": sorted({entry["net"] for entry in entries}),
        "unbiased_pmos_body_nets": entries,
    }


def _parasitic_integrity_report(
    parasitics: Mapping[str, Any],
) -> dict[str, Any] | None:
    """The ``extraction.parasitic_integrity`` block (issue #2764):
    propagate the extraction's own per-net parasitic RC network integrity
    diagnostics into this command's summary.

    ``klt pex`` re-simulates the netlist the extraction wrote, so a broken
    emitted network (a ladder split into islands, a shorted pair, a
    NaN-valued resistor) is exactly the kind of defect that would otherwise
    surface only as a confusing ``delta[]`` row. ``klt extract --parasitics``
    already checks the network it emits and reports per-net status values;
    `klt pex` carries them through so a committed pex record is
    self-describing about the health of the network its numbers were
    measured on.

    ``None`` when the extraction report carries no ``integrity`` summary
    (never for this command's own runs -- extraction always runs with
    ``--parasitics`` here -- but kept for a caller-supplied report from an
    older build). The per-net entries are re-attached from the extraction's
    ``parasitics.nets[]`` entries (where ``run_extract`` inlines them, so
    rerun canonicalization re-sorts them together) in that same order,
    verbatim, under the summary's ``nets`` key -- byte-identical payloads to
    ``klt extract``'s own inline blocks. This block is reported, never
    folded into ``status``: like ``body_bias`` above, grading on it is a
    policy decision for the evidence consumer, not one this command makes.
    """
    summary = parasitics.get("integrity")
    if not isinstance(summary, dict):
        return None
    nets = [
        {
            "net": entry.get("net"),
            "net_id": entry.get("net_id"),
            "rc_model": entry.get("rc_model"),
            "integrity": entry.get("integrity"),
        }
        for entry in parasitics.get("nets") or []
        if isinstance(entry, dict) and entry.get("integrity") is not None
    ]
    return {**summary, "nets": nets}


def _resistance_graph_echo(
    parasitics: Mapping[str, Any], r_cut_ohm: float | None
) -> dict[str, Any]:
    """The ``extraction.resistance_graph`` block (issue #4037): the request
    (``r_cut_ohm``) and, per net with a graph result, which scalar the
    extracted netlist's lumped R used (``resistance_source``) next to both
    candidate values. ``klt extract`` is the source of every value."""
    nets = []
    for entry in parasitics.get("nets") or []:
        graph = entry.get("resistance_graph") if isinstance(entry, dict) else None
        if not isinstance(graph, dict):
            continue
        nets.append(
            {
                "net": entry.get("net"),
                "net_id": entry.get("net_id"),
                "resistance_source": graph.get("resistance_source"),
                "selected_resistance_ohm": graph.get("selected_resistance_ohm"),
                "legacy_resistance_ohm": graph.get("legacy_resistance_ohm"),
            }
        )
    return {"r_cut_ohm": r_cut_ohm, "nets": nets}


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
    return env_provenance.repo_relative_path(path or None, repo_root=repo_root)


def _content_id(path: str | None) -> str | None:
    """``sha256:<64 lowercase hex>`` of the exact bytes of ``path`` (issue
    #2875), or ``None`` when it is not a readable file. The same digest
    convention ``provenance.input.content_hash`` uses, so ``klt signoff``
    re-hashes the cited file with the identical function."""
    digest = sha256_file(path)
    return None if digest is None else f"sha256:{digest}"


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
    `.include`/`.inc` line in the testbench body. Content both simulation
    legs need (a PDK switch-parameter file, shared ``.param`` cards) must
    NOT be folded into the DUT file: the selected line is replaced by the
    extracted netlist for the extracted-side run, so anything folded into
    the schematic DUT is present on the schematic side only. Inline it
    into the testbench body instead.
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
            "`.include`/`.inc` line in the testbench body, the DUT. Do not "
            "fold another included file (a PDK switch-parameter file, shared "
            "`.param` cards) into that DUT file: the DUT include is replaced "
            "by the extracted netlist on the extracted side, so folded "
            "content would exist on the schematic side only. Inline content "
            "both sides need into the testbench body instead; if that is "
            "not possible, this testbench cannot be represented by the "
            "current request shape"
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
    lines = [
        re.split(r"\s\$|;", raw, maxsplit=1)[0].rstrip()
        for raw in netlist_text.splitlines()
    ]
    return _fold_spice_continuations(lines)


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


def _no_preflight_mismatch(*diagnostics: dict[str, Any] | None) -> bool:
    """True when none of the pre-flight mismatch diagnostics fired, i.e. the
    extracted-side simulation is worth attempting."""
    return all(d is None for d in diagnostics)


def _pin_order_preflight(
    command_mode: bool,
    earlier: tuple[dict[str, Any] | None, ...],
    **kwargs: Any,
) -> dict[str, Any] | None:
    """The ``pin_order_mismatch`` pre-flight (issue #2890), gated: ``None`` in
    ``--measure-command`` mode, and once an earlier (mutually exclusive)
    pre-flight diagnostic has fired; otherwise :func:`_pin_order_mismatch`."""
    if command_mode or not _no_preflight_mismatch(*earlier):
        return None
    return _pin_order_mismatch(**kwargs)


def _pin_order_mismatch(
    *,
    reference_netlist: str,
    extracted_netlist: str,
    top_cell: str,
) -> dict[str, Any] | None:
    """The structured ``pin_order_mismatch`` diagnostic (issue #2890), or
    ``None``.

    `klt pex` reuses the testbench's own positional ``X...`` instantiation
    line on both legs, but `klt extract` writes the ``.SUBCKT`` header pins
    in a deterministic order (real ports first, then parasitic references,
    each alphabetical) while a schematic DUT declares them in symbol order.
    When both sides declare ``top_cell`` with the same number of pins and the
    same pin *set* (case-insensitively) but a different *sequence*,
    :func:`_pin_count_mismatch` stays silent and the extracted leg simulates
    with signals wired to the wrong pins. Same shape as
    :func:`_pin_count_mismatch` (``ngspice_message`` is always ``None``: no
    engine is consulted). Differing counts or pin sets are
    :func:`_pin_count_mismatch`'s territory; a flat schematic DUT is
    :func:`_flat_dut_mismatch`'s.
    """
    schematic_interface = _subckt_interfaces(_read_text(reference_netlist) or "").get(
        top_cell.lower()
    )
    extracted_interface = _subckt_interfaces(_read_text(extracted_netlist) or "").get(
        top_cell.lower()
    )
    if schematic_interface is None or extracted_interface is None:
        return None
    schematic_pins = [pin.lower() for pin in schematic_interface[1]]
    extracted_pins = [pin.lower() for pin in extracted_interface[1]]
    if len(schematic_pins) != len(extracted_pins):
        return None
    if sorted(schematic_pins) != sorted(extracted_pins):
        return None
    if schematic_pins == extracted_pins:
        return None
    return {
        "subcircuit": extracted_interface[0],
        "schematic": _mismatch_side(reference_netlist, schematic_interface),
        "extracted": _mismatch_side(extracted_netlist, extracted_interface),
        "ngspice_message": None,
        "detail": (
            f"the schematic DUT declares `.SUBCKT {schematic_interface[0]}` "
            f"with pins in the order ({', '.join(schematic_interface[1])}) "
            "but the extracted netlist declares the same pins in the order "
            f"({', '.join(extracted_interface[1])}) -- klt pex reuses the "
            "testbench's positional subcircuit instantiation line "
            "unmodified on both legs, so the extracted leg would wire "
            "signals and supplies to the wrong pins and report meaningless "
            "deltas. The extracted order is deterministic (real ports "
            "first, then parasitic references, each alphabetical); wrap the "
            "schematic DUT in a `.SUBCKT` that declares the pins in the "
            "extracted order so one instantiation line is valid for both "
            "legs"
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


#: Device-instance card prefixes whose trailing (non-node) token names a
#: model or `.subckt` -- see :func:`_device_model_counts`. Deliberately
#: excludes passive cards (`R`/`C`/`L`) and sources (`V`/`I`): those are out
#: of :func:`_model_mismatch`'s scope (issue #2402's own Acceptance
#: Criteria -- "device-instance-based ... on `M`/`X` cards or equivalent",
#: not every card that happens to carry a trailing token).
_DEVICE_MODEL_CARD_PREFIXES = "MXQD"


def _split_spice_tokens(line: str) -> list[str]:
    """Split a SPICE card on whitespace, keeping a quoted ``'...'``/``"..."``
    or braced ``{...}`` expression (which may contain spaces, e.g.
    ``ad='int((nf+1)/2) * W/nf * 0.18u'``) inside its ``name=value`` token."""
    tokens: list[str] = []
    current: list[str] = []
    quote = ""
    depth = 0
    for ch in line:
        if quote:
            current.append(ch)
            if ch == quote:
                quote = ""
        elif ch in "'\"":
            quote = ch
            current.append(ch)
        elif ch == "{":
            depth += 1
            current.append(ch)
        elif ch == "}":
            depth = max(depth - 1, 0)
            current.append(ch)
        elif ch.isspace() and depth == 0:
            if current:
                tokens.append("".join(current))
                current = []
        else:
            current.append(ch)
    if current:
        tokens.append("".join(current))
    return tokens


def _device_model_counts(netlist_text: str) -> dict[str, tuple[str, int]]:
    """``{lowercased model/subcircuit name: (name as written, instance
    count)}`` for every `M` (raw MOSFET)/`X` (subcircuit call, `klt
    extract`'s own convention for a bound MOS/bipolar device -- see
    ``pdk_models.py``'s ``_ModelBindingSpiceWriterDelegate.write_device``)/
    `Q` (raw BJT)/`D` (raw diode) device-instance card in ``netlist_text``,
    in file order.

    The model/subcircuit name is *the last whitespace token on the card that
    is not a ``name=value`` parameter assignment* -- SPICE's own convention
    is that a device card's trailing tokens, after its nodes, are either
    exactly one model/subcircuit name or a run of ``name=value`` parameters
    (e.g. ``L=``/``W=``/``AS=``/``AD=``), never both interleaved. This holds
    identically for a raw ``M<name> d g s b <model> [L=... W=...]`` card and
    for an ``X<name> <nodes...> <subckt> [param=value ...]`` subcircuit
    call, so one rule covers both without needing to know each card's exact
    node count up front.

    Lowercased keys, original-case values -- the same case-insensitive-key
    convention :func:`_subckt_interfaces` uses, since ngspice is
    case-insensitive about model/subcircuit names the same way it is about
    `.subckt` names.
    """
    counts: dict[str, tuple[str, int]] = {}
    for line in _logical_lines(netlist_text):
        stripped = line.strip()
        if not stripped or stripped[0].upper() not in _DEVICE_MODEL_CARD_PREFIXES:
            continue
        parts = [token for token in _split_spice_tokens(stripped) if "=" not in token]
        if len(parts) < 2:
            continue  # no nodes/model at all -- not a real device instance
        name = parts[-1]
        key = name.lower()
        existing_name, existing_count = counts.get(key, (name, 0))
        counts[key] = (existing_name, existing_count + 1)
    return counts


def _reference_is_hierarchical(netlist_text: str) -> bool:
    """``True`` if ``netlist_text`` contains an `X` device-instance card
    whose target names a `.subckt` **defined elsewhere in the same file**
    -- i.e. the netlist is hierarchical (a leaf `.subckt` plus one or more
    instance calls of it) rather than flat.

    `klt extract`'s own output is always flat, by design
    (``docs/cli/extract.md``: "Extraction is flat, not hierarchical"), so a
    hierarchical *reference* netlist compared against it is exactly the
    shape issue #1085 already documents for `klt lvs` (a leaf `.subckt`
    called N times on the reference side has no counterpart circuit on the
    flat extracted side to pair against). The same mismatch shows up here:
    a device nested inside a locally-defined `.subckt` appears once per
    *definition* in `_device_model_counts`'s card scan, not once per
    physical *instance*, while the flat extracted side always counts once
    per instance -- so a purely hierarchy-representation difference can
    produce the same divergent multiset a genuine device-flavour swap
    would, without being one. See :func:`_model_mismatch`.
    """
    local_subckts = set(_subckt_interfaces(netlist_text))
    if not local_subckts:
        return False
    for line in _logical_lines(netlist_text):
        stripped = line.strip()
        if not stripped or stripped[0].upper() != "X":
            continue
        parts = [token for token in _split_spice_tokens(stripped) if "=" not in token]
        if len(parts) < 2:
            continue
        if parts[-1].lower() in local_subckts:
            return True
    return False


def _model_mismatch(
    *, reference_netlist: str, extracted_netlist: str
) -> dict[str, Any] | None:
    """The structured ``model_mismatch`` diagnostic (issue #2402), or
    ``None`` when the reference (schematic) and extracted netlists'
    device model/`.subckt` instantiations agree exactly.

    A *different* condition from :func:`_pin_count_mismatch`/
    :func:`_flat_dut_mismatch`: those two compare the top-level `.SUBCKT`
    *interface* each side's testbench-facing wrapper declares (its pin
    list). This compares what is *inside* that interface -- which concrete
    device model each side's `M`/`X`/`Q`/`D` cards actually instantiate (see
    :func:`_device_model_counts`).

    **The canonical trigger.** A layout drawn with no voltage-domain marker
    geometry always extracts onto a PDK family's *default* MOS flavour
    (`klt extract --pdk`'s documented fallback -- see ``docs/cli/extract.md``
    -- e.g. sky130's thin-oxide ``sky130_fd_pr__nfet_01v8``), while a
    reference netlist naming a different flavour (e.g. the thick-oxide/
    high-voltage ``sky130_fd_pr__nfet_g5v0``) is *topologically* identical --
    same device count, same connectivity, same top-level pin list -- and
    therefore invisible to `pin_count_mismatch`/`flat_dut_mismatch`. The two
    netlists are nonetheless electrically unrelated: every `delta[]` row
    this run produces compares two different physical devices, not a
    parasitic effect, yet nothing else in the `klt pex` envelope reports it.

    **A hierarchical reference is a known false-positive shape.** When the
    reference netlist is hierarchical (:func:`_reference_is_hierarchical`),
    a divergence can be a pure hierarchy-vs-flat representation artifact
    (issue #1085) rather than a real device-flavour mismatch -- the
    ``detail`` names this explicitly instead of asserting the two sides are
    electrically unrelated, since that claim is not reliable in this shape.

    **Reported, not enforced.** Never changes `status`/grade -- the same
    "surface it structurally and let the caller decide" precedent
    :func:`_body_bias_report` established (issue #1983). A caller
    deliberately comparing a schematic against a different flavour (e.g.
    stress-testing at a different voltage domain) is a legitimate use that
    should not be blocked; the point is only that the divergence must be
    *visible* rather than silently folded into an ordinary, gradeable delta.

    The comparison is a multiset (instance *count* per model name, not just
    which names appear): a run whose two sides name the same models but a
    different number of each still reports the divergence, since that
    could equally mean a device was dropped/duplicated on one side.

    The additive ``kind`` field (issue #3054) separates the two meanings:
    ``"model_set"`` when either ``*_only`` list is non-empty (a wrong model
    flavour), ``"count_only"`` when the model sets are identical and only
    the instance counts differ (usually finger/multiplier decomposition),
    each with its own ``detail`` wording.
    """
    reference_text = _read_text(reference_netlist) or ""
    reference_counts = _device_model_counts(reference_text)
    extracted_counts = _device_model_counts(_read_text(extracted_netlist) or "")

    reference_multiset = {
        key: count for key, (_name, count) in reference_counts.items()
    }
    extracted_multiset = {
        key: count for key, (_name, count) in extracted_counts.items()
    }
    if reference_multiset == extracted_multiset:
        return None

    reference_only = sorted(
        name
        for key, (name, _count) in reference_counts.items()
        if key not in extracted_multiset
    )
    extracted_only = sorted(
        name
        for key, (name, _count) in extracted_counts.items()
        if key not in reference_multiset
    )

    kind = "model_set" if reference_only or extracted_only else "count_only"
    hierarchical_reference = _reference_is_hierarchical(reference_text)
    if hierarchical_reference:
        unrelated_clause = (
            "the reference netlist is hierarchical (it instantiates at "
            "least one locally-defined `.subckt`), while `klt extract`'s "
            "output is always flat -- see docs/cli/extract.md's "
            '"Extraction is flat, not hierarchical" note and issue #1085 '
            "(the same shape `klt lvs`'s `options.flatten_reference` "
            "exists for). This divergence may simply be a "
            "hierarchy-representation artifact -- a device nested inside a "
            "locally-defined `.subckt` is counted once per definition here, "
            "not once per physical instance -- rather than a genuine "
            "device-flavour mismatch; re-run with `klt pex "
            "--flatten-schematic` (issue #2889), which flattens the "
            "schematic DUT to PDK leaf devices before the schematic-side "
            "run, before treating this as evidence the two sides are "
            "unrelated. "
        )
    elif kind == "count_only":
        unrelated_clause = (
            "the reference (schematic) and extracted netlists instantiate "
            "the same device models/`.subckt`s and differ only in how many "
            "instances of each they carry. The usual cause is "
            "finger/multiplier decomposition: the layout draws a device as "
            "several fingers while the schematic has one instance (with "
            "`nf`/`m`, or none), a difference `klt lvs` has already "
            "reconciled. It does not by itself make `delta[]` unrelated, "
            "but representation effects (fingering, AS/AD) are folded "
            "into `delta[]` alongside the parasitics. "
        )
    else:
        unrelated_clause = (
            "the reference (schematic) and extracted netlists instantiate "
            "different device models/`.subckt`s, and the two sides may be "
            "electrically unrelated, so `delta[]` rows could compare two "
            "different physical devices rather than a parasitic effect. "
        )

    return {
        "kind": kind,
        "reference_only": reference_only,
        "extracted_only": extracted_only,
        "counts": {
            "reference": dict(sorted(reference_counts.values())),
            "extracted": dict(sorted(extracted_counts.values())),
        },
        "detail": (
            unrelated_clause
            + (
                f"Only in the reference netlist: {', '.join(reference_only)}. "
                if reference_only
                else ""
            )
            + (
                f"Only in the extracted netlist: {', '.join(extracted_only)}. "
                if extracted_only
                else ""
            )
            + (
                "A common cause: the layout carries no voltage-domain "
                "marker geometry, so `klt extract --pdk` bound every MOS "
                "to the PDK family's default flavour while the reference "
                "netlist names a different one -- see docs/cli/pex.md's "
                "`model_mismatch` section"
                if kind == "model_set" and not hierarchical_reference
                else "See docs/cli/pex.md's `model_mismatch` section"
            )
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


def _backend_failure(testbench_path: str, exc: SimError) -> dict[str, Any]:
    """The ``backend_failure`` record for an extracted-side
    :class:`~klayout_tools.sim.SimError` that is not a pin-count mismatch
    (issue #2990)."""
    return {
        "testbench": testbench_path,
        "side": "extracted",
        "message": str(exc),
        "code": exc.code,
    }


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


# Only keys `klt sim` actually emits in its `environment` (see sim.py's
# `run_sim`): which engine/build ran, and -- for an off-host backend that
# fills it -- the `remote` block carrying the job identity.
_SIDE_ENVIRONMENT_KEYS = (
    "engine",
    "engine_version",
    "ngspice_binary",
    "engine_host",
    "client_ngspice_binary",
    "remote",
)


def _project_side_evidence(
    sim_report: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    """Issue #2872 / #2876: the per-side execution evidence of one `klt sim`
    response, transported unchanged (diagnostics are never re-classified or
    filtered here -- `sim.py` owns that vocabulary). ``None`` when that side
    was never simulated. ``produced_value`` is derived from the source
    corner's own measurements (any non-null ``value``), never from
    ``delta[]``. Keyed by ``corner_id`` within its testbench entry."""
    if sim_report is None:
        return None
    environment = sim_report.get("environment") or {}
    corners = []
    for corner in sim_report.get("corners", []):
        corners.append(
            {
                "corner_id": corner["corner_id"],
                "status": corner.get("status"),
                "diagnostics": copy.deepcopy(corner.get("diagnostics") or []),
                "produced_value": any(
                    m.get("value") is not None for m in corner.get("measurements", [])
                ),
            }
        )
    return {
        "environment": {
            key: copy.deepcopy(environment[key])
            for key in _SIDE_ENVIRONMENT_KEYS
            if key in environment
        },
        "corners": corners,
    }


def _project_testbench_sides(
    schematic_report: Mapping[str, Any], extracted_report: Mapping[str, Any] | None
) -> dict[str, Any]:
    """``sides`` block of one ``testbenches[]`` entry, plus
    ``nothing_simulated_corners``: corner ids where *neither* side produced
    any non-null measurement (an execution failure), as opposed to a
    one-sided failure (a real pex finding). Requires both sides to have run."""
    schematic = _project_side_evidence(schematic_report)
    extracted = _project_side_evidence(extracted_report)
    nothing: list[str] = []
    if schematic is not None and extracted is not None:
        produced = {c["corner_id"]: c["produced_value"] for c in schematic["corners"]}
        for corner in extracted["corners"]:
            cid = corner["corner_id"]
            if not corner["produced_value"] and not produced.get(cid, False):
                nothing.append(cid)
    return {
        "sides": {"schematic": schematic, "extracted": extracted},
        "nothing_simulated_corners": nothing,
    }


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
    a silently-fabricated ``"pass"``). An optional measurement that is
    ``"absent"`` (issue #3620) on exactly one side is a lost -- or newly
    appearing -- figure, so also ``"error"``; absent on both sides is
    ``"pass"`` (the figure legitimately does not exist)."""
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
    if (schematic_status == "absent") != (extracted_status == "absent"):
        return "error"
    if extracted_status == "absent":
        return "pass"
    if extracted_status == "fail":
        return "fail"
    return "pass"


def _side_status(measurement: dict[str, Any] | None) -> str:
    """One side's own limit status for a delta row's additive
    ``schematic_status`` / ``extracted_status`` (issue #2989); a missing
    measurement is ``"error"``, matching :func:`_row_status`."""
    return measurement["status"] if measurement is not None else "error"


#: Issue #3062: the failing stage of an errored ``delta[]`` row, keyed by the
#: diagnostic / ``SimError`` code that names it. One table so the mapping is
#: testable; anything absent is ``"unknown"`` (never guessed).
_ERROR_STAGE_BY_CODE = {
    "batch_no_capacity": "submit",
    "batch_concurrency_cap": "submit",
    "batch_job_failed": "execute",
    "batch_job_timeout": "execute",
    "batch_poll_timeout": "execute",
    "batch_job_interrupted": "execute",
    "missing_include": "execute",
    "timeout_budget_unreachable": "execute",
    "budget_exceeded": "execute",
    "pin_count_mismatch": "execute",
    "lost_shard": "collect",
    "orphaned": "collect",
}


def _error_stage(code: str | None, message: str = "") -> str:
    """``submit`` / ``execute`` / ``collect`` / ``parse`` / ``unknown`` for
    one failure (issue #3062). The code table wins; a message that says its
    output could not be recovered/parsed is ``collect``/``parse``."""
    if code in _ERROR_STAGE_BY_CODE:
        return _ERROR_STAGE_BY_CODE[code]
    lowered = message.lower()
    if "output recovery" in lowered or "collect" in lowered:
        return "collect"
    if "unparseable" in lowered or "could not parse" in lowered:
        return "parse"
    return "unknown"


def _side_error(
    side: str,
    sim_report: Mapping[str, Any] | None,
    corner_id: str,
    *,
    message: str | None = None,
    code: str | None = None,
) -> dict[str, Any]:
    """The additive ``error`` object of an errored ``delta[]`` row (issue
    #3062): ``side``, ``stage``, ``message`` and, when the source has one,
    ``code`` -- taken from that side's corner ``diagnostics[]`` (first
    error-severity entry), else from the caller-supplied ``message``/``code``
    (a ``SimError``), never invented. ``remote`` carries the side's
    ``environment.remote`` (the batch job id) when present."""
    remote = None
    if sim_report is not None:
        remote = (sim_report.get("environment") or {}).get("remote")
        if message is None:
            for corner in sim_report.get("corners", []):
                if corner.get("corner_id") != corner_id:
                    continue
                diagnostics = corner.get("diagnostics") or []
                chosen = next(
                    (d for d in diagnostics if d.get("severity") == "error"),
                    diagnostics[0] if diagnostics else None,
                )
                if chosen is not None:
                    message = chosen.get("message") or chosen.get("code")
                    code = chosen.get("code")
                break
    text = message or "no diagnostic was recorded for this errored row"
    error: dict[str, Any] = {
        "side": side,
        "stage": _error_stage(code, text),
        "message": text,
    }
    if code:
        error["code"] = code
    if remote is not None:
        error["remote"] = copy.deepcopy(remote)
    return error


def _device_only_fields(
    schematic_value: Any,
    extracted_value: Any,
    device_only_measurement: dict[str, Any] | None,
) -> dict[str, Any]:
    """The additive ``--device-only-leg`` fields of one delta row (issue
    #3298): the parasitic-free leg's own value and the two components of
    ``delta_pct``, each by the same :func:`_delta_pct` rule -- representation
    is device-only vs schematic, parasitic is extracted vs device-only, so
    ``(1+r/100)(1+p/100) == 1+delta_pct/100`` for positive-valued
    measurements. ``device_only_measurement`` is ``None`` when the leg
    produced nothing for this row: every field is then ``null`` and
    ``device_only_status`` is ``"error"``, leaving the row's own ``status``
    and ``delta_pct`` untouched."""
    value = (
        device_only_measurement["value"]
        if device_only_measurement is not None
        else None
    )
    return {
        "device_only_value": value,
        "representation_delta_pct": _delta_pct(schematic_value, value),
        "parasitic_delta_pct": _delta_pct(value, extracted_value),
        "device_only_status": (
            device_only_measurement["status"]
            if device_only_measurement is not None and value is not None
            else "error"
        ),
    }


def _build_delta_rows(
    *,
    spec_row_prefix: str | None,
    schematic_report: dict[str, Any],
    extracted_report: dict[str, Any],
    device_only_report: dict[str, Any] | None = None,
    device_only_leg: bool = False,
) -> list[dict[str, Any]]:
    schematic_index = _index_corner_measurements(schematic_report)
    device_only_index = (
        _index_corner_measurements(device_only_report)
        if device_only_report is not None
        else {}
    )
    rows: list[dict[str, Any]] = []
    for corner in extracted_report["corners"]:
        corner_id = corner["corner_id"]
        schematic_measurements = schematic_index.get(corner_id, {})
        for extracted_measurement in corner["measurements"]:
            name = extracted_measurement["name"]
            spec_row = name if spec_row_prefix is None else f"{spec_row_prefix}.{name}"
            schematic_measurement = schematic_measurements.get(name)
            schematic_value = (
                schematic_measurement["value"]
                if schematic_measurement is not None
                else None
            )
            row = {
                "spec_row": spec_row,
                "corner_id": corner_id,
                "schematic_value": schematic_value,
                "extracted_value": extracted_measurement["value"],
                "delta_pct": _delta_pct(
                    schematic_value, extracted_measurement["value"]
                ),
                "status": _row_status(schematic_measurement, extracted_measurement),
                "schematic_status": _side_status(schematic_measurement),
                "extracted_status": _side_status(extracted_measurement),
            }
            if row["status"] == "error":
                # Issue #3062: blame the schematic side when it is the one
                # with no trustworthy value, else the extracted side.
                # Issue #3620: an asymmetric ``absent`` blames the absent side.
                schematic_bad = schematic_measurement is None or (
                    schematic_measurement["status"] == "error"
                    or (
                        schematic_measurement["status"] == "absent"
                        and extracted_measurement["status"] != "absent"
                    )
                )
                row["error"] = (
                    _side_error("schematic", schematic_report, corner_id)
                    if schematic_bad
                    else _side_error("extracted", extracted_report, corner_id)
                )
            if device_only_leg:
                row.update(
                    _device_only_fields(
                        schematic_value,
                        extracted_measurement["value"],
                        device_only_index.get(corner_id, {}).get(name),
                    )
                )
            rows.append(row)
    return rows


def _static_extracted_failure(
    pin_count_mismatch: dict[str, Any] | None,
    flat_dut_mismatch: dict[str, Any] | None,
) -> tuple[str, str | None] | None:
    """``(message, code)`` for a statically detected extracted-side mismatch."""
    static = pin_count_mismatch or flat_dut_mismatch
    if static is None or not static.get("detail"):
        return None
    return str(static["detail"]), "pin_count_mismatch" if pin_count_mismatch else None


def _unextracted_delta_rows(
    *,
    spec_row_prefix: str | None,
    schematic_report: dict[str, Any],
    failure_message: str | None = None,
    failure_code: str | None = None,
    device_only_leg: bool = False,
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
            row = {
                "spec_row": name
                if spec_row_prefix is None
                else f"{spec_row_prefix}.{name}",
                "corner_id": corner["corner_id"],
                "schematic_value": measurement["value"],
                "extracted_value": None,
                "delta_pct": None,
                "status": "error",
                "schematic_status": _side_status(measurement),
                "extracted_status": "error",
                # Issue #3062: the extracted side has no report, so the
                # cause is the failure the caller recorded, if any.
                "error": _side_error(
                    "extracted",
                    None,
                    corner["corner_id"],
                    message=failure_message,
                    code=failure_code,
                ),
            }
            if device_only_leg:
                row.update(_device_only_fields(measurement["value"], None, None))
            rows.append(row)
    return rows


def _build_coverage(
    *,
    testbenches_summary: list[dict[str, Any]],
    delta: list[dict[str, Any]],
    corner_count: int,
) -> dict[str, Any]:
    """Comparison coverage counts only rows with two comparable measurements."""
    checked = []
    skipped = []
    for row_index, row in enumerate(delta):
        # `row_index` (this row's own position in the report's `delta` list)
        # scopes the identity -- `spec_row` is a *display* label
        # (`<testbench file stem>.<name>`, docs/cli/pex.md), and two distinct
        # testbench files sharing a basename in different directories (e.g.
        # `a/request.json` and `b/request.json`) produce the same stem, which
        # would otherwise collide as one "unique" work identity for two
        # genuinely separate comparisons.
        identity = work_id("comparison", row_index, row["corner_id"], row["spec_row"])
        if row["status"] in {"pass", "fail"}:
            checked.append(identity)
        else:
            skipped.append({"id": identity, "reason": "unavailable_comparison"})
    for index, testbench in enumerate(testbenches_summary):
        if not testbench["corner_count"] or not testbench["measurement_names"]:
            skipped.append(
                {"id": work_id("testbench", index), "reason": "no_comparison_pairs"}
            )
    return {
        "testbenches": len(testbenches_summary),
        "delta_rows": len(delta),
        "corners_compared": corner_count,
        **build_check_coverage(
            checked=checked,
            skipped=skipped,
            nothing_checked_reasons=[REASON_NO_DELTA_ROWS] if not delta else None,
        ),
    }


def _status_from_coverage(
    coverage: dict[str, Any], *, failed: bool, errored: bool
) -> str:
    """Map PEX findings and comparison coverage through the shared rule."""
    rollup = coverage_rollup({"coverage": coverage}, failed=failed, errored=errored)
    return rollup_status(rollup, success="pass", failure="fail", errored="error")


def _measurement_number(raw: Any) -> float | None:
    """``raw`` as a finite float, or ``None`` when it is JSON ``null`` --
    :class:`ValueError` for anything else (a bool, a string, a NaN/Infinity a
    lenient JSON parser let through).

    ``bool`` is rejected explicitly: it is an ``int`` subclass in Python, so
    ``{"value": true}`` would otherwise silently become ``1.0`` and be diffed
    as a real measured number.
    """
    if raw is None:
        return None
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise ValueError("must be a number or null")
    value = float(raw)
    if not math.isfinite(value):
        raise ValueError("must be a finite number (not NaN or Infinity)")
    return value


def _normalize_measurement_document(
    document: Any, *, side: str, command_label: str
) -> dict[str, Any]:
    """Validate one side's **measurement document** (issue #2478 --
    ``--measure-command``'s stdout) and reduce it to the same per-corner
    shape :func:`_build_delta_rows` already consumes from a `klt sim`
    response.

    The document is a JSON object with one required key:

    .. code-block:: json

       {
         "corners": [
           {
             "corner_id": "tt/1.800V/27C",
             "measurements": [
               {"name": "t_cross", "value": 1.24e-9, "status": "pass", "unit": "s"}
             ]
           }
         ]
       }

    That is deliberately a **subset of `klt sim`'s own response shape**
    (``corners[].corner_id``, ``corners[].measurements[].name``/``value``/
    ``status``), not a new vocabulary: a harness that can already produce the
    rows with `klt sim` can pipe that response straight through, and a
    harness that cannot still only has to emit the four fields a delta row is
    computed from. Every other key -- at any level -- is ignored, so a real
    `klt sim` response (with its ``schema_version``, ``provenance``,
    ``artifacts`` and the rest) validates unchanged.

    Rules, all of them hard errors (:class:`PexError`) rather than a silently
    degraded row, because a malformed document from *one* side would
    otherwise render as ordinary ``"error"`` delta rows indistinguishable
    from a real measurement failure:

    - ``corners`` must be a non-empty list of objects.
    - ``corner_id`` must be a non-empty string, unique within the document.
    - ``measurements`` must be a non-empty list of objects.
    - ``name`` must be a non-empty string, unique within its corner.
    - ``value`` must be a finite number, or ``null`` for "this row produced no
      trustworthy value" (the caller's own "unresolved" outcome -- see this
      module's docstring). ``null`` renders an ``"error"`` delta row, never a
      fabricated pass.
    - ``status``, when present, must be one of ``"pass"``/``"fail"``/
      ``"error"``. When absent it is derived: ``"error"`` for a ``null``
      value, ``"pass"`` otherwise. A caller that owns its own limits states
      them; a caller that has none gets the same "measured, therefore
      comparable" default `klt sim` gives an unlimited measurement.

    Returns ``{"corners": [...], "corner_count": int, "measurement_names":
    [...]}`` -- ``corners`` carrying exactly the normalised ``corner_id``/
    ``measurements`` keys, so every downstream consumer here is shared with
    testbench mode rather than forked.
    """

    def fail(detail: str) -> PexError:
        return PexError(
            f"--measure-command `{command_label}` ({side} side): its stdout "
            f"is not a valid measurement document -- {detail}. See "
            'docs/cli/pex.md\'s "Measuring with a caller-supplied command" '
            "section for the required shape"
        )

    if not isinstance(document, dict):
        raise fail("the document must be a JSON object")
    corners = document.get("corners")
    if not isinstance(corners, list) or not corners:
        raise fail("`corners` must be a non-empty list")

    normalized: list[dict[str, Any]] = []
    seen_corners: set[str] = set()
    measurement_names: list[str] = []
    for corner_index, corner in enumerate(corners):
        corner_id, rows = _normalize_measurement_corner(
            corner, where=f"corners[{corner_index}]", fail=fail
        )
        if corner_id in seen_corners:
            raise fail(
                f"corners[{corner_index}].corner_id {corner_id!r} is declared twice"
            )
        seen_corners.add(corner_id)
        for row in rows:
            if row["name"] not in measurement_names:
                measurement_names.append(row["name"])
        normalized.append({"corner_id": corner_id, "measurements": rows})

    return {
        "corners": normalized,
        "corner_count": len(normalized),
        "measurement_names": measurement_names,
    }


def _normalize_measurement_corner(
    corner: Any, *, where: str, fail: Any
) -> tuple[str, list[dict[str, Any]]]:
    """One ``corners[]`` entry of a measurement document, validated and
    reduced to ``(corner_id, [normalised measurement row, ...])`` -- the
    per-corner half of :func:`_normalize_measurement_document`, split out
    only so each half stays independently readable.

    ``fail`` is that function's own error factory, so every message here
    carries the same "which command, which side" prefix.
    """
    if not isinstance(corner, dict):
        raise fail(f"{where} must be an object")
    corner_id = corner.get("corner_id")
    if not isinstance(corner_id, str) or not corner_id.strip():
        raise fail(f"{where}.corner_id must be a non-empty string")

    measurements = corner.get("measurements")
    if not isinstance(measurements, list) or not measurements:
        raise fail(f"{where}.measurements must be a non-empty list")

    rows: list[dict[str, Any]] = []
    seen_names: set[str] = set()
    for row_index, row in enumerate(measurements):
        normalized_row = _normalize_measurement_row(
            row, where=f"{where}.measurements[{row_index}]", fail=fail
        )
        name = normalized_row["name"]
        if name in seen_names:
            raise fail(
                f"{where}.measurements[{row_index}].name {name!r} is "
                f"declared twice in {corner_id}"
            )
        seen_names.add(name)
        rows.append(normalized_row)
    return corner_id, rows


def _normalize_measurement_row(row: Any, *, where: str, fail: Any) -> dict[str, Any]:
    """One ``measurements[]`` entry of a measurement document, validated and
    reduced to ``{"name", "value", "status"}`` -- see
    :func:`_normalize_measurement_document` for the rules this enforces."""
    if not isinstance(row, dict):
        raise fail(f"{where} must be an object")
    name = row.get("name")
    if not isinstance(name, str) or not name.strip():
        raise fail(f"{where}.name must be a non-empty string")

    try:
        value = _measurement_number(row.get("value"))
    except ValueError as exc:
        raise fail(f"{where}.value {exc}") from exc

    status = row.get("status")
    if status is None:
        status = "error" if value is None else "pass"
    elif status not in _MEASUREMENT_STATUSES:
        raise fail(
            f"{where}.status must be one of "
            f"{', '.join(_MEASUREMENT_STATUSES)} (got {status!r})"
        )
    return {"name": name, "value": value, "status": status}


def _run_measure_command(
    command: Sequence[str],
    *,
    side: str,
    netlist_path: str,
    artifacts_dir: str,
    timeout_s: float,
    backend: str | None = None,
) -> tuple[dict[str, Any], int]:
    """Run the caller-supplied measurement command (issue #2478) once, for
    one side, and return ``(normalized measurement document, exit status)``.

    The netlist under measurement is passed **two ways**, so a harness can
    read it whichever is more convenient and neither spelling is load-bearing
    on the other:

    - as the command's **final positional argument**, appended to the argv
      the caller gave (``--measure-command``'s own words are never rewritten
      -- no placeholder substitution, no shell);
    - as ``$KLT_PEX_NETLIST`` in the child's environment, alongside
      ``$KLT_PEX_SIDE`` (``"schematic"`` or ``"extracted"``) and
      ``$KLT_PEX_ARTIFACTS_DIR`` (a per-side directory under this run's own
      ``--outdir``, created before the command runs, for whatever decks/logs/
      raw files the harness wants to keep).

    When ``backend`` is given (``klt pex --backend``), it is also exported as
    ``$KLT_PEX_BACKEND`` and ``$KLT_SIM_BACKEND`` so a child ``klt sim`` obeys
    it (``--backend`` beats the inherited env, as in `klt sim` itself). When
    it is ``None`` neither is set and the inherited environment applies.

    Run with :func:`subprocess.run` and an argv **list** -- never
    ``shell=True`` -- the same way `klt signoff` runs a command-backed
    evidence entry (``signoff.py``'s :func:`_resolve_evidence`), so no shell
    metacharacter in a netlist path can change what executes.

    A non-zero exit, a command that cannot be executed at all, a timeout, or
    stdout that does not parse as JSON is a :class:`PexError` (exit 1), never
    a silently-empty side: an unmeasured side would otherwise render as
    ordinary ``"error"`` delta rows, hiding a broken harness inside a report
    that still looks like a real comparison.
    """
    argv = [*command, netlist_path]
    command_label = shlex.join(command)
    os.makedirs(artifacts_dir, exist_ok=True)
    env = dict(os.environ)
    env["KLT_PEX_SIDE"] = side
    env["KLT_PEX_NETLIST"] = netlist_path
    env["KLT_PEX_ARTIFACTS_DIR"] = artifacts_dir
    if backend is not None:
        env["KLT_PEX_BACKEND"] = backend
        env["KLT_SIM_BACKEND"] = backend
    # The parent holds the pex root locked while blocked here; let a nested
    # `klt sim` under that root (its default `.klt/sim` beside a request in
    # $KLT_PEX_ARTIFACTS_DIR) proceed instead of tripping outdir_locked (#3291).
    env[output_lock.INHERIT_ENV] = output_lock.canonical_dir(artifacts_dir)

    try:
        completed = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            env=env,
        )
    except subprocess.TimeoutExpired as exc:
        raise PexError(
            f"--measure-command `{command_label}` ({side} side) did not "
            f"finish within {timeout_s:g}s -- raise the cap with "
            "`--measure-timeout-s` if the harness is genuinely this slow"
        ) from exc
    except OSError as exc:
        raise PexError(
            f"--measure-command `{command_label}` ({side} side) could not be "
            f"executed: {exc}"
        ) from exc

    if completed.returncode != 0:
        raise PexError(
            f"--measure-command `{command_label}` ({side} side) exited "
            f"{completed.returncode}{_stderr_tail(completed.stderr)}"
        )

    try:
        document = json.loads(completed.stdout)
    except ValueError as exc:
        raise PexError(
            f"--measure-command `{command_label}` ({side} side) exited 0 but "
            f"its stdout is not valid JSON: {exc}"
            f"{_stderr_tail(completed.stderr)}"
        ) from exc

    return (
        _normalize_measurement_document(
            document, side=side, command_label=command_label
        ),
        completed.returncode,
    )


def _reject_extract_only_conflicts(conflicts: Sequence[tuple[bool, str]]) -> None:
    """Raise :class:`PexError` naming the first ``--extract-only`` conflict
    (issue #3739): ``conflicts`` is ``(present, flag-description)`` pairs."""
    for present, name in conflicts:
        if present:
            raise PexError(
                f"--extract-only cannot be combined with {name}: it "
                "extracts and reports only, so there is no measurement "
                "leg for that to apply to"
            )


def _resolve_reference_netlist_path(
    extract_only: bool,
    measured_reference_netlist: str | None,
    dut_paths: Sequence[str],
) -> str:
    """The one ``reference_netlist`` for the run (issue #3739): ``""`` for
    ``--extract-only`` (no schematic side; every consumer of the path is
    skipped and :func:`_report_path` / :func:`_content_id` read it as
    ``null``), the caller's own ``--reference-netlist`` in
    ``--measure-command`` mode, else the DUT."""
    if extract_only:
        return ""
    if measured_reference_netlist is not None:
        return measured_reference_netlist
    return dut_paths[0]


def _validate_measure_mode(
    measure_command: Sequence[str] | None,
    testbench_paths: Sequence[str],
    reference_netlist: str | None,
    measure_timeout_s: float | None,
    *,
    extract_only: bool = False,
    flatten_schematic: bool = False,
    device_only_leg: bool = False,
) -> list[str] | None:
    """Resolve and validate which of :func:`run_pex`'s input contracts
    this call uses (issue #2478) -- returning the measurement command's argv
    when it is ``--measure-command`` mode, and ``None`` when it is the
    default testbench mode (or ``--extract-only`` mode, issue #3739, which
    measures nothing and so is validated here as a third, mutually exclusive
    contract).

    One place, one set of messages, so the library and the CLI can never
    disagree about what "exactly one of the two" means (the CLI deliberately
    does *not* express the exclusion with an argparse mutually-exclusive
    group -- `<testbench>` is a positional, and argparse's own message for
    that case says much less than these do).
    """
    if extract_only:
        _reject_extract_only_conflicts(
            [
                (bool(testbench_paths), "a testbench request"),
                (measure_command is not None, "--measure-command"),
                (reference_netlist is not None, "--reference-netlist"),
                (measure_timeout_s is not None, "--measure-timeout-s"),
                (device_only_leg, "--device-only-leg"),
                (flatten_schematic, "--flatten-schematic"),
            ]
        )
        return None

    if measure_command is None:
        if reference_netlist is not None:
            raise PexError(
                "--reference-netlist is only meaningful with "
                "--measure-command: in testbench mode the schematic DUT is "
                "the file every testbench `.include`s, and reporting a "
                "different one would make `reference_netlist` a claim rather "
                "than an observation"
            )
        if measure_timeout_s is not None:
            raise PexError(
                "--measure-timeout-s is only meaningful with --measure-command"
            )
        if not testbench_paths:
            raise PexError("at least one testbench request is required")
        return None

    command = list(measure_command)
    if not command:
        raise PexError(
            "--measure-command must name a command to run (got an empty argument list)"
        )
    if testbench_paths:
        raise PexError(
            "--measure-command replaces the testbench set: it drives the "
            "measurement itself, once per side, so no `klt sim` request "
            f"testbench may be given alongside it (got "
            f"{len(list(testbench_paths))})"
        )
    if reference_netlist is None:
        raise PexError(
            "--measure-command requires --reference-netlist: with no "
            "testbench to read a `.include`/`.inc` DUT reference from, the "
            "schematic-side netlist to measure against cannot be inferred, "
            "and `reference_netlist` is a mandatory disclosure of this "
            "command's report (see docs/cli/pex.md)"
        )
    return command


def _resolve_measured_reference(reference_netlist: str) -> str:
    """``--reference-netlist``'s absolute path, after the same "does this
    file plausibly *be* a DUT" check testbench mode applies to its
    `.include` target (Gap 1, issue #1255).

    A `reference_netlist` naming a parameter/model/corner file with no
    circuit of its own would make the schematic leg -- and therefore every
    ``delta[]`` row diffed against it -- meaningless, while the report still
    looked like a real comparison. Refused up front instead.
    """
    path = os.path.abspath(reference_netlist)
    text = _read_text(path)
    if text is None:
        raise PexError(f"--reference-netlist could not be read: {path}")
    if not _dut_declares_a_circuit(text):
        raise PexError(
            f"--reference-netlist {path!r} declares no `.SUBCKT` and no "
            "circuit element of its own -- it parses as parameter/model/"
            "corner-file content only, so it cannot be the schematic DUT the "
            "schematic-side measurement is taken on (the same check klt pex "
            "applies to a testbench's `.include` target, issue #1255)"
        )
    return path


def _measure_timeout(measure_timeout_s: float | None) -> float:
    """The per-invocation cap in force -- the caller's own, or
    :data:`MEASURE_COMMAND_TIMEOUT_S`."""
    return MEASURE_COMMAND_TIMEOUT_S if measure_timeout_s is None else measure_timeout_s


def _measure_both_sides(
    command: Sequence[str],
    *,
    reference_netlist: str,
    extracted_netlist: str,
    work_dir: str,
    timeout_s: float,
    repo_root: str | None,
    backend: str | None = None,
    device_only_netlist: str | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Run ``command`` once per side and return ``(delta rows, the report's
    ``measurement.sides`` block)`` (issue #2478).

    ``device_only_netlist`` (``--device-only-leg``, issue #4293) adds a third
    invocation on the parasitic-free netlist with ``$KLT_PEX_SIDE=device-only``;
    a failure there is a :class:`PexError`, like the other two sides.

    The *diff* is still :func:`_build_delta_rows`, byte-for-byte the function
    testbench mode uses -- the caller-supplied command replaces the two `klt
    sim` calls, nothing else. ``spec_row_prefix`` is ``None`` because there
    is exactly one measurement pair in this mode (and no testbench file to
    name), so a row's ``spec_row`` is the measurement name the caller's own
    document declared, bare -- the same as a single-testbench run's.
    """
    side_reports: dict[str, dict[str, Any]] = {}
    sides: dict[str, Any] = {}
    legs = [
        ("schematic", reference_netlist),
        ("extracted", extracted_netlist),
    ]
    if device_only_netlist is not None:
        legs.append(("device-only", device_only_netlist))
    for side, side_netlist in legs:
        side_report, exit_status = _run_measure_command(
            command,
            side=side,
            netlist_path=side_netlist,
            artifacts_dir=os.path.join(work_dir, "measure", side),
            timeout_s=timeout_s,
            backend=backend,
        )
        side_reports[side] = side_report
        sides[side.replace("-", "_")] = {
            "netlist": _report_path(side_netlist, repo_root=repo_root),
            "exit_status": exit_status,
            "corner_count": side_report["corner_count"],
            "measurement_names": side_report["measurement_names"],
        }

    rows = _build_delta_rows(
        spec_row_prefix=None,
        schematic_report=side_reports["schematic"],
        extracted_report=side_reports["extracted"],
        device_only_report=side_reports.get("device-only"),
        device_only_leg=device_only_netlist is not None,
    )
    return rows, sides


def _measurement_block(
    command: Sequence[str] | None,
    measure_timeout_s: float | None,
    sides: dict[str, Any] | None,
    *,
    extract_only: bool = False,
) -> dict[str, Any]:
    """The report's additive ``measurement`` block (issue #2478) -- always
    present, so a reader of a committed record never has to infer how a run's
    two legs were measured from the presence/absence of ``testbenches[]``.

    ``command is None`` is testbench mode, and every other field is ``null``
    there: exactly the information a pre-#2478 report carried, stated rather
    than implied. ``extract_only`` (issue #3739) reports ``mode:
    "extract-only"`` with the same null fields: nothing was measured.
    """
    if extract_only:
        return {
            "mode": MEASUREMENT_MODE_EXTRACT_ONLY,
            "command": None,
            "timeout_s": None,
            "sides": None,
        }
    if command is None:
        return {
            "mode": MEASUREMENT_MODE_TESTBENCH,
            "command": None,
            "timeout_s": None,
            "sides": None,
        }
    return {
        "mode": MEASUREMENT_MODE_COMMAND,
        "command": list(command),
        "timeout_s": _measure_timeout(measure_timeout_s),
        "sides": sides,
    }


def _stderr_tail(stderr: str | None, *, lines: int = 10) -> str:
    """The last ``lines`` non-blank lines of a failed measurement command's
    stderr, formatted for appending to a :class:`PexError` message -- or the
    empty string when it wrote nothing.

    Bounded on purpose: a harness that dies mid-simulation can emit megabytes
    of engine chatter, and the error envelope this ends up in
    (``docs/json-contract.md``) is meant to be read, not to carry a log.
    """
    if not stderr:
        return ""
    tail = [line for line in stderr.splitlines() if line.strip()][-lines:]
    if not tail:
        return ""
    return " -- stderr tail:\n" + "\n".join(tail)


def _prepare_extracted_request(
    *,
    testbench_path: str,
    extracted_netlist_path: str,
    work_dir: str,
    label: str,
) -> str:
    """The extracted-side DUT-swapped request copy -- see
    :func:`_prepare_dut_swapped_request`."""
    return _prepare_dut_swapped_request(
        testbench_path=testbench_path,
        dut_netlist_path=extracted_netlist_path,
        work_dir=work_dir,
        label=label,
        side="extracted",
    )


def _prepare_schematic_request(
    *,
    testbench_path: str,
    flattened_netlist_path: str,
    work_dir: str,
    label: str,
) -> str:
    """The schematic-side DUT-swapped request copy ``--flatten-schematic``
    runs (issue #2889): the testbench's DUT `.include` re-pointed at the
    flattened schematic DUT, every other field (including the request's own
    ``netlist_source``) unchanged -- see :func:`_prepare_dut_swapped_request`."""
    return _prepare_dut_swapped_request(
        testbench_path=testbench_path,
        dut_netlist_path=flattened_netlist_path,
        work_dir=work_dir,
        label=label,
        side="schematic",
    )


def _prepare_dut_swapped_request(
    *,
    testbench_path: str,
    dut_netlist_path: str,
    work_dir: str,
    label: str,
    side: str,
) -> str:
    """Build (and write, under ``work_dir``) a ``side``-side copy of the
    testbench request at ``testbench_path``: identical in every field except
    ``netlist`` (re-pointed at a new body file whose DUT `.include` names
    ``dut_netlist_path``, see :func:`_rewrite_dut_include`) and, for
    ``side == "extracted"`` only, ``netlist_source`` (forced to
    ``"extracted"``). Returns the written request file's path, suitable for
    :func:`~klayout_tools.sim.run_sim`.

    A relative ``models.lib`` the original request declares *in the
    request-dir-relative shape* (no ``models.pdk``/``models.pdk_root``) is
    resolved to an absolute path first -- the written copy lives in
    ``work_dir``, not next to the original testbench file, so a relative
    reference would otherwise resolve against the wrong directory.

    A ``models`` block that *does* carry ``pdk``/``pdk_root`` is copied
    through **unmodified** (issue #1395): in that shape a relative ``lib``
    is PDK-variant-relative, not request-dir-relative -- see
    :func:`~klayout_tools.sim._resolve_models_lib`, the resolution both a
    standalone ``klt sim`` run and this command's own schematic-side leg
    use. Eagerly joining it against ``request_dir`` here produced a bogus
    ``<request-dir>/libs.tech/.../sky130.lib.spice`` absolute path (which,
    being absolute, then defeated the PDK branch entirely) and failed the
    extracted-side leg alone with ``model library not found``. Leaving the
    pair intact lets ``run_sim`` resolve both legs identically.
    """
    request = load_request(testbench_path)
    request_dir = os.path.dirname(os.path.abspath(testbench_path))

    schematic_body_path = _resolve_relative(request["netlist"], request_dir)
    with open(schematic_body_path, encoding="utf-8") as handle:
        body_text = handle.read()
    body_dir = os.path.dirname(schematic_body_path)
    include_line, _dut_path = _find_dut_include(body_text, body_dir)
    swapped_body_text = _rewrite_dut_include(body_text, include_line, dut_netlist_path)

    swapped_body_path = os.path.join(work_dir, f"{label}.{side}-dut-testbench.spice")
    with open(swapped_body_path, "w", encoding="utf-8") as handle:
        handle.write(swapped_body_text)

    swapped_request = copy.deepcopy(request)
    models = swapped_request.get("models")
    if (
        isinstance(models, dict)
        and isinstance(models.get("lib"), str)
        # Issue #1395: only the no-`pdk`/no-`pdk_root` shape is
        # request-dir-relative. Never rewrite the PDK-resolved shape.
        and models.get("pdk") is None
        and models.get("pdk_root") is None
    ):
        models = dict(models)
        models["lib"] = _resolve_relative(models["lib"], request_dir)
        swapped_request["models"] = models
    swapped_request["netlist"] = swapped_body_path
    if side in ("extracted", "device-only"):
        swapped_request["netlist_source"] = "extracted"

    swapped_request_path = os.path.join(work_dir, f"{label}.{side}-request.json")
    with open(swapped_request_path, "w", encoding="utf-8") as handle:
        json.dump(swapped_request, handle, indent=2)
    return swapped_request_path


def _testbench_inputs(
    testbench_path: str,
    dut_path: str,
    identity: Mapping[str, Any],
    *,
    repo_root: str | None,
) -> dict[str, Any]:
    """The path + content-identity fields of one ``testbenches[]`` entry
    (issue #2875). Shared by both summary-construction branches so a
    diagnostic/partial run (extracted side skipped) carries exactly the same
    keys as a full one. Digests are present even when a path is redacted as
    ``scope: external``."""
    return {
        "request": _report_path(testbench_path, repo_root=repo_root),
        "request_sha256": identity["request_sha256"],
        "netlist": _report_path(identity["netlist"], repo_root=repo_root),
        "netlist_sha256": identity["netlist_sha256"],
        "schematic_netlist": _report_path(dut_path, repo_root=repo_root),
        "schematic_netlist_sha256": identity["schematic_netlist_sha256"],
    }


def _own_pex_root(
    ownership: contextlib.ExitStack, artifacts_dir: str | None, layout_path: str
) -> None:
    """Own the PEX artifact root (``artifacts_dir`` or ``.klt/pex/``) (#2770)."""
    ownership.enter_context(
        output_lock.hold_output_dir(
            artifacts_dir
            if artifacts_dir
            else os.path.join(
                os.path.dirname(os.path.abspath(layout_path)), ".klt", "pex"
            )
        )
    )


#: File name of the flattened schematic DUT ``--flatten-schematic`` writes
#: under the run's artifact root (issue #2889).
FLATTENED_SCHEMATIC_NAME = "schematic-dut.flat.spice"


#: A file-level ``.lib <path> <section>`` card (the path form, not a
#: ``.lib <section>`` section header inside a library file).
_LIB_PATH_RE = re.compile(
    r"^(?P<prefix>\s*\.lib\s+)(?P<target>\S+)(?P<rest>\s+\S+\s*)$", re.IGNORECASE
)


def _absolutize_includes(text: str, source_dir: str) -> str:
    """``text`` with every relative ``.include``/``.inc``/``.lib <path>``
    target joined onto ``source_dir`` (issue #2889; absolute, ``$``- and
    ``~``-prefixed targets are left exactly as written): the flattened
    copy is written under the artifact root, not next to the original DUT,
    so a relative path would otherwise resolve against the wrong directory
    (the #2891 shape)."""
    lines = []
    for line in text.splitlines(keepends=True):
        body = line.rstrip("\n")
        match = _INCLUDE_RE.match(body) or _LIB_PATH_RE.match(body)
        target = _strip_quotes(match.group("target").strip()) if match else ""
        if match and target and not target.startswith(("/", "$", "~")):
            rest = match.groupdict().get("rest") or ""
            resolved = os.path.join(source_dir, target)
            line = f'{match.group("prefix")}"{resolved}"{rest}{line[len(body) :]}'
        lines.append(line)
    return "".join(lines)


def _flatten_schematic_dut(
    reference_netlist_path: str,
    *,
    work_dir: str,
    top: str,
    repo_root: str | None,
) -> tuple[str, dict[str, Any]]:
    """Flatten the schematic DUT ``reference_netlist_path`` to PDK leaf
    devices (issue #2889, ``klt pex --flatten-schematic``) and write it to
    ``<work_dir>/schematic-dut.flat.spice``.

    Returns ``(flattened path, schematic_flatten report block)``. Every
    locally defined subcircuit is inlined; ``top`` (the cell `klt extract`
    wrapped its output in) is always kept as a `.subckt` definition so the
    testbench's own `X...` line still binds. `X` targets with no local
    definition are PDK leaf devices -- SPICE text alone cannot tell them
    from a missing user subcircuit -- and are listed as
    ``external_subckts`` for the caller to audit. See
    :mod:`klayout_tools.spice_flatten` for the full rule set.

    A flattener failure (cycle, `X`-call pin-count mismatch, a card it
    cannot rename safely, unbalanced `.subckt`/`.ends`) is a
    :class:`PexError` with ``code="schematic_flatten_<reason>"`` -- never a
    partially flattened netlist.
    """
    text = _read_text(reference_netlist_path)
    if text is None:
        raise PexError(
            "--flatten-schematic: schematic DUT could not be read: "
            f"{reference_netlist_path}",
            code="schematic_flatten_malformed",
        )
    try:
        result = flatten_spice(
            text,
            top=top,
            banner=(
                "flattened by klt pex --flatten-schematic from "
                f"{os.path.basename(reference_netlist_path)} (issue #2889)"
            ),
        )
    except SpiceFlattenError as exc:
        raise PexError(
            "--flatten-schematic: cannot flatten schematic DUT "
            f"{reference_netlist_path}: {exc}",
            code=f"schematic_flatten_{exc.code}",
        ) from exc

    flat_path = os.path.join(work_dir, FLATTENED_SCHEMATIC_NAME)
    with open(flat_path, "w", encoding="utf-8") as handle:
        handle.write(
            _absolutize_includes(
                result.text, os.path.dirname(os.path.abspath(reference_netlist_path))
            )
        )
    return flat_path, {
        "netlist": _report_path(flat_path, repo_root=repo_root),
        "netlist_sha256": _content_id(flat_path),
        "subckts": result.roots,
        "inlined_instances": result.inlined_instances,
        "inlined_subckts": result.inlined_subckts,
        "external_subckts": result.external_subckts,
        "already_flat": result.inlined_instances == 0,
    }


_STAR_NODE_SUFFIX = re.compile(r"__t\d+(?=\s|$)")


def _logical_cards(text: str) -> list[str]:
    """``text``'s SPICE cards with ``+`` continuation lines joined and
    comments dropped."""
    return _fold_spice_continuations(
        [
            line.strip()
            for line in text.splitlines()
            if line.strip() and not line.startswith("*")
        ]
    )


def _device_only_parity(
    parasitic_netlist: str, device_only_netlist: str
) -> dict[str, Any]:
    """Whether the parasitic-free extraction (issue #3298) carries the same
    devices as the parasitic one, with the parasitic extraction's
    per-terminal ``<net>__tN`` star nodes mapped back to ``<net>``: every
    non-``R``/``C`` card must match as a multiset, and every ``R``/``C`` card
    the device-only netlist has (a real resistor/capacitor device) must
    also be in the parasitic one -- the parasitic ``R``/``C`` cards cannot be
    told from real ones by name, so they are the only cards allowed to be
    extra on that side. The leg is only a *device-only* leg if this holds, so
    a divergence is reported rather than assumed away
    (``device_cards_match: false``)."""

    def cards(path: str, *, strip_star: bool) -> list[str]:
        found = [
            card
            for card in _logical_cards(_read_text(path) or "")
            if not card.startswith(".")
        ]
        if strip_star:
            found = [_STAR_NODE_SUFFIX.sub("", card) for card in found]
        return found

    def active(found: list[str]) -> collections.Counter[str]:
        return collections.Counter(c for c in found if c[0].upper() not in "RC")

    parasitic_cards = cards(parasitic_netlist, strip_star=True)
    device_only_cards = cards(device_only_netlist, strip_star=False)
    passive_extra = collections.Counter(
        c for c in device_only_cards if c[0].upper() in "RC"
    ) - collections.Counter(c for c in parasitic_cards if c[0].upper() in "RC")
    return {
        "device_card_count": len(device_only_cards),
        "device_cards_match": (
            active(parasitic_cards) == active(device_only_cards) and not passive_extra
        ),
    }


def _reject_resistance_graph_with_distributed_rc(
    resistance_graph: bool, distributed_rc: bool
) -> None:
    """Issue #4037: the ladder keeps the legacy per-level resistance."""
    if resistance_graph and distributed_rc:
        raise PexError(
            "--resistance-graph cannot be combined with --distributed-rc: the "
            "ladder keeps the legacy per-level resistance, so a critical net "
            "would mix resistance sources"
        )


def _extract_device_only_leg(
    device_only_leg: bool,
    extracted_netlist_path: str,
    layout_path: str,
    deck_name: str,
    *,
    top: str | None,
    pdk_variant: str | None,
    pdk_root: str | None,
    deck_options: Any,
    declared_pins: Any,
    rename_declared_pins: bool,
    label_layers: Any,
) -> tuple[str | None, dict[str, Any] | None]:
    """The parasitic-free leg's netlist path and its parity against the
    parasitic extraction (issue #3298), or ``(None, None)`` without
    ``--device-only-leg``. It is the same extraction with
    ``parasitics=False`` -- the parasitics-only options (``critical_nets``,
    ``distributed_rc``, ``mom_rlc_*``) are not passed, as ``run_extract``
    rejects them without parasitics."""
    if not device_only_leg:
        return None, None
    stem, ext = os.path.splitext(extracted_netlist_path)
    try:
        device_only_extract = run_extract(
            layout_path,
            deck_name,
            output=f"{stem}.device-only{ext}",
            top=top,
            pdk_variant=pdk_variant,
            pdk_root=pdk_root,
            parasitics=False,
            deck_options=deck_options,
            declared_pins=declared_pins,
            rename_declared_pins=rename_declared_pins,
            label_layers=label_layers,
        )
    except ExtractError as exc:
        raise PexError(f"device-only extraction failed: {exc}") from exc
    netlist_path = os.path.abspath(device_only_extract["netlist_path"])
    return netlist_path, _device_only_parity(extracted_netlist_path, netlist_path)


def _run_device_only_sim(
    device_only_netlist_path: str | None,
    *,
    testbench_path: str,
    work_dir: str,
    label: str,
    backend: Any,
) -> dict[str, Any] | None:
    """One testbench re-run against the parasitic-free netlist (issue #3298),
    or ``None`` without the leg. A failure here costs only the device-only
    fields of this testbench's rows (``null``, ``device_only_status:
    "error"``), never the run."""
    if device_only_netlist_path is None:
        return None
    try:
        return run_sim(
            _prepare_dut_swapped_request(
                testbench_path=testbench_path,
                dut_netlist_path=device_only_netlist_path,
                work_dir=work_dir,
                label=label,
                side="device-only",
            ),
            artifacts_dir=os.path.join(work_dir, label, "device-only"),
            backend=backend,
        )
    except SimError:
        return None


def _device_only_result_fields(
    device_only_netlist_path: str | None,
    device_only_parity: dict[str, Any] | None,
    *,
    repo_root: Any,
) -> dict[str, Any]:
    """Additive report fields (issue #3298), present only under
    ``--device-only-leg`` so a run without it is byte-identical to before."""
    if device_only_netlist_path is None:
        return {}
    return {
        "device_only_netlist": _report_path(
            device_only_netlist_path, repo_root=repo_root
        ),
        "device_only_netlist_sha256": _content_id(device_only_netlist_path),
        **(device_only_parity or {}),
    }


@output_lock.owns_run
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
    deck_options: Mapping[str, str] | None = None,
    declared_pins: frozenset[str] | None = None,
    rename_declared_pins: bool = False,
    label_layers: Mapping[str, tuple[int, int] | None] | None = None,
    critical_nets: Sequence[str] | None = None,
    critical_net_patterns: Sequence[str] | None = None,
    critical_net_all: bool = False,
    critical_net_excludes: Sequence[str] | None = None,
    distributed_rc: bool = False,
    resistance_graph: bool = False,
    resistance_graph_r_cut_ohm: float | None = None,
    parasitic_variants: Sequence[str] | None = None,
    substrate_tie_ohms: float | None = None,
    mom_rlc_net: str | None = None,
    mom_rlc_resistance_ohm: float | None = None,
    mom_rlc_capacitance_ff: float | None = None,
    mom_rlc_inductance_nh: float | None = None,
    measure_command: Sequence[str] | None = None,
    reference_netlist: str | None = None,
    measure_timeout_s: float | None = None,
    flatten_schematic: bool = False,
    device_only_leg: bool = False,
    extract_only: bool = False,
    _ownership: contextlib.ExitStack | None = None,
) -> dict[str, Any]:
    """Extract ``layout_path`` (with lumped-RC parasitics) and re-run every
    testbench in ``testbench_paths`` against both the schematic DUT it
    already references and the newly-extracted one, producing a per-corner,
    per-spec-row delta report.

    ``deck_name``/``top``/``pdk_variant``/``pdk_root``/``output`` are passed
    through to :func:`~klayout_tools.extract.run_extract` (``parasitics``
    is always ``True`` here -- ``klt pex`` has no schematic-equivalent-only
    mode; use `klt extract` directly for that). ``deck_options`` (``klt pex
    --deck-option <key>=<value>``, repeatable) and ``declared_pins`` (``klt
    pex --pins <a,b,...>``), issue #1558, are likewise passed straight
    through -- the two ``klt extract`` flags this command had no
    passthrough for until then. Without ``deck_options`` a design that
    commits to a non-default deck flavour (e.g. gf180mcu's ``poly_res``/
    ``mim_cap`` axes, where one drawn geometry has several PDK-offered
    interpretations) silently extracted against the *deck's* default
    flavour instead, with no warning -- and every ``delta[]`` row was then
    computed from the wrong parasitics. ``declared_pins`` is the same
    per-net demotion ``klt extract --pins`` performs, which is also how a
    caller resolves a ``pin_count_mismatch`` caused by flat extraction
    promoting more top-level pins than the schematic DUT declares. ``None``
    for both (the default) resolves the deck and promotes pins exactly as
    before this feature existed. ``rename_declared_pins`` (``klt pex
    --rename-declared-pins``, issue #2854) is likewise passed straight
    through to :func:`~klayout_tools.extract.run_extract` -- it renames each
    surviving promoted pin net whose component labels contain exactly one
    ``declared_pins`` name to that name, so the extracted DUT's ``.SUBCKT``
    header carries the canonical interface names the schematic DUT declares
    even when a boundary net carries extra drawn labels (the same rename
    ``klt extract --rename-declared-pins`` performs; see ``run_extract``'s
    own ``rename_declared_pins`` docstring paragraph). The effective value
    is echoed as ``extraction.rename_declared_pins`` (``False`` when the
    flag was never given -- the extraction is then byte-identical to before
    this parameter existed). ``label_layers`` (``klt pex --label-layer
    <role>=<layer>/<datatype>``, repeatable, issue #2686) is likewise passed
    straight through to :func:`~klayout_tools.extract.run_extract` -- it
    redirects which GDS purpose extraction reads net/pin name text from, per
    label role, for a layout whose names sit on a text purpose the curated
    deck does not read (see ``run_extract``'s own ``label_layers`` docstring
    paragraph, issue #2656). Without it such a layout extracts with no
    promoted top-level pins and the extracted-side testbench cannot bind the
    DUT. The applied mapping is echoed as ``extraction.label_layers`` (``None``
    when no override was given -- the extraction is then byte-identical to
    before this parameter existed). ``critical_nets`` (``klt
    pex --critical-net``, repeatable, issue #976, Epic #709 Phase 2a) is
    also passed straight through to :func:`~klayout_tools.extract.run_extract`
    -- extracts lateral (same-layer, sidewall) coupling capacitance for any
    same-layer net pair naming one of these nets, in addition to Phase 1's
    always-on vertical-overlap coupling (issue #760), so the re-simulated
    extracted-side testbench (and the resulting `delta[]` rows) reflect it.
    ``None``/empty (the default) skips it entirely -- byte-identical to
    before this feature existed. ``critical_net_patterns``/
    ``resistance_graph``/``resistance_graph_r_cut_ohm`` (``klt pex
    --resistance-graph [--resistance-graph-r-cut OHM]``, issue #4037) forward
    to ``klt extract``'s terminal-aware resistance graph and make the
    extracted netlist use each ``resistance_source == "graph"`` net's
    ``selected_resistance_ohm`` as its lumped R (``legacy`` nets keep the
    legacy scalar). ``extraction.resistance_graph`` echoes the request and one
    ``{net, net_id, resistance_source, selected_resistance_ohm,
    legacy_resistance_ohm}`` entry per net, only when the flag was given --
    otherwise the report and netlist are byte-identical. Combining it with
    ``distributed_rc`` raises :class:`PexError` (the ladder stays on the
    legacy scalar, so a critical net would mix resistance sources).

    ``critical_net_all``/``critical_net_excludes`` (``klt pex
    --critical-net-pattern``/``--critical-net-all``/``--critical-net-exclude``,
    issue #2974) are the glob / all-nets forms of ``critical_nets``, passed
    straight through; ``extraction.critical_nets`` then echoes the resolved
    concrete names. ``distributed_rc`` (``klt pex
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

    ``coverage`` preserves the legacy counters alongside common checked-work
    v1 identities, skips, applicability and unknown execution. Known zero
    actual checks produces ``not_checked`` unless a real failure/error wins.
    See :func:`_build_coverage` and ``docs/coverage-contract.md``.

    **``measure_command`` (issue #2478) replaces the testbench set, not the
    comparison.** When it is given (``klt pex --measure-command``),
    ``testbench_paths`` must be empty and ``reference_netlist`` is required:
    there is no testbench `.include` line to derive the schematic DUT from,
    so the caller names it. The command is then run **twice** -- once with
    ``reference_netlist``, once with the netlist this call extracted -- and
    each invocation's stdout is parsed as a *measurement document*
    (:func:`_normalize_measurement_document`). Every ``delta[]`` row is still
    computed here, by the same :func:`_build_delta_rows`, from the two
    documents; the caller supplies measurements, never a finished
    comparison. ``measure_timeout_s`` caps one invocation
    (:data:`MEASURE_COMMAND_TIMEOUT_S` by default). The whole mode is
    disclosed in the report's ``measurement`` block. ``pin_count_mismatch``,
    ``flat_dut_mismatch`` and ``pin_order_mismatch`` are always ``null`` in
    this mode -- all diagnose the testbench-`X...`-line-reuse contract, which a
    caller-supplied command does not use -- while ``model_mismatch``,
    ``extraction``, ``body_bias`` and ``provenance`` are computed exactly as
    in testbench mode. See this module's "Measuring with a caller-supplied
    command" docstring section.


    **``flatten_schematic`` (issue #2889) flattens the schematic leg.**
    ``klt extract``'s output is always flat while a schematic DUT is
    normally hierarchical, which makes ``model_mismatch`` fire on hierarchy
    alone and can leave the two legs incomparable. With ``flatten_schematic``
    (``klt pex --flatten-schematic``) the one resolved schematic DUT is
    flattened to PDK leaf devices once (:func:`_flatten_schematic_dut`,
    written to ``<artifacts root>/schematic-dut.flat.spice``), every
    schematic-side ``run_sim`` call runs a DUT-swapped copy of its testbench
    (:func:`_prepare_schematic_request`) -- or, in ``--measure-command``
    mode, the command is handed the flattened netlist -- and
    ``flat_dut_mismatch``/``pin_count_mismatch``/``model_mismatch`` compare
    the flattened netlist against the extraction. ``reference_netlist`` and
    ``testbenches[].schematic_netlist`` (and their digests) still name the
    caller's original DUT; the flattened file's path, digest and inlining
    summary are reported in ``schematic_flatten``. ``False`` (the default)
    leaves every schematic-side run exactly as before this parameter
    existed: ``flatten_schematic: false``, ``schematic_flatten: null``.

    **``extract_only`` (issue #3739)** is a third, mutually exclusive input
    mode: extract and report, measure nothing. ``extraction``, ``body_bias``
    and ``provenance`` come from the same ``run_extract`` call as a graded
    run; ``testbenches`` and ``delta`` are empty, ``reference_netlist`` is
    ``{"path": null, ...}`` with a ``null`` digest, the netlist diagnostics
    are ``null``, ``measurement.mode`` is ``"extract-only"`` and ``status`` is
    ``"not_checked"`` (``coverage.reasons`` ``no_delta_rows``) -- nothing was
    compared. Combining it with testbenches, ``measure_command``,
    ``reference_netlist``, ``measure_timeout_s``, ``device_only_leg`` or
    ``flatten_schematic`` raises :class:`PexError`.

    **``device_only_leg`` (issue #3298)** adds a third, parasitic-free leg:
    the same layout is extracted again with ``parasitics=False`` (no star
    nodes, no ``R``/``C`` cards -- no text stripping, so nothing depends on
    the ``<net>__tN`` naming), written beside the extracted netlist as
    ``<name>.device-only<ext>``, and every testbench is re-run against it.
    Each ``delta[]`` row then also carries ``device_only_value``,
    ``representation_delta_pct`` (device-only vs schematic) and
    ``parasitic_delta_pct`` (extracted vs device-only), separating
    device-representation effects (fingers/units, AS/AD/PS/PD, dummies) from
    parasitics; the result adds ``device_only_netlist``,
    ``device_only_netlist_sha256``, ``device_card_count`` and
    ``device_cards_match``. Without it the report is byte-identical.
    Incompatible with ``measure_command`` (:class:`PexError`).

    **Output-directory ownership (issue #2770).** The run owns its artifact
    root (``artifacts_dir``, default ``.klt/pex/`` beside the layout) for its
    whole lifetime through :func:`klayout_tools.output_lock.hold_output_dir`,
    taken before anything is created: a competing cooperating run raises
    :class:`~klayout_tools.output_lock.OutputDirLockedError`. The per-testbench
    ``run_sim`` calls on ``<root>/<label>/schematic|extracted`` inherit that
    ownership (they never re-acquire it), and a separate cooperating
    ``run_sim``/``run_pex`` on one of those child directories is refused
    while this run holds the root. ``_ownership`` is internal: the exit
    stack that releases the lock when the run ends.
    """
    _own_pex_root(_ownership, artifacts_dir, layout_path)
    # Issue #2478: exactly one of `testbench_paths` / `measure_command`.
    # `command` is the measurement command's argv in `--measure-command`
    # mode and `None` in the default testbench mode -- the single flag every
    # branch below reads.
    command = _validate_measure_mode(
        measure_command,
        testbench_paths,
        reference_netlist,
        measure_timeout_s,
        extract_only=extract_only,
        flatten_schematic=flatten_schematic,
        device_only_leg=device_only_leg,
    )
    command_mode = command is not None
    _reject_resistance_graph_with_distributed_rc(resistance_graph, distributed_rc)

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
            # Issue #1558: `--deck-option`/`--pins` passthrough. `None` for
            # both whenever the flags were never given, which is the exact
            # `run_extract` call this module made before they existed.
            deck_options=deck_options,
            declared_pins=declared_pins,
            # Issue #2854: `--rename-declared-pins` passthrough. `False`
            # whenever the flag was never given, which is the exact
            # `run_extract` call this module made before it existed.
            rename_declared_pins=rename_declared_pins,
            # Issue #2686: `--label-layer` passthrough. `None` when the flag
            # was never given -- the exact `run_extract` call made before it.
            label_layers=label_layers,
            critical_nets=critical_nets,
            critical_net_patterns=critical_net_patterns,
            critical_net_all=critical_net_all,
            critical_net_excludes=critical_net_excludes,
            distributed_rc=distributed_rc,
            # Issue #4037: `False`/`None` whenever the flags were never given,
            # which is the exact `run_extract` call made before they existed.
            resistance_graph=resistance_graph,
            resistance_graph_r_cut_ohm=resistance_graph_r_cut_ohm,
            emit_selected_resistance=resistance_graph,
            parasitic_variants=parasitic_variants,
            substrate_tie_ohms=substrate_tie_ohms,
            mom_rlc_net=mom_rlc_net,
            mom_rlc_resistance_ohm=mom_rlc_resistance_ohm,
            mom_rlc_capacitance_ff=mom_rlc_capacitance_ff,
            mom_rlc_inductance_nh=mom_rlc_inductance_nh,
        )
    except ExtractError as exc:
        raise PexError(f"extraction failed: {exc}") from exc

    # Issue #1525: `run_extract`'s own `netlist_path` field echoes `-o`/
    # `--output` back exactly as the caller spelled it (relative stays
    # relative) -- fine for `klt extract`'s own standalone report (still
    # resolves against this process's cwd, which never changes), but this
    # module bakes the same string *verbatim, as text* into the generated
    # extracted-side testbench's `.include` line (`_prepare_extracted_request`
    # -> `_rewrite_dut_include`). That testbench is then handed to `run_sim`,
    # whose per-corner `ngspice -b` invocation resolves a relative `.include`
    # against the *including file's own directory* (a corner-scoped
    # subdirectory nested under `work_dir`/`--outdir`), not this process's
    # cwd -- so a relative `netlist_path` resolves correctly only by
    # coincidence (`work_dir` happening to equal the caller's cwd). Resolving
    # to an absolute path here, once, immediately after extraction and before
    # `work_dir` is even computed (so still unambiguously relative to the
    # caller's own invocation cwd), makes the `.include` line resolve
    # correctly regardless of `-o`/`--outdir`'s own relative/absolute shape.
    # `_report_path` below (issue #1261) still renders this run's own
    # `netlist`/`reference_netlist` JSON fields as repo-relative
    # `{path, scope}` -- an absolute `extracted_netlist_path` does not leak
    # into this module's user-facing report.
    extracted_netlist_path = os.path.abspath(extract_report["netlist_path"])

    device_only_netlist_path, device_only_parity = _extract_device_only_leg(
        device_only_leg,
        extracted_netlist_path,
        layout_path,
        deck_name,
        top=top,
        pdk_variant=pdk_variant,
        pdk_root=pdk_root,
        deck_options=deck_options,
        declared_pins=declared_pins,
        rename_declared_pins=rename_declared_pins,
        label_layers=label_layers,
    )

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
    # Issue #2875: per-testbench content identities of the schematic-side
    # inputs, taken at the resolution points below -- before either side
    # simulates -- so the digests describe the files this run used.
    input_identity: list[dict[str, Any]] = []

    # Issue #2478: in `--measure-command` mode there is no testbench at all,
    # so the loop below is a no-op (`testbench_paths` is validated empty
    # above) and the schematic DUT is the caller's own `--reference-netlist`
    # instead of a `.include` target. It still has to *be* a DUT: the same
    # `_dut_declares_a_circuit` check testbench mode applies to its
    # `.include` target (Gap 1, issue #1255) applies here for the same
    # reason -- a `reference_netlist` naming a parameter/corner file with no
    # circuit of its own would make the schematic leg (and therefore every
    # `delta[]` row) meaningless while the report still looked like a real
    # comparison.
    measured_reference_netlist = (
        _resolve_measured_reference(reference_netlist)
        if command is not None and reference_netlist is not None
        else None
    )

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
            raise PexError(
                f"testbench '{testbench_path}': {exc}", code=exc.code
            ) from exc

        request_dir = os.path.dirname(os.path.abspath(testbench_path))
        schematic_body_path = _resolve_relative(request["netlist"], request_dir)
        if not os.path.isfile(schematic_body_path):
            raise PexError(
                f"testbench '{testbench_path}': netlist not found: "
                f"{schematic_body_path}"
            )
        with open(schematic_body_path, encoding="utf-8") as handle:
            body_text = handle.read()
        input_identity.append(
            {
                "request_sha256": _content_id(testbench_path),
                "netlist": schematic_body_path,
                "netlist_sha256": _content_id(schematic_body_path),
            }
        )
        _include_line, dut_path = _find_dut_include(
            body_text, os.path.dirname(schematic_body_path)
        )
        # Gap 1 (issue #1255): the single `.include`/`.inc` target must
        # itself plausibly be a DUT -- a shared corner/parameter file with
        # no circuit content of its own used to be silently swapped in as
        # if it were the DUT.
        _check_dut_declares_a_circuit(testbench_path, dut_path)
        dut_paths.append(dut_path)
        input_identity[-1]["schematic_netlist_sha256"] = _content_id(dut_path)

    if len(set(dut_paths)) > 1:
        raise PexError(
            "testbenches reference different schematic DUT netlists "
            f"({sorted(set(dut_paths))!r}) -- klt pex expects every "
            "testbench in one run to `.include` the same block's schematic "
            "netlist, so a single reference_netlist can be reported"
        )
    # One `reference_netlist` for the whole run either way: every testbench's
    # single `.include` target in testbench mode, the caller's own
    # `--reference-netlist` in `--measure-command` mode (issue #2478).
    # Issue #3739: `--extract-only` has no schematic side at all -- the
    # reference netlist is `None` and the three netlist diagnostics below
    # have nothing to compare, so they are skipped rather than faked.
    reference_netlist_path = _resolve_reference_netlist_path(
        extract_only, measured_reference_netlist, dut_paths
    )

    # Issue #2875: hashed at the resolution point, before either side runs
    # (`None` for `--extract-only`, which has no reference netlist).
    reference_netlist_sha256 = _content_id(reference_netlist_path)

    # Issue #2889: `--flatten-schematic` -- the netlist the schematic leg
    # actually runs (and every diagnostic below compares) is a flattened
    # copy of the DUT; `reference_netlist` keeps naming the caller's file.
    schematic_flatten: dict[str, Any] | None = None
    schematic_netlist_path = reference_netlist_path
    if flatten_schematic:
        schematic_netlist_path, schematic_flatten = _flatten_schematic_dut(
            reference_netlist_path,
            work_dir=work_dir,
            top=extract_report["top"],
            repo_root=repo_root,
        )

    # Gap 2 (issue #1255): a schematic DUT netlisted flat (no `.SUBCKT`
    # wrapper at all) against `klt extract`'s always-`.SUBCKT`-wrapped
    # output is diagnosed once, up front, for the whole run -- unlike
    # `_pin_count_mismatch` below (reactive: only ever consulted once a
    # simulation attempt has already come back with nothing measured), this
    # must run *before* either side simulates, since a testbench with no
    # `.ic` card might otherwise just simulate a disconnected fixture and
    # report a spurious passing number rather than an error at all (see
    # `_flat_dut_mismatch`'s own docstring).
    #
    # Issue #2478: inapplicable in `--measure-command` mode, and therefore
    # always `None` there. This diagnostic exists because testbench mode
    # re-runs the testbench's own unmodified `X...` instantiation line
    # against both netlists, so a flat schematic DUT leaves every
    # source/probe disconnected on the extracted side. A caller-supplied
    # command builds each side's deck itself from the netlist path it is
    # handed, so neither the premise nor the consequence holds -- reporting
    # it anyway would be a false alarm, and (worse) would skip the
    # extracted-side measurement the harness is perfectly able to take.
    flat_dut_mismatch = (
        None
        if command_mode or extract_only
        else _flat_dut_mismatch(
            reference_netlist=schematic_netlist_path,
            extracted_netlist=extracted_netlist_path,
            top_cell=extract_report["top"],
        )
    )

    # Issue #2402: a device model/`.subckt` divergence (e.g. a MOS
    # voltage-flavour mismatch) is orthogonal to both diagnostics above --
    # it can fire (or not) independently of whether the top-level pin lists
    # agree -- so it is computed unconditionally, once, from the same two
    # netlists' own text, before either side simulates. See
    # `_model_mismatch`'s own docstring. Computed in `--measure-command` mode
    # too (issue #2478): unlike the two pin-list diagnostics, it compares
    # what the two netlists *contain*, which is independent of how either
    # side was measured.
    model_mismatch = (
        None
        if extract_only
        else _model_mismatch(
            reference_netlist=schematic_netlist_path,
            extracted_netlist=extracted_netlist_path,
        )
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
    #
    # Issue #2478: like `flat_dut_mismatch` above, inapplicable (and always
    # `None`) in `--measure-command` mode -- the testbench `X...`-line reuse
    # that makes a pin-count difference fatal is exactly what that mode does
    # not do.
    pin_count_mismatch: dict[str, Any] | None = None
    # Issue #2990: the first extracted-side `SimError` that is not a pin-count
    # mismatch (e.g. a batch-fleet capacity refusal), `None` otherwise.
    backend_failure: dict[str, Any] | None = None
    if not command_mode and not extract_only and flat_dut_mismatch is None:
        pin_count_mismatch = _pin_count_mismatch(
            reference_netlist=schematic_netlist_path,
            extracted_netlist=extracted_netlist_path,
            subcircuit=extract_report["top"],
        )

    # Issue #2890: same pins in a different order. Pre-flight like the two
    # diagnostics above, mutually exclusive with both (counts must be equal,
    # and the schematic must declare the subcircuit), and `None` in
    # `--measure-command` mode for the same reason. It skips the extracted
    # leg and yields `status: "error"` / exit 4, like its siblings.
    pin_order_mismatch = _pin_order_preflight(
        command_mode,
        (flat_dut_mismatch, pin_count_mismatch),
        reference_netlist=reference_netlist_path,
        extracted_netlist=extracted_netlist_path,
        top_cell=extract_report["top"],
    )

    # `--measure-command` mode (issue #2478): `testbench_paths` is empty (it
    # is validated so above), so the per-testbench loop below is a no-op and
    # the two legs are measured here instead -- once per side, by the
    # caller's own command, against the two netlists *this* command chose.
    # The diff itself is still computed here, by the same
    # `_build_delta_rows` testbench mode uses.
    measurement_sides: dict[str, Any] | None = None
    if command is not None:
        command_rows, measurement_sides = _measure_both_sides(
            command,
            reference_netlist=schematic_netlist_path,
            extracted_netlist=extracted_netlist_path,
            work_dir=work_dir,
            timeout_s=_measure_timeout(measure_timeout_s),
            repo_root=repo_root,
            backend=backend,
            device_only_netlist=device_only_netlist_path,
        )
        delta.extend(command_rows)

    for index, testbench_path in enumerate(testbench_paths):
        label = labels[index]
        dut_path = dut_paths[index]

        try:
            # Issue #2889: `--flatten-schematic` runs a DUT-swapped copy of
            # the testbench; otherwise the testbench itself, unmodified.
            schematic_request_path = (
                _prepare_schematic_request(
                    testbench_path=testbench_path,
                    flattened_netlist_path=schematic_netlist_path,
                    work_dir=work_dir,
                    label=label,
                )
                if flatten_schematic
                else testbench_path
            )
            schematic_report = run_sim(
                schematic_request_path,
                artifacts_dir=os.path.join(work_dir, label, "schematic"),
                backend=backend,
            )
        except SimError as exc:
            # Issue #2990: keep `SimError.code` (e.g. `batch_no_capacity`)
            # so the CLI's error envelope lets a caller retry mechanically.
            raise PexError(
                f"testbench '{testbench_path}': schematic-side simulation "
                f"failed: {exc}",
                code=exc.code,
            ) from exc

        extracted_report: dict[str, Any] | None = None
        # Issue #3062: why the extracted side has no report, for the rows'
        # ``error`` objects: ``(message, code)``.
        extracted_failure: tuple[str, str | None] | None = None
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
        if _no_preflight_mismatch(
            flat_dut_mismatch, pin_count_mismatch, pin_order_mismatch
        ):
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
                # SimError is recorded as `backend_failure` (issue #2990)
                # and handled like the pin-count case: the schematic side's
                # completed results are kept and every delta row is an
                # error row, instead of discarding them by aborting.
                mismatch = _pin_count_mismatch_from_message(
                    str(exc),
                    reference_netlist=schematic_netlist_path,
                    extracted_netlist=extracted_netlist_path,
                )
                if mismatch is None:
                    backend_failure = backend_failure or _backend_failure(
                        testbench_path, exc
                    )
                else:
                    pin_count_mismatch = pin_count_mismatch or mismatch
                extracted_failure = (str(exc), exc.code)

        spec_row_prefix = (
            None
            if single_testbench
            else os.path.splitext(os.path.basename(testbench_path))[0]
        )
        if extracted_report is None:
            if extracted_failure is None:
                extracted_failure = _static_extracted_failure(
                    pin_count_mismatch, flat_dut_mismatch
                )
            delta.extend(
                _unextracted_delta_rows(
                    spec_row_prefix=spec_row_prefix,
                    schematic_report=schematic_report,
                    failure_message=extracted_failure[0] if extracted_failure else None,
                    failure_code=extracted_failure[1] if extracted_failure else None,
                    device_only_leg=device_only_leg,
                )
            )
            testbenches_summary.append(
                {
                    **_testbench_inputs(
                        testbench_path,
                        dut_path,
                        input_identity[index],
                        repo_root=repo_root,
                    ),
                    "corner_count": schematic_report["corner_count"],
                    "measurement_names": [
                        m["name"] for m in schematic_report["measurements"]
                    ],
                    **_project_testbench_sides(schematic_report, None),
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
                reference_netlist=schematic_netlist_path,
                extracted_netlist=extracted_netlist_path,
            )

        device_only_report = _run_device_only_sim(
            device_only_netlist_path,
            testbench_path=testbench_path,
            work_dir=work_dir,
            label=label,
            backend=backend,
        )

        rows = _build_delta_rows(
            spec_row_prefix=spec_row_prefix,
            schematic_report=schematic_report,
            extracted_report=extracted_report,
            device_only_report=device_only_report,
            device_only_leg=device_only_leg,
        )
        delta.extend(rows)
        testbenches_summary.append(
            {
                **_testbench_inputs(
                    testbench_path,
                    dut_path,
                    input_identity[index],
                    repo_root=repo_root,
                ),
                "corner_count": extracted_report["corner_count"],
                "measurement_names": [
                    m["name"] for m in extracted_report["measurements"]
                ],
                **_project_testbench_sides(schematic_report, extracted_report),
            }
        )

    corner_count = len({row["corner_id"] for row in delta})
    coverage = _build_coverage(
        testbenches_summary=testbenches_summary, delta=delta, corner_count=corner_count
    )
    passed = sum(1 for row in delta if row["status"] == "pass")
    failed = sum(1 for row in delta if row["status"] == "fail")
    errored = sum(1 for row in delta if row["status"] == "error")
    # Issue #2989: schematic-side limit failures, reported beside -- never
    # folded into -- `status`, `coverage` or the exit code.
    schematic_failed = sum(1 for row in delta if row["schematic_status"] == "fail")
    # Issue #2109: use the shared coverage decision table so a partial
    # comparison can never be reported as an unconditional pass, while real
    # failures and errors retain precedence over coverage qualification.
    status = _status_from_coverage(coverage, failed=bool(failed), errored=bool(errored))

    parasitics = extract_report.get("parasitics") or {}
    result: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "status": status,
        "layout": _report_path(layout_path, repo_root=repo_root),
        "netlist": _report_path(extracted_netlist_path, repo_root=repo_root),
        "reference_netlist": _report_path(reference_netlist_path, repo_root=repo_root),
        # Additive field (issue #2875): content identity of the schematic
        # DUT / `--reference-netlist` the schematic leg used.
        "reference_netlist_sha256": reference_netlist_sha256,
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
            # Additive field (issue #2686): `klt extract`'s own
            # `label_layers` echo for this run's extraction -- the applied
            # `--label-layer` mapping as `{role: [layer, datatype] | null}`,
            # or `None` when the flag was never given.
            "label_layers": extract_report.get("label_layers"),
            # Additive field (issue #2854): echoes the effective
            # `--rename-declared-pins` value back -- `False` when the flag
            # was never given, byte-identical to before this feature
            # existed. The renames themselves are `klt extract`'s own
            # `pin_renames` report, visible here through the extraction's
            # written netlist (`.SUBCKT` header) rather than re-embedded.
            "rename_declared_pins": bool(extract_report.get("rename_declared_pins")),
            # Additive field (issue #2764): the extraction's own
            # per-net parasitic RC network integrity diagnostics --
            # threshold/tolerances, aggregate counts, and one entry per net
            # (verbatim per-net payloads, `parasitics.nets[]` order). See
            # `_parasitic_integrity_report`.
            "parasitic_integrity": _parasitic_integrity_report(parasitics),
            # Additive field (issue #3056): echoes `--parasitic-variant`
            # back so a variant run is attributable -- present only when
            # the flag was given, byte-identical to before otherwise.
            **(
                {"parasitic_variant": parasitics["parasitic_variant"]}
                if "parasitic_variant" in parasitics
                else {}
            ),
            # Additive field (issue #4037): present only with
            # `--resistance-graph`.
            **(
                {
                    "resistance_graph": _resistance_graph_echo(
                        parasitics, resistance_graph_r_cut_ohm
                    )
                }
                if resistance_graph
                else {}
            ),
        },
        # Additive field (issue #1983): whether the extracted netlist this
        # run re-simulated has a DC bias path for every device body, reduced
        # from the extraction's own `unbiased_pmos_body_nets[]` (issue #555).
        # Reported beside `status`, never folded into it -- see
        # `_body_bias_report` for why an item-7 citation needs this in the
        # artifact it cites, and why this command does not grade on it.
        "body_bias": _body_bias_report(extract_report),
        # Additive field (issue #2478): how this run's two legs were
        # measured, always present so a reader of a committed record never
        # has to infer it from the presence/absence of `testbenches[]`.
        # `mode: "testbench"` (the default) means the legs came from `klt
        # sim` requests, and `command`/`timeout_s`/`sides` are all `null` --
        # byte-identical information to before this field existed.
        # `mode: "command"` means a caller-supplied measurement command
        # produced both legs: `command` echoes its argv verbatim (never a
        # shell string -- it is never run through a shell), `timeout_s` the
        # per-invocation cap in force, and `sides` records, per side, the
        # exact netlist that side was measured on, the command's exit status
        # there, and how many corners/rows it reported. That is the
        # disclosure `klt signoff`'s item-7 reader needs to tell an
        # externally-measured post-layout comparison from a `klt
        # sim`-measured one -- and to re-run it.
        "measurement": _measurement_block(
            command, measure_timeout_s, measurement_sides, extract_only=extract_only
        ),
        "testbenches": testbenches_summary,
        "corner_count": corner_count,
        "delta": delta,
        # Actual comparable rows, separately from attempted/error row counts.
        "coverage": coverage,
        "passed": passed,
        "failed": failed,
        "errored": errored,
        # Additive field (issue #2989): rows whose schematic side itself
        # failed its limits; `status` still grades the extracted side only.
        "schematic_failed": schematic_failed,
        # Additive field (issue #1030): `None` on every run whose extracted
        # side actually simulated -- byte-identical to before this feature
        # existed. Non-`None` names the schematic/extracted top-level pin-list
        # mismatch that made the extracted-side deck unrunnable, with both
        # sides' pin lists (see `_pin_count_mismatch`).
        "pin_count_mismatch": pin_count_mismatch,
        # Additive field (issue #2990): `None` on every run whose extracted
        # side(s) did not raise -- byte-identical information to before.
        # Non-`None` is `{testbench, side, message, code}` for the first
        # extracted-side backend failure (`code` is `SimError.code`, e.g.
        # `batch_no_capacity`, or `None`); that testbench's `delta[]` rows
        # are `error` rows with the schematic value intact.
        "backend_failure": backend_failure,
        # Additive field (issue #1255, Gap 2): `None` on every run whose
        # schematic DUT is `.SUBCKT`-wrapped (the normal case) --
        # byte-identical to before this feature existed. Non-`None` names
        # the flat-schematic-DUT-vs-`.SUBCKT`-wrapped-extraction mismatch
        # that made the extracted-side deck unrunnable (see
        # `_flat_dut_mismatch`); when this fires, no extracted-side
        # simulation is even attempted, so `pin_count_mismatch` above stays
        # `None`.
        "flat_dut_mismatch": flat_dut_mismatch,
        # Additive field (issue #2890): `None` unless both sides declare the
        # top subcircuit with the same pin set in a different order (see
        # `_pin_order_mismatch`); when it fires the extracted leg is skipped
        # exactly as for the two diagnostics above.
        "pin_order_mismatch": pin_order_mismatch,
        # Additive field (issue #2402): `None` on every run whose reference
        # (schematic) and extracted netlists instantiate the same device
        # models/`.subckt`s the same number of times -- byte-identical to
        # before this feature existed. Non-`None` names the divergence (e.g.
        # a MOS voltage-flavour substitution the layout's lack of
        # voltage-domain marker geometry silently produced) that makes this
        # run's `delta[]` a comparison between two electrically unrelated
        # netlists rather than a parasitic effect; unlike `pin_count_
        # mismatch`/`flat_dut_mismatch`, it never skips the extracted-side
        # simulation and never changes `status` -- see `_model_mismatch`.
        "model_mismatch": model_mismatch,
        # Additive fields (issue #2889): echo of `--flatten-schematic`
        # (`false` when not given) and, when given, the flattened schematic
        # DUT the schematic leg ran -- path, sha256 and inlining summary
        # (`null` otherwise, byte-identical information to before).
        "flatten_schematic": bool(flatten_schematic),
        "schematic_flatten": schematic_flatten,
        "provenance": extract_report["provenance"],
    }
    result.update(
        _device_only_result_fields(
            device_only_netlist_path, device_only_parity, repo_root=repo_root
        )
    )
    return result
