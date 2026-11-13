"""``klt pex`` multi-top mode: one graded run over several drawn tops.

Issue #5494. ``klt pex --top`` is repeatable: with two or more values each
named cell is extracted **on its own** -- one
:func:`~klayout_tools.extract.run_extract` call per top, with the caller's
flags unchanged, so each top is extracted exactly as a single-top run with
the same flags would extract it -- and the results are concatenated into the
one ``-o`` netlist, which therefore defines a ``.SUBCKT`` per top.

**Why the capability exists.** A link bench instantiates two or more cells
that are *separate tops in the layout*: a driver cell, a declared channel
model, a receiver cell. There is no single ``--top`` below the full array
that covers both, so before this mode a both-ends-extracted leg could not be
produced at all -- a caller had two one-ended runs, whose deltas do not
compose into the both-ends number, or an array-sized extraction paying
array-sized simulation for two cells' worth of coverage. The post-layout row
(``docs/design-evidence-tiers.md`` item 7) stayed a *declared gap* rather
than a measured number.

**What the mode holds fixed.**

- The **exactly-one-``.include``** rule (#1030, #2871) is untouched: one
  swap point, one ``reference_netlist``, so the "which include is the DUT"
  ambiguity never reopens. The price is that the testbench's single schematic
  DUT file must declare a ``.SUBCKT`` for every ``--top``
  (:func:`_require_subckt_per_top`).
- **Each top is extracted independently** -- no parasitics are modelled
  *between* tops, exactly as two separate single-top runs would behave.
  Inter-cell routing capacitance and cross-extraction supply interaction are
  absent. This mode makes the both-ends leg possible; it does not make it a
  full-array extraction.
- **The JSON change is additive.** One new field, ``tops``
  (:func:`_tops_block`), ``null`` on a zero/one-``--top`` run. Every existing
  field keeps its type and meaning; the singular ones go on describing the
  *combined* netlist the extracted leg simulated, reduced so they can never
  read cleaner than the worst top (:func:`_combine_extract_reports`,
  :func:`_first_firing`).
- **What the first version does not define, it refuses by name**, before any
  extraction (:func:`reject_unsupported_flags`). Each refused flag is
  single-top by construction; inventing a per-top syntax for every one of
  them would make this mode's contract several times larger than the
  capability it adds. Support for any individual flag is additive later.

This module imports nothing from :mod:`klayout_tools.pex` at module scope --
``pex`` imports *it* -- so the handful of functions that need a ``pex``
helper import it inside the call, the same way
:mod:`klayout_tools.pex_async` does.

See ``docs/cli/pex.md``'s "Several drawn tops in one run (``--top A --top
B``)" for the user-facing contract.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping, Sequence
from typing import Any

from ._provenance import sha256_file
from .extract import ExtractError, _default_output_path
from .pex_multi_top_flags import (
    MULTI_TOP_UNSUPPORTED_CODE,
)


def _pex_work_dir(artifacts_dir: str | None, layout_path: str) -> str:
    """Where this run writes its own generated artifacts: ``--outdir`` when
    given, else a ``.klt/pex`` directory next to ``<layout>``.

    Factored out (issue #5494) only so the multi-top extraction loop -- which
    stages one netlist per ``--top`` before the combined ``-o`` exists, and
    therefore runs *before* the work directory would otherwise be computed --
    resolves the identical directory the rest of the run uses. The expression
    is unchanged.
    """
    if artifacts_dir:
        return os.path.abspath(artifacts_dir)
    return os.path.join(os.path.dirname(os.path.abspath(layout_path)), ".klt", "pex")


def _per_top_netlist_path(staging_dir: str, index: int, top: str) -> str:
    """Where the ``index``-th ``--top``'s own extraction is written under
    ``staging_dir`` (issue #5494).

    Named ``<NN>-<cell>.spice`` -- the ordinal prefix makes ``--top`` order
    readable on disk and guarantees two cells whose names differ only in
    characters this sanitises apart cannot collide, and the whole thing lives
    in its own ``tops/`` subdirectory of the run's ``--outdir`` so it can
    never collide with the combined ``-o`` netlist (which the caller may well
    have pointed *at* ``--outdir``).
    """
    slug = re.sub(r"[^A-Za-z0-9._-]", "_", top) or "top"
    return os.path.join(staging_dir, f"{index:02d}-{slug}.spice")


#: A SPICE ``.GLOBAL`` card (issue #5494). ``klt extract --parasitics``
#: emits one per extraction for the substrate identities it synthesized
#: (issue #1503), so concatenating two extractions of the same PDK repeats
#: the identical card -- see :func:`_concatenate_extractions`.
_GLOBAL_CARD_RE = re.compile(r"^\s*\.global\b", re.IGNORECASE)
#: A SPICE continuation line, which belongs to the card above it.
_CONTINUATION_RE = re.compile(r"^\s*\+")


def _concatenate_extractions(
    per_top_paths: Sequence[str], tops: Sequence[str], combined_path: str
) -> None:
    """Write ``combined_path`` as the per-top extractions concatenated in
    ``--top`` order (issue #5494), with repeated ``.GLOBAL`` cards commented
    out rather than emitted twice.

    Each ``klt extract`` run writes exactly one ``.SUBCKT <top> ... .ENDS``
    block (`klt pex` never passes ``hierarchical_cells``), preceded by its
    header comments and -- when ``--parasitics`` synthesized a substrate
    identity -- a ``.GLOBAL`` card naming it (issue #1503). Concatenating
    two such files therefore yields two disjoint subcircuit definitions,
    which is exactly what the one re-pointed DUT ``.include`` needs, plus
    *the same* ``.GLOBAL vsubs`` card twice.

    **Why the de-duplication is here and not left to the simulator.** A
    repeated declaration of an already-global net is at best redundant and at
    worst a parse error depending on the engine, and this command's whole
    contract is that the extracted leg actually runs. Keeping the first
    occurrence of each distinct card and commenting out later identical ones
    (rather than deleting them) leaves the combined file self-describing
    about what was dropped and why, so a reader of a committed artifact does
    not have to diff it against the per-top files to understand it. A later
    card declaring a *different* net set is kept: it is new information, not
    a repeat.

    The combined file's first line is a `klt pex` title comment. SPICE reads
    line 1 as the deck title whatever it contains, and `klt extract`'s own
    output already opens with a comment line, so this changes nothing about
    how the file parses while naming the mode that produced it.
    """
    from .pex import PexError, _read_text

    chunks: list[str] = [
        f"* combined extraction written by klt pex: "
        f"{len(per_top_paths)} extractions concatenated in --top order "
        f"({', '.join(tops)}) -- each top is extracted on its own, so no "
        f"parasitic coupling between tops is modelled"
    ]
    seen_global: set[str] = set()
    for path, top in zip(per_top_paths, tops, strict=True):
        text = _read_text(path)
        if text is None:
            raise PexError(
                f"--top {top}: extraction wrote no readable netlist at {path}"
            )
        chunks.append(f"\n* ---- klt pex --top {top} ----")
        kept: list[str] = []
        dropping = False
        for line in text.splitlines():
            if _GLOBAL_CARD_RE.match(line):
                key = " ".join(line.split()).lower()
                dropping = key in seen_global
                seen_global.add(key)
                if dropping:
                    kept.append(
                        "* klt pex: duplicate card already declared above: "
                        f"{line.strip()}"
                    )
                    continue
            elif dropping and _CONTINUATION_RE.match(line):
                kept.append(f"* klt pex: {line.strip()}")
                continue
            else:
                dropping = False
            kept.append(line)
        chunks.append("\n".join(kept))
    try:
        os.makedirs(os.path.dirname(os.path.abspath(combined_path)), exist_ok=True)
        with open(combined_path, "w", encoding="utf-8") as handle:
            handle.write("\n".join(chunks).rstrip("\n") + "\n")
    except OSError as exc:
        raise PexError(
            f"cannot write the combined extracted netlist {combined_path}: {exc}"
        ) from exc


def _require_subckt_per_top(reference_netlist: str, tops: Sequence[str]) -> None:
    """Every ``--top`` must be declared as a ``.SUBCKT`` by the schematic DUT
    (issue #5494) -- raised before any simulation, naming the missing top.

    Multi-top mode keeps the exactly-one-``.include`` rule (#1030, #2871):
    the testbench's single DUT file is swapped for the one combined extracted
    netlist, and the testbench's own unmodified ``X...`` lines instantiate
    each cell by name on both legs. So the schematic DUT must define every
    cell the combined extraction defines, or the schematic leg instantiates a
    subcircuit nothing declares while the extracted leg runs fine -- a
    comparison between a working deck and a broken one, reported as a
    ``delta[]`` row.

    This is a hard error, not one of the three reported mismatch blocks,
    because unlike them it is a statement about the *caller's own arguments*
    (a ``--top`` the schematic does not model) rather than about a
    schematic/layout divergence the report exists to describe. It also
    necessarily precedes :func:`_flat_dut_mismatch`: a schematic DUT with no
    ``.SUBCKT`` at all fails this check on the first top, which is a strictly
    more actionable message than the flat-DUT diagnostic's for a multi-top
    run (it names which cell is missing, not merely that none are present).
    """
    from .pex import PexError, _read_text, _subckt_interfaces

    declared = _subckt_interfaces(_read_text(reference_netlist) or "")
    missing = [top for top in tops if top.lower() not in declared]
    if not missing:
        return
    present = ", ".join(name for name, _pins in declared.values()) or "(none)"
    raise PexError(
        f"the schematic DUT ({reference_netlist}) declares no `.SUBCKT` for "
        f"{'these --top cells' if len(missing) > 1 else 'the --top cell'} "
        f"{', '.join(missing)} -- it declares {present}. A multi-top run "
        "swaps the testbench's single DUT `.include` for one combined "
        "extracted netlist defining a `.SUBCKT` per --top, and re-runs the "
        "testbench's own instantiation lines unmodified against both legs, "
        "so the schematic DUT must define every --top cell too",
        code=MULTI_TOP_UNSUPPORTED_CODE,
    )


def _per_top_preflight(
    tops: Sequence[str],
    *,
    schematic_netlist: str,
    reference_netlist: str,
    extracted_netlist: str,
) -> list[dict[str, Any]]:
    """The per-top ``.SUBCKT`` requirement plus the three static pre-flight
    diagnostics, once per ``--top`` (issue #5494), in ``--top`` order.

    ``[]`` for a zero- or one-``--top`` run: the single-top path keeps
    computing its own three diagnostics from ``extract_report["top"]``,
    byte-identically to before this mode existed.

    Each entry is ``{top, flat_dut_mismatch, pin_count_mismatch,
    pin_order_mismatch}`` -- the same three functions a single-top run calls
    on ``extract_report["top"]``, asked instead about each named subcircuit
    inside the one combined netlist. They already take the subcircuit as an
    argument (``subcircuit``/``top_cell``) and compare only that name's
    interface, so a combined file holding several ``.SUBCKT`` blocks needs no
    change in them: each call is scoped to one top and cannot false-positive
    on another top's header.

    The same mutual exclusion holds per top as for a single-top run: a top
    whose counts differ is not also asked about pin *order*.
    ``flat_dut_mismatch`` cannot fire at all here -- it tests "the schematic
    declares no ``.SUBCKT`` whatsoever", which :func:`_require_subckt_per_top`
    has already rejected with a better message -- and is still computed, per
    top, so ``tops[]`` carries the field in the same shape the top level
    does rather than a special-cased ``null``.
    """
    from .pex import _flat_dut_mismatch, _pin_count_mismatch, _pin_order_preflight

    if len(tops) < 2:
        return []
    # Raised here, before any `klt sim` call: a `--top` the schematic does
    # not model would otherwise leave the schematic leg instantiating an
    # undeclared subcircuit while the extracted leg ran fine.
    _require_subckt_per_top(schematic_netlist, tops)
    entries: list[dict[str, Any]] = []
    for top in tops:
        flat = _flat_dut_mismatch(
            reference_netlist=schematic_netlist,
            extracted_netlist=extracted_netlist,
            top_cell=top,
        )
        count = (
            None
            if flat is not None
            else _pin_count_mismatch(
                reference_netlist=schematic_netlist,
                extracted_netlist=extracted_netlist,
                subcircuit=top,
            )
        )
        order = _pin_order_preflight(
            False,
            (flat, count),
            reference_netlist=reference_netlist,
            extracted_netlist=extracted_netlist,
            top_cell=top,
        )
        entries.append(
            {
                "top": top,
                "flat_dut_mismatch": flat,
                "pin_count_mismatch": count,
                "pin_order_mismatch": order,
            }
        )
    return entries


def _first_firing(entries: Sequence[Mapping[str, Any]], field: str) -> Any:
    """The first ``--top``'s non-``None`` ``field`` diagnostic, else ``None``
    (issue #5494) -- how a multi-top run's *singular* ``pin_count_mismatch``
    / ``flat_dut_mismatch`` / ``pin_order_mismatch`` field is chosen.

    Reporting the first firing top (rather than ``None``, or a list) keeps
    the field's existing type and meaning: non-``None`` still means "the
    extracted leg was not runnable and was skipped", and each block already
    identifies its own subcircuit, so the top it belongs to is readable from
    the block itself. Every top's own verdict is in ``tops[]``.
    """
    for entry in entries:
        if entry.get(field) is not None:
            return entry[field]
    return None


def extract_every_top(
    layout_path: str,
    deck_name: str,
    tops: Sequence[str],
    *,
    output: str | None,
    artifacts_dir: str | None,
    extract_kwargs: Mapping[str, Any],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Extract ``tops`` and return ``(report, per_top_reports)`` -- issue
    #5494.

    **Zero or one top** is the single-top path, and is deliberately the
    *same* ``run_extract`` call this module made before this mode existed:
    the caller's own ``output``/``top``, no staging directory created, ``-o``
    written by `klt extract` itself. ``report`` is that call's report and
    ``per_top_reports`` is ``[report]``.

    **Two or more tops**: one ``run_extract`` call per top, in ``--top``
    order, each with ``extract_kwargs`` unchanged -- so each top is extracted
    exactly as a single-top run with the same flags would extract it --
    writing into its own file under ``<outdir>/tops/``. Only once *every*
    extraction has succeeded are those files concatenated into ``-o``
    (:func:`_concatenate_extractions`), which is why a failure names the top
    that failed and leaves ``-o`` untouched rather than half-written, and why
    the combined file is not streamed as the loop goes. ``report`` is then the
    synthetic combined report :func:`~klayout_tools.pex_multi_top_report.
    _combine_extract_reports` builds, so every downstream reader keeps
    reading one report whose values describe the file the extracted leg ran.
    """
    # `run_extract` is read off `pex` at call time, deliberately: `pex` is the
    # one module that names this command's extraction entry point (its
    # `--device-only-leg` extraction calls it too), so one patch point covers
    # every extraction a `klt pex` run performs.
    from .pex import PexError, run_extract
    from .pex_multi_top_report import _combine_extract_reports

    if len(tops) < 2:
        try:
            report = run_extract(
                layout_path,
                deck_name,
                output=output,
                top=tops[0] if tops else None,
                **extract_kwargs,
            )
        except ExtractError as exc:
            raise PexError(f"extraction failed: {exc}") from exc
        return report, [report]

    # `-o`'s default is resolved with `klt extract`'s own helper rather than
    # restating the convention, so the combined file lands exactly where a
    # single-top run's would.
    combined_netlist_path = os.path.abspath(
        output if output is not None else _default_output_path(layout_path)
    )
    staging_dir = os.path.join(_pex_work_dir(artifacts_dir, layout_path), "tops")
    try:
        os.makedirs(staging_dir, exist_ok=True)
    except OSError as exc:
        raise PexError(
            f"cannot create the per-top extraction directory {staging_dir}: {exc}"
        ) from exc
    per_top_reports: list[dict[str, Any]] = []
    for index, one_top in enumerate(tops):
        try:
            per_top_reports.append(
                run_extract(
                    layout_path,
                    deck_name,
                    output=_per_top_netlist_path(staging_dir, index, one_top),
                    top=one_top,
                    **extract_kwargs,
                )
            )
        except ExtractError as exc:
            raise PexError(f"extraction failed for --top {one_top}: {exc}") from exc
    _concatenate_extractions(
        [report["netlist_path"] for report in per_top_reports],
        tops,
        combined_netlist_path,
    )
    return (
        _combine_extract_reports(
            per_top_reports,
            netlist_path=combined_netlist_path,
            netlist_sha256=sha256_file(combined_netlist_path),
        ),
        per_top_reports,
    )
