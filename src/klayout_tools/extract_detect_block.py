"""``klt extract`` pre-report detection/warning block (issue #5196, stage 7).

Moved verbatim out of ``run_extract``: the run of net/layer/device-class
detections that sit between the flat-view ``devices[]``/``nets[]`` description
and the parasitics pass -- merged and split net labels, case-fold collisions,
unbiased PMOS bodies, fixed-value MiM capacitors, ``--pdk`` dropped
parameters, single-terminal nets, ignored layers and device-recognition-only
layers. Every one of them is a pure read of the already-built ``devices[]`` /
``nets[]`` / ``device_counts`` / deck, so the block has no ordering
relationship with the writer stages that follow it beyond the one it already
had: it must run after ``_describe_devices``/``_describe_nets`` and before the
report is assembled.

``warnings`` is mutated in place, in the same order, exactly as the inline
block did. ``DetectionFindings`` is a :class:`typing.NamedTuple` so the call
site can keep unpacking the results into the same local names it used before.

``_fixed_value_capacitor_note``/``_fixed_value_capacitor_classes`` still live
in ``extract.py`` and are imported inside the function body: ``extract.py``
imports this module at its top level, so a module-level ``from .extract
import ...`` would be a circular import. Everything else comes from
``extract_report.py``/``extract_warnings.py``, neither of which reaches back
into ``extract.py``.
"""

from __future__ import annotations

from typing import Any, NamedTuple

from .extract_report import (
    _case_collision_warnings,
    _describe_device_recognition_only_layers,
    _describe_ignored_layers,
    _detect_merged_net_labels,
    _detect_single_terminal_nets,
    _detect_split_net_labels,
    _detect_unbiased_pmos_body_nets,
    _split_net_label_warnings,
)
from .extract_warnings import (
    ignored_layer_warnings,
    merged_net_label_warnings,
    pdk_dropped_param_warnings,
    single_terminal_net_warnings,
    unbiased_pmos_body_warnings,
)


class DetectionFindings(NamedTuple):
    """The structured report fields this block produces, in the order
    ``run_extract`` computed them inline. ``fixed_value_capacitor_note`` is
    the one entry that is also consumed downstream (by the ``--parasitics``
    netlist header, see ``extract_netlist_write.write_flat_netlist``) rather
    than only landing in the report."""

    merged_net_labels: list[dict[str, Any]]
    split_net_labels: list[dict[str, Any]]
    unbiased_pmos_body_nets: list[dict[str, Any]]
    fixed_value_capacitor_note: list[str]
    single_terminal_nets: list[dict[str, Any]]
    ignored_layers: list[dict[str, Any]]
    device_recognition_only_layers: list[dict[str, Any]]


def build_detection_findings(
    *,
    path: Any,
    deck: Any,
    deck_name: Any,
    devices: Any,
    nets: Any,
    device_counts: Any,
    model_bindings: Any,
    warnings: Any,
) -> DetectionFindings:
    from .extract import _fixed_value_capacitor_classes, _fixed_value_capacitor_note

    # Nets whose KLayout-assigned name is a multi-label merge (issue #470):
    # `Net.expanded_name()` joins every distinct label found on one
    # electrical net with `,` (see `tests/test_extract.py`'s
    # `_make_inverter_layout(extra_y_label=...)` fixture, built for issue
    # #312's SPICE-name-sanitization fix), but this response's `nets[]`
    # (built by `_describe_nets`, in `run_extract`) has already rewritten
    # that to the `|`-joined spelling KLayout's own `NetlistSpiceWriter` uses
    # for the net's node references in the written SPICE
    # (`spice_safe_net_name`, issue #696) -- so `merged_net_labels[].net`
    # below is byte-identical to the netlist's own spelling, not a separately
    # (comma-) spelled alias of it. That is a silent signal that two
    # differently-named nets were shorted together on the layout side --
    # e.g. a `gen-compose` `pins[]` entry naming a port that other
    # connectivity already reaches. Surfaced two ways: a structured
    # `merged_net_labels[]` entry (so a caller does not have to re-derive the
    # label list from the joined string) and a matching prose `warnings[]`
    # entry (so a caller checking only `warnings[]`, per the documented
    # contract, still sees it). See docs/cli/extract.md's "Merged net labels"
    # section for the false-positive limitation: a label that legitimately
    # contains a literal `|` is indistinguishable from a real collision by
    # this heuristic.
    merged_net_labels = _detect_merged_net_labels(nets)
    warnings.extend(merged_net_label_warnings(merged_net_labels))

    # The inverse (issue #3920): one drawn label on 2+ distinct nets, e.g.
    # two unstrapped `VDD` rails. Same two-way surfacing as above; see
    # docs/cli/extract.md's "Split net labels" section.
    split_net_labels = _detect_split_net_labels(nets)
    warnings.extend(_split_net_label_warnings(split_net_labels))

    # Issue #4200: nets differing only by case merge on `klt lvs` read-back.
    warnings.extend(_case_collision_warnings(nets))

    # PMOS body terminals tied to an anonymous, unbiased net (issue #555):
    # a PMOS body extracts onto a KLayout-synthesized `$<n>` net with no DC
    # bias path whenever no well tie -- drawn on a distinct `tap` layer, or
    # derived from `tap_nplus`/`tap_pplus` implants (issue #1084) -- reaches
    # this specific device's `nwell` island; a deck with neither mechanism
    # at all (e.g. gf180mcu before #1084) hits this for *every* PMOS,
    # unconditionally. The net name was already readable via
    # `devices[].nets["b"]`, but nothing flagged it as *this specific* gap.
    # Surfaced two ways: a structured `unbiased_pmos_body_nets[]` entry (so
    # a caller does not have to re-derive the anonymous-net convention
    # itself) and a matching prose `warnings[]` entry. The `warnings[]`
    # entry is a single aggregate line with the count baked in, mirroring
    # `_detect_unmodelled_poly_bodies`'s aggregate pattern (issue #599) --
    # one line per device would blow up `warnings[]` at scale (e.g. 148
    # entries for 148 floating PMOS bodies) and defeat literal-list pinning
    # by a caller.
    unbiased_pmos_body_nets = _detect_unbiased_pmos_body_nets(devices, deck)
    warnings.extend(unbiased_pmos_body_warnings(unbiased_pmos_body_nets))

    # Drawn MiM capacitors written as a fixed-value `C` card (issue #3090): the
    # value is the deck's nominal area+perimeter law, so PDK process-corner
    # sections cannot move it (a `--pdk`-bound `X` subckt call can). One
    # aggregate line, plus the same fact in the `--parasitics` netlist header
    # (`extract_netlist_write.write_flat_netlist`, which takes this note as an
    # argument).
    fixed_value_capacitor_note = _fixed_value_capacitor_note(
        _fixed_value_capacitor_classes(deck, device_counts, model_bindings)
    )
    warnings.extend(fixed_value_capacitor_note)

    # `--pdk`-bound device classes whose target subcircuit has no parameter
    # for something the extractor measured (issue #695): today, only a
    # sky130 `pnp` bipolar binding, whose fixed-geometry `pnp_05v5_*` cells
    # take no per-instance base/collector-area/perimeter or emitter-count
    # override at all (see `pdk_models.DeviceBinding.dropped_params` and
    # `_BIPOLAR_DROPPED_PARAMS`'s docstring for the full provenance). MOS
    # bindings carry every measured parameter onto the `X` card as of #695
    # (see `devices[].params`' `as_um2`/`ad_um2`/`ps_um`/`pd_um`, documented
    # on `run_extract`) and so never reach this branch. One aggregate line
    # per affected class (not per device), mirroring
    # `unbiased_pmos_body_nets`'s pattern just above -- `device_counts` is
    # keyed by `devices[].class`, independent of `--pdk`, so it is safe to
    # consult here even though it was computed before the `--pdk`-bound
    # netlist is written.
    warnings.extend(pdk_dropped_param_warnings(model_bindings, device_counts))

    # Nets touching exactly one device terminal and no declared pin (issue
    # #596): there is no DC path through such a node from anywhere else in
    # the netlist, so a downstream simulator hits a singular matrix on it --
    # several stages past where this is structurally detectable from the
    # extracted netlist alone. Surfaced two ways: a structured
    # `single_terminal_nets[]` entry (so a caller does not have to
    # cross-reference `nets[]`/`devices[]` itself) and a matching prose
    # `warnings[]` entry, phrased more strongly for a gate terminal (almost
    # never intentional) than for source/drain/body/resistor-equivalent
    # terminals (can be a legitimate unterminated dummy tie). Each bucket
    # gets its own single aggregate `warnings[]` line with the count baked
    # in (issue #599), mirroring `_detect_unmodelled_poly_bodies`'s
    # two-message-class aggregate pattern -- one line per net would blow up
    # `warnings[]` at scale.
    single_terminal_nets = _detect_single_terminal_nets(devices, nets)
    warnings.extend(single_terminal_net_warnings(single_terminal_nets))

    # Layers carrying shapes the deck's connectivity graph never reads (issue
    # #220): geometry there is invisible to extraction, so surface it rather
    # than let it become a silent LVS mismatch downstream.
    ignored_layers = _describe_ignored_layers(path, deck)

    # A material `ignored_layers` result gets a matching `warnings[]` entry
    # (issue #666): `_describe_layers_in_set` already drops every
    # `shapes == 0` entry before it reaches `ignored_layers`, so a non-empty
    # list here is by construction "material" -- geometry that is genuinely
    # invisible to this extraction's connectivity graph, not a stray empty
    # layer declaration. Before this, `ignored_layers` was a diagnostic-only
    # field: a routed net split across an undeclared metal level extracted
    # "successfully" with no signal in `warnings[]`, the one field
    # `docs/cli/extract.md` documents as the minimal self-check every `klt`
    # command output should get. One aggregate line with the shape total
    # baked in (issue #599's pattern), not one line per layer -- mirroring
    # `metals_without_coefficient`'s `warnings[]` entry, raised by the
    # parasitics pass that runs after this block.
    warnings.extend(ignored_layer_warnings(ignored_layers, deck_name))

    # Layers carrying shapes the deck reads for bipolar/capacitor/resistor/
    # diode device recognition but never treats as a `metals`/`vias`
    # connectivity level (issue #619): such a layer does not appear in
    # `ignored_layers` above (it *is* read), but two nets joined only through
    # it will not merge -- exactly the gap that hid sky130's own met3/met4
    # routing-connectivity ceiling behind a clean-looking `ignored_layers`
    # report before this deck's `metals` stack grew to cover them too. Unlike
    # `ignored_layers`, this is *not* mirrored into `warnings[]`: an
    # `ExtractionDeck.device_recognition_only_layers` entry is the deck's own
    # marker/mask geometry (a resistor's `poly.res` marker, a bipolar's ID
    # mark, a MiM cap's top-plate mark) -- layers that are *never* candidate
    # connectivity levels by PDK design, so their presence is routine, not a
    # gap. Reporting them here is diagnostic context for the rare case where
    # a caller genuinely needs to distinguish "read but not merged" from
    # "never read," not a signal that something is wrong with this
    # extraction. See `ExtractionDeck.device_recognition_only_layers`'s
    # docstring and docs/cli/extract.md's "Device-recognition-only layers"
    # section.
    device_recognition_only_layers = _describe_device_recognition_only_layers(
        path, deck
    )

    return DetectionFindings(
        merged_net_labels=merged_net_labels,
        split_net_labels=split_net_labels,
        unbiased_pmos_body_nets=unbiased_pmos_body_nets,
        fixed_value_capacitor_note=fixed_value_capacitor_note,
        single_terminal_nets=single_terminal_nets,
        ignored_layers=ignored_layers,
        device_recognition_only_layers=device_recognition_only_layers,
    )
