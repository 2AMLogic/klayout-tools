"""``klt extract`` command: serialise the extraction report as text or JSON.

Output goes through the shared envelope helpers in :mod:`.output`, as with
every other ``klt`` subcommand -- see ``docs/json-contract.md``.

Exit codes (see ``docs/cli/extract.md`` for the full table):
    0 - extraction succeeded, netlist written
    1 - failed to run (bad file, unknown deck, unresolvable PDK, missing/
        ambiguous top cell, engine error) -- returned by ``emit_error`` as
        ``output.ERROR_EXIT_CODE``
(2 is reserved for argparse usage errors, as with every other ``klt``
subcommand. There is no "ran but found problems" outcome for extraction --
it either produces a netlist or it fails -- so, unlike ``klt drc``/``klt
lvs``, there is no exit code 3; see docs/cli/extract.md.)
"""

import argparse

from ..extract import ExtractError, run_extract
from .output import emit_error, emit_success


def _parse_deck_options(raw: list[str] | None) -> dict[str, str] | None:
    """Parse the ``--deck-option`` flag's ``KEY=VALUE`` entries (issue #595,
    repeatable) into a ``dict``, or ``None`` when the flag was never given.

    Raises :class:`ExtractError` for a malformed entry (no ``=``, or a blank
    key) -- a likely typo, not a meaningful "no options" request. A later
    ``KEY`` overrides an earlier one with the same key (last-one-wins,
    matching how argparse's own ``append`` action preserves given order).
    """
    if raw is None:
        return None
    options: dict[str, str] = {}
    for entry in raw:
        key, sep, value = entry.partition("=")
        key = key.strip()
        if not sep or not key:
            raise ExtractError(
                f"--deck-option entry {entry!r} is not KEY=VALUE -- "
                "e.g. --deck-option poly_res=2k"
            )
        options[key] = value.strip()
    return options


def _parse_declared_pins(raw: str | None) -> frozenset[str] | None:
    """Parse the ``--pins`` flag's comma-separated value (issue #514) into a
    ``frozenset`` of declared pin names, or ``None`` when the flag was
    omitted entirely (skips the declared-pin-set reconciliation).

    Raises :class:`ExtractError` if the flag was given but every
    comma-separated token is blank (e.g. ``--pins ""`` or ``--pins ,,``) --
    a likely mistake, not a meaningful "declare zero pins" request.
    """
    if raw is None:
        return None
    names = frozenset(name.strip() for name in raw.split(",") if name.strip())
    if not names:
        raise ExtractError(
            "--pins was given but contains no non-empty name "
            f"(got {raw!r}) -- pass a comma-separated list of net names, "
            "e.g. --pins A,B,VDD,VSS"
        )
    return names


def run(args: argparse.Namespace) -> int:
    try:
        declared_pins = _parse_declared_pins(args.pins)
        deck_options = _parse_deck_options(args.deck_options)
        report = run_extract(
            args.file,
            args.deck,
            output=args.output,
            top=args.top,
            pdk_variant=args.pdk,
            pdk_root=args.pdk_root,
            parasitics=args.parasitics,
            top_cell_pins_only=args.top_cell_pins,
            declared_pins=declared_pins,
            # `--defer-resistor-fixed-offset` (issue #588) is the CLI half of
            # the deferred-correction contract `docs/cli/lvs.md` describes:
            # the flag is phrased as "defer" (opt in to omitting the
            # correction), `run_extract`'s kwarg as "apply" (default `True`),
            # so the CLI default stays today's always-applied behavior.
            apply_resistor_fixed_offset=not args.defer_resistor_fixed_offset,
            # `--deck-option` (issue #595): selects a caller-visible flavour
            # of a shared-geometry device family (e.g. gf180mcu's
            # `poly_res`). `None` when the flag was never given, unchanged
            # from every call site that predates it.
            deck_options=deck_options,
            # `--abstract-cells`/`--abstract-cell-lef` (issue #620): cell-
            # level black-box abstraction. `()` when the flag(s) were never
            # given, unchanged from every call site that predates them.
            abstract_cell_patterns=tuple(args.abstract_cells or ()),
            abstract_cell_lef_paths=tuple(args.abstract_cell_lef or ()),
            # `--mom-net` (issue #798): cross-checks (and replaces) one
            # net's --parasitics ground capacitance against `klt mom`.
            # `None` when the flag was never given, unchanged from every
            # call site that predates it.
            mom_net=args.mom_net,
            # `--spef` (issue #948): writes --parasitics's per-net R/C model
            # as a SPEF file at this path. `None` when the flag was never
            # given, unchanged from every call site that predates it.
            spef_output=args.spef,
        )
    except ExtractError as exc:
        return emit_error("extract", str(exc), args.format)

    emit_success(report, args.format, _print_text)

    return 0


def _print_text(report: dict) -> None:
    print(f"file: {report['file']}")
    print(f"deck: {report['deck']}")
    # Present only when `--deck-option` selected a non-default flavour
    # (issue #595) -- mirrors `provenance.deck.options` in the JSON report.
    # `or {}` at each hop, not just a `.get` default: a present-but-`null`
    # `provenance`/`deck` would otherwise chain a `.get()` onto `None`.
    deck_provenance = (report.get("provenance") or {}).get("deck") or {}
    deck_options = deck_provenance.get("options")
    if deck_options:
        print(f"deck_options: {deck_options}")
    print(f"top: {report['top']}")
    print(f"dbu_um: {report['dbu_um']}")
    print(f"status: {report['status']}")
    print(f"netlist_path: {report['netlist_path']}")
    print(f"netlist_sha256: {report['netlist_sha256']}")
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

    warnings = report["warnings"]
    if warnings:
        print()
        print("warnings:")
        for warning in warnings:
            print(f"  {warning}")
