"""``klt pex --dry-run`` (issue #5464): validate a committed post-layout
invocation and print its expansion, without extracting or simulating.

``klt sim --dry-run`` (issue #3064) gives a committed ``klt sim`` request a
cheap cold-start check. ``klt pex`` had no equivalent, so a committed
post-layout invocation could only be checked by paying for a full extraction
plus both simulation legs -- and the obvious workaround (``klt sim <pex
testbench> --dry-run``) checks the measurement request alone, skipping
everything ``klt pex`` itself adds.

This module runs, in :func:`klayout_tools.pex.run_pex`'s own order,
every check that needs no extraction engine:

1. the pre-extraction flag refusals (async flags, capacity wait, measure
   mode, ``--resistance-graph`` with ``--distributed-rc``) -- minus the
   output-directory lock, which a dry run must not take;
2. the extraction preflight (PDK/model bindings, the parasitics deck,
   ``validate_run_extract_args``, then
   :func:`klayout_tools.extract_preflight.preflight_layout`'s file checks,
   deck resolution, layout read, ``--top`` resolution and option refusals);
3. the per-testbench DUT resolution that in a real run only happens *after*
   extraction has already been paid for (request load, body netlist, the
   single ``.include``, "does the target declare a circuit", one DUT for the
   whole run) -- or, in ``--measure-command`` mode, the same check on
   ``--reference-netlist``;
4. one nested ``klt sim --dry-run`` per testbench.

It reuses the helpers those steps already live in rather than copying them,
so an error here is the error the real run gives for the same input. What it
cannot see is named in the report's ``not_checked[]`` rather than implied to
have passed.

Nothing is written: no netlist at ``-o``, no ``.klt/pex/`` or ``--outdir``
tree, no ``--envelope-out`` file from this module, and the output-directory
lock is never taken.
"""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from typing import Any

from . import env_provenance, pex_async
from ._paths import _resolve_relative
from .decks import UnknownExtractionDeckError, get_parasitics_deck
from .extract import ExtractError
from .extract_pdk_resolve import resolve_pdk_and_model_bindings
from .extract_preflight import preflight_layout, require_declared_set_for_rename
from .extract_validate import validate_run_extract_args
from .pex import (
    SCHEMA_VERSION,
    PexError,
    _check_dut_declares_a_circuit,
    _find_dut_include,
    _load_testbench_request,
    _reject_resistance_graph_with_distributed_rc,
    _require_single_dut,
    _resolve_measured_reference,
    _resolve_reference_netlist_path,
    _validate_measure_mode,
)
from .pex_multi_top_flags import resolve_tops
from .sim import SimError, run_sim

#: Codes a dry run always reports in ``not_checked[]`` -- what no amount of
#: request validation can see without the extracted netlist. Documented in
#: ``docs/cli/pex.md``.
ALWAYS_NOT_CHECKED = (
    # Anything needing the extracted netlist: net-name selectors
    # (--critical-net, --mom-rlc-net) resolving to real nets, and whether the
    # label roles a deck reads are actually present in the layout.
    "extraction",
    # pin_count_mismatch / pin_order_mismatch / flat_dut_mismatch /
    # model_mismatch -- all compare the extracted netlist's own text.
    "netlist_diagnostics",
    # The extracted-side `klt sim` staging, which runs against a DUT-swapped
    # request pointing at a netlist that does not exist yet.
    "extracted_leg_preflight",
)


def _not_checked(
    *,
    tie_nets: Sequence[tuple[str, str]] | None,
    command: Sequence[str] | None,
    flatten_schematic: bool,
) -> list[str]:
    """``not_checked[]``: the always-on codes plus one per flag whose effect
    only exists downstream of the extraction this run did not do."""
    codes = list(ALWAYS_NOT_CHECKED)
    if tie_nets:
        # --tie-net names a net of *this extraction's* body-bias report.
        codes.append("tie_nets")
    if command is not None:
        # --measure-command is deliberately not executed by a dry run.
        codes.append("measure_command")
    if flatten_schematic:
        # The flattener runs against the resolved schematic DUT at run time.
        codes.append("flatten_schematic")
    return codes


def _extraction_preflight(
    layout_path: str,
    deck_name: str,
    *,
    top: str | None,
    pdk_variant: str | None,
    pdk_root: str | None,
    deck_options: Mapping[str, str] | None,
    declared_pins: frozenset[str] | None,
    rename_declared_pins: bool,
    tie_declared_pins: bool,
    label_layers: Mapping[str, tuple[int, int] | None] | None,
    device_models: Mapping[str, str] | None,
    critical_nets: Sequence[str] | None,
    critical_net_patterns: Sequence[str] | None,
    critical_net_all: bool,
    critical_net_excludes: Sequence[str] | None,
    distributed_rc: bool,
    resistance_graph: bool,
    resistance_graph_r_cut_ohm: float | None,
    parasitic_variants: Sequence[str] | None,
    substrate_tie_ohms: float | None,
    mom_rlc_net: str | None,
    mom_rlc_resistance_ohm: float | None,
    mom_rlc_capacitance_ff: float | None,
    mom_rlc_inductance_nh: float | None,
) -> str:
    """Every engine-free check ``run_extract`` makes, in its own order, for
    the ``parasitics=True`` call ``klt pex`` always issues. Returns the
    resolved top-cell name.

    :class:`~klayout_tools.extract.ExtractError` is re-raised as the same
    ``extraction failed: ...`` :class:`~klayout_tools.pex.PexError`
    :func:`~klayout_tools.pex.run_pex` wraps its own ``run_extract`` call in,
    so a dry run's message is the real run's message.
    """
    try:
        pdk_info, _model_bindings = resolve_pdk_and_model_bindings(
            deck_name=deck_name,
            deck_options=deck_options,
            pdk_variant=pdk_variant,
            pdk_root=pdk_root,
            device_models=device_models,
        )
        try:
            get_parasitics_deck(deck_name)
        except UnknownExtractionDeckError as exc:
            raise ExtractError(str(exc)) from exc

        _tie, critical_nets_set, parasitics_nets_set, _variants, net_selectors = (
            validate_run_extract_args(
                mom_net=None,
                spef_output=None,
                substrate_spreading_net=None,
                substrate_tie_ohms=substrate_tie_ohms,
                critical_nets=critical_nets,
                parasitics_nets=None,
                parasitic_variants=parasitic_variants,
                parasitics_top_cell_only=False,
                resistance_graph=resistance_graph,
                resistance_graph_r_cut_ohm=resistance_graph_r_cut_ohm,
                emit_selected_resistance=resistance_graph,
                critical_net_patterns=critical_net_patterns,
                critical_net_all=critical_net_all,
                critical_net_excludes=critical_net_excludes,
                parasitics_net_patterns=None,
                strict_net_patterns=False,
                distributed_rc=distributed_rc,
                resistance_paths=False,
                resistance_path_max_nodes=None,
                resistance_path_max_resistors=None,
                mom_rlc_net=mom_rlc_net,
                mom_rlc_resistance_ohm=mom_rlc_resistance_ohm,
                mom_rlc_capacitance_ff=mom_rlc_capacitance_ff,
                mom_rlc_inductance_nh=mom_rlc_inductance_nh,
                matched_device_groups=None,
                parasitics=True,
            )
        )

        preflight = preflight_layout(
            layout_path,
            deck_name,
            top=top,
            deck_options=deck_options,
            label_layers=label_layers,
            refuse_options={
                "--top-cell-pins": False,
                "--abstract-cells": False,
                "--abstract-cell-lef": False,
                "--mom-net": False,
                "--probe": False,
                "--critical-net": bool(critical_nets_set)
                or (net_selectors is not None and net_selectors.has_critical),
                "--parasitics-net": bool(parasitics_nets_set)
                or (
                    net_selectors is not None
                    and bool(net_selectors.parasitics_patterns)
                ),
                "--def-pins": False,
                "--pin-source-cells": False,
                "--rename-declared-pins": rename_declared_pins,
                "--tie-declared-pins": tie_declared_pins,
                "--subcircuit": False,
                "--hierarchical-cells": False,
                "--substrate-spreading": False,
                "--pdk": pdk_info is not None,
            },
        )
        require_declared_set_for_rename(rename_declared_pins, declared_pins, None)
        require_declared_set_for_rename(
            tie_declared_pins, declared_pins, None, flag="--tie-declared-pins"
        )
    except ExtractError as exc:
        raise PexError(f"extraction failed: {exc}") from exc
    return preflight.top_cell.name


def _resolve_testbenches(
    testbench_paths: Sequence[str],
) -> tuple[list[str], list[str]]:
    """``run_pex``'s own first pass, which in a real run only happens once
    extraction has been paid for: ``(body_paths, dut_paths)``."""
    body_paths: list[str] = []
    dut_paths: list[str] = []
    for testbench_path in testbench_paths:
        request = _load_testbench_request(testbench_path)
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
        _check_dut_declares_a_circuit(testbench_path, dut_path)
        body_paths.append(schematic_body_path)
        dut_paths.append(dut_path)
    _require_single_dut(dut_paths)
    return body_paths, dut_paths


def _resolved_deck_options(
    deck_name: str, deck_options: Mapping[str, str] | None
) -> dict[str, str]:
    """The fully-resolved deck-option mapping this invocation would extract
    with -- the same one ``provenance.deck.options`` records."""
    from .decks import resolve_deck_option_values

    try:
        return dict(resolve_deck_option_values(deck_name, deck_options))
    except Exception:  # pragma: no cover -- the deck resolved moments ago
        return dict(deck_options or {})


def run_pex_dry_run(
    layout_path: str,
    testbench_paths: Sequence[str],
    deck_name: str,
    *,
    top: str | Sequence[str] | None = None,
    pdk_variant: str | None = None,
    pdk_root: str | None = None,
    backend: str | None = None,
    max_units_per_job: int | None = None,
    max_concurrent_jobs: int | None = None,
    hosts: int | None = None,
    resume: bool | None = None,
    capacity_wait_s: float | None = None,
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
    submit_only: bool = False,
    attach: str | None = None,
) -> dict[str, Any]:
    """Validate ``layout_path`` / ``testbench_paths`` / ``deck_name`` and
    report the expansion, extracting and simulating nothing.

    Returns the ``dry_run`` report described in ``docs/cli/pex.md``. Raises
    :class:`~klayout_tools.pex.PexError` (exit 1 through
    ``cli/pex_cmd.py``'s normal error envelope) on any validation failure --
    with the message the non-dry invocation gives for the same input,
    because the same helpers raise it.
    """
    # `klt sim --dry-run` refuses the same pair (`sim.py`'s `dry_run and
    # (submit_only or attach_job_id is not None)`): both are about dispatching
    # or collecting real jobs, which is exactly what a dry run does not do.
    if submit_only or attach is not None:
        flag = "--submit-only" if submit_only else "--attach"
        raise PexError(
            f"--dry-run cannot be combined with {flag}: a dry run neither "
            "submits nor collects jobs (the same pair `klt sim --dry-run` "
            "refuses)"
        )

    # The same pre-extraction refusals `run_pex` makes, in its own order --
    # minus `_own_pex_root`, which would take the output-directory lock.
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
    command = _validate_measure_mode(
        measure_command,
        testbench_paths,
        reference_netlist,
        measure_timeout_s,
        extract_only=extract_only,
        flatten_schematic=flatten_schematic,
        device_only_leg=device_only_leg,
    )
    _reject_resistance_graph_with_distributed_rc(resistance_graph, distributed_rc)
    # Issue #5494: `klt pex --top` is repeatable, so this entry point now
    # receives a list too. A dry run is refused with two or more tops rather
    # than silently validating only one of them: its own report is singular
    # (`top`, one resolved cell name) and its `_extraction_preflight` resolves
    # one top cell, so validating a multi-top request properly means a second
    # additive change to *this* command's contract -- out of scope for the
    # issue that made `--top` repeatable, and tracked in #5511. A
    # zero-or-one-`--top` dry run is byte-identical to before.
    tops = resolve_tops(top, dry_run=True)

    repo_root = env_provenance.find_repo_root(
        os.path.dirname(os.path.abspath(layout_path))
    )

    resolved_top = _extraction_preflight(
        layout_path,
        deck_name,
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
        parasitic_variants=parasitic_variants,
        substrate_tie_ohms=substrate_tie_ohms,
        mom_rlc_net=mom_rlc_net,
        mom_rlc_resistance_ohm=mom_rlc_resistance_ohm,
        mom_rlc_capacitance_ff=mom_rlc_capacitance_ff,
        mom_rlc_inductance_nh=mom_rlc_inductance_nh,
    )

    _body_paths, dut_paths = _resolve_testbenches(testbench_paths)
    measured_reference_netlist = (
        _resolve_measured_reference(reference_netlist)
        if command is not None and reference_netlist is not None
        else None
    )
    reference_netlist_path = _resolve_reference_netlist_path(
        extract_only, measured_reference_netlist, dut_paths
    )

    # Issue #4616/#4624/#5280: the shard flags the schematic leg would pass,
    # so the nested dry run expands exactly the unit list a real leg would.
    batch_shard = pex_async.batch_shard_flags(
        max_units_per_job=max_units_per_job,
        max_concurrent_jobs=max_concurrent_jobs,
        hosts=hosts,
        resume=resume,
        capacity_wait_s=capacity_wait_s,
    )

    testbenches: list[dict[str, Any]] = []
    for index, testbench_path in enumerate(testbench_paths):
        try:
            sim_report = run_sim(
                testbench_path, dry_run=True, backend=backend, **batch_shard
            )
        except SimError as exc:
            raise PexError(
                f"testbench '{testbench_path}': schematic-side simulation "
                f"failed: {exc}",
                code=exc.code,
            ) from exc
        testbenches.append(
            {
                "request": env_provenance.repo_relative_path(
                    testbench_path or None, repo_root=repo_root
                ),
                "schematic_netlist": env_provenance.repo_relative_path(
                    dut_paths[index] or None, repo_root=repo_root
                ),
                # The nested `klt sim --dry-run` report, unmodified.
                "sim": sim_report,
            }
        )

    return {
        # The same envelope every `klt pex` report rides, at the same
        # version: every field below is additive (docs/json-contract.md).
        "schema_version": SCHEMA_VERSION,
        "dry_run": True,
        "status": "valid",
        "layout": env_provenance.repo_relative_path(
            layout_path or None, repo_root=repo_root
        ),
        "deck": deck_name,
        "top": resolved_top,
        "deck_options": _resolved_deck_options(deck_name, deck_options),
        "declared_pins": sorted(declared_pins) if declared_pins else None,
        # `--extract-only` measures nothing at all, so it plans no leg.
        "legs": [] if extract_only else _legs(device_only_leg),
        "reference_netlist": env_provenance.repo_relative_path(
            reference_netlist_path or None, repo_root=repo_root
        ),
        "testbenches": testbenches,
        "not_checked": _not_checked(
            tie_nets=tie_nets, command=command, flatten_schematic=flatten_schematic
        ),
    }


def _legs(device_only_leg: bool) -> list[str]:
    """The measurement legs this invocation would run. Every leg expands to
    the same unit list -- the extracted and device-only requests are deep
    copies of the testbench request with only the DUT netlist swapped (see
    ``pex._prepare_dut_swapped_request``)."""
    return ["schematic", "extracted"] + (["device-only"] if device_only_leg else [])
