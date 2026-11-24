"""Prepare a gate-level reference and retain its binding evidence for LVS."""

from __future__ import annotations

from typing import Any, NamedTuple

from . import lvs as _lvs


class _GateLevelPreparation(NamedTuple):
    """A gate-level Verilog reference converted *before* extraction
    (issue #7822).

    Everything here is derived from the request and the reference file
    alone -- no extracted netlist is needed -- so `run_lvs` resolves it
    before the (potentially multi-minute) layout extraction stage and
    hands it to :func:`_read_reference_stage`, which then reads the
    already-converted text instead of re-converting. An invalid reference
    therefore fails in seconds with the very same "could not convert
    gate-level-verilog reference netlist ..." diagnostic, instead of
    after the extraction has finished. The conversion metadata (pin-order
    resolution and its provenance, port aliases, case folds) travels with
    the converted text so the later stage records exactly what a
    non-prepared run would have; operations that need the extracted
    netlist (supply pairing, the power-only prune, the port-alias join)
    stay in the later stage.
    """

    pin_order_files: list[str]
    pin_orders: dict[str, list[str]]
    pdk_info: dict[str, Any] | None
    sources: list[dict[str, Any]]
    port_aliases: dict[str, dict[str, str]]
    case_folds: list[dict[str, str]]
    converted_text: str


def _prepare_reference_stage(
    reference_spec: dict[str, Any], request_dir: str, opts: Any
) -> _GateLevelPreparation | None:
    """Issue #7822: validate and convert a ``gate-level-verilog`` reference
    up front, before layout extraction.

    Returns ``None`` for every other reference form (their reads stay in
    :func:`_read_reference_stage`, unchanged). For the gate-level form this
    resolves the pin-order sources, converts the Verilog to plain-element
    SPICE and collects the conversion's metadata (port aliases, case folds)
    exactly as :func:`klayout_tools.lvs._read_reference_netlist` would --
    same steps, same diagnostics -- so a reference the converter rejects
    names the failing input immediately rather than after extraction.
    """
    if reference_spec.get("form", "plain-element") != "gate-level-verilog":
        return None
    reference_netlist_path = _lvs._require_path(
        reference_spec, "netlist", "reference", request_dir
    )
    pin_order_files = _lvs._resolve_reference_pin_order_files(
        reference_spec, request_dir
    )
    resolved_pin_orders = _lvs._resolve_gate_level_pin_orders(
        reference_spec.get("library"),
        reference_spec.get("pdk"),
        reference_spec.get("pdk_root"),
        pin_order_files=pin_order_files,
    )
    text = _read_reference_text(reference_netlist_path)
    port_aliases: dict[str, dict[str, str]] = {}
    case_folds: list[dict[str, str]] = []
    converted = _convert_reference_text(
        reference_netlist_path,
        text,
        resolved_pin_orders.pin_orders,
        opts.finfet_layout,
        case_folds,
        port_aliases,
    )
    return _GateLevelPreparation(
        pin_order_files=pin_order_files,
        pin_orders=resolved_pin_orders.pin_orders,
        pdk_info=resolved_pin_orders.pdk_info,
        sources=resolved_pin_orders.sources,
        port_aliases=port_aliases,
        case_folds=case_folds,
        converted_text=converted,
    )


def _convert_reference_text(path, text, pin_orders, finfet, case_folds, port_aliases):
    try:
        converted = _lvs._convert_gate_level_reference(
            text, pin_orders, finfet, case_folds
        )
    except _lvs.VerilogNetlistError as exc:
        raise _lvs.LvsError(
            f"could not convert gate-level-verilog reference netlist "
            f"'{path}' to plain-element form: {exc}"
        ) from exc
    if port_aliases is not None:
        port_aliases.update(_lvs.collect_gate_level_port_aliases(text))
    return converted


def _read_reference_text(path: str) -> str:
    """Preserve diagnostics for missing files and misdirected binary input."""
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            return handle.read()
    except OSError as exc:
        raise _lvs.LvsError(
            f"could not read reference netlist '{path}': {exc}"
        ) from exc
