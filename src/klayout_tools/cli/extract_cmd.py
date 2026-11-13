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

The positional input slot also accepts a **request document** (issue #1867) --
a path to a JSON file, ``-`` for stdin, or an inline JSON object string --
carrying every flag below as a field, so the whole stage can be committed as
one diffable, content-hashable file the way ``klt lvs``/``klt sta``/``klt
synthesize``/``klt place-and-route`` already are. The two forms are **mutually
exclusive**: a request document plus any of this command's own input flags is
a clean application error (exit 1), never a silent override -- see
``cli/_request_document.py`` for why, and ``docs/cli/extract.md``'s "Request
document" section for the schema.

``--check <report>`` (issue #1149) switches ``klt extract`` from running a
fresh extraction into *verifying a previously committed* ``--format json``
report still reproduces -- see ``docs/cli/extract.md``, "--check". The input
path is read from the committed report itself unless the positional ``file``
is also given (issue #2699), which then overrides it unconditionally. Cheap
mode (default): re-hash the input/deck named in the report and compare
against its own recorded ``content_hash`` values, no extraction engine re-run. Full
mode (``--check
<report> --rerun``): actually re-run the extraction the report describes
and diff verdict-bearing fields. Both reuse ``status: "match"`` /
``"drifted"`` and the same 0/3 exit-code split described above (see
``klayout_tools._report_verify``).
"""

import argparse
import os
import sys
from typing import Any

from .. import extract_batch
from .._jsonnum import is_int
from .._paths import looks_like_request_document
from ..extract import (
    REQUEST_SCHEMA,
    ExtractError,
    check_extract_report,
    def_net_instance_pins,
    load_request_arg,
    rerun_extract_report,
    run_extract,
)
from ..extract_runset import DEFAULT_ENGINE, RUNSET_ENGINE
from . import _request_document as reqdoc
from ._parsing import (
    parse_deck_options,
    parse_declared_pins,
    parse_device_models,
    parse_label_layers,
    parse_probes,
)
from .extract_text import _print_text
from .output import (
    EXIT_USAGE_ERROR,
    emit_error,
    emit_success,
    fail_on_unmodelled_exit,
    render_check_text,
    render_rerun_drift,
)

EXIT_OK = 0
EXIT_DRIFTED = 3
#: Alias for the --check/--rerun "still consistent" outcome (issue #1149) --
#: same numeric value as a normal run's success exit, just named for the
#: "match"/"drifted" vocabulary those modes report under.
EXIT_MATCH = EXIT_OK


def _parse_deck_vars(raw: list[str] | None) -> dict[str, str] | None:
    """``--deck-var NAME=VALUE`` (repeatable) -> ``{NAME: VALUE}`` (issue
    #5481). ``None`` when the flag was never given, so the runset engine can
    tell "no vars" from "an empty mapping was requested"."""
    return parse_deck_options(raw, ExtractError, flag="--deck-var")


def _parse_deck_options(raw: list[str] | None) -> dict[str, str] | None:
    """``--deck-option`` (issue #595, repeatable) as a ``dict``, or ``None``
    when the flag was never given -- this command's :class:`ExtractError`
    binding of the shared :func:`.._parsing.parse_deck_options` helper
    ``klt pex`` shares (issue #1558)."""
    return parse_deck_options(raw, ExtractError)


def _parse_label_layers(
    raw: list[str] | None,
) -> dict[str, tuple[int, int] | None] | None:
    """``--label-layer`` (issue #2656, repeatable) as the ``{role: (layer,
    datatype) | None}`` mapping ``run_extract`` takes, or ``None`` when the
    flag was never given -- this command's :class:`ExtractError` binding of
    the shared :func:`.._parsing.parse_label_layers` helper."""
    return parse_label_layers(raw, ExtractError)


def _parse_device_models(raw: list[str] | None) -> dict[str, str] | None:
    """``--device-model`` (issue #4234, repeatable) as the ``{class: model}``
    mapping ``run_extract`` takes, or ``None`` when the flag was never given."""
    return parse_device_models(raw, ExtractError)


def _parse_declared_pins(raw: str | None) -> frozenset[str] | None:
    """``--pins`` (issue #514) as a ``frozenset``, or ``None`` when the flag
    was omitted entirely -- this command's :class:`ExtractError` binding of
    the shared :func:`.._parsing.parse_declared_pins` helper ``klt pex``
    shares (issue #1558)."""
    return parse_declared_pins(raw, ExtractError)


def _parse_pin_source_cells(raw: str | None) -> frozenset[str] | None:
    """Parse the ``--pin-source-cells`` flag's comma-separated value (issue
    #1513) into a ``frozenset`` of cell names, or ``None`` when the flag was
    omitted entirely (skips the probe-based declared-pin reconciliation).

    Mirrors :func:`_parse_declared_pins`'s own comma-separated convention
    and blank-token rejection -- these are cell *names* rather than net
    names, but the "flag given with nothing usable in it" mistake is the
    same shape either way.
    """
    if raw is None:
        return None
    names = frozenset(name.strip() for name in raw.split(",") if name.strip())
    if not names:
        raise ExtractError(
            "--pin-source-cells was given but contains no non-empty name "
            f"(got {raw!r}) -- pass a comma-separated list of cell names, "
            "e.g. --pin-source-cells routing__interconnect_top"
        )
    return names


def _parse_hierarchical_cells(raw: str | None) -> tuple[str, ...]:
    """Parse the ``--hierarchical-cells`` flag's comma-separated value (issue
    #2722) into a tuple of cell names, ``()`` when the flag was omitted."""
    if raw is None:
        return ()
    names = tuple(name.strip() for name in raw.split(",") if name.strip())
    if not names:
        raise ExtractError(
            "--hierarchical-cells was given but contains no non-empty name "
            f"(got {raw!r}) -- pass a comma-separated list of cell names, "
            "e.g. --hierarchical-cells stage_a,stage_b"
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


def _parse_resistance_paths(
    raw: list[list[str]] | None,
) -> list[tuple[int, str, str]] | None:
    """Parse the repeatable ``--resistance-path NET_ID FROM TO`` triples
    (issue #2765) into ``(net_id, from, to)`` tuples, or ``None`` when the
    flag was never given. ``NET_ID`` must be an integer
    ``parasitics.nets[].net_id``; ``FROM``/``TO`` are exact emitted node IDs
    (``terminals[].leg_net``), passed through verbatim.
    """
    if raw is None:
        return None
    queries: list[tuple[int, str, str]] = []
    for net_id_raw, source, target in raw:
        try:
            net_id = int(net_id_raw)
        except ValueError:
            raise ExtractError(
                f"--resistance-path NET_ID must be an integer "
                f"parasitics.nets[].net_id (got {net_id_raw!r})"
            ) from None
        queries.append((net_id, source, target))
    return queries


def _request_probes(request: dict) -> list[Any] | None:
    """The request document's ``probes`` -- an array of ``--probe`` spec
    strings and/or ``{"name", "layer", "x", "y"}`` objects (issue #2986),
    handed to the shared :func:`.._parsing.parse_probes` unchanged."""
    value = request.get("probes")
    if value is None:
        return None
    if not isinstance(value, list):
        raise ExtractError(
            "`klt extract` request field 'probes' must be an array of probe "
            f"objects or spec strings (got {type(value).__name__})"
        )
    return value


def _request_resistance_paths(request: dict) -> list[list[str]] | None:
    """The request document's ``resistance_paths`` -- an array of
    ``{"net_id": <int>, "from": <str>, "to": <str>}`` objects -- normalized
    into the ``[[NET_ID, FROM, TO], ...]`` argv encoding the repeatable
    ``--resistance-path`` flag accumulates (issue #2765), so both forms run
    through :func:`_parse_resistance_paths` unchanged."""
    value = request.get("resistance_paths")
    if value is None:
        return None
    expected = 'an array of {"net_id": int, "from": str, "to": str} objects'
    if not isinstance(value, list):
        raise ExtractError(
            f"`klt extract` request field 'resistance_paths' must be {expected}"
        )
    triples: list[list[str]] = []
    for entry in value:
        if (
            not isinstance(entry, dict)
            or set(entry) != {"net_id", "from", "to"}
            or isinstance(entry["net_id"], bool)
            or not isinstance(entry["net_id"], int)
            or not isinstance(entry["from"], str)
            or not isinstance(entry["to"], str)
        ):
            raise ExtractError(
                f"`klt extract` request field 'resistance_paths' must be "
                f"{expected} (got entry {entry!r})"
            )
        triples.append([str(entry["net_id"]), entry["from"], entry["to"]])
    return triples


def _request_int(request: dict, key: str) -> int | None:
    """``request[key]`` as an integer, or ``None`` when absent/``null``
    (``bool`` and non-integral numbers are rejected)."""
    value = request.get(key)
    if value is None:
        return None
    if not is_int(value):
        raise ExtractError(
            f"`klt extract` request field {key!r} must be an integer (got "
            f"{type(value).__name__}: {value!r})"
        )
    return value


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


#: Every top-level field a ``klt extract`` request document may carry (issue
#: #1867), mapped to the argparse ``dest`` it stands in for -- one entry per
#: flag this command accepts, plus the positional ``file``. Every field name
#: is its ``dest`` verbatim (the repeatable flags already carry plural
#: ``dest``s: ``--critical-net`` -> ``critical_nets``, ``--matched-group`` ->
#: ``matched_groups``). ``tests/test_extract.py`` asserts this covers the
#: subparser's whole flag surface, so a flag added later cannot silently
#: become unreachable from the request form.
_REQUEST_FIELD_DESTS = {
    # `deck_vars` is the one request key whose argv dest differs from its own
    # spelling (`--deck-var` accumulates into `deck_var`), so it is folded in
    # explicitly below rather than through the identity comprehension.
    **{"deck_vars": "deck_var"},
    **{
        name: name
        for name in (
            "file",
            "deck",
            "output",
            "no_netlist",
            "progress",
            "top",
            "pdk",
            "pdk_root",
            "parasitics",
            "mom_net",
            "substrate_spreading",
            "spef",
            "critical_nets",
            "parasitics_nets",
            "critical_net_patterns",
            "critical_net_all",
            "critical_net_excludes",
            "parasitics_net_patterns",
            "strict_net_patterns",
            "fail_on_unmodelled",
            "parasitic_variants",
            "parasitics_top_cell_only",
            "resistance_graph",
            "resistance_graph_r_cut",
            "distributed_rc",
            "substrate_tie_ohms",
            "resistance_paths",
            "resistance_path_max_nodes",
            "resistance_path_max_resistors",
            "mom_rlc_net",
            "mom_rlc_resistance_ohm",
            "mom_rlc_capacitance_ff",
            "mom_rlc_inductance_nh",
            "def_net_names",
            "def_net_connections",
            "top_cell_pins",
            "pins",
            "def_pins",
            "rename_declared_pins",
            "tie_declared_pins",
            "pin_source_cells",
            "deck_options",
            "label_layers",
            "device_models",
            "probes",
            "defer_resistor_fixed_offset",
            "abstract_cells",
            "abstract_cell_lef",
            "subcircuit",
            "subcircuit_output",
            "hierarchical_cells",
            "matched_groups",
            "engine",
            "deck_file",
            "runset_timeout",
        )
    },
}


def _apply_request(args: argparse.Namespace) -> argparse.Namespace:
    """Load the request document in ``args.file`` and return a namespace with
    its fields in place of the argv flags they mirror (issue #1867).

    Relative paths inside the document (``file``, ``output``, ``spef``,
    ``def_pins``, ``def_net_connections``, ``abstract_cell_lef``,
    ``subcircuit_output``,
    ``pdk_root``) resolve against the document's own directory for the file
    form, or the current working directory for the stdin/inline forms --
    ``klt lvs``'s convention, implemented by the same shared helper.

    Structured fields are normalized back into the exact argv encodings
    (``deck_options`` -> ``["KEY=VALUE", ...]``, ``matched_groups`` ->
    ``["NAME=A,B", ...]``, ``pins`` -> ``"A,B"``) rather than short-circuiting
    into ``run_extract``'s parameters directly, so the document form runs
    through this module's own ``_parse_*`` validation unchanged and both
    forms produce byte-identical reports by construction.
    """
    request, base_dir = load_request_arg(args.file)
    reqdoc.check_schema(
        request, expected=REQUEST_SCHEMA, verb="extract", error_cls=ExtractError
    )
    reqdoc.check_known_fields(
        request, tuple(_REQUEST_FIELD_DESTS), verb="extract", error_cls=ExtractError
    )
    reqdoc.reject_argv_flags(args, verb="extract", error_cls=ExtractError)

    def _str(key: str) -> str | None:
        return reqdoc.get_str(request, key, verb="extract", error_cls=ExtractError)

    def _path(key: str) -> str | None:
        return reqdoc.get_path(
            request, key, base_dir=base_dir, verb="extract", error_cls=ExtractError
        )

    def _bool(key: str) -> bool:
        return reqdoc.get_bool(request, key, verb="extract", error_cls=ExtractError)

    def _number(key: str) -> float | None:
        return reqdoc.get_number(request, key, verb="extract", error_cls=ExtractError)

    def _str_list(key: str, *, paths: bool = False) -> list[str] | None:
        return reqdoc.get_str_list(
            request,
            key,
            verb="extract",
            error_cls=ExtractError,
            base_dir=base_dir if paths else None,
        )

    def _comma_joined(key: str) -> str | None:
        return reqdoc.get_comma_joined(
            request, key, verb="extract", error_cls=ExtractError
        )

    return argparse.Namespace(
        **{
            **vars(args),
            "file": _path("file"),
            "deck": _str("deck"),
            # `--engine`/`--deck-file`/`--deck-var`/`--runset-timeout` (issue
            # #5481). `engine` keeps the argv default when absent, so a
            # document that predates the key selects the curated engine.
            "engine": _str("engine") or args.engine,
            "deck_file": _path("deck_file"),
            "deck_var": reqdoc.get_str_map_as_pairs(
                request,
                "deck_vars",
                verb="extract",
                flag="--deck-var",
                error_cls=ExtractError,
            ),
            "runset_timeout": _number("runset_timeout"),
            "output": _path("output"),
            "no_netlist": _bool("no_netlist"),
            # Tri-state like the flag: absent keeps the stderr-is-not-a-TTY
            # auto default (issue #3181).
            "progress": _bool("progress") if "progress" in request else None,
            "top": _str("top"),
            "pdk": _str("pdk"),
            "pdk_root": _path("pdk_root"),
            "parasitics": _bool("parasitics"),
            "mom_net": _str("mom_net"),
            "substrate_spreading": _str("substrate_spreading"),
            "spef": _path("spef"),
            "critical_nets": _str_list("critical_nets"),
            "parasitics_nets": _str_list("parasitics_nets"),
            "critical_net_patterns": _str_list("critical_net_patterns"),
            "critical_net_all": _bool("critical_net_all"),
            "critical_net_excludes": _str_list("critical_net_excludes"),
            "parasitics_net_patterns": _str_list("parasitics_net_patterns"),
            "strict_net_patterns": _bool("strict_net_patterns"),
            "fail_on_unmodelled": _bool("fail_on_unmodelled"),
            "parasitic_variants": _str_list("parasitic_variants"),
            "parasitics_top_cell_only": _bool("parasitics_top_cell_only"),
            "resistance_graph": _bool("resistance_graph"),
            "resistance_graph_r_cut": _number("resistance_graph_r_cut"),
            "distributed_rc": _bool("distributed_rc"),
            "substrate_tie_ohms": _number("substrate_tie_ohms"),
            "resistance_paths": _request_resistance_paths(request),
            "resistance_path_max_nodes": _request_int(
                request, "resistance_path_max_nodes"
            ),
            "resistance_path_max_resistors": _request_int(
                request, "resistance_path_max_resistors"
            ),
            "mom_rlc_net": _str("mom_rlc_net"),
            "mom_rlc_resistance_ohm": _number("mom_rlc_resistance_ohm"),
            "mom_rlc_capacitance_ff": _number("mom_rlc_capacitance_ff"),
            "mom_rlc_inductance_nh": _number("mom_rlc_inductance_nh"),
            "def_net_names": _bool("def_net_names"),
            "def_net_connections": _path("def_net_connections"),
            "top_cell_pins": _bool("top_cell_pins"),
            "pins": _comma_joined("pins"),
            "def_pins": _path("def_pins"),
            "rename_declared_pins": _bool("rename_declared_pins"),
            "tie_declared_pins": _bool("tie_declared_pins"),
            "pin_source_cells": _comma_joined("pin_source_cells"),
            "deck_options": reqdoc.get_str_map_as_pairs(
                request,
                "deck_options",
                verb="extract",
                flag="--deck-option",
                error_cls=ExtractError,
            ),
            "label_layers": reqdoc.get_label_layer_map_as_pairs(
                request,
                "label_layers",
                verb="extract",
                flag="--label-layer",
                error_cls=ExtractError,
            ),
            "device_models": reqdoc.get_str_map_as_pairs(
                request,
                "device_models",
                verb="extract",
                flag="--device-model",
                error_cls=ExtractError,
            ),
            "probes": _request_probes(request),
            "defer_resistor_fixed_offset": _bool("defer_resistor_fixed_offset"),
            "abstract_cells": _str_list("abstract_cells"),
            "abstract_cell_lef": _str_list("abstract_cell_lef", paths=True),
            "subcircuit": _str("subcircuit"),
            "subcircuit_output": _path("subcircuit_output"),
            "hierarchical_cells": _comma_joined("hierarchical_cells"),
            "matched_groups": reqdoc.get_group_map_as_pairs(
                request,
                "matched_groups",
                verb="extract",
                flag="--matched-group",
                error_cls=ExtractError,
            ),
        }
    )


def _refuse_batch_for_runset_engine(args: argparse.Namespace) -> None:
    """Issue #5481: the runset engine needs the standalone ``klayout``
    application and the PDK install on the *executing* host, and the batch
    runner's request can ship neither -- so refuse rather than submit a job
    that cannot honour the engine that was asked for."""
    if getattr(args, "engine", DEFAULT_ENGINE) != RUNSET_ENGINE:
        return
    raise ExtractError(
        f"--backend batch cannot run engine '{RUNSET_ENGINE}' -- the runset "
        "needs the standalone klayout application and the PDK install on the "
        "executing host. Run it locally (--backend local)"
    )


def run(args: argparse.Namespace) -> int:
    # `file` or `--check` is required -- omitting both is a usage error
    # (exit 2) enforced by `parser._require_one_input` before `run()` is ever
    # called. `file` may accompany `--check` (issue #2699) as an explicit
    # input override.
    if args.check is not None:
        if getattr(args, "backend", None) == "batch":
            return emit_error(
                "extract",
                "--backend batch only runs fresh extractions; --check "
                "re-verifies a committed report on this host",
                args.format,
            )
        return _run_check(args)

    if args.rerun:
        return emit_error("extract", "--rerun requires --check <report>", args.format)

    try:
        # `--backend` > $KLT_EXTRACT_BACKEND > local (issue #4613). `batch`
        # never falls back to a local run: every batch failure is exit 1.
        if extract_batch.resolve_backend(getattr(args, "backend", None)) == "batch":
            _refuse_batch_for_runset_engine(args)
            report = _run_batch(args)
            emit_success(report, args.format, _print_text)
            return EXIT_OK
        # Issue #1867: the positional slot carries either a layout path or a
        # request document; `_apply_request` normalizes the latter into the
        # same namespace fields the argv form produces, so everything below
        # sees exactly one shape and the two forms cannot drift.
        if args.file is not None and looks_like_request_document(args.file):
            args = _apply_request(args)
        # `--deck` names the *curated* deck; the runset engine is selected by
        # `--deck-file` instead and refuses `--deck` by name (issue #5481).
        if not args.deck and args.engine != RUNSET_ENGINE:
            raise ExtractError("argument --deck is required")
        if args.parasitic_variants and not args.parasitics:
            # Usage error (exit 2), caught here rather than in `run_extract`
            # so a request document gets the same exit code as argv.
            return emit_error(
                "extract",
                "--parasitic-variant requires --parasitics",
                args.format,
                exit_code=EXIT_USAGE_ERROR,
            )
        declared_pins = _parse_declared_pins(args.pins)
        def_pins = _parse_def_pins(args.def_pins)
        pin_source_cells = _parse_pin_source_cells(args.pin_source_cells)
        hierarchical_cells = _parse_hierarchical_cells(args.hierarchical_cells)
        deck_options = _parse_deck_options(args.deck_options)
        deck_vars = _parse_deck_vars(args.deck_var)
        label_layers = _parse_label_layers(args.label_layers)
        device_models = _parse_device_models(args.device_models)
        probes = parse_probes(args.probes, ExtractError)
        def_net_connections = _parse_def_net_connections(
            args.def_net_connections, spef_output=args.spef
        )
        matched_device_groups = _parse_matched_groups(args.matched_groups)
        resistance_paths = _parse_resistance_paths(args.resistance_paths)
        report = run_extract(
            args.file,
            args.deck,
            output=args.output,
            no_netlist=args.no_netlist,
            # `--progress` (issue #3181): stage lines + heartbeat on stderr.
            # Unset means "heartbeat only, and only when stderr is not a
            # TTY": an agent or CI run gets liveness on a long extraction,
            # while a short run keeps stderr clean (the error envelope lives
            # there) and an interactive shell stays quiet.
            progress=(
                args.progress
                if args.progress is not None
                else ("heartbeat" if not sys.stderr.isatty() else False)
            ),
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
            # `--label-layer` (issue #2656): reads net/pin name text for one
            # label role off a different GDS purpose than this deck's own
            # curated default. `None` when the flag was never given,
            # unchanged from every call site that predates it.
            label_layers=label_layers,
            # `--device-model` (issue #4234): caller-named PDK model for a
            # bipolar class with no curated binding. `None` when never given.
            device_models=device_models,
            # `--probe` (issue #2986): point-to-net queries answered on this
            # run's own extraction. `[]` when the flag was never given.
            probes=probes,
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
            # `--substrate-spreading` (issue #2561): two-terminal closed-form
            # estimate of the bulk resistance between this net's substrate
            # taps. `None` when the flag was never given, unchanged from
            # every call site that predates it.
            substrate_spreading_net=args.substrate_spreading,
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
            # `--parasitics-net` (issue #1700): scopes --parasitics' per-net
            # ground R/C pass onto these net names instead of measuring
            # every net in the design. `None` when the flag was never given,
            # unchanged from every call site that predates it.
            parasitics_nets=args.parasitics_nets,
            # Issue #2974: glob / all-nets selectors resolved against the
            # extracted nets. Defaults leave every earlier call unchanged.
            critical_net_patterns=args.critical_net_patterns,
            critical_net_all=args.critical_net_all,
            critical_net_excludes=args.critical_net_excludes,
            parasitics_net_patterns=args.parasitics_net_patterns,
            strict_net_patterns=args.strict_net_patterns,
            # `--parasitic-variant` (issue #3056): drops R/C elements at
            # injection time. `None` when the flag was never given,
            # unchanged from every call site that predates it.
            parasitic_variants=args.parasitic_variants,
            # `--parasitics-top-cell-only` (issue #1704): additionally
            # splits each net's ground R/C into a top-cell-drawn/instance-
            # drawn share. `False` when the flag was never given, unchanged
            # from every call site that predates it.
            parasitics_top_cell_only=args.parasitics_top_cell_only,
            # `--resistance-graph` (issue #2982): additive opt-in
            # `parasitics.nets[].resistance_graph`. `False` when the flag
            # was never given, unchanged from every call site before it.
            resistance_graph=args.resistance_graph,
            # `--resistance-graph-r-cut` (issue #2983): per-cut via
            # resistance for the graph; `None` when never given.
            resistance_graph_r_cut_ohm=args.resistance_graph_r_cut,
            # `--distributed-rc` (issue #977, Epic #709 Phase 2b): replaces
            # the star/Gamma-shunt R/C model with a distributed ladder for
            # every `--critical-net`-named net. `False` when the flag was
            # never given, unchanged from every call site that predates it.
            distributed_rc=args.distributed_rc,
            # `--substrate-tie-ohms` (issue #3089): `None` when the flag was
            # never given -- the default 1e12 tie.
            substrate_tie_ohms=args.substrate_tie_ohms,
            # `--resistance-path` (issue #2765): shortest series path
            # resistance queries over --distributed-rc ladders. `None` when
            # the flags were never given, unchanged from every call site
            # that predates them.
            resistance_paths=resistance_paths,
            resistance_path_max_nodes=args.resistance_path_max_nodes,
            resistance_path_max_resistors=args.resistance_path_max_resistors,
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
            # `--def-pins` (issue #1390): the DEF-merge-aware counterpart to
            # `--pins` -- derives the declared top-level port set from a
            # routed DEF's `PINS` section. `None` when never given.
            def_pins=def_pins,
            # `--rename-declared-pins` (issue #2854): renames each surviving
            # promoted pin net to its declared name, so the written
            # `.SUBCKT` header carries canonical names. `False` when never given.
            rename_declared_pins=args.rename_declared_pins,
            # `--tie-declared-pins` (issue #4383).
            tie_declared_pins=args.tie_declared_pins,
            # `--pin-source-cells` (issue #1513): the positional, probe-based
            # declared-pin mechanism for a `klt gen-compose`d assembly with no
            # governing top-level DEF -- derives the port set from which labels
            # live inside the named cell(s). `None` when never given.
            pin_source_cells=pin_source_cells,
            # `--subcircuit`/`--subcircuit-output` (issue #2245): additionally
            # writes one named `.SUBCKT <cell>` deck for a sub-cell's own
            # extracted devices, so a post-layout testbench can instantiate a
            # routed block's sub-circuit standalone. `None` when the flags were
            # never given, unchanged from every call site that predates them.
            subcircuit_cell=args.subcircuit,
            subcircuit_output=args.subcircuit_output,
            # `--hierarchical-cells` (issue #2722): opt-in hierarchical
            # `.SUBCKT` emission. `()` when the flag was never given.
            hierarchical_cells=hierarchical_cells,
            # `--engine`/`--deck-file`/`--deck-var`/`--runset-timeout` (issue
            # #5481): the PDK-runset engine. Defaults leave every call that
            # predates them running the curated deck exactly as before.
            engine=args.engine,
            deck_file=args.deck_file,
            deck_vars=deck_vars,
            runset_timeout=args.runset_timeout,
        )
    except ExtractError as exc:
        # `error.code` is set only for classified failures (issue #2761's
        # FinFET geometry refusals); every other error keeps the two-field
        # envelope byte-for-byte.
        return emit_error(
            "extract",
            str(exc),
            args.format,
            code=getattr(exc, "code", None),
            details=getattr(exc, "details", None),
        )

    if (rc := fail_on_unmodelled_exit(report, args)) is not None:  # issue #5388
        return rc

    emit_success(report, args.format, _print_text)

    return EXIT_OK


def _run_batch(args: argparse.Namespace) -> dict[str, Any]:
    """The ``--backend batch`` run (issue #4613): the request document is
    forwarded whole; the argv form forwards ``extract_batch.ARGV_FORWARDED``
    plus ``--deck-option`` and refuses any other flag, since the runner's
    request is built from exactly what is shipped."""
    if args.file is not None and looks_like_request_document(args.file):
        request, base_dir = load_request_arg(args.file)
        reqdoc.check_schema(
            request, expected=REQUEST_SCHEMA, verb="extract", error_cls=ExtractError
        )
        reqdoc.check_known_fields(
            request, tuple(_REQUEST_FIELD_DESTS), verb="extract", error_cls=ExtractError
        )
        reqdoc.reject_argv_flags(args, verb="extract", error_cls=ExtractError)
        output = reqdoc.get_path(
            request, "output", base_dir=base_dir, verb="extract", error_cls=ExtractError
        )
    else:
        flags = getattr(args, "request_argv_flags", None) or {}
        unsupported = sorted(
            option
            for dest, (option, default) in flags.items()
            if getattr(args, dest, default) != default
            and dest not in extract_batch.ARGV_FORWARDED
            and dest not in ("deck_options", "output", "progress")
        )
        if unsupported:
            raise ExtractError(
                f"{', '.join(unsupported)} cannot be forwarded by --backend "
                "batch from the command line; put the run in a request "
                "document (docs/cli/extract.md, 'Request document'), or use "
                "--backend local"
            )
        if not args.deck:
            raise ExtractError("argument --deck is required")
        request = extract_batch.argv_request(
            {
                **vars(args),
                "deck_options": _parse_deck_options(args.deck_options),
            }
        )
        base_dir, output = os.getcwd(), args.output
    if not request.get("deck"):
        raise ExtractError("argument --deck is required")
    return extract_batch.run_extract_batch(
        request,
        base_dir,
        output=output,
        timeout_s=args.timeout_s,
        batch_root=args.batch_root,
        batch_include=args.batch_include or (),
        batch_exclude=args.batch_exclude or (),
    )


def _run_check(args: argparse.Namespace) -> int:
    try:
        if args.rerun:
            result = rerun_extract_report(args.check, input_path=args.file)
        else:
            result = check_extract_report(args.check, input_path=args.file)
    except ExtractError as exc:
        return emit_error("extract", str(exc), args.format)

    text_renderer = render_rerun_drift if args.rerun else render_check_text
    emit_success(result, args.format, text_renderer)

    return EXIT_MATCH if result["status"] == "match" else EXIT_DRIFTED
