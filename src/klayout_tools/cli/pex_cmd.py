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
from ..output_lock import OutputDirLockedError, OutputLockError
from ..pex import PexError, run_pex
from ..pex_dry_run import run_pex_dry_run
from ._parsing import (
    parse_deck_options,
    parse_declared_pins,
    parse_device_models,
    parse_label_layers,
    parse_tie_nets,
)
from .output import EXIT_USAGE_ERROR, emit_error, emit_success, write_json_file
from .pex_text import (
    _errored_rows_summary,
    _print_dry_run_text,
    _print_submitted_text,
    _print_text,
)

EXIT_PASS = 0
EXIT_ROW_FAILED = 3
EXIT_ROW_ERRORED = 4

#: Namespace fields a ``--submit-only`` run does not record in its manifest
#: (issue #4692): the verb plumbing, the output format, and the async flags
#: themselves -- a bare ``--attach`` supplies its own. ``dry_run`` (issue
#: #5464) is per-invocation plumbing in the same sense: a dry run is a
#: property of the *invocation*, not of the recorded run, and recording it
#: would let a manifest clobber the ``--dry-run`` the caller just typed on a
#: bare ``--attach`` (see ``_rehydrate_bare_attach``).
_REQUEST_EXCLUDED = frozenset(
    {
        "command",
        "func",
        "format",
        "submit_only",
        "attach_manifest",
        "envelope_out",
        "dry_run",
    }
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
    (issue #4692): take the request the ``--submit-only`` run recorded.

    ``_REQUEST_EXCLUDED`` keys are skipped (issue #5464). Current code never
    records them, but a manifest written by an older ``klt`` -- or by hand --
    can still carry one, and those fields describe *this* invocation (the
    output format, the async flags, ``--dry-run``), not the recorded run. A
    stale ``"dry_run": false`` must not overwrite the ``--dry-run`` the caller
    just typed, which would turn the refusal into a real attach.
    """
    request = pex_async.load_request(args.attach_manifest)
    for key, value in request.items():
        if key in _REQUEST_EXCLUDED:
            continue
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
        if getattr(args, "dry_run", False):
            # Issue #5464: a separate entry point, not a `dry_run=` parameter
            # threaded through `run_pex`/`run_extract` -- a dry run performs a
            # strict, engine-free subset of the real run's checks and shares
            # their helpers, so a second control flow through the real path
            # would buy nothing. Returns early: a dry run has no delta rows to
            # grade and writes no `--envelope-out` file (see docs/cli/pex.md).
            dry_report = run_pex_dry_run(
                args.layout,
                args.testbenches,
                args.deck,
                top=args.top,
                pdk_variant=args.pdk,
                pdk_root=args.pdk_root,
                backend=args.backend,
                max_units_per_job=args.max_units_per_job,
                max_concurrent_jobs=args.max_concurrent_jobs,
                hosts=args.hosts,
                resume=args.resume,
                capacity_wait_s=args.wait_for_capacity,
                deck_options=deck_options,
                declared_pins=declared_pins,
                rename_declared_pins=args.rename_declared_pins,
                tie_declared_pins=args.tie_declared_pins,
                label_layers=label_layers,
                tie_nets=tie_nets,
                device_models=device_models,
                critical_nets=args.critical_nets,
                critical_net_patterns=args.critical_net_patterns,
                critical_net_all=args.critical_net_all,
                critical_net_excludes=args.critical_net_excludes,
                distributed_rc=args.distributed_rc,
                resistance_graph=args.resistance_graph,
                resistance_graph_r_cut_ohm=args.resistance_graph_r_cut,
                parasitic_variants=args.parasitic_variants,
                substrate_tie_ohms=args.substrate_tie_ohms,
                mom_rlc_net=args.mom_rlc_net,
                mom_rlc_resistance_ohm=args.mom_rlc_resistance_ohm,
                mom_rlc_capacitance_ff=args.mom_rlc_capacitance_ff,
                mom_rlc_inductance_nh=args.mom_rlc_inductance_nh,
                measure_command=measure_command,
                reference_netlist=args.reference_netlist,
                measure_timeout_s=args.measure_timeout_s,
                flatten_schematic=args.flatten_schematic,
                device_only_leg=args.device_only_leg,
                extract_only=args.extract_only,
                submit_only=args.submit_only,
                attach=args.attach_manifest,
            )
            emit_success(dry_report, args.format, _print_dry_run_text)
            return EXIT_PASS
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
            capacity_wait_s=args.wait_for_capacity,
            # `--deck-option` (issue #1558): selects a flavour of a shared-geometry
            # device family (e.g. gf180mcu's `poly_res`/`mim_cap`), passed to
            # `klt extract --deck-option`; `None` when the flag was never given.
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

    return _emit_report(args, report)


def _emit_report(args: argparse.Namespace, report: dict) -> int:
    """Mirror a completed ``run_pex`` report to ``--envelope-out``, print it
    in the requested format, and grade it into this command's exit code."""
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
