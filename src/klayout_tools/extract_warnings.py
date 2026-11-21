"""Prose ``warnings[]`` builders for ``klt extract`` (issue #5196).

Pure functions moved out of ``run_extract``: each takes already-detected
findings and returns the message lines to append to ``warnings[]``.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any


def merged_net_label_warnings(
    merged_net_labels: Sequence[dict[str, Any]],
) -> list[str]:
    """Prose ``warnings[]`` line per multi-label merged net (issue #470)."""
    warnings: list[str] = []
    for merged_entry in merged_net_labels:
        labels_str = ", ".join(merged_entry["labels"])
        warnings.append(
            f"net '{merged_entry['net']}' merges "
            f"{len(merged_entry['labels'])} distinct labels ({labels_str}) "
            "onto one net -- KLayout joins multiple labels found on the "
            "same electrical net (as '|' here and in the written netlist, "
            "matching NetlistSpiceWriter's own node-name escaping); this "
            "usually means two differently-named nets were shorted "
            "together in the layout -- see docs/cli/extract.md's "
            "'Merged net labels' section."
        )
    return warnings


def unbiased_pmos_body_warnings(unbiased_pmos_body_nets: Sequence[Any]) -> list[str]:
    """Aggregate warning for PMOS bodies on anonymous unbiased nets (issue #555)."""
    warnings: list[str] = []
    if unbiased_pmos_body_nets:
        device_word = "device" if len(unbiased_pmos_body_nets) == 1 else "devices"
        warnings.append(
            f"{len(unbiased_pmos_body_nets)} PMOS {device_word} tie their "
            "body to an anonymous net with no DC bias path -- no drawn (or "
            f"derivable) well tie connects this PMOS body to a real supply "
            "rail on this layout, so it is left floating rather than tied "
            "to a real supply rail; resimulating this netlist directly "
            "will not reproduce the schematic-level PMOS body bias -- see "
            "unbiased_pmos_body_nets[] for the full list. See "
            "docs/cli/extract.md's 'Parasitic (RC) extraction' section."
        )
    return warnings


def pdk_dropped_param_warnings(
    model_bindings: Mapping[str, Any] | None, device_counts: Mapping[str, int]
) -> list[str]:
    """One warning per bound class whose target drops measured params (issue #695)."""
    warnings: list[str] = []
    if model_bindings is not None:
        for class_name in sorted(model_bindings):
            binding = model_bindings[class_name]
            if not binding.dropped_params:
                continue
            count = device_counts.get(class_name, 0)
            if count == 0:
                continue
            device_word = "device" if count == 1 else "devices"
            params_str = "/".join(binding.dropped_params)
            # `binding.subckt` is empty for a "bipolar" binding -- the real
            # subcircuit name is resolved per device, from `binding.variants`
            # (see `_select_bipolar_variant`), not fixed for the whole class
            # -- so name the *target* generically rather than print an empty
            # quoted string.
            target = f"'{binding.subckt}'" if binding.subckt else "its bound target"
            warnings.append(
                f"--pdk binds {count} '{class_name}' {device_word} onto "
                f"{target}, which has no parameter for the extractor's "
                f"measured {params_str} -- these values are "
                "dropped from the written netlist (rerun without --pdk to "
                "recover them from the bare device-class card form). See "
                "docs/cli/extract.md's 'SPICE model binding' section."
            )
    return warnings


def single_terminal_net_warnings(
    single_terminal_nets: Sequence[Mapping[str, Any]],
) -> list[str]:
    """Aggregate warnings for single-terminal nets (issues #596, #599)."""
    warnings: list[str] = []
    single_terminal_gate_count = sum(
        1 for entry in single_terminal_nets if entry["terminal_kind"] == "gate"
    )
    single_terminal_other_count = len(single_terminal_nets) - single_terminal_gate_count
    if single_terminal_gate_count:
        net_word = "net" if single_terminal_gate_count == 1 else "nets"
        warnings.append(
            f"{single_terminal_gate_count} {net_word} connect to exactly "
            "one device terminal -- a MOS gate -- and are not a declared "
            "pin: these gates have no DC path from anywhere else in the "
            "netlist, so they are almost certainly unconnected inputs "
            "rather than legitimate floating nodes; resimulating this "
            "netlist directly will hit a singular matrix on these nets -- "
            "see single_terminal_nets[] for the full list."
        )
    if single_terminal_other_count:
        net_word = "net" if single_terminal_other_count == 1 else "nets"
        warnings.append(
            f"{single_terminal_other_count} {net_word} connect to exactly "
            "one device terminal -- source/drain/body/resistor-equivalent, "
            "not a gate -- and are not a declared pin; this can be a "
            "legitimate single-terminal tie (e.g. an intentionally "
            "unterminated dummy's diffusion tie), but confirm these nets "
            "have no other intended connectivity -- see "
            "single_terminal_nets[] for the full list."
        )
    return warnings


def ignored_layer_warnings(
    ignored_layers: Sequence[Mapping[str, Any]], deck_name: str
) -> list[str]:
    """Aggregate warning for layers outside the connectivity graph (issue #666)."""
    warnings: list[str] = []
    if ignored_layers:
        layer_word = "layer" if len(ignored_layers) == 1 else "layers"
        be_word = "is" if len(ignored_layers) == 1 else "are"
        total_shapes = sum(entry["shapes"] for entry in ignored_layers)
        shape_word = "shape" if total_shapes == 1 else "shapes"
        layers_str = ", ".join(
            f"{entry['layer']}/{entry['datatype']}" for entry in ignored_layers
        )
        warnings.append(
            f"{len(ignored_layers)} {layer_word} ({layers_str}) carrying "
            f"{total_shapes} {shape_word} {be_word} outside '{deck_name}' "
            "deck's connectivity graph -- this geometry is invisible to "
            "extraction, so a net routed only through it extracts as "
            "multiple disconnected nets instead of one, which will "
            "silently mismatch a downstream `klt lvs` reference netlist -- "
            "see ignored_layers[] for the full per-layer shape counts. See "
            "docs/cli/extract.md's 'ignored_layers' field documentation."
        )
    return warnings
