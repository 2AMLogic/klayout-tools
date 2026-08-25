"""``klt extract`` command: serialise the extraction report as text or JSON.

Output goes through the shared envelope helpers in :mod:`.output`, as with
every other ``klt`` subcommand -- see ``docs/json-contract.md``.

Exit codes (see ``docs/cli/extract.md`` for the full table):
    0 - extraction succeeded, netlist written (or, under --check, the
        report still holds)
    1 - failed to run (bad file, unknown deck, unresolvable PDK, missing/
        ambiguous top cell, engine error) -- returned by ``emit_error`` as
        ``output.ERROR_EXIT_CODE``
    3 - under --check, the committed report drifted from the current deck/
        input
(2 is reserved for argparse usage errors, as with every other ``klt``
subcommand. A fresh (non-``--check``) extraction still has no "ran but
found problems" outcome -- it either produces a netlist or it fails -- so
exit code 3 is only ever reached via --check; see docs/cli/extract.md.)

``--check <report>`` (issue #1149) switches ``klt extract`` from running a
fresh extraction into *verifying a previously committed* ``--format json``
report still reproduces -- see ``docs/cli/extract.md``, "--check" -- and is
mutually exclusive with the positional ``file`` argument (the input path is
read from the committed report itself). Cheap mode (default): re-hash the
input/deck named in the report and compare against its own recorded
``content_hash`` values, no extraction engine re-run. Full mode (``--check
<report> --rerun``): actually re-run the extraction the report describes
and diff verdict-bearing fields. Both reuse ``status: "match"`` /
``"drifted"`` and the same 0/3 exit-code split described above (see
``klayout_tools._report_verify``).
"""

import argparse

from ..extract import (
    ExtractError,
    check_extract_report,
    def_net_instance_pins,
    rerun_extract_report,
    run_extract,
)
from .output import emit_error, emit_success

EXIT_OK = 0
EXIT_DRIFTED = 3
#: Alias for the --check/--rerun "still consistent" outcome (issue #1149) --
#: same numeric value as a normal run's success exit, just named for the
#: "match"/"drifted" vocabulary those modes report under.
EXIT_MATCH = EXIT_OK


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


def _parse_matched_groups(raw: list[str] | None) -> dict[str, tuple[str, ...]] | None:
    """Parse the ``--matched-group`` flag's ``NAME=INST1,INST2[,...]``
    entries (issue #1018, repeatable) into a ``dict``, or ``None`` when the
    flag was never given.

    Raises :class:`ExtractError` for a malformed entry (no ``=``, a blank
    group name, or fewer than two non-empty instance names -- there is
    nothing to compare with just one), or for a group name repeated across
    two ``--matched-group`` flags -- a `dict` cannot represent that
    ambiguity, so it is caught here rather than silently keeping only the
    last occurrence (unlike ``--deck-option``'s intentional last-one-wins
    convention for a single scalar value).
    """
    if raw is None:
        return None
    groups: dict[str, tuple[str, ...]] = {}
    for entry in raw:
        name, sep, instances_raw = entry.partition("=")
        name = name.strip()
        if not sep or not name:
            raise ExtractError(
                f"--matched-group entry {entry!r} is not NAME=INST1,INST2 -- "
                "e.g. --matched-group mirror_leg=$1,$2"
            )
        if name in groups:
            raise ExtractError(
                f"--matched-group name {name!r} was given more than once -- "
                "declare every member of a group in a single "
                "--matched-group entry, e.g. "
                f"--matched-group {name}=$1,$2,$3"
            )
        instances = tuple(
            inst.strip() for inst in instances_raw.split(",") if inst.strip()
        )
        if len(instances) < 2:
            raise ExtractError(
                f"--matched-group {name!r} names {len(instances)} "
                "instance(s) -- a matched group needs at least two, e.g. "
                f"--matched-group {name}=$1,$2"
            )
        groups[name] = instances
    return groups


def _parse_def_net_connections(
    raw: str | None, *, spef_output: str | None
) -> dict[str, tuple[tuple[str, str], ...]] | None:
    """Parse the ``--def-net-connections`` flag's DEF path (issue #961) into
    :func:`def_net_instance_pins`'s ``{net: ((inst, pin), ...)}`` mapping, or
    ``None`` when the flag was omitted entirely.

    Raises :class:`ExtractError` when given without ``--spef`` -- this data
    is only ever consumed by the SPEF writer, so a caller passing it without
    ``--spef`` almost certainly meant to pass both, and silently ignoring it
    would hide that mistake.
    """
    if raw is None:
        return None
    if spef_output is None:
        raise ExtractError("--def-net-connections requires --spef")
    return def_net_instance_pins(raw)


def _parse_def_pins(raw: str | None) -> frozenset[str] | None:
    """Parse the ``--def-pins`` flag's DEF path (issue #1390) into the
    ``run_extract``/``def_pins`` declared-port-name set, or ``None`` when the
    flag was omitted entirely.

    Reuses ``place_and_route.def_pin_names`` -- the exact same DEF ``PINS``-
    section scan ``klt place-and-route``'s own internal SPEF pipeline
    already relies on (issue #961) -- imported locally so a plain ``klt
    extract`` run (the overwhelming majority of invocations, which never
    touch this flag) never pays ``place_and_route.py``'s own import cost.

    Raises :class:`ExtractError` when the given path carries no parseable
    DEF ``PINS`` section: unlike ``def_net_instance_pins`` (whose ``{}``
    "absence is not proof of zero" default is safe because every lookup
    site tolerates it), a caller passing ``--def-pins`` has explicitly
    asked for its declared-pin-set reconciliation to run -- silently
    skipping it here would demote *every* promoted pin instead of the
    intended subset, the exact silent-zero-pins failure mode issue #1385
    already treats as a hard warning elsewhere in this module.
    """
    if raw is None:
        return None
    from ..place_and_route import def_pin_names

    names = def_pin_names(raw)
    if names is None:
        raise ExtractError(
            f"--def-pins {raw!r} has no parseable DEF `PINS` section -- pass "
            "the routed DEF `klt place-and-route` wrote for this layout "
            "(its own response.def_path), or use --pins to declare pin "
            "names by hand instead"
        )
    return names


def run(args: argparse.Namespace) -> int:
    # `file` and `--check` are a required, mutually exclusive argparse group
    # (parser.py) -- omitting both, or giving both, is already a usage error
    # (exit 2) by the time `run()` is ever called.
    if args.check is not None:
        return _run_check(args)

    if args.rerun:
        return emit_error("extract", "--rerun requires --check <report>", args.format)

    try:
        if not args.deck:
            raise ExtractError("argument --deck is required")
        declared_pins = _parse_declared_pins(args.pins)
        def_pins = _parse_def_pins(args.def_pins)
        deck_options = _parse_deck_options(args.deck_options)
        def_net_connections = _parse_def_net_connections(
            args.def_net_connections, spef_output=args.spef
        )
        matched_device_groups = _parse_matched_groups(args.matched_groups)
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
            # `--def-net-names` (issue #951): names routed nets from the DEF
            # net name their geometry carries as a GDS shape property instead
            # of from text labels. `False` when the flag was never given,
            # unchanged from every call site that predates it.
            def_net_names=args.def_net_names,
            # `--critical-net` (issue #976, Epic #709 Phase 2a): scopes the
            # lateral (same-layer sidewall) coupling pass onto these net
            # names. `None` when the flag was never given, unchanged from
            # every call site that predates it.
            critical_nets=args.critical_nets,
            # `--distributed-rc` (issue #977, Epic #709 Phase 2b): replaces
            # the star/Gamma-shunt R/C model with a distributed ladder for
            # every `--critical-net`-named net. `False` when the flag was
            # never given, unchanged from every call site that predates it.
            distributed_rc=args.distributed_rc,
            # `--def-net-connections` (issue #961): real cell-instance
            # `*CONN`/`*RES` correlation for --spef, parsed from a routed
            # DEF's own `NETS` section. `None` when the flag was never
            # given, unchanged from every call site that predates it.
            def_net_connections=def_net_connections,
            # `--mom-rlc-net`/`--mom-rlc-resistance-ohm`/
            # `--mom-rlc-capacitance-ff`/`--mom-rlc-inductance-nh` (issue
            # #988, Epic #709 Phase 3a): substitutes a caller-supplied R/L/C
            # for one named net's Phase 1/2 lumped-RC ground model. `None`
            # for all four when the flags were never given, unchanged from
            # every call site that predates them.
            mom_rlc_net=args.mom_rlc_net,
            mom_rlc_resistance_ohm=args.mom_rlc_resistance_ohm,
            mom_rlc_capacitance_ff=args.mom_rlc_capacitance_ff,
            mom_rlc_inductance_nh=args.mom_rlc_inductance_nh,
            # `--matched-group` (issue #1018): declares a set of device
            # instances expected to stay geometrically matched; checked
            # post-extraction for equal `params`. `None` when the flag was
            # never given, unchanged from every call site that predates it.
            matched_device_groups=matched_device_groups,
            # `--def-pins` (issue #1390): the automatic, DEF-merge-aware
            # counterpart to `--pins` -- derives the declared top-level
            # port set from a routed DEF's own `PINS` section instead of
            # requiring a caller to hand-derive it. `None` when the flag
            # was never given, unchanged from every call site that
            # predates it.
            def_pins=def_pins,
        )
    except ExtractError as exc:
        return emit_error("extract", str(exc), args.format)

    emit_success(report, args.format, _print_text)

    return EXIT_OK


def _run_check(args: argparse.Namespace) -> int:
    try:
        if args.rerun:
            result = rerun_extract_report(args.check)
        else:
            result = check_extract_report(args.check)
    except ExtractError as exc:
        return emit_error("extract", str(exc), args.format)

    text_renderer = _print_rerun_text if args.rerun else _print_check_text
    emit_success(result, args.format, text_renderer)

    return EXIT_MATCH if result["status"] == "match" else EXIT_DRIFTED


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
        # Additive (issue #976): only printed when --critical-net was given.
        critical_nets = parasitics.get("critical_nets")
        if critical_nets:
            print(f"critical_nets: {', '.join(critical_nets)}")
        # Additive (issue #977): only printed when --distributed-rc was given.
        if parasitics.get("distributed_rc"):
            distributed_count = sum(
                1 for n in parasitics["nets"] if n.get("rc_model") == "distributed"
            )
            print(f"distributed_rc: {distributed_count} net(s)")
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

    warnings = report["warnings"]
    if warnings:
        print()
        print("warnings:")
        for warning in warnings:
            print(f"  {warning}")


def _print_check_text(result: dict) -> None:
    print(f"report: {result['report']}")
    print(f"status: {result['status']}")
    for check in result["checks"]:
        mark = "OK" if check["match"] else "DRIFTED"
        print(f"  [{mark}] {check['field']}")
        if not check["match"]:
            print(f"      expected: {check['expected']}")
            print(f"      actual:   {check['actual']}")


def _print_rerun_text(result: dict) -> None:
    print(f"report: {result['report']}")
    print(f"status: {result['status']}")
    drift = result["drift"]
    if not drift:
        return
    print()
    print("drift:")
    for entry in drift:
        print(f"  {entry['field']}:")
        print(f"      committed: {entry['committed']!r}")
        print(f"      fresh:     {entry['fresh']!r}")
