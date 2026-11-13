"""``klt extract`` text-mode rendering (issue #5481).

Moved verbatim out of ``cli/extract_cmd.py``: every ``_print_*`` helper that
turns a ``klt extract`` report into the human-readable courtesy output, with
``_print_text`` as the entry point ``run()`` hands to ``emit_success``. JSON is
the contract; this module is the courtesy surface, so keeping it beside the
adapter rather than inside it mirrors ``klt drc``'s own ``cli/drc_text.py``
split and leaves ``extract_cmd.py`` as argument/request plumbing.

The only behaviour change in the move is the additive ``_print_engine`` block
(issue #5481): a report carrying ``engine`` -- which only the
``klayout-runset`` engine emits -- also prints its runset and ``-rd`` globals.
A curated-deck report has no such key and its text output is unchanged.
"""

from __future__ import annotations

from .._jsonnum import is_int  # noqa: F401 -- kept for parity with the adapter
from ..env_provenance import render_path_field


def _print_resistance_paths(parasitics: dict) -> None:
    """Text-mode lines for ``parasitics.resistance_paths[]`` (issue #2765);
    prints nothing when no ``--resistance-path`` was given."""
    for query in parasitics.get("resistance_paths") or ():
        head = (
            f"resistance_path: net_id={query['net_id']} "
            f"{query['from']} -> {query['to']}  status={query['status']}"
        )
        if query["status"] == "ok":
            print(
                f"{head}  series_R={query['resistance_ohm']} ohm  "
                f"via {' -> '.join(query['path_nodes'])}"
            )
        else:
            print(f"{head}  ({query['reason']})")


def _print_device_models(device_models: dict | None) -> None:
    """One ``device_models: CLASS=SUBCKT, ...`` line (issue #4234); silent
    when ``--device-model`` was never given."""
    if not device_models:
        return
    rendered = ", ".join(
        f"{name}={model}" for name, model in sorted(device_models.items())
    )
    print(f"device_models: {rendered}")


def _print_label_layers(label_layers: dict | None) -> None:
    """One human-readable line for the response's ``label_layers`` echo
    (issue #2656), or nothing when ``--label-layer`` was not given.

    The ``deck`` name alone no longer answers "which GDS purpose did this run
    read net/pin names from" once it can be overridden per request, so the text
    surface states it too rather than leaving it visible only under
    ``--format json``. Written as a helper taking the (possibly ``None``)
    block, like :func:`_print_substrate_spreading` below, so the call site in
    :func:`_print_text` adds no branch to an already long function.
    """
    if not label_layers:
        return
    rendered = ", ".join(
        f"{role}={'none' if layer is None else f'{layer[0]}/{layer[1]}'}"
        for role, layer in sorted(label_layers.items())
    )
    print(f"label_layers: {rendered}")


def _print_probes(probes: list | None) -> None:
    """One human-readable line per ``--probe`` result (issue #2986), or
    nothing when none was given."""
    for probe in probes or []:
        where = f"{probe['layer']} ({probe['x_um']}, {probe['y_um']})"
        if probe["found"]:
            print(f"probe {probe['name']}: {where} -> {probe['net']}")
        else:
            print(f"probe {probe['name']}: {where} -> not found")


def _print_substrate_spreading(block: dict | None) -> None:
    """One human-readable line for ``parasitics.substrate_spreading``
    (issue #2561), or nothing when ``--substrate-spreading`` was not given.

    The min/max across the reported pairs is the honest one-line summary: a
    single "the" resistance does not exist for a net with more than two taps
    (see ``pairwise_approximation``), and the JSON carries every pair for a
    caller that needs them. The line says "not a network solve" out loud so
    a reader skimming text output cannot mistake it for one.
    """
    if block is None:
        return
    print(
        f"substrate_spreading: net={block['net']}  "
        f"taps={block['tap_count']}  "
        f"pairs={block['pair_count']}  "
        f"R={block['min_resistance_ohm']}..{block['max_resistance_ohm']} ohm  "
        f"(rho={block['resistivity_ohm_cm']} ohm-cm, "
        "two-terminal estimate, not a network solve)"
    )


def _print_engine(report: dict) -> None:
    """Additive (issue #5481): printed only by the ``klayout-runset`` engine,
    whose report carries ``engine``. A curated-deck report has no such key, so
    its text output is unchanged -- ``deck`` below names its deck instead."""
    if not report.get("engine"):
        return
    print(f"engine: {report['engine']}")
    print(f"deck_file: {report.get('deck_file')}")
    deck_vars = report.get("deck_vars")
    if deck_vars:
        rendered = ", ".join(f"{k}={v}" for k, v in sorted(deck_vars.items()))
        print(f"deck_vars: {rendered}")


def _print_text(report: dict) -> None:
    # `file` is the `{path, scope}` object `env_provenance.repo_relative_path`
    # defines (issue #2659) -- rendered through the same courtesy helper
    # `klt sim`/`klt pex` use, so the absolute path is never printed even in
    # text mode (`<outside repo>`/`<unresolved>` instead). `netlist_path`
    # below is an output path, still a plain string, and prints as-is.
    print(f"file: {render_path_field(report['file'])}")
    _print_engine(report)
    print(f"deck: {report['deck']}")
    # Present whenever this deck declares a caller-selectable option at all
    # (issues #595, #2394) -- mirrors `provenance.deck.options` in the JSON
    # report, which since #2394 is the *resolved* set rather than only the
    # caller's `--deck-option` overrides. Each silently-defaulted value is
    # marked `(default)` from `options_explicit`, so the text surface cannot
    # present one option's answer as *the* answer either.
    # `or {}` at each hop, not just a `.get` default: a present-but-`null`
    # `provenance`/`deck` would otherwise chain a `.get()` onto `None`.
    deck_provenance = (report.get("provenance") or {}).get("deck") or {}
    deck_options = deck_provenance.get("options")
    if deck_options:
        # No `options_explicit` at all == a record predating #2394, whose
        # `options` held caller-passed keys only -- so every key there was
        # explicit, and none should be marked `(default)`.
        explicit = deck_provenance.get("options_explicit")
        rendered = ", ".join(
            f"{key}={value}"
            if explicit is None or explicit.get(key)
            else f"{key}={value} (default)"
            for key, value in sorted(deck_options.items())
        )
        print(f"deck_options: {rendered}")
    # Additive (issue #2656): only printed when --label-layer was given.
    # Called unconditionally, with the absent/empty test inside the helper, so
    # this line adds no branch to an already long function -- the same shape
    # `_print_substrate_spreading` below already uses.
    _print_label_layers(report.get("label_layers"))
    _print_device_models(report.get("device_models"))
    _print_probes(report.get("probes"))
    print(f"top: {report['top']}")
    print(f"dbu_um: {report['dbu_um']}")
    print(f"status: {report['status']}")
    # Both are None under `--no-netlist` (issue #2850).
    print(f"netlist_path: {report['netlist_path']}")
    print(f"netlist_sha256: {report['netlist_sha256']}")
    print(f"netlist_normalized_sha256: {report['netlist_normalized_sha256']}")
    print(
        f"devices: {report['device_count']}  "
        f"nets: {report['net_count']}  "
        f"pins: {report['pin_count']}"
    )

    pdk = report["pdk"]
    if pdk is not None:
        print(f"pdk: {pdk['variant']} ({pdk['version'] or '-'})")

    parasitics = report.get("parasitics")
    if parasitics is not None:
        print(
            f"parasitics: R={parasitics['r_count']} C={parasitics['c_count']}  "
            f"total_R={parasitics['total_resistance_ohm']} ohm  "
            f"total_C={parasitics['total_capacitance_ff']} fF"
        )
        # Additive (issue #760): only printed once coupling capacitance
        # exists at all (`cc_count == 0` is the common case for a small,
        # isolated cell today), matching this function's existing convention
        # of omitting empty/zero optional sections rather than printing a
        # row of zeros.
        if parasitics.get("cc_count"):
            print(
                f"parasitics_coupling: CC={parasitics['cc_count']}  "
                f"total_CC={parasitics['total_coupling_capacitance_ff']} fF"
            )
        # Additive (issue #976): only printed when --critical-net was given.
        critical_nets = parasitics.get("critical_nets")
        if critical_nets:
            print(f"critical_nets: {', '.join(critical_nets)}")
        # Additive (issue #1700): only printed when --parasitics-net was
        # given -- the scoped run's `parasitics.nets[]` is deliberately short,
        # so say why rather than leaving a reader to guess.
        parasitics_nets = parasitics.get("parasitics_nets")
        if parasitics_nets:
            print(f"parasitics_nets: {', '.join(parasitics_nets)}")
        # Additive (issue #3056): only printed when --parasitic-variant was given.
        if parasitics.get("parasitic_variant"):
            print(f"parasitic_variant: {', '.join(parasitics['parasitic_variant'])}")
        # Additive (issue #977): only printed when --distributed-rc was given.
        if parasitics.get("distributed_rc"):
            distributed_count = sum(
                1 for n in parasitics["nets"] if n.get("rc_model") == "distributed"
            )
            print(f"distributed_rc: {distributed_count} net(s)")
        # Additive (issue #2765): only prints when --resistance-path was given.
        _print_resistance_paths(parasitics)
        # Additive (issue #1704): only printed when --parasitics-top-cell-only
        # was given -- each `parasitics.nets[]` entry's
        # `resistance_ohm_top_cell`/`capacitance_ff_top_cell` otherwise stay
        # `None`, which text mode does not otherwise surface.
        if parasitics.get("top_cell_only"):
            top_cell_split_count = sum(
                1
                for n in parasitics["nets"]
                if n.get("capacitance_ff_top_cell") is not None
            )
            print(f"parasitics_top_cell_only: {top_cell_split_count} net(s)")
        # Additive (issue #798): only printed when --mom-net was given.
        mom_crosscheck = parasitics.get("mom_crosscheck")
        if mom_crosscheck is not None:
            print(
                f"mom_crosscheck: net={mom_crosscheck['net']}  "
                f"lumped_rc={mom_crosscheck['lumped_rc_capacitance_ff']} fF  "
                f"mom={mom_crosscheck['mom_capacitance_ff']} fF  "
                f"delta={mom_crosscheck['delta_ff']} fF "
                f"({mom_crosscheck['delta_pct']}%)"
            )
        # Additive (issue #2561): only printed when --substrate-spreading was
        # given. Called unconditionally, with the `None` test inside the
        # helper, so this line adds no branch to an already long function.
        _print_substrate_spreading(parasitics.get("substrate_spreading"))
        # Additive (issue #988): only printed when --mom-rlc-net was given.
        mom_rlc_override = parasitics.get("mom_rlc_override")
        if mom_rlc_override is not None:
            print(
                f"mom_rlc_override: net={mom_rlc_override['net']}  "
                f"r_ohm={mom_rlc_override['resistance_ohm']}  "
                f"c_ff={mom_rlc_override['capacitance_ff']}  "
                f"l_nh={mom_rlc_override['inductance_nh']}"
            )

    # Additive (issue #948): only printed when --spef was given.
    spef_path = report.get("spef_path")
    if spef_path is not None:
        print(f"spef_path: {spef_path}")

    device_counts = report["device_counts"]
    if device_counts:
        print()
        print("device_counts:")
        for class_name in sorted(device_counts):
            print(f"  {class_name}: {device_counts[class_name]}")

    ignored_layers = report.get("ignored_layers", [])
    if ignored_layers:
        print()
        print("ignored_layers (shapes not read by this deck's connectivity):")
        for entry in ignored_layers:
            print(f"  {entry['layer']}/{entry['datatype']}: {entry['shapes']} shape(s)")

    device_recognition_only_layers = report.get("device_recognition_only_layers", [])
    if device_recognition_only_layers:
        print()
        print(
            "device_recognition_only_layers (read for device recognition, "
            "not a metals/vias connectivity level):"
        )
        for entry in device_recognition_only_layers:
            print(f"  {entry['layer']}/{entry['datatype']}: {entry['shapes']} shape(s)")

    matched_device_groups = report.get("matched_device_groups", [])
    if matched_device_groups:
        print()
        print("matched_device_groups (--matched-group):")
        for group in matched_device_groups:
            status = "OK" if not group["mismatched_fields"] else "MISMATCH"
            print(f"  {group['name']}: {', '.join(group['instances'])} [{status}]")
            for mismatch in group["mismatched_fields"]:
                values_str = ", ".join(
                    f"{name}={value}" for name, value in mismatch["values"].items()
                )
                print(f"    {mismatch['field']}: {values_str}")
            if group["unresolved_instances"]:
                print(f"    unresolved: {', '.join(group['unresolved_instances'])}")

    # Additive (issue #2245): a no-op unless --subcircuit was given.
    _print_subcircuit_text(report.get("subcircuit"))
    # Additive (issue #2722): a no-op unless --hierarchical-cells was given.
    _print_hierarchy_text(report.get("hierarchy"))

    # Additive (issue #2473): a no-op unless --def-pins was given and its
    # declared-vs-promoted reconciliation raised an error-severity finding.
    _print_def_pin_promotion_text(report.get("def_pin_promotion"))

    warnings = report["warnings"]
    if warnings:
        print()
        print("warnings:")
        for warning in warnings:
            print(f"  {warning}")


def _print_def_pin_promotion_text(promotion: dict | None) -> None:
    """Render the `def_pin_promotion` block's `error`-severity findings
    (issue #2473) -- nothing at all when `--def-pins` was never given, and
    nothing when every declared DEF `PINS` name either promoted or missed
    for an ordinary (non-geometry) reason, which is the common case. The
    full prose for each finding is already in `warnings[]` below; this block
    is the at-a-glance count and layer/datatype summary."""
    if promotion is None or not promotion["findings"]:
        return
    print()
    print(
        f"def_pin_promotion (--def-pins): {promotion['promoted']} of "
        f"{promotion['declared']} declared DEF PINS name(s) promoted"
    )
    for finding in promotion["findings"]:
        print(
            f"  [{finding['severity']}] {finding['layer']}/"
            f"{finding['datatype']} (outside this deck's connectivity "
            f"graph): {', '.join(finding['pins'])}"
        )


def _print_hierarchy_text(hierarchy: dict | None) -> None:
    """Render the `hierarchy` block (issue #2722) -- nothing at all when
    `--hierarchical-cells` was never given."""
    if hierarchy is None:
        return
    print(
        f"hierarchy: {len(hierarchy['cells'])} cell(s)  "
        f"top devices: {hierarchy['top_device_count']}  "
        f"top instances: {hierarchy['top_instance_count']}"
    )
    for entry in hierarchy["cells"]:
        print(
            f"  {entry['cell']}: {entry['placements']} placement(s)  "
            f"devices: {entry['device_count']}  "
            f"pins: {' '.join(entry['pins'])}"
        )


def _print_subcircuit_text(subcircuit: dict | None) -> None:
    """Render the `subcircuit` block (issue #2245) -- nothing at all when
    `--subcircuit` was never given."""
    if subcircuit is None:
        return
    print()
    print(f"subcircuit ({subcircuit['cell']}):")
    print(f"  path: {subcircuit['path']}")
    print(
        f"  devices: {subcircuit['device_count']}  "
        f"nets: {subcircuit['net_count']}  "
        f"pins: {len(subcircuit['pins'])}"
    )
    for pin in subcircuit["pins"]:
        print(f"    {pin['name']} [{pin['role']}]")
    print(f"  instance_line: {subcircuit['instance_line']}")
    excluded = subcircuit["excluded_parasitics"]
    if excluded["r_count"] or excluded["c_count"] or excluded["l_count"]:
        print(
            "  excluded_parasitics (attributed to the parent deck): "
            f"{excluded['r_count']} R  {excluded['c_count']} C  "
            f"{excluded['l_count']} L  "
            f"{excluded['capacitance_ff']} fF  "
            f"{excluded['resistance_ohm']} ohm"
        )
