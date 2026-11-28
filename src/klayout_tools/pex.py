"""Post-layout extraction and comparison for ``klt pex``.

:func:`run_pex` accepts a routed layout, an extraction deck or runset, and
the measurement inputs for the selected mode. Pass ``klt sim`` testbench
requests or a measurement command and reference netlist to compare schematic
and extracted results. Extraction-only mode produces an extraction report.

The library selects the netlists, computes the measurement deltas, and returns
a JSON-serializable dictionary. The CLI formats that report. See
``docs/cli/pex.md`` for the modes, options, and report fields.
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

from . import (
    env_provenance,
    output_lock,
    pex_async,
    pex_binding,
    pex_runset,
    pex_sim_evidence,
)
from ._jsonnum import is_finite_number, is_number
from ._paths import _resolve_relative
from ._provenance import content_hash, sha256_file
from ._text import read_text as _read_text
from .coverage import (
    REASON_NO_DELTA_ROWS,
    build_check_coverage,
    coverage_rollup,
    rollup_status,
    work_id,
)
from .extract import SPICE_GLOBAL_GROUND_NODE, ExtractError, run_extract
from .pex_asap7 import (
    adapt_leg_netlists,
    with_fidelity_warnings,
)
from .pex_body_bias import (  # noqa: F401 -- re-exported, see pex_body_bias
    BODY_BIAS_BIASED,
    BODY_BIAS_TIED,
    BODY_BIAS_UNBIASED,
    TIE_SOURCE_DECLARED,
    _body_bias_report,
)
from .pex_errors import PexError as PexError
from .pex_multi_top import (
    _first_firing,
    _per_top_preflight,
    _pex_work_dir,
    extract_every_top,
)
from .pex_multi_top_flags import resolve_tops
from .pex_multi_top_report import _extraction_block, _tops_block
from .pex_netlist_text import (
    _device_model_counts,
    _dut_declares_a_circuit,
    _expand_reference_includes,
    _has_include_card,
    _instantiated_model_counts,
    _logical_cards,
    _reference_is_hierarchical,
    _subckt_interfaces,
)
from .pex_resistance_graph import (
    _reject_resistance_graph_with_distributed_rc,
    _reject_resistance_network_without_graph,
)
from .pex_resistance_graph import (
    _resistance_graph_echo as _resistance_graph_echo,
)
from .sim import SimError, load_request, run_sim
from .sim_osdi_pin import rebase_osdi_preload
from .sim_request_overrides import refuse_corner_only_flags
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
#: such record. See `env_provenance.repo_relative_path`.
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

#: A measurement document row's permitted ``status`` values -- the same three
#: a ``delta[]`` row itself uses, and the same three `klt sim` reports per
#: measurement, so :func:`_row_status` needs no command-mode special case.
_MEASUREMENT_STATUSES = ("pass", "fail", "error")


def _validate_tie_nets(
    ties: Sequence[tuple[str, str]], extract_report: Mapping[str, Any]
) -> None:
    """Reject a ``--tie-net`` whose extracted half is not one of the
    extraction's own unbiased nets (issue #4245), naming the valid ones.

    Confining the flag to reported floating bodies stops it rewriting
    arbitrary nets of the extracted netlist.
    """
    valid = sorted(
        {e["net"] for e in extract_report.get("unbiased_pmos_body_nets") or []}
    )
    for extracted, _schematic in ties:
        if extracted not in valid:
            listing = (
                ", ".join(repr(net) for net in valid)
                if valid
                else "none -- this extraction reported no unbiased net"
            )
            raise PexError(
                f"--tie-net: {extracted!r} is not an unbiased well/body net of "
                f"this extraction (valid: {listing}). Anonymous net names are "
                "synthesized per extraction; read them from "
                "`body_bias.unbiased_nets` of an untied run.",
                code="invalid_tie_net",
            )


def _apply_tie_nets(netlist_path: str, ties: Sequence[tuple[str, str]]) -> None:
    """Merge each tied extracted net into its schematic net, in place, in
    ``netlist_path`` (issue #4245).

    Only whole whitespace-delimited tokens on device/instance cards are
    rewritten (never comments, directives or ``name=value`` parameters), and
    all ties are applied in one pass so a chain such as ``a=b`` / ``b=c``
    cannot cascade.
    """
    mapping = dict(ties)
    pattern = re.compile(
        r"(?<!\S)(" + "|".join(re.escape(n) for n in mapping) + r")(?!\S)"
    )
    with open(netlist_path, encoding="utf-8", newline="") as handle:
        lines = handle.read().splitlines(keepends=True)
    seen: set[str] = set()

    def swap(match: re.Match[str]) -> str:
        seen.add(match.group(1))
        return mapping[match.group(1)]

    out = [
        line if line.lstrip().startswith(("*", ".")) else pattern.sub(swap, line)
        for line in lines
    ]
    missing = [net for net in mapping if net not in seen]
    if missing:
        raise PexError(
            f"--tie-net: {missing[0]!r} does not appear on any device card of "
            f"the extracted netlist {netlist_path}",
            code="invalid_tie_net",
        )
    with open(netlist_path, "w", encoding="utf-8", newline="") as handle:
        handle.write("".join(out))


def _tie_nets_in_legs(
    ties: Sequence[tuple[str, str]] | None,
    extract_report: Mapping[str, Any],
    extracted_netlist_path: str,
    device_only_netlist_path: str | None,
) -> None:
    """Validate ``ties`` against the extraction, then apply them to every
    netlist a simulated leg reads (issue #4245). A no-op when ``ties`` is empty.
    """
    if not ties:
        return
    _validate_tie_nets(ties, extract_report)
    _apply_tie_nets(extracted_netlist_path, ties)
    if device_only_netlist_path is not None:
        _apply_tie_nets(device_only_netlist_path, ties)


def _substrate_tie_fields(parasitics: Mapping[str, Any]) -> dict[str, Any]:
    tie = parasitics.get("substrate_dc_tie")
    if tie is None:
        return {}
    return {"substrate_dc_tie": {**tie, "reference_node": SPICE_GLOBAL_GROUND_NODE}}


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
    schematic_top: str | None = None,
    ngspice_message: str | None = None,
) -> dict[str, Any] | None:
    """The structured ``pin_count_mismatch`` diagnostic (issue #1030), or
    ``None`` when no mismatch can be established.

    Compares the ``.SUBCKT`` interfaces the schematic DUT
    (``reference_netlist``) and the extraction (``extracted_netlist``)
    declare for the same subcircuit name and reports the first one whose pin
    *counts* differ -- the exact condition ngspice rejects with ``Too
    few/many parameters for subcircuit type``, since `klt pex` reuses the
    same positional nodes on both legs, including when their subcircuit names differ.

    ``subcircuit`` (when ngspice itself named one) restricts the comparison
    to that subcircuit. ``ngspice_message`` is ngspice's own line, carried
    through verbatim; when it is present but the two netlists' headers
    cannot be compared (unreadable file, a name only one side declares) the
    diagnostic is still returned, with ``null`` pin data -- ngspice's own
    verdict is authoritative on its own.
    """
    schematic_text = _read_text(reference_netlist)
    extracted_text = _read_text(extracted_netlist)
    public = schematic_top is not None
    schematic_interfaces = _subckt_interfaces(
        schematic_text or "", file_scope_only=public
    )
    extracted_interfaces = _subckt_interfaces(
        extracted_text or "", file_scope_only=public
    )

    candidates = (
        [subcircuit.lower()] if subcircuit is not None else list(extracted_interfaces)
    )
    for key in candidates:
        schematic_interface = schematic_interfaces.get((schematic_top or key).lower())
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
                f"({', '.join(schematic_interface[1])}) -- klt pex uses the same "
                "positional testbench nodes for both subcircuits, so "
                "ngspice refuses the extracted-side deck. "
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


def _refuse_submit_on_preflight_mismatch(
    *,
    submitting: bool,
    flat_dut_mismatch: dict[str, Any] | None,
    pin_count_mismatch: dict[str, Any] | None,
    pin_order_mismatch: dict[str, Any] | None,
) -> None:
    """Issue #4696: raise :class:`PexError` for the first fired static
    pre-flight diagnostic (which skips the extracted leg), so ``--submit-only``
    refuses before any leg is submitted. No-op unless ``submitting``."""
    if not submitting:
        return
    for code, diagnostic in (
        ("flat_dut_mismatch", flat_dut_mismatch),
        ("pin_count_mismatch", pin_count_mismatch),
        ("pin_order_mismatch", pin_order_mismatch),
    ):
        if diagnostic is not None:
            raise PexError(
                f"--submit-only: the extracted leg cannot be submitted "
                f"({code}): {diagnostic.get('detail') or code}",
                code=code,
            )


def _no_preflight_mismatch(*diagnostics: dict[str, Any] | None) -> bool:
    """True when none of the pre-flight mismatch diagnostics fired, i.e. the
    extracted-side simulation is worth attempting."""
    return all(d is None for d in diagnostics)


def _pin_order_preflight(
    command_mode: bool,
    earlier: tuple[dict[str, Any] | None, ...],
    *,
    extract_only: bool = False,
    **kwargs: Any,
) -> dict[str, Any] | None:
    """The ``pin_order_mismatch`` pre-flight (issue #2890), gated: ``None`` in
    ``--measure-command`` mode, ``None`` in ``--extract-only`` mode -- an
    explicit gate since issue #5477, no longer implied by an empty
    ``reference_netlist`` (:func:`run_pex` states why once; its siblings
    ``flat_dut_mismatch``/``pin_count_mismatch`` are gated at their own call
    sites) -- and once an earlier (mutually exclusive) pre-flight diagnostic
    has fired; otherwise :func:`_pin_order_mismatch`."""
    if command_mode or extract_only or not _no_preflight_mismatch(*earlier):
        return None
    return _pin_order_mismatch(**kwargs)


def _pin_order_mismatch(
    *,
    reference_netlist: str,
    extracted_netlist: str,
    top_cell: str,
    schematic_top: str | None = None,
) -> dict[str, Any] | None:
    """Report equal pin sets whose order differs between the two selected roots.

    X cards connect nodes positionally. Extraction orders its real ports then
    parasitic references alphabetically; the schematic uses symbol order.
    The pin-count and flat-DUT checks handle their own failure cases.
    """
    schematic_interface = _subckt_interfaces(
        _read_text(reference_netlist) or "", file_scope_only=True
    ).get((schematic_top or top_cell).lower())
    extracted_interface = _subckt_interfaces(
        _read_text(extracted_netlist) or "", file_scope_only=True
    ).get(top_cell.lower())
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
            "same positional testbench nodes, so the extracted leg would wire "
            "signals and supplies to the wrong pins and report meaningless "
            "deltas. The extracted order is deterministic (real ports "
            "first, then parasitic references, each alphabetical; `--pins` "
            "overrides it); or wrap the schematic DUT in a `.SUBCKT` that "
            "declares the pins in the extracted order so one instantiation "
            "line is valid for both legs"
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
            "completion and report a spurious value rather than error. Under "
            "`--flatten-schematic` klt pex wraps a fragment DUT for you; "
            "otherwise wrap the schematic DUT in a matching "
            "`.SUBCKT ... .ENDS` (with the "
            "same top-level pin list `klt extract` produces) so the "
            "testbench's own instantiation line can be reused unmodified "
            "on both sides, the same convention `pin_count_mismatch` "
            "already requires"
        ),
    }


def _model_mismatch(
    *, reference_netlist: str, extracted_netlist: str, extracted_top: str | None = None
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

    **A hierarchical extraction is expanded, not scanned** (issue #6559): the
    runset engine writes every circuit it produced, so the extracted side is
    counted as the devices instantiated from ``extracted_top``
    (:func:`_instantiated_model_counts`) -- a local cell name is never a
    device family. A flat extraction counts exactly as before.

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
    reference_counts = _device_model_counts(
        _expand_reference_includes(reference_text, reference_netlist)
    )
    extracted_counts = _instantiated_model_counts(
        _read_text(extracted_netlist) or "", extracted_top
    )

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
    if not reference_counts and _has_include_card(reference_text):
        unrelated_clause = (
            "the reference netlist yielded no device cards, and it contains "
            "an `.include` that could not be resolved or read (missing "
            "file, `$VAR` path, or include cap), so this divergence may "
            "only reflect the unread include rather than a genuine "
            "model-set mismatch. "
        )
    elif hierarchical_reference:
        unrelated_clause = (
            "the reference netlist is hierarchical (it instantiates at least one "
            "locally-defined `.subckt`), while `klt extract`'s curated engine "
            "writes a flat netlist (`--engine klayout-runset` may write a "
            "hierarchical one) -- see docs/cli/extract.md's \"Extraction is flat, "
            "not hierarchical\" note and issue #1085 (the same shape `klt lvs`'s "
            "`options.flatten_reference` exists for). This divergence may simply "
            "be a hierarchy-representation artifact -- a device nested inside a "
            "locally-defined `.subckt` is counted once per definition here, not "
            "once per physical instance -- rather than a genuine device-flavour "
            "mismatch; re-run with `klt pex --flatten-schematic` (issue #2889), "
            "which flattens the schematic DUT to PDK leaf devices before the "
            "schematic-side run, before treating this as evidence the two sides "
            "are unrelated. "
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
    "pin_order_mismatch": "execute",
    "flat_dut_mismatch": "execute",
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
    measurement: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Describe why a delta row errored (#3062). Select the diagnostic
    explaining the row's failure, including a confirmed measurement window miss.
    A caller can supply the message and code directly. Include the side's
    remote job metadata when available. A measurement-harness message alone
    is an execute-stage error (#4555)."""
    remote = None
    if sim_report is not None:
        remote = (sim_report.get("environment") or {}).get("remote")
        if message is None:
            for corner in sim_report.get("corners", []):
                if corner.get("corner_id") != corner_id:
                    continue
                chosen = pex_sim_evidence.explaining_diagnostic(
                    corner.get("diagnostics") or [], measurement
                )
                if chosen is not None:
                    message = chosen.get("message") or chosen.get("code")
                    code = chosen.get("code")
                break
    stage = None
    if not message and measurement is not None and measurement.get("message"):
        message = measurement["message"]
        stage = "execute"
    text = message or "no diagnostic was recorded for this errored row"
    error: dict[str, Any] = {
        "side": side,
        "stage": stage or _error_stage(code, text),
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
    testbench: str | None = None,
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
            schematic_measurement = schematic_measurements.get(name)
            schematic_value = (
                schematic_measurement["value"]
                if schematic_measurement is not None
                else None
            )
            row = {
                **pex_sim_evidence.delta_labels(testbench, spec_row_prefix, name),
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
                    _side_error(
                        "schematic",
                        schematic_report,
                        corner_id,
                        measurement=schematic_measurement,
                    )
                    if schematic_bad
                    else _side_error(
                        "extracted",
                        extracted_report,
                        corner_id,
                        measurement=extracted_measurement,
                    )
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
    **mismatches: dict[str, Any] | None,
) -> tuple[str, str] | None:
    """Use the top-level mismatch name and detail for the skipped leg."""
    for code, mismatch in mismatches.items():
        if mismatch and mismatch.get("detail"):
            return str(mismatch["detail"]), code
    return None


def _unextracted_delta_rows(
    *,
    testbench: str | None = None,
    spec_row_prefix: str | None,
    schematic_report: dict[str, Any],
    failure_message: str | None = None,
    failure_code: str | None = None,
    device_only_leg: bool = False,
    schematic_probes: tuple[str, ...] = (),
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
            if name in schematic_probes:
                continue
            row = {
                **pex_sim_evidence.delta_labels(testbench, spec_row_prefix, name),
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
    inapplicable = []
    for row_index, row in enumerate(delta):
        # `row_index` (this row's own position in the report's `delta` list)
        # scopes the identity -- `spec_row` is a *display* label
        # (`<testbench file stem>.<name>`, docs/cli/pex.md), and two distinct
        # testbench files sharing a basename in different directories (e.g.
        # `a/request.json` and `b/request.json`) produce the same stem, which
        # would otherwise collide as one "unique" work identity for two
        # genuinely separate comparisons.
        identity = work_id("comparison", row_index, row["corner_id"], row["spec_row"])
        if row.get("extracted_status") == "skipped":
            inapplicable.append(
                {"id": identity, "reason": "schematic_only_measurement"}
            )
        elif row["status"] in {"pass", "fail"}:
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
            inapplicable=inapplicable,
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
    if not is_number(raw):
        raise ValueError("must be a number or null")
    if not math.isfinite(value := float(raw)):
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
    normalized = {"name": name, "value": value, "status": status}
    if "message" in row:
        # Issue #4555: optional free-text reason a harness gives for a
        # value-less measurement; surfaced only via ``delta[].error``.
        message = row["message"]
        if not isinstance(message, str) or not message.strip():
            raise fail(f"{where}.message must be a non-empty string")
        normalized["message"] = message
    return normalized


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
    """The one ``reference_netlist`` for the run: the caller's own
    ``--reference-netlist`` whenever one was given (``--measure-command``
    mode, issue #2478, and ``--extract-only`` mode, issue #5477), ``""`` for
    a bare ``--extract-only`` (no schematic side at all; every consumer of
    the path is skipped and ``env_provenance.repo_relative_path`` /
    :func:`content_hash` read it as ``null``), else the testbenches' own
    single ``.include``d DUT. A non-empty path in ``--extract-only`` mode
    feeds :func:`_model_mismatch` alone, not a schematic leg -- every
    measurement-leg consumer stays gated on ``extract_only`` at its own call
    site; see :func:`run_pex`."""
    if measured_reference_netlist is not None:
        return measured_reference_netlist
    if extract_only:
        return ""
    return dut_paths[0]


def _validate_simulation_timeout(
    timeout_s: float | None, measure_command: Sequence[str] | None, extract_only: bool
) -> None:
    if timeout_s is None:
        return
    if not is_finite_number(timeout_s) or timeout_s <= 0:
        raise PexError("--timeout-s must be a finite positive number of seconds")
    if extract_only or measure_command is not None:
        raise PexError(
            "--timeout-s requires testbench mode and cannot be used with "
            "--extract-only or --measure-command (use --measure-timeout-s for commands)"
        )


def _validate_measure_mode(
    measure_command: Sequence[str] | None,
    testbench_paths: Sequence[str],
    reference_netlist: str | None,
    measure_timeout_s: float | None,
    *,
    timeout_s: float | None = None,
    extract_only: bool = False,
    flatten_schematic: bool = False,
    schematic_top: str | None = None,
    device_only_leg: bool = False,
) -> list[str] | None:
    """Validate testbench, external measurement-command and extraction-only modes.
    Both dry-run and execution use the same checks before extraction starts.
    """
    if schematic_top is not None and (extract_only or measure_command is not None):
        raise PexError("--schematic-top requires testbench mode")
    _validate_simulation_timeout(timeout_s, measure_command, extract_only)
    if extract_only:
        _reject_extract_only_conflicts(
            [
                (bool(testbench_paths), "a testbench request"),
                (measure_command is not None, "--measure-command"),
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
    document declared, bare, and ``testbench`` is ``null`` (issue #5681).
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
            "netlist": env_provenance.repo_relative_path(
                side_netlist or None, repo_root=repo_root
            ),
            "exit_status": exit_status,
            "corner_count": side_report["corner_count"],
            "measurement_names": side_report["measurement_names"],
        }

    rows = _build_delta_rows(
        testbench=None,
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


def _bound_testbenches(
    schematic: str | None,
    physical: str,
    schematic_path: str,
    body_paths: Sequence[str],
    extracted_path: str | None = None,
) -> list[str | None]:
    """Resolve the requested roots and every testbench before dispatching a leg."""
    if schematic is None:
        return [None] * len(body_paths)
    try:
        return pex_binding.bind(
            schematic, physical, schematic_path, body_paths, extracted_path
        )
    except ValueError as exc:
        raise PexError(
            f"cannot bind schematic DUT '{schematic}' to layout top "
            f"'{physical}': {exc}",
            code="dut_binding",
        ) from exc


def _prepare_extracted_request(
    *,
    testbench_path: str,
    extracted_netlist_path: str,
    work_dir: str,
    label: str,
    bound_body: str | None = None,
) -> str:
    """The extracted-side DUT-swapped request copy -- see
    :func:`_prepare_dut_swapped_request`."""
    return _prepare_dut_swapped_request(
        testbench_path=testbench_path,
        dut_netlist_path=extracted_netlist_path,
        work_dir=work_dir,
        label=label,
        side="extracted",
        bound_body=bound_body,
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
    bound_body: str | None = None,
    side: str,
) -> str:
    """Build (and write, under ``work_dir``) a ``side``-side copy of the
    testbench request at ``testbench_path``: identical in every field except
    ``netlist`` (re-pointed at a new body file whose DUT `.include` names
    ``dut_netlist_path``, see :func:`_rewrite_dut_include`) and, for
    ``side == "extracted"`` only, ``netlist_source`` (forced to
    ``"extracted"``). Returns the written request file's path, suitable for
    :func:`~klayout_tools.sim.run_sim`.

    Resolve request-relative OSDI preloads and literal ``models.lib`` paths
    against the original testbench directory. Keep literal PDK-root preloads
    for the executing host to resolve.

    A ``models`` block that *does* carry ``pdk``/``pdk_root`` is copied
    through **unmodified** (issue #1395): in that shape a relative ``lib``
    is PDK-variant-relative, not request-dir-relative -- see
    :func:`~klayout_tools.sim.resolve_models_lib`, the resolution both a
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
    body_text = bound_body
    if body_text is None:
        with open(schematic_body_path, encoding="utf-8") as handle:
            body_text = handle.read()
    body_dir = os.path.dirname(schematic_body_path)
    include_line, _dut_path = _find_dut_include(body_text, body_dir)
    swapped_body_text = _rewrite_dut_include(body_text, include_line, dut_netlist_path)

    swapped_body_path = os.path.join(work_dir, f"{label}.{side}-dut-testbench.spice")
    with open(swapped_body_path, "w", encoding="utf-8") as handle:
        handle.write(swapped_body_text)

    swapped_request = copy.deepcopy(request)
    rebase_osdi_preload(swapped_request, request_dir)
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
        pex_sim_evidence.schematic_probes(swapped_request, omit=True)

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
        "request": env_provenance.repo_relative_path(
            testbench_path or None, repo_root=repo_root
        ),
        "request_sha256": identity["request_sha256"],
        "netlist": env_provenance.repo_relative_path(
            identity["netlist"] or None, repo_root=repo_root
        ),
        "netlist_sha256": identity["netlist_sha256"],
        "schematic_netlist": env_provenance.repo_relative_path(
            dut_path or None, repo_root=repo_root
        ),
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
    wrap_pins: Sequence[str] | None = None,
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

    A *fragment* DUT (no ``.subckt <top>``) is wrapped in one whose pins are
    ``wrap_pins`` -- the extracted netlist's own header order (issue #4625);
    ``schematic_flatten["wrapper"]`` then lists those pins (else ``None``).
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
            wrap_pins=wrap_pins,
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
        "netlist": env_provenance.repo_relative_path(
            flat_path or None, repo_root=repo_root
        ),
        "netlist_sha256": content_hash(flat_path),
        "subckts": result.roots,
        "inlined_instances": result.inlined_instances,
        "inlined_subckts": result.inlined_subckts,
        "external_subckts": result.external_subckts,
        "already_flat": result.inlined_instances == 0,
        "wrapper": (
            {"synthesized": True, "pins": result.wrapper_pins}
            if result.wrapper_pins is not None
            else None
        ),
    }


_STAR_NODE_SUFFIX = re.compile(r"__t\d+(?=\s|$)")


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


def _extract_device_only_leg(
    device_only_leg: bool,
    extracted_netlist_path: str,
    layout_path: str,
    deck_name: str | None,
    *,
    options: pex_runset.PexExtractOptions,
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
            parasitics=False,
            **options.device_only_kwargs(),
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
    bound_body: str | None = None,
    backend: Any,
    batch_shard: Mapping[str, int | float | bool | None] | None = None,
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
                bound_body=bound_body,
            ),
            artifacts_dir=os.path.join(work_dir, label, "device-only"),
            backend=backend,
            **(batch_shard or {}),
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
        "device_only_netlist": env_provenance.repo_relative_path(
            device_only_netlist_path or None, repo_root=repo_root
        ),
        "device_only_netlist_sha256": content_hash(device_only_netlist_path),
        **(device_only_parity or {}),
    }


def _load_testbench_request(
    testbench_path: str, timeout_s: float | None = None
) -> dict[str, Any]:
    """``load_request`` with a :class:`SimError` re-raised as a
    :class:`PexError` naming the testbench."""
    try:
        request = load_request(testbench_path)
        refuse_corner_only_flags(
            request, pdk_root=None, timeout_s=timeout_s, collect_partial=False
        )
        return request
    except SimError as exc:
        raise PexError(f"testbench '{testbench_path}': {exc}", code=exc.code) from exc


def _require_single_dut(dut_paths: Sequence[str]) -> None:
    """Every testbench in one run must ``.include`` the same schematic DUT."""
    if len(set(dut_paths)) > 1:
        raise PexError(
            "testbenches reference different schematic DUT netlists "
            f"({sorted(set(dut_paths))!r}) -- klt pex expects every "
            "testbench in one run to `.include` the same block's schematic "
            "netlist, so a single reference_netlist can be reported"
        )


@output_lock.owns_run
def run_pex(
    layout_path: str,
    testbench_paths: Sequence[str],
    deck_name: str | None,
    *,
    output: str | None = None,
    top: str | Sequence[str] | None = None,
    pdk_variant: str | None = None,
    pdk_root: str | None = None,
    artifacts_dir: str | None = None,
    backend: str | None = None,
    max_units_per_job: int | None = None,
    max_concurrent_jobs: int | None = None,
    hosts: int | None = None,
    resume: bool | None = None,
    capacity_wait_s: float | None = None,
    timeout_s: float | None = None,
    deck_options: Mapping[str, str] | None = None,
    declared_pins: frozenset[str] | None = None,
    rename_declared_pins: bool = False,
    tie_declared_pins: bool = False,
    label_layers: Mapping[str, tuple[int, int] | None] | None = None,
    tie_nets: Sequence[tuple[str, str]] | None = None,
    device_models: Mapping[str, str] | None = None,
    critical_nets: Sequence[str] | None = None,
    critical_net_patterns: Sequence[str] | None = None,
    critical_net_all: bool = False,
    critical_net_excludes: Sequence[str] | None = None,
    distributed_rc: bool = False,
    resistance_graph: bool = False,
    resistance_graph_r_cut_ohm: float | None = None,
    resistance_network: bool = False,
    resistance_graph_caps: Mapping[str, int] | None = None,
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
    schematic_top: str | None = None,
    device_only_leg: bool = False,
    extract_only: bool = False,
    submit_only: bool = False,
    attach: str | None = None,
    submit_request: Mapping[str, Any] | None = None,
    runset: pex_runset.PexRunset | None = None,
    _ownership: contextlib.ExitStack | None = None,
) -> dict[str, Any]:
    """Extract ``layout_path`` (with lumped-RC parasitics) and re-run every
    testbench in ``testbench_paths`` against both the schematic DUT it
    already references and the newly-extracted one, producing a per-corner,
    per-spec-row delta report.

    **Multi-top mode (issue #5494).** ``top`` is one cell name (the original
    contract, unchanged) or a *sequence* of them (``klt pex --top A --top
    B``), each then extracted on its own into the one concatenated ``output``
    netlist. Contract: :mod:`klayout_tools.pex_multi_top`'s docstring.

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
    before this feature existed.

    ``resistance_graph`` selects the extracted graph resistance or legacy
    fallback per net. Its report echoes terminals, pair resistances, fallback
    reasons and the optional per-cut resistance. Explicit caps are echoed
    with all four effective limits. ``resistance_network`` requires the graph
    and writes a Kron-reduced network for each solved net with three or more
    terminals. The graph is incompatible with the distributed RC ladder.

    ``critical_net_patterns``/``critical_net_all``/``critical_net_excludes`` (``klt pex
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
    rules. All four default to ``None``, which disables substitution.
    Each testbench simulation receives ``backend`` and the optional per-corner
    ``timeout_s`` override. See ``docs/cli/sim.md``, "Execution backends".
    Each side shards its own grid with ``max_units_per_job``, ``max_concurrent_jobs``,
    ``hosts``, ``resume`` and ``capacity_wait_s``, and attaches using its own
    ``batch-shards.json``. Omitted overrides preserve the request's batch settings.
    ``artifacts_dir`` defaults to ``.klt/pex/`` beside ``layout_path``. It holds
    the generated testbench/request copies (see :func:`_prepare_extracted_request`)
    and each simulation's ``options.keep_artifacts`` output under
    ``<artifacts_dir>/<testbench label>/schematic|extracted/``.

    The schematic leg passes the original testbench file to `run_sim`. The
    extracted leg redirects its DUT include to the extracted netlist and sets
    `netlist_source: "extracted"` (see :func:`_prepare_extracted_request`).

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
    ``klt extract``'s curated-engine output is flat while a schematic DUT
    is normally hierarchical (a ``--engine klayout-runset`` extraction may
    not be), which makes ``model_mismatch`` fire on hierarchy alone and can
    leave the two legs incomparable. With ``flatten_schematic``
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
    ``measure_timeout_s``, ``device_only_leg`` or ``flatten_schematic``
    raises :class:`PexError`.

    **``extract_only`` with ``reference_netlist`` (issue #5477)** is the one
    exception to that list. ``model_mismatch`` compares what the two
    netlists *contain* -- the per-model ``M``/``X``/``Q``/``D`` instance
    multiset (:func:`_model_mismatch`) -- so it is well-defined with no
    measurement leg at all, and it is the only deck-agnostic signal that an
    extraction is missing a whole device family: a curated deck that
    declares no class for a family whose recognition region the PDK
    *derives* (rather than drawing a marker layer for) leaves
    ``extraction.unsupported_devices`` empty and the devices simply absent
    from the netlist. Supplying the schematic netlist makes that divergence
    nameable -- ``kind: "model_set"``, the missing model in
    ``reference_only`` -- in an envelope whose ``delta[]`` is not yet
    runnable. ``reference_netlist``/``reference_netlist_sha256`` then report
    the caller's file; ``measurement.mode`` stays ``"extract-only"``,
    ``delta`` stays ``[]``, ``status`` stays ``"not_checked"``, and
    ``flat_dut_mismatch``/``pin_count_mismatch``/``pin_order_mismatch`` stay
    ``null`` -- all three describe a measurement leg's testbench
    ``X...``-line reuse, which this mode does not do. Without a
    ``reference_netlist`` the envelope is byte-identical to before.

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

    **``runset`` (issues #6559/#6606)** swaps the curated-deck extraction for
    ``klt extract --engine klayout-runset``: no parasitics when ``deck_name`` is
    ``None``, else that curated deck's parasitics attached by net correspondence;
    ``extraction.engine``/``parasitics_carried`` say which (see :mod:`.pex_runset`).
    ``None`` -- the curated route -- leaves the envelope byte-identical.

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
    # Issue #4619: refuse unrepresentable async combinations before any I/O.
    pex_async.validate_async_flags(
        submit_only=submit_only,
        attach=attach,
        backend=backend,
        resume=resume,
        measure_command=measure_command,
        extract_only=extract_only,
        device_only_leg=device_only_leg,
    )
    pex_async.validate_capacity_wait(capacity_wait_s, backend)
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
        timeout_s=timeout_s,
        extract_only=extract_only,
        flatten_schematic=flatten_schematic,
        schematic_top=schematic_top,
        device_only_leg=device_only_leg,
    )
    command_mode = command is not None
    _reject_resistance_graph_with_distributed_rc(resistance_graph, distributed_rc)
    # Issue #5494: two or more `--top` values put this run in multi-top mode
    # (`pex_multi_top`), refusing up front what that mode does not define.
    tops = resolve_tops(
        top,
        flatten_schematic=flatten_schematic,
        device_only_leg=device_only_leg,
        tie_nets=tie_nets,
        tie_declared_pins=tie_declared_pins,
        mom_rlc_net=mom_rlc_net,
        measure_command=measure_command,
        extract_only=extract_only,
        submit_only=submit_only,
        attach=attach,
        resume=resume,
        schematic_top=schematic_top,
        runset_engine=runset is not None,
    )
    multi_top = len(tops) > 1
    _reject_resistance_network_without_graph(resistance_network, resistance_graph)
    pex_runset.refuse_unsupported(
        runset,
        device_only_leg=device_only_leg,
        tie_nets=tie_nets,
        resistance_network=resistance_network,
    )

    # Issue #1261: every input-path field this run's own JSON response
    # echoes (`layout`/`netlist`/`reference_netlist`, each `testbenches[]`
    # entry's `request`/`schematic_netlist`) is normalised against this one
    # repo root, resolved once from `layout_path`'s own location -- the same
    # "walk up from the input" default `env_provenance.find_repo_root` uses
    # for its own caller.
    repo_root = env_provenance.find_repo_root(
        os.path.dirname(os.path.abspath(layout_path))
    )

    # Validate side selectors on every testbench before running extraction.
    requests = [_load_testbench_request(path, timeout_s) for path in testbench_paths]
    probe_names = [pex_sim_evidence.schematic_probes(request) for request in requests]

    # Issue #7818: the 24 extraction options every helper downstream of this
    # entry point reads, bundled once -- the flat keyword signature above is
    # the public contract and stays unchanged.
    options = pex_runset.PexExtractOptions(
        top=tops[0] if tops else None,
        pdk_variant=pdk_variant,
        pdk_root=pdk_root,
        deck_options=deck_options,
        declared_pins=declared_pins,
        rename_declared_pins=rename_declared_pins,
        tie_declared_pins=tie_declared_pins,
        label_layers=label_layers,
        device_models=device_models,
        critical_nets=critical_nets,
        critical_net_patterns=critical_net_patterns,
        critical_net_all=critical_net_all,
        critical_net_excludes=critical_net_excludes,
        distributed_rc=distributed_rc,
        resistance_graph=resistance_graph,
        resistance_graph_r_cut_ohm=resistance_graph_r_cut_ohm,
        resistance_network=resistance_network,
        resistance_graph_caps=resistance_graph_caps,
        parasitic_variants=parasitic_variants,
        substrate_tie_ohms=substrate_tie_ohms,
        mom_rlc_net=mom_rlc_net,
        mom_rlc_resistance_ohm=mom_rlc_resistance_ohm,
        mom_rlc_capacitance_ff=mom_rlc_capacitance_ff,
        mom_rlc_inductance_nh=mom_rlc_inductance_nh,
    )
    extract_report, per_top_reports = extract_every_top(
        layout_path,
        deck_name,
        tops,
        output=output,
        artifacts_dir=artifacts_dir,
        runset=runset,
        options=options,
    )

    # Use an absolute DUT include: each swapped bench runs in a corner workdir.
    # The report's scoped paths are produced separately from these engine paths.
    extracted_netlist_path = os.path.abspath(extract_report["netlist_path"])

    device_only_netlist_path, device_only_parity = _extract_device_only_leg(
        device_only_leg,
        extracted_netlist_path,
        layout_path,
        deck_name,
        options=options,
    )

    # Issue #4245: `--tie-net` merges reported-unbiased body nets into
    # schematic nets in the netlist every simulated leg below reads. Checked
    # against the extraction first, applied before any simulation; `None`
    # leaves the netlist (and the whole report) untouched.
    ext_path, dev_path = extracted_netlist_path, device_only_netlist_path
    _tie_nets_in_legs(tie_nets, extract_report, ext_path, dev_path)
    # The tie rewrites `extracted_netlist_path` in place, so the extract-time
    # hash no longer identifies the file the report points at: re-hash it.
    extracted_netlist_sha256 = (
        sha256_file(extracted_netlist_path)
        if tie_nets
        else extract_report["netlist_sha256"]
    )

    work_dir = _pex_work_dir(artifacts_dir, layout_path)
    os.makedirs(work_dir, exist_ok=True)
    leg_extracted, leg_device_only = adapt_leg_netlists(
        deck_name, ext_path, dev_path, work_dir, skip=extract_only or command_mode
    )
    # Issue #4619: routes each `run_sim` leg (blocking / submit / attach).
    legs = pex_async.AsyncLegs(
        submit_only=submit_only,
        attach=attach,
        work_dir=work_dir,
        request=submit_request,
    )

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

    # Command and extraction-only modes can supply a schematic DUT directly.
    measured_reference_netlist = (
        _resolve_measured_reference(reference_netlist)
        if reference_netlist is not None
        else None
    )

    # Check every DUT include before simulation. A missing include or testbenches
    # pointing at different DUTs must fail before launching any measurement leg.
    for index, testbench_path in enumerate(testbench_paths):
        label = f"{index:02d}-{os.path.splitext(os.path.basename(testbench_path))[0]}"
        labels.append(label)

        request = requests[index]
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
                "request_sha256": content_hash(testbench_path),
                "netlist": schematic_body_path,
                "netlist_sha256": content_hash(schematic_body_path),
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
        input_identity[-1]["schematic_netlist_sha256"] = content_hash(dut_path)

    _require_single_dut(dut_paths)
    # Issue #4619: an `--attach` manifest must match this run's inputs.
    request_shas = {
        label: identity["request_sha256"]
        for label, identity in zip(labels, input_identity, strict=True)
    }
    legs.verify(
        extracted_netlist_sha256=extracted_netlist_sha256,
        request_sha256=request_shas,
        schematic_top=schematic_top,
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
    reference_netlist_sha256 = content_hash(reference_netlist_path)

    # Issue #2889: `--flatten-schematic` -- the netlist the schematic leg
    # actually runs (and every diagnostic below compares) is a flattened
    # copy of the DUT; `reference_netlist` keeps naming the caller's file.
    bound_bodies = _bound_testbenches(
        schematic_top,
        extract_report["top"],
        reference_netlist_path,
        [item["netlist"] for item in input_identity],
        extracted_netlist_path,
    )
    schematic_flatten: dict[str, Any] | None = None
    schematic_netlist_path = reference_netlist_path
    if flatten_schematic:
        schematic_netlist_path, schematic_flatten = _flatten_schematic_dut(
            reference_netlist_path,
            work_dir=work_dir,
            top=schematic_top or extract_report["top"],
            repo_root=repo_root,
            wrap_pins=(
                _subckt_interfaces(_read_text(extracted_netlist_path) or "").get(
                    extract_report["top"].lower(), (None, None)
                )[1]
            ),
        )

    per_top_preflight = _per_top_preflight(
        tops,
        schematic_netlist=schematic_netlist_path,
        reference_netlist=reference_netlist_path,
        extracted_netlist=extracted_netlist_path,
    )

    flat_dut_mismatch = (
        _first_firing(per_top_preflight, "flat_dut_mismatch")
        if multi_top
        else None
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
    # Issue #5477: computed in `--extract-only` mode too whenever the caller
    # supplied a `--reference-netlist` -- comparing the two netlists' text
    # needs no measurement leg at all, and there it is reported, not enforced
    # (`status` stays `not_checked`, the exit code stays 4). `run_pex` states
    # why once.
    model_mismatch = (
        None
        if not schematic_netlist_path
        else _model_mismatch(
            reference_netlist=schematic_netlist_path,
            extracted_netlist=extracted_netlist_path,
            extracted_top=extract_report["top"],
        )
    )

    testbenches_summary: list[dict[str, Any]] = []
    delta: list[dict[str, Any]] = []
    # Compare the two selected headers once for every testbench. Older
    # ngspice versions can silently simulate a mismatched call with dangling
    # terminals. When static comparison is inconclusive, the extracted
    # engine's own pin-count error is still handled below (#1030, #1041).
    pin_count_mismatch: dict[str, Any] | None = None
    # Issue #2990: the first extracted-side `SimError` that is not a pin-count
    # mismatch (e.g. a batch-fleet capacity refusal), `None` otherwise.
    backend_failure: dict[str, Any] | None = None
    # Issue #5282: legs that lost shards (partial result, resumable).
    unfinished: list[dict[str, Any]] = []
    if multi_top:
        pin_count_mismatch = _first_firing(per_top_preflight, "pin_count_mismatch")
    elif not command_mode and not extract_only and flat_dut_mismatch is None:
        pin_count_mismatch = _pin_count_mismatch(
            reference_netlist=schematic_netlist_path,
            extracted_netlist=extracted_netlist_path,
            subcircuit=extract_report["top"],
            schematic_top=schematic_top,
        )

    # Issue #2890: same pins in a different order. Pre-flight like the two
    # diagnostics above, mutually exclusive with both (counts must be equal,
    # and the schematic must declare the subcircuit), and `None` in
    # `--measure-command` mode for the same reason. It skips the extracted
    # leg and yields `status: "error"` / exit 4, like its siblings.
    pin_order_mismatch = (
        _first_firing(per_top_preflight, "pin_order_mismatch")
        if multi_top
        else _pin_order_preflight(
            command_mode,
            (flat_dut_mismatch, pin_count_mismatch),
            extract_only=extract_only,
            reference_netlist=reference_netlist_path,
            extracted_netlist=extracted_netlist_path,
            top_cell=extract_report["top"],
            schematic_top=schematic_top,
        )
    )

    # Issue #4696: a static pre-flight diagnostic skips the extracted leg, so
    # a --submit-only run would record only the schematic leg. Refuse before
    # any leg is submitted rather than pay for shards that can never be graded.
    _refuse_submit_on_preflight_mismatch(
        submitting=legs.submitting,
        flat_dut_mismatch=flat_dut_mismatch,
        pin_count_mismatch=pin_count_mismatch,
        pin_order_mismatch=pin_order_mismatch,
    )

    # `--measure-command` mode (issue #2478): `testbench_paths` is empty (it
    # is validated so above), so the per-testbench loop below is a no-op and
    # the two legs are measured here instead -- once per side, by the
    # caller's own command, against the two netlists *this* command chose.
    # The diff itself is still computed here, by the same
    # `_build_delta_rows` testbench mode uses.
    measurement_sides: dict[str, Any] | None = None
    # Issue #4616: the sharding flags ride to every `run_sim` call. Only the
    # given ones are passed, so omitting both is the call shape of before.
    batch_shard = pex_async.batch_shard_flags(
        max_units_per_job=max_units_per_job,
        max_concurrent_jobs=max_concurrent_jobs,
        hosts=hosts,
        resume=resume,
        capacity_wait_s=capacity_wait_s,
        timeout_s=timeout_s,
    )
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
            schematic_report = legs.run(
                run_sim,
                schematic_request_path,
                testbench=label,
                side="schematic",
                artifacts_dir=os.path.join(work_dir, label, "schematic"),
                backend=backend,
                **batch_shard,
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
                    extracted_netlist_path=leg_extracted,
                    bound_body=bound_bodies[index],
                    work_dir=work_dir,
                    label=label,
                )
                extracted_report = legs.run(
                    run_sim,
                    extracted_request_path,
                    testbench=label,
                    side="extracted",
                    artifacts_dir=os.path.join(work_dir, label, "extracted"),
                    backend=backend,
                    **batch_shard,
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

        if legs.submitting:
            # Issue #4619: submitted, not run -- nothing to grade yet.
            continue

        unfinished += pex_sim_evidence.unfinished_legs(
            label, schematic_report, extracted_report
        )

        stem = os.path.splitext(os.path.basename(testbench_path))[0]
        spec_row_prefix = None if single_testbench else stem
        probes = probe_names[index]
        delta.extend(
            pex_sim_evidence.schematic_probe_rows(
                probes,
                schematic_report,
                extracted_report,
                testbench=stem,
                prefix=spec_row_prefix,
                device_only_leg=device_only_leg,
            )
        )
        if extracted_report is None:
            if extracted_failure is None:
                extracted_failure = _static_extracted_failure(
                    pin_count_mismatch=pin_count_mismatch,
                    flat_dut_mismatch=flat_dut_mismatch,
                    pin_order_mismatch=pin_order_mismatch,
                )
            delta.extend(
                _unextracted_delta_rows(
                    testbench=stem,
                    spec_row_prefix=spec_row_prefix,
                    schematic_report=schematic_report,
                    failure_message=extracted_failure[0] if extracted_failure else None,
                    failure_code=extracted_failure[1] if extracted_failure else None,
                    schematic_probes=probes,
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
                    **pex_sim_evidence.project_testbench_sides(schematic_report, None),
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
            leg_device_only,
            testbench_path=testbench_path,
            work_dir=work_dir,
            label=label,
            bound_body=bound_bodies[index],
            backend=backend,
            batch_shard=batch_shard,
        )

        rows = _build_delta_rows(
            testbench=stem,
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
                    m["name"]
                    for m in (schematic_report if probes else extracted_report)[
                        "measurements"
                    ]
                ],
                **pex_sim_evidence.project_testbench_sides(
                    schematic_report, extracted_report
                ),
            }
        )

    if legs.submitting:
        return legs.finish_submit(
            extracted_netlist_sha256=extracted_netlist_sha256,
            request_sha256=request_shas,
        )

    # Issue #4721: every leg collected -- an attach's shard maps are spent.
    legs.remove_collected_shard_maps()

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

    result: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "status": status,
        **({"simulation_timeout_s": timeout_s} if timeout_s is not None else {}),
        "layout": env_provenance.repo_relative_path(
            layout_path or None, repo_root=repo_root
        ),
        "netlist": env_provenance.repo_relative_path(
            extracted_netlist_path or None, repo_root=repo_root
        ),
        "reference_netlist": env_provenance.repo_relative_path(
            reference_netlist_path or None, repo_root=repo_root
        ),
        # Additive field (issue #2875): content identity of the schematic
        # DUT / `--reference-netlist` the schematic leg used.
        "reference_netlist_sha256": reference_netlist_sha256,
        "extraction": _extraction_block(
            extract_report,
            deck_name=deck_name,
            netlist_sha256=extracted_netlist_sha256,
            resistance_graph=resistance_graph,
            resistance_graph_r_cut_ohm=resistance_graph_r_cut_ohm,
            resistance_graph_caps=resistance_graph_caps,
        ),
        # Additive field (issue #5494): `None` on a zero/one-`--top` run;
        # otherwise one block per `--top`, where per-top attribution lives.
        # See `pex_multi_top._tops_block`.
        "tops": _tops_block(
            per_top_reports,
            per_top_preflight,
            multi_top=multi_top,
            deck_name=deck_name,
            resistance_graph=resistance_graph,
            resistance_graph_r_cut_ohm=resistance_graph_r_cut_ohm,
            resistance_graph_caps=resistance_graph_caps,
            repo_root=repo_root,
        ),
        # Additive field (issue #1983): whether the extracted netlist this
        # run re-simulated has a DC bias path for every device body, reduced
        # from the extraction's own `unbiased_pmos_body_nets[]` (issue #555).
        # Reported beside `status`, never folded into it -- see
        # `_body_bias_report` for why an item-7 citation needs this in the
        # artifact it cites, and why this command does not grade on it.
        "body_bias": _body_bias_report(extract_report, tie_nets),
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
        # Additive field (issue #5282): sim legs that lost shards. `[]` when
        # every leg finished; else `{testbench, side, corner_ids,
        # unfinished_shards, resumable, shard_map}` per leg. Finished corners
        # keep their ordinary `delta[]` rows; re-run with `--resume`.
        "unfinished": unfinished,
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
    result |= _device_only_result_fields(
        device_only_netlist_path, device_only_parity, repo_root=repo_root
    )
    return with_fidelity_warnings(result, deck_name)
