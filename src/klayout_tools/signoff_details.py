"""Per-kind ``detail`` excerpt builders for :mod:`.signoff`'s checks
(issues #2839, #4859; split out for the #2766 per-file size ratchet)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from .signoff_power_delivery import _strap_layers


def _erc_detail(envelope: dict[str, Any]) -> dict[str, Any]:
    """:func:`_detail`'s ``"erc"`` branch (issue #2025), split out so adding
    a kind does not push :func:`_detail`'s own dispatch chain up by the size
    of the branch as well as by the branch itself."""
    rule_counts: dict[str, int] = {}
    for finding in envelope.get("erc_findings") or []:
        rule = finding.get("rule") if isinstance(finding, dict) else None
        if isinstance(rule, str):
            rule_counts[rule] = rule_counts.get(rule, 0) + 1
    return {
        "file": envelope.get("file"),
        "spec": envelope.get("spec"),
        "gate_role": envelope.get("gate_role"),
        "gate_count": envelope.get("gate_count"),
        "erc_finding_count": envelope.get("erc_finding_count"),
        # The connectivity half's own roll-up (issue #2179), beside the
        # antenna-driven `status` this check's `checks[].status` already
        # reports. Surfaced rather than merely consulted because it is what
        # makes an otherwise contradictory-looking entry legible: a check
        # with `status: "not_checked"` and `passed: true` is a run on a PDK
        # with no antenna-ratio table whose connectivity rules all passed
        # (:func:`_erc_passed`), and this field is the evidence for that.
        # `None` for a pre-#2179 envelope, which states no such verdict.
        "erc_status": envelope.get("erc_status"),
        # Per-rule breakdown, so "which of the five ERC rules fired" is
        # readable without re-opening the envelope -- the same reason `klt
        # drc`'s own `rule_counts` exists. Item 11 grades only three of these
        # rules (see :func:`_erc_supply_findings`), so a reader needs to see
        # which ones actually fired.
        "erc_rule_counts": dict(sorted(rule_counts.items())),
    }


def _place_and_route_detail(envelope: dict[str, Any]) -> dict[str, Any]:
    """:func:`_detail`'s ``"place-and-route"`` branch (issue #2025), split
    out for the same reason as :func:`_erc_detail`."""
    power = envelope.get("power") or {}
    straps = power.get("straps")
    return {
        "hdl_toplevel": envelope.get("hdl_toplevel"),
        "stage_reached": envelope.get("stage_reached"),
        "gds_path": envelope.get("gds_path"),
        "verilog_path": envelope.get("verilog_path"),
        # The `power` block is the only part of this response this module
        # reasons about -- T1 item 11's digital branch (issue #2025).
        # Surfaced verbatim so a "no PDN" verdict is readable beside the
        # artifact that caused it.
        "power_pdn": power.get("pdn"),
        "power_global_connect": power.get("global_connect"),
        "power_net": power.get("power_net"),
        "ground_net": power.get("ground_net"),
        "tapcell_master": power.get("tapcell_master"),
        "strap_layers": _strap_layers(envelope),
        "strap_count": len(straps) if isinstance(straps, list) else None,
    }


# --------------------------------------------------------------------------- #
# Provenance consistency (issue #251's provenance block; #309 AC #2)
# --------------------------------------------------------------------------- #


def _socket_check_detail(envelope: dict[str, Any]) -> dict[str, Any]:
    """:func:`_detail`'s ``"socket-check"`` excerpt (issue #2839)."""
    return {
        "socket": envelope.get("socket"),
        "socket_name": envelope.get("socket_name"),
        "failed_checks": [
            check.get("name")
            for check in envelope.get("checks") or []
            if isinstance(check, dict) and check.get("status") == "fail"
        ],
    }


def _seam_check_detail(envelope: dict[str, Any]) -> dict[str, Any]:
    """:func:`_detail`'s ``"seam-check"`` excerpt (issue #4189)."""
    violations = [v for v in envelope.get("violations") or [] if isinstance(v, dict)]
    return {
        "tiling": envelope.get("tiling"),
        "layer": envelope.get("layer"),
        "violation_count": len(violations),
        "violation_kinds": sorted({str(v.get("kind")) for v in violations}),
    }


def _synthesize_detail(envelope: dict[str, Any]) -> dict[str, Any]:
    """:func:`_detail`'s ``"synthesize"`` excerpt (issue #4015)."""
    structural = envelope.get("structural") or {}
    return {
        "netlist_path": envelope.get("netlist_path"),
        "netlist_sha256": envelope.get("netlist_sha256"),
        "structural": {
            key: structural.get(key)
            for key in (
                "unexpected_latches",
                "comb_loops",
                "multi_driven",
                "has_critical",
            )
        },
    }


def _lef_abstract_detail(envelope: dict[str, Any]) -> dict[str, Any]:
    """:func:`_detail`'s ``"lef-abstract"`` excerpt (issue #2839)."""
    return {
        "macro_name": envelope.get("macro_name"),
        "output": envelope.get("output"),
        "self_check_status": (envelope.get("self_check") or {}).get("status"),
        "power_pins_status": (envelope.get("power_pins") or {}).get("status"),
    }


def _model_run_detail(envelope: dict[str, Any]) -> dict[str, Any]:
    """:func:`_detail`'s ``"model-run"`` excerpt (issue #4859)."""
    return {
        "method": envelope.get("method"),
        "measurement_count": len(envelope.get("measurements") or []),
        "executed_by_klt": False,
    }


#: :func:`_detail`'s per-kind excerpt builders for the kinds that have one
#: of their own (issue #2839 moved ``erc``/``place-and-route`` here).
_DETAIL_BUILDERS: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
    "erc": _erc_detail,
    "place-and-route": _place_and_route_detail,
    "socket-check": _socket_check_detail,
    "seam-check": _seam_check_detail,
    "lef-abstract": _lef_abstract_detail,
    "model-run": _model_run_detail,
    "synthesize": _synthesize_detail,
}


# --------------------------------------------------------------------------- #
# Citation-summary lines for the ``--format text`` rendering (moved here from
# ``cli/signoff_cmd.py``, which sits at its file-size ratchet ceiling; the CLI
# imports them back by name, so every call site reads exactly as before).
# --------------------------------------------------------------------------- #

#: How many entries of each `coverage` list the text rendering names before
#: summarising the rest as `+N more`. The JSON output always carries every
#: entry -- this cap only keeps a terminal line readable for a deck with
#: dozens of rule-free layers.
_COVERAGE_PREVIEW = 4


def _format_coverage(coverage: dict) -> str:
    """One line summarising a `drc` citation's three disclosed `coverage`
    fields (issue #2002): each field's entry count, plus the first few
    entries by name.

    Counts first so a non-zero gap is visible without reading the names, and
    every field is always shown -- including a `0` -- because "this deck
    skipped no rules" is exactly the statement item 3 asks a claim to make,
    and it must not be indistinguishable from a field that went unreported
    (an envelope with no `coverage` block at all prints no coverage line at
    all; see the call site).
    """
    parts = []
    for field in (
        "layers_in_stream_without_rules",
        "rules_skipped",
        "deck_scope",
    ):
        entries = coverage.get(field) or []
        shown = ", ".join(str(entry) for entry in entries[:_COVERAGE_PREVIEW])
        if len(entries) > _COVERAGE_PREVIEW:
            shown += f", +{len(entries) - _COVERAGE_PREVIEW} more"
        suffix = f" ({shown})" if entries else ""
        parts.append(f"{field}={len(entries)}{suffix}")
    return ", ".join(parts)


def _format_body_bias(body_bias: dict) -> str:
    """One line summarising a `pex` citation's `body_bias` statement (issue
    #1983): the verdict, and -- when it is `"unbiased"` -- how many devices
    and which synthesized nets are involved.

    The verdict is always shown, including `"biased"`, for the same reason
    `_format_coverage` always shows a `0`: "every device body had a DC bias
    path" is the statement item 7's evidence is being asked to make, and it
    must not be indistinguishable from an artifact that never made it (an
    envelope with no `body_bias` block prints no line at all; see the call
    site).
    """
    status = body_bias.get("status") or "unknown"
    if status == "biased":
        return f"{status} (every device body has a DC bias path)"
    count = body_bias.get("unbiased_device_count") or 0
    nets = body_bias.get("unbiased_nets") or []
    shown = ", ".join(str(net) for net in nets[:_COVERAGE_PREVIEW])
    if len(nets) > _COVERAGE_PREVIEW:
        shown += f", +{len(nets) - _COVERAGE_PREVIEW} more"
    suffix = f" on {shown}" if nets else ""
    return (
        f"{status} ({count} device(s) with no DC bias path{suffix}) -- "
        "these post-layout numbers are not comparable to the schematic leg; "
        "see docs/cli/extract.md"
    )
