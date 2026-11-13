"""``klt pex`` multi-top mode: the ``--top`` list, and what the mode refuses.

Issue #5494, the entry half of :mod:`klayout_tools.pex_multi_top`'s mode (see
that module's docstring for what multi-top extraction *is* and why it
exists). Everything here runs before anything is read or written:
:func:`resolve_tops` turns the caller's ``--top`` into a validated list and
refuses, by name, every flag two or more tops do not define.

Kept separate from the extraction mechanics so the refusal list -- the part a
reader most often wants to check against ``docs/cli/pex.md`` -- is one short
file, and so neither :mod:`klayout_tools.pex` nor
:mod:`klayout_tools.pex_dry_run` has to import the extraction machinery just
to validate its arguments. Imports nothing from :mod:`klayout_tools.pex` at
module scope (``pex`` imports *this*); the two functions that raise
``PexError`` import it inside the call, the same way
:mod:`klayout_tools.pex_async` does.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

#: Issue #5494: ``PexError.code`` for a flag/argument combination the
#: repeatable-``--top`` (multi-top) mode does not define. Every such refusal
#: is raised *before* anything is extracted, in the style of
#: :func:`_reject_extract_only_conflicts` -- see
#: :func:`_reject_multi_top_conflicts`.
MULTI_TOP_UNSUPPORTED_CODE = "pex_multi_top_unsupported"


def _normalize_tops(top: str | Sequence[str] | None) -> list[str]:
    """``top`` as a list of cell names, in the order ``--top`` was given.

    ``klt pex --top`` is repeatable (issue #5494), so ``argparse`` hands this
    module a list; a bare string (every library caller that predates the
    flag's repeatability, and ``klt extract``'s own single-valued spelling)
    is accepted unchanged and normalised to a one-entry list. ``None`` --
    the "let the stream's single top cell decide" default -- normalises to
    the empty list, which is the same ``top=None`` call ``run_extract``
    received before this mode existed.

    A repeated cell name is a usage error rather than a silently
    de-duplicated or doubly-extracted top: extracting the same cell twice
    would write two identical ``.SUBCKT`` blocks into the one combined
    netlist, which ngspice rejects as a redefinition, and double-count its
    devices/nets in the summed ``extraction`` counts. Compared
    case-insensitively because ngspice itself is case-insensitive about
    subcircuit names (the same reason :func:`_subckt_interfaces` lowercases
    its keys), so ``--top RES --top res`` is the same collision.
    """
    from .pex import PexError

    if top is None:
        tops: list[str] = []
    elif isinstance(top, str):
        tops = [top]
    else:
        tops = list(top)
    seen: dict[str, str] = {}
    for name in tops:
        key = name.lower()
        if key in seen:
            raise PexError(
                f"--top {name!r} was given more than once "
                f"(already given as {seen[key]!r}): each --top must name a "
                "different cell -- extracting one cell twice would write "
                "two identical `.SUBCKT` blocks into the one combined "
                "netlist and double-count its devices and nets",
                code=MULTI_TOP_UNSUPPORTED_CODE,
            )
        seen[key] = name
    return tops


def _reject_multi_top_conflicts(
    tops: Sequence[str], conflicts: Sequence[tuple[bool, str, str]]
) -> None:
    """Raise :class:`PexError` naming the first flag that two or more
    ``--top`` values do not define (issue #5494).

    ``conflicts`` is ``(present, flag-spelling, why)`` triples, checked in
    order, *before* any extraction runs -- the same "refuse what this mode
    does not define, by name, up front" shape
    :func:`_reject_extract_only_conflicts` already uses for
    ``--extract-only``.

    This is deliberately a refusal rather than a design: every flag listed
    at the call site is single-top by construction (it takes one cell name,
    names one net of one extraction, or records one request a list-valued
    top would not round-trip), and inventing a per-top syntax for each one
    here would make this mode's contract several times larger than the
    capability it adds. Support for any individual flag is additive later;
    a refusal now is not a dead end, and it is what keeps a multi-top run's
    envelope honest about what it measured.
    """
    from .pex import PexError

    if len(tops) < 2:
        return
    for present, flag, why in conflicts:
        if present:
            raise PexError(
                f"{flag} cannot be combined with {len(tops)} --top values "
                f"({', '.join(tops)}): {why}. Extract the tops in separate "
                "single-`--top` runs if you need that flag, or drop it",
                code=MULTI_TOP_UNSUPPORTED_CODE,
            )


def reject_unsupported_flags(
    tops: Sequence[str],
    *,
    flatten_schematic: bool = False,
    device_only_leg: bool = False,
    tie_nets: Sequence[tuple[str, str]] | None = None,
    tie_declared_pins: bool = False,
    mom_rlc_net: str | None = None,
    measure_command: Sequence[str] | None = None,
    extract_only: bool = False,
    submit_only: bool = False,
    attach: str | None = None,
    resume: bool | None = None,
    dry_run: bool = False,
) -> None:
    """Refuse, by name, every flag two or more ``--top`` values do not define
    (issue #5494) -- a no-op for zero or one top.

    This is the canonical list; ``docs/cli/pex.md``'s "Flags refused with two
    or more ``--top``" table is its documented twin. Each entry's *why* is
    the reason the flag is single-top by construction, not a note about
    difficulty:

    - ``--flatten-schematic`` / ``--device-only-leg`` each take one top cell.
    - ``--tie-net``'s names are valid for one extraction only (its own help
      says so), and a multi-top run has one extraction per top.
    - ``--tie-declared-pins``' interaction with one shared ``--pins`` set
      across several tops' differing interfaces is not traced.
    - ``--mom-rlc-net`` substitutes an R/L/C for one net of one extraction.
    - ``--measure-command`` measures one netlist per side.
    - ``--extract-only`` has no schematic side, so the per-top ``.SUBCKT``
      pre-flight this mode is built on has nothing to check against.
    - ``--submit-only`` / ``--attach``: the manifest records a single-valued
      top and would not round-trip a list.
    - ``--resume``: a sharded leg's resume fingerprint content-hashes the
      testbench *body*, not the extracted netlist its DUT ``.include`` names,
      so it cannot tell one ``--top`` list's shards from another's.
    - ``--dry-run`` (issue #5464) reports one resolved ``top`` and validates
      one extraction request, so it cannot describe a multi-top run yet.
      Passed only by :mod:`klayout_tools.pex_dry_run`.
    """
    _reject_multi_top_conflicts(
        tops,
        (
            (
                flatten_schematic,
                "--flatten-schematic",
                "it flattens the schematic DUT against one named top cell's "
                "extracted interface",
            ),
            (
                device_only_leg,
                "--device-only-leg",
                "the parasitic-free third leg extracts one top cell",
            ),
            (
                bool(tie_nets),
                "--tie-net",
                "its net names are valid for one extraction only, and a "
                "multi-top run has one extraction per top",
            ),
            (
                tie_declared_pins,
                "--tie-declared-pins",
                "its interaction with one shared --pins set across several "
                "tops' differing interfaces is not defined",
            ),
            (
                mom_rlc_net is not None,
                "--mom-rlc-net",
                "it substitutes an R/L/C for one net of one extraction",
            ),
            (
                measure_command is not None,
                "--measure-command",
                "a caller-supplied command measures one netlist per side, and "
                "this mode's reference side is the schematic DUT's own "
                "per-top `.SUBCKT` set",
            ),
            (
                extract_only,
                "--extract-only",
                "it reports an extraction without a schematic side, so the "
                "per-top `.SUBCKT` pre-flight this mode is built on has "
                "nothing to check against",
            ),
            (
                submit_only,
                "--submit-only",
                "the submit manifest records a single-valued top and would "
                "not round-trip a list on --attach",
            ),
            (
                attach is not None,
                "--attach",
                "the submit manifest records a single-valued top, so an "
                "attach cannot verify it against a list",
            ),
            (
                bool(resume),
                "--resume",
                "a sharded leg's resume fingerprint hashes the testbench "
                "body, not the extracted netlist the DUT `.include` names, "
                "so it cannot tell one --top list's shards from another's",
            ),
            (
                dry_run,
                "--dry-run",
                "a dry run reports one resolved `top` and validates one "
                "extraction request, so it cannot describe a multi-top run "
                "yet; drop --dry-run to run the multi-top extraction, or "
                "dry-run each top on its own",
            ),
        ),
    )


def resolve_tops(top: str | Sequence[str] | None, **flags: Any) -> list[str]:
    """``top`` normalised to a list (:func:`_normalize_tops`) with every flag
    two or more values do not define already refused
    (:func:`reject_unsupported_flags`) -- issue #5494.

    One call so no caller can normalise without refusing, or refuse against a
    ``top`` it has not normalised. Both halves raise before anything is read
    or written.
    """
    tops = _normalize_tops(top)
    reject_unsupported_flags(tops, **flags)
    return tops
