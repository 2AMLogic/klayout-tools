"""``klt pex`` command: serialise the schematic-vs-extracted delta report as
text or JSON.

Output goes through the shared envelope helpers in :mod:`.output`, as with
every other ``klt`` subcommand -- see ``docs/json-contract.md``.

Exit codes (see ``docs/cli/pex.md`` for the full table):
    0 - every delta row passed
    1 - failed to run at all (bad layout/testbench, unresolvable deck/PDK,
        a missing, ambiguous, or implausible `.include` DUT reference
        (issue #1255, Gap 1), disagreeing schematic references, an
        extraction or simulation failure) -- returned by ``emit_error`` as
        ``output.ERROR_EXIT_CODE``
    3 - ran successfully, at least one delta row failed its measurement limit
    4 - at least one delta row errored (schematic or extracted side did not
        produce a trustworthy value) -- including a schematic/extracted
        pin-list mismatch, which reports a named ``pin_count_mismatch``
        block here rather than aborting with exit 1 (issue #1030), and a
        flat-schematic-DUT-vs-`.SUBCKT`-wrapped-extraction mismatch, which
        reports a named ``flat_dut_mismatch`` block the same way (issue
        #1255, Gap 2), and a same-pins-different-order mismatch, which
        reports a named ``pin_order_mismatch`` block the same way (issue
        #2890)
(2 is reserved for argparse usage errors, as with every other ``klt``
subcommand. Exit codes 3/4 mirror `klt sim`'s own precedent -- see
docs/cli/sim.md's "Exit codes" section.)
"""

import argparse
import os
import shlex
import sys

from .. import pex_async
from ..env_provenance import render_path_field
from ..output_lock import OutputDirLockedError, OutputLockError
from ..pex import PexError, run_pex
from ._parsing import (
    parse_deck_options,
    parse_declared_pins,
    parse_device_models,
    parse_label_layers,
    parse_tie_nets,
)

# Issue #2686: the same one-line `label_layers:` text rendering `klt extract`
# prints for its own echo, reused rather than copied.
from .extract_cmd import _print_label_layers
from .output import EXIT_USAGE_ERROR, emit_error, emit_success, write_json_file

EXIT_PASS = 0
EXIT_ROW_FAILED = 3
EXIT_ROW_ERRORED = 4

#: Namespace fields a ``--submit-only`` run does not record in its manifest
#: (issue #4692): the verb plumbing, the output format, and the async flags
#: themselves -- a bare ``--attach`` supplies its own.
_REQUEST_EXCLUDED = frozenset(
    {"command", "func", "format", "submit_only", "attach_manifest", "envelope_out"}
)
#: Request fields holding filesystem paths, stored absolute so a bare
#: ``--attach`` from another working directory resolves them identically.
_REQUEST_PATH_FIELDS = ("layout", "output", "outdir", "pdk_root", "reference_netlist")


def _submit_request(args: argparse.Namespace) -> dict:
    """The invocation's request, as recorded in a ``--submit-only`` manifest
    (issue #4692): every flag that shapes the run, paths made absolute."""
    request = {k: v for k, v in vars(args).items() if k not in _REQUEST_EXCLUDED}
    for key in _REQUEST_PATH_FIELDS:
        if request.get(key):
            request[key] = os.path.abspath(request[key])
    request["testbenches"] = [os.path.abspath(t) for t in args.testbenches]
    return request


def _write_envelope(path: str | None, payload: dict) -> str | None:
    """Write ``payload`` to ``--envelope-out`` (issue #4692); an error message
    when that fails, ``None`` otherwise (or when the flag was not given)."""
    if path is None:
        return None
    try:
        write_json_file(payload, path)
    except OSError as exc:
        return f"cannot write --envelope-out '{path}': {exc}"
    return None


def _fail(
    args: argparse.Namespace,
    message: str,
    *,
    code: str | None = None,
    details: dict | None = None,
    exit_code: int | None = None,
) -> int:
    """``emit_error`` for ``klt pex``, mirroring the envelope to
    ``--envelope-out`` too."""
    error: dict = {"command": "pex", "message": message}
    if code is not None:
        error["code"] = code
    for key, value in (details or {}).items():
        error.setdefault(key, value)
    # Best effort: the original failure is what the caller must see.
    _write_envelope(
        getattr(args, "envelope_out", None), {"schema_version": 1, "error": error}
    )
    kwargs = {} if exit_code is None else {"exit_code": exit_code}
    return emit_error("pex", message, args.format, code=code, details=details, **kwargs)


def _lock_details(exc: OutputLockError) -> dict | None:
    """Structured owner of a refused output directory (issue #4692); ``None``
    for lock errors that carry none. Unknown owner fields are ``null``."""
    if not isinstance(exc, OutputDirLockedError):
        return None
    return {
        "owner_pid": exc.owner_pid,
        "owner_acquired_at": exc.owner_acquired_at,
        "locked_directory": exc.locked_directory,
        "relation": exc.relation,
    }


def _rehydrate_bare_attach(args: argparse.Namespace) -> None:
    """``klt pex --attach MANIFEST`` with no layout, ``--deck`` or testbenches
    (issue #4692): take the request the ``--submit-only`` run recorded."""
    request = pex_async.load_request(args.attach_manifest)
    for key, value in request.items():
        if hasattr(args, key):
            setattr(args, key, value)


def run(args: argparse.Namespace) -> int:
    try:
        if (
            args.attach_manifest
            and args.layout is None
            and args.deck is None
            and not args.testbenches
        ):
            _rehydrate_bare_attach(args)
        missing = [
            flag
            for flag, value in (("layout", args.layout), ("--deck", args.deck))
            if value is None
        ]
        if missing:
            return _fail(
                args,
                f"{' and '.join(missing)} required (only a bare "
                "`--attach MANIFEST` may omit them)",
                exit_code=EXIT_USAGE_ERROR,
            )
        # `--deck-option`/`--pins` (issue #1558): parsed with the same shared
        # helpers `klt extract` uses (`cli/_parsing.py`), bound to this
        # command's own `PexError` so a malformed entry is this command's
        # clean exit-1 error envelope rather than a traceback.
        deck_options = parse_deck_options(args.deck_options, PexError)
        declared_pins = parse_declared_pins(args.pins, PexError)
        # `--label-layer` (issue #2686): the same shared parser `klt extract
        # --label-layer` uses (issue #2656), bound to `PexError`. Role names
        # are validated by the deck during extraction, not here.
        label_layers = parse_label_layers(args.label_layers, PexError)
        # `--tie-net` (issue #4245): `EXTRACTED=SCHEMATIC` pairs; whether the
        # extracted half is a reported unbiased net is checked by `run_pex`.
        tie_nets = parse_tie_nets(args.tie_nets, PexError)
        # `--device-model` (issue #4234): shared parser, bound to `PexError`.
        device_models = parse_device_models(args.device_models, PexError)
        # `--measure-command` (issue #2478): one shell-quoted string on the
        # command line, an argv *list* everywhere below -- `shlex.split`
        # here is the only place the string form exists. The command is
        # never handed to a shell, so quoting is resolved once, visibly,
        # rather than by whatever `/bin/sh` the host happens to have.
        measure_command = _parse_measure_command(args.measure_command)
        report = run_pex(
            args.layout,
            args.testbenches,
            args.deck,
            output=args.output,
            top=args.top,
            pdk_variant=args.pdk,
            pdk_root=args.pdk_root,
            artifacts_dir=args.outdir,
            backend=args.backend,
            max_units_per_job=args.max_units_per_job,
            max_concurrent_jobs=args.max_concurrent_jobs,
            hosts=args.hosts,
            resume=args.resume,
            # `--deck-option` (issue #1558): selects a caller-visible flavour
            # of a shared-geometry device family (e.g. gf180mcu's
            # `poly_res`/`mim_cap`), passed straight through to `klt extract
            # --deck-option`. `None` when the flag was never given, unchanged
            # from every call site that predates it.
            deck_options=deck_options,
            # `--pins` (issue #1558): the declared top-level pin set, passed
            # straight through to `klt extract --pins`. `None` when the flag
            # was never given, unchanged from every call site that predates
            # it.
            declared_pins=declared_pins,
            # `--rename-declared-pins` (issue #2854): renames each surviving
            # promoted pin net to its declared name, passed straight through
            # to `klt extract --rename-declared-pins`. `False` when the flag
            # was never given, unchanged from every call site that predates
            # it.
            rename_declared_pins=args.rename_declared_pins,
            # `--tie-declared-pins` (issue #4383), passed to `klt extract`.
            tie_declared_pins=args.tie_declared_pins,
            # `--label-layer` (issue #2686): which GDS purpose extraction
            # reads net/pin name text from, per label role, passed straight
            # through to `klt extract --label-layer`. `None` when the flag
            # was never given, unchanged from every call site that predates
            # it.
            label_layers=label_layers,
            # `--tie-net` (issue #4245): merges reported-unbiased body nets
            # into schematic nets in the simulated extracted netlist. `None`
            # when the flag was never given, unchanged from before it.
            tie_nets=tie_nets,
            # `--device-model` (issue #4234): passed straight through to
            # `klt extract --device-model`. `None` when never given.
            device_models=device_models,
            # `--critical-net` (issue #976, Epic #709 Phase 2a): scopes the
            # lateral coupling pass onto these net names. `None` when the
            # flag was never given, unchanged from every call site that
            # predates it.
            critical_nets=args.critical_nets,
            # Issue #2974: glob / all-nets forms of --critical-net.
            critical_net_patterns=args.critical_net_patterns,
            critical_net_all=args.critical_net_all,
            critical_net_excludes=args.critical_net_excludes,
            # `--distributed-rc` (issue #977, Epic #709 Phase 2b): replaces
            # the star/Gamma-shunt R/C model with a distributed ladder for
            # every `--critical-net`-named net. `False` when the flag was
            # never given, unchanged from every call site that predates it.
            distributed_rc=args.distributed_rc,
            # `--resistance-graph[-r-cut]` (issue #4037): `False`/`None` when
            # never given, unchanged from every call site that predates them.
            resistance_graph=args.resistance_graph,
            resistance_graph_r_cut_ohm=args.resistance_graph_r_cut,
            # `--substrate-tie-ohms` (issue #3089): `None` when never given.
            substrate_tie_ohms=args.substrate_tie_ohms,
            # `--mom-rlc-net`/`--mom-rlc-resistance-ohm`/
            # `--mom-rlc-capacitance-ff`/`--mom-rlc-inductance-nh` (issue
            # #988, Epic #709 Phase 3a): substitutes a caller-supplied R/L/C
            # for one named net's Phase 1/2 lumped-RC/coupling-C value.
            # `None` for all four when the flags were never given,
            # unchanged from every call site that predates them.
            # `--parasitic-variant` (issue #3056): `None` when the flag was
            # never given, unchanged from every call site that predates it.
            parasitic_variants=args.parasitic_variants,
            mom_rlc_net=args.mom_rlc_net,
            mom_rlc_resistance_ohm=args.mom_rlc_resistance_ohm,
            mom_rlc_capacitance_ff=args.mom_rlc_capacitance_ff,
            mom_rlc_inductance_nh=args.mom_rlc_inductance_nh,
            # `--measure-command`/`--reference-netlist`/
            # `--measure-timeout-s` (issue #2478): measure both legs with a
            # caller-supplied command instead of `klt sim` testbenches.
            # `None` for all three when the flags were never given, which is
            # the exact `run_pex` call every site that predates them makes.
            measure_command=measure_command,
            reference_netlist=args.reference_netlist,
            measure_timeout_s=args.measure_timeout_s,
            # `--flatten-schematic` (issue #2889): flatten the schematic DUT
            # to PDK leaf devices before the schematic-side run. `False`
            # when the flag was never given, unchanged from every call site
            # that predates it.
            flatten_schematic=args.flatten_schematic,
            # `--device-only-leg` (issue #3298): a parasitic-free third leg
            # splitting `delta[]` into representation and parasitic parts.
            # `False` when the flag was never given.
            device_only_leg=args.device_only_leg,
            # `--extract-only` (issue #3739): extract and report, no
            # measurement leg. `False` when the flag was never given.
            extract_only=args.extract_only,
            # `--submit-only` / `--attach` (issue #4619): async batch runs.
            submit_only=args.submit_only,
            attach=args.attach_manifest,
            submit_request=_submit_request(args) if args.submit_only else None,
        )
    except PexError as exc:
        # Issue #2889: a classified error (today: `schematic_flatten_*`)
        # carries `error.code`; every other PexError has `code=None`, so
        # its envelope is byte-identical to before.
        return _fail(args, str(exc), code=exc.code)
    except OutputLockError as exc:
        return _fail(args, str(exc), code=exc.code, details=_lock_details(exc))

    write_error = _write_envelope(args.envelope_out, report)
    if write_error is not None:
        return _fail(args, write_error)

    if report["status"] == "submitted":
        # Issue #4619: submitted, not run -- there is no verdict to grade.
        emit_success(report, args.format, _print_submitted_text)
        return EXIT_PASS

    emit_success(report, args.format, _print_text)

    if report["status"] in {"error", "not_checked"}:
        summary = _errored_rows_summary(report)
        if summary:
            print(summary, file=sys.stderr)
        return EXIT_ROW_ERRORED
    if report["status"] == "fail":
        return EXIT_ROW_FAILED
    return EXIT_PASS


def _print_submitted_text(report: dict) -> None:
    print(f"submitted: {report['leg_count']} legs  manifest: {report['manifest_path']}")
    for leg in report["legs"]:
        print(f"  {leg['testbench']} {leg['side']}: {leg['attach']}")


def _errored_rows_summary(report: dict) -> str | None:
    """One stderr line for a run that exited 4 on errored ``delta[]`` rows
    (issue #3062): the count, the first distinct cause and its stage -- so
    the cause is visible without opening the JSON. ``None`` when no row
    carries an ``error`` object."""
    errored = [row for row in report.get("delta", []) if row.get("error")]
    if not errored:
        return None
    first = errored[0]["error"]
    return (
        f"klt pex: {len(errored)} of {len(report['delta'])} delta rows errored; "
        f"first cause ({first['side']} side, stage {first['stage']}"
        + (f", {first['code']}" if first.get("code") else "")
        + f"): {' '.join(str(first['message']).split())}"
    )


def _parse_measure_command(raw: str | None) -> list[str] | None:
    """``--measure-command``'s shell-quoted string as an argv list (issue
    #2478), or ``None`` when the flag was never given.

    A string that splits to nothing (empty, or only whitespace/quotes) is a
    clean :class:`~klayout_tools.pex.PexError` -- the same exit-1 error
    envelope every other malformed `klt pex` flag produces -- rather than an
    argv list that would fail much later inside :func:`subprocess.run`.
    """
    if raw is None:
        return None
    try:
        argv = shlex.split(raw)
    except ValueError as exc:
        raise PexError(f"--measure-command could not be parsed: {exc}") from exc
    if not argv:
        raise PexError("--measure-command is empty -- name the command to run")
    return argv


def _print_mismatch(mismatch: dict | None, name: str) -> None:
    """Print ``mismatch`` (``pin_count_mismatch``, ``flat_dut_mismatch`` or
    ``pin_order_mismatch`` --
    both share the same structured shape: ``subcircuit``, a ``schematic``
    and ``extracted`` per-side detail dict, ``ngspice_message``, and
    ``detail``) under its own ``<name>:`` heading, or nothing at all when
    ``mismatch`` is ``None``."""
    if mismatch is None:
        return
    print()
    print(f"{name}:")
    for side in ("schematic", "extracted"):
        entry = mismatch[side]
        pins = entry["pins"]
        print(
            f"  {side}: {entry['netlist']}  "
            f"subckt={entry['subcircuit']}  pins={entry['pin_count']}"
            + (f"  ({', '.join(pins)})" if pins else "")
        )
    if mismatch["ngspice_message"]:
        print(f"  ngspice: {mismatch['ngspice_message']}")
    print(f"  {mismatch['detail']}")


def _print_schematic_flatten(block: dict | None) -> None:
    """One ``schematic_flatten:`` line (issue #2889), or nothing when
    ``--flatten-schematic`` was not given."""
    if block is None:
        return
    print(
        f"schematic_flatten: {render_path_field(block['netlist'])}  "
        f"inlined={block['inlined_instances']}  {block['netlist_sha256']}"
    )


def _print_device_only_leg(report: dict) -> None:
    """One ``device_only_netlist:`` line (issue #3298), or nothing when
    ``--device-only-leg`` was not given."""
    if "device_only_netlist" not in report:
        return
    print(
        f"device_only_netlist: {render_path_field(report['device_only_netlist'])}  "
        f"device_cards={report['device_card_count']}  "
        f"match={report['device_cards_match']}"
    )


def _device_only_desc(row: dict) -> str:
    """The ``--device-only-leg`` split of one delta row (issue #3298), or
    nothing for a row that carries none."""
    if "device_only_value" not in row:
        return ""

    def pct(value: float | None) -> str:
        return f"{value:+.3f}%" if value is not None else "-"

    return (
        f"  device_only={row['device_only_value']!r}  "
        f"representation={pct(row['representation_delta_pct'])}  "
        f"parasitic={pct(row['parasitic_delta_pct'])}"
    )


def _print_measurement(measurement: dict) -> None:
    """The text report's ``measurement:`` line(s): ``--extract-only``
    (issue #3739) and ``--measure-command`` runs only."""
    if measurement.get("mode") == "extract-only":
        print("measurement: extract-only  (nothing measured; no delta rows)")
    if measurement.get("mode") == "command":
        print(f"measurement: command  {shlex.join(measurement['command'])}")
        for side, entry in (measurement.get("sides") or {}).items():
            print(
                f"  {side}: {render_path_field(entry['netlist'])}  "
                f"exit={entry['exit_status']}  "
                f"corners={entry['corner_count']}  "
                f"measurements={', '.join(entry['measurement_names'])}"
            )


def _print_text(report: dict) -> None:
    # Issue #1261: `layout`/`netlist`/`reference_netlist` are the
    # `{path, scope}` shape `env_provenance.repo_relative_path` defines --
    # `render_path_field` is the same courtesy-rendering helper `klt
    # env-provenance`'s own text output uses, so a path outside the repo
    # never prints an absolute path here either.
    print(f"layout: {render_path_field(report['layout'])}")
    print(f"netlist: {render_path_field(report['netlist'])}")
    print(f"reference_netlist: {render_path_field(report['reference_netlist'])}")
    # Additive (issue #2889): only printed when --flatten-schematic was given.
    _print_schematic_flatten(report.get("schematic_flatten"))
    # Additive (issue #3298): only printed when --device-only-leg was given.
    _print_device_only_leg(report)
    print(f"status: {report['status']}")
    print(
        f"corners: {report['corner_count']}  "
        f"rows: {len(report['delta'])}  "
        f"passed: {report['passed']}  "
        f"failed: {report['failed']}  "
        f"errored: {report['errored']}"
    )

    extraction = report["extraction"]
    print(
        f"extraction: deck={extraction['deck']}  "
        f"devices={extraction['device_count']}  nets={extraction['net_count']}"
    )
    # Additive (issue #2686): only printed when --label-layer was given.
    _print_label_layers(extraction.get("label_layers"))
    # Additive (issue #976): only printed when --critical-net was given.
    critical_nets = extraction.get("critical_nets")
    if critical_nets:
        print(f"critical_nets: {', '.join(critical_nets)}")
    # Additive (issue #977): only printed when --distributed-rc was given.
    if extraction.get("distributed_rc"):
        print("distributed_rc: on")
    # Additive (issue #4037): only printed when --resistance-graph was given.
    resistance_graph = extraction.get("resistance_graph")
    if resistance_graph is not None:
        graph_nets = [
            n for n in resistance_graph["nets"] if n["resistance_source"] == "graph"
        ]
        print(
            f"resistance_graph: on  graph_nets={len(graph_nets)}/"
            f"{len(resistance_graph['nets'])}"
        )
    # Additive (issue #988): only printed when --mom-rlc-net was given.
    mom_rlc_override = extraction.get("mom_rlc_override")
    if mom_rlc_override is not None:
        print(
            f"mom_rlc_override: net={mom_rlc_override['net']}  "
            f"r_ohm={mom_rlc_override['resistance_ohm']}  "
            f"c_ff={mom_rlc_override['capacitance_ff']}  "
            f"l_nh={mom_rlc_override['inductance_nh']}"
        )

    # Additive (issue #2478): only printed for a `--measure-command` run --
    # a testbench-mode report's `measurement` block carries no information
    # the `testbenches:` section below does not already show.
    measurement = report.get("measurement") or {}
    _print_measurement(measurement)

    # Additive (issue #1030): only printed when the extracted-side deck was
    # rejected for a schematic/extracted top-level pin-list mismatch.
    _print_mismatch(report.get("pin_count_mismatch"), "pin_count_mismatch")
    # Additive (issue #1255, Gap 2): only printed when the schematic DUT is
    # flat (no `.SUBCKT` wrapper) against a `.SUBCKT`-wrapped extraction.
    _print_mismatch(report.get("flat_dut_mismatch"), "flat_dut_mismatch")
    # Additive (issue #2890): only printed when both sides declare the same
    # pins in a different order.
    _print_mismatch(report.get("pin_order_mismatch"), "pin_order_mismatch")

    testbenches = report["testbenches"]
    if testbenches:
        print()
        print("testbenches:")
        for tb in testbenches:
            print(
                f"  {render_path_field(tb['request'])}  corners={tb['corner_count']}  "
                f"measurements={', '.join(tb['measurement_names'])}"
            )

    delta = report["delta"]
    if not delta:
        return

    print()
    print("delta:")
    for row in delta:
        delta_pct = row["delta_pct"]
        delta_desc = f"{delta_pct:+.3f}%" if delta_pct is not None else "-"
        print(
            f"  {row['spec_row']} @ {row['corner_id']}  [{row['status']}]  "
            f"schematic={row['schematic_value']!r}  "
            f"extracted={row['extracted_value']!r}  delta={delta_desc}"
            + _device_only_desc(row)
        )
