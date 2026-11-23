"""Up-front ``klt extract`` argument validation (issue #5196).

Moved verbatim out of ``run_extract``: the flag-combination checks that run
before any extraction work, in their original order with their original
messages. Returns the values the block derives (frozen net sets, parsed
parasitic variants, net selectors, the substrate-tie resistance).
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from typing import Any, NamedTuple

from ._jsonnum import is_int
from .extract_base import SUBSTRATE_DC_TIE_RESISTANCE_OHM
from .extract_netselect import _build_net_selectors, _selects_critical
from .extract_resistance_graph_caps import validate_resistance_graph_caps
from .extract_spef import ExtractError


class ValidatedExtractArgs(NamedTuple):
    substrate_tie_requested: Any
    critical_nets_set: Any
    parasitics_nets_set: Any
    variants: Any
    net_selectors: Any


#: The ``--report-parasitics`` shapes (issue #5816). ``full`` is the
#: documented default (byte-identical to the pre-#5816 output); ``slim``
#: drops the data a `--spef` file beside the report already carries.
_REPORT_PARASITICS_SHAPES = ("full", "slim")


def _validate_report_parasitics(report_parasitics: Any, spef_output: Any) -> None:
    """``--report-parasitics`` (issue #5816) validation, in a helper so
    :func:`validate_run_extract_args`'s own (baselined) complexity does not
    grow -- the same route ``--resistance-path``'s checks above already
    take.

    The CLI form is argparse-``choices``-constrained, but the request form
    carries the value as a plain string, so the value check runs here where
    both forms meet -- same "a flag naming something invalid is an error,
    not a silent no-op" convention as ``--mom-net`` above. ``slim`` is
    meaningful only beside a SPEF -- its whole point is dropping the JSON
    data the ``--spef`` file already carries -- so it requires ``--spef``,
    the same convention as ``--def-net-connections``.
    """
    if report_parasitics not in _REPORT_PARASITICS_SHAPES:
        raise ExtractError(
            f"--report-parasitics must be 'full' or 'slim' (got {report_parasitics!r})"
        )
    if report_parasitics == "slim" and spef_output is None:
        raise ExtractError("--report-parasitics slim requires --spef")


def _require_parasitics_for_resistance_graph(
    resistance_graph: bool, parasitics: bool
) -> None:
    if resistance_graph and not parasitics:
        raise ExtractError("--resistance-graph requires --parasitics")


def _require_resistance_graph_for_emit(
    emit_selected_resistance: bool, resistance_graph: bool
) -> None:
    """``emit_selected_resistance`` (issue #4037) reads the graph result."""
    if emit_selected_resistance and not resistance_graph:
        raise ExtractError("emit_selected_resistance requires resistance_graph")


def _validate_resistance_graph_r_cut(
    resistance_graph: bool, r_cut_ohm: float | None
) -> None:
    if r_cut_ohm is None:
        return
    if not resistance_graph:
        raise ExtractError("--resistance-graph-r-cut requires --resistance-graph")
    if isinstance(r_cut_ohm, bool) or not (
        isinstance(r_cut_ohm, (int, float))
        and math.isfinite(r_cut_ohm)
        and r_cut_ohm >= 0
    ):
        raise ExtractError(
            "--resistance-graph-r-cut must be a finite number >= 0 (0 = ideal via)"
        )


def validate_run_extract_args(
    *,
    mom_net: Any,
    spef_output: Any,
    report_parasitics: Any,
    substrate_spreading_net: Any,
    substrate_tie_ohms: Any,
    critical_nets: Any,
    parasitics_nets: Any,
    parasitic_variants: Any,
    parasitics_top_cell_only: Any,
    resistance_graph: Any,
    resistance_graph_r_cut_ohm: Any,
    resistance_graph_caps: Any,
    emit_selected_resistance: Any,
    critical_net_patterns: Any,
    critical_net_all: Any,
    critical_net_excludes: Any,
    parasitics_net_patterns: Any,
    strict_net_patterns: Any,
    distributed_rc: Any,
    resistance_paths: Any,
    resistance_path_max_nodes: Any,
    resistance_path_max_resistors: Any,
    mom_rlc_net: Any,
    mom_rlc_resistance_ohm: Any,
    mom_rlc_capacitance_ff: Any,
    mom_rlc_inductance_nh: Any,
    matched_device_groups: Any,
    parasitics: Any,
) -> ValidatedExtractArgs:
    from .extract_parasitics import parse_parasitic_variants

    if mom_net is not None and not parasitics:
        raise ExtractError("--mom-net requires --parasitics")

    # `--spef` (issue #948) requires `--parasitics`, same reasoning as
    # `--mom-net` above: there is no per-net R/C model to translate into SPEF
    # without the lumped-RC pass this flag reuses (see :func:`_write_spef`).
    if spef_output is not None and not parasitics:
        raise ExtractError("--spef requires --parasitics")

    # `--report-parasitics` (issue #5816): validated up front in a helper so
    # this function's own (baselined) complexity does not grow.
    _validate_report_parasitics(report_parasitics, spef_output)

    # `--substrate-spreading` (issue #2561) requires `--parasitics`: its
    # estimate is reported inside the `parasitics` block, alongside the
    # `substrate_dc_tie` shunt whose "two taps on one bulk net are shorted"
    # simplification it exists to quantify -- without the lumped-RC pass
    # there is no such block to report into. Same "a flag naming something
    # invalid is an error, not a silent no-op" convention as `--mom-net`
    # above. Raised from a helper rather than inline so this validation adds
    # no branch to an already heavily-branching function.
    _require_parasitics("--substrate-spreading", substrate_spreading_net, parasitics)

    # `--substrate-tie-ohms` (issue #3089): validated up front, same helper
    # route as above; `substrate_tie_requested` is the default (1e12) when
    # the flag was not given.
    substrate_tie_requested = _resolve_substrate_tie_ohms(
        substrate_tie_ohms, parasitics
    )

    # `--critical-net` (issue #976) requires `--parasitics`: it scopes the
    # lateral-coupling pass onto the same lumped-RC extraction this flag
    # piggybacks on, same reasoning as `--mom-net`/`--spef` above.
    critical_nets_set = frozenset(critical_nets) if critical_nets else None
    if critical_nets_set is not None and not parasitics:
        raise ExtractError("--critical-net requires --parasitics")

    # `--parasitics-net` (issue #1700) requires `--parasitics`: it scopes the
    # lumped-RC ground pass itself, so without that pass there is nothing to
    # scope -- same reasoning as `--critical-net` immediately above.
    parasitics_nets_set = frozenset(parasitics_nets) if parasitics_nets else None
    if parasitics_nets_set is not None and not parasitics:
        raise ExtractError("--parasitics-net requires --parasitics")

    # `--parasitic-variant` (issue #3056) requires `--parasitics`: it edits
    # the injected network, which only exists with that pass.
    variants = parse_parasitic_variants(parasitic_variants, ExtractError)
    if variants is not None and not parasitics:
        raise ExtractError("--parasitic-variant requires --parasitics")

    # `--parasitics-top-cell-only` (issue #1704) requires `--parasitics`:
    # it splits the same lumped-RC ground pass this flag piggybacks on into
    # a top-cell-drawn/instance-drawn share -- same reasoning as
    # `--parasitics-net`/`--critical-net` above.
    if parasitics_top_cell_only and not parasitics:
        raise ExtractError("--parasitics-top-cell-only requires --parasitics")

    # `--resistance-graph` (issue #2982) attaches to the same per-net
    # `parasitics.nets[]` entries, so it needs `--parasitics`.
    _require_parasitics_for_resistance_graph(resistance_graph, parasitics)
    _validate_resistance_graph_r_cut(resistance_graph, resistance_graph_r_cut_ohm)
    # `--resistance-graph-max-*` (issue #6964) raise the builder's own caps;
    # their rules live with the cap table, so both stay one source of truth.
    validate_resistance_graph_caps(resistance_graph, resistance_graph_caps)
    _require_resistance_graph_for_emit(emit_selected_resistance, resistance_graph)

    # `--distributed-rc` (issue #977) requires `--critical-net`: it reuses
    # that flag's own net set as the "which nets get the ladder" scope
    # rather than inventing a second net classification mechanism -- with
    # nothing named there is nothing to scope this onto.
    net_selectors = _build_net_selectors(
        critical_net_patterns,
        critical_net_all,
        critical_net_excludes,
        parasitics_net_patterns,
        strict_net_patterns,
        parasitics=parasitics,
    )
    if (
        distributed_rc
        and not critical_nets_set
        and not _selects_critical(net_selectors)
    ):
        raise ExtractError("--distributed-rc requires --critical-net")

    # `--resistance-path` (issue #2765): validated up front, in a helper so
    # this function's own (baselined) complexity does not grow.
    _validate_resistance_path_args(
        resistance_paths,
        resistance_path_max_nodes,
        resistance_path_max_resistors,
        parasitics=parasitics,
        distributed_rc=distributed_rc,
    )

    # `--mom-rlc-net` (issue #988, Epic #709 Phase 3a): substitutes a
    # caller-supplied R/L/C for one named net's Phase 1/2 lumped-RC model --
    # see `run_extract`'s own `mom_rlc_net` docstring paragraph. Validated up
    # front, same "a flag naming something invalid is an error, not a silent
    # no-op" convention every other opt-in flag above follows.
    mom_rlc_values_given = (
        mom_rlc_resistance_ohm is not None
        or mom_rlc_capacitance_ff is not None
        or mom_rlc_inductance_nh is not None
    )
    if mom_rlc_net is not None and not mom_rlc_values_given:
        raise ExtractError(
            "--mom-rlc-net requires at least one of --mom-rlc-resistance-ohm/"
            "--mom-rlc-capacitance-ff/--mom-rlc-inductance-nh"
        )
    if mom_rlc_net is None and mom_rlc_values_given:
        raise ExtractError(
            "--mom-rlc-resistance-ohm/--mom-rlc-capacitance-ff/"
            "--mom-rlc-inductance-nh require --mom-rlc-net"
        )
    if mom_rlc_net is not None and not parasitics:
        raise ExtractError("--mom-rlc-net requires --parasitics")
    for label, value in (
        ("--mom-rlc-resistance-ohm", mom_rlc_resistance_ohm),
        ("--mom-rlc-capacitance-ff", mom_rlc_capacitance_ff),
        ("--mom-rlc-inductance-nh", mom_rlc_inductance_nh),
    ):
        if value is not None and value < 0:
            raise ExtractError(f"{label} must be >= 0 (got {value!r})")
    if (
        mom_rlc_net is not None
        and distributed_rc
        and critical_nets_set is not None
        and mom_rlc_net in critical_nets_set
    ):
        raise ExtractError(
            f"--mom-rlc-net {mom_rlc_net!r} also names a --distributed-rc "
            "net -- a caller-supplied lumped R/L/C override and a "
            "multi-segment distributed ladder cannot both model the same "
            "net's parasitics at once"
        )

    # `--matched-group` (issue #1018): a declared group needs at least two
    # instance names -- there is nothing to compare with just one, so this is
    # a likely typo rather than a meaningful "declare a group of one"
    # request, matching this module's existing "a flag naming something
    # malformed is an error, not a silent no-op" convention (e.g.
    # `--deck-option`'s own KEY=VALUE validation).
    if matched_device_groups:
        for group_name, instance_names in matched_device_groups.items():
            if len(instance_names) < 2:
                raise ExtractError(
                    f"--matched-group {group_name!r} names "
                    f"{len(instance_names)} instance(s) -- a matched group "
                    "needs at least two instances to compare"
                )
    return ValidatedExtractArgs(
        substrate_tie_requested,
        critical_nets_set,
        parasitics_nets_set,
        variants,
        net_selectors,
    )


def _resolve_substrate_tie_ohms(value: float | None, parasitics: bool) -> float:
    """Validate ``--substrate-tie-ohms`` (issue #3089) and return the
    requested tie resistance, defaulting to
    :data:`SUBSTRATE_DC_TIE_RESISTANCE_OHM` when the flag was not given.

    Raises :class:`ExtractError` when given without ``--parasitics`` (the
    tie only exists in a parasitic netlist) or when not a finite value
    ``>= 0``.
    """
    _require_parasitics("--substrate-tie-ohms", value, parasitics)
    if value is None:
        return SUBSTRATE_DC_TIE_RESISTANCE_OHM
    if not math.isfinite(value) or value < 0:
        raise ExtractError(
            f"--substrate-tie-ohms must be a finite value >= 0 (got {value!r})"
        )
    return float(value)


def _require_parasitics(flag: str, value: Any, parasitics: bool) -> None:
    """Reject ``flag`` when it was given without ``--parasitics``.

    Several flags piggyback on the lumped-RC pass and are meaningless
    without it (``--mom-net``, ``--spef``, ``--critical-net``, …, and
    ``--substrate-spreading`` as of issue #2561), and all raise the same
    ``"<flag> requires --parasitics"`` :class:`ExtractError`. The older ones
    still test for it inline in :func:`run_extract` — deliberately left
    alone, since rewriting them is not this issue's business — but a *new*
    one routes through here instead, so it adds no further branch to a
    function that already carries many.
    """
    if value is not None and not parasitics:
        raise ExtractError(f"{flag} requires --parasitics")


def _validate_resistance_path_args(
    resistance_paths: Sequence[tuple[int, str, str]] | None,
    max_nodes: int | None,
    max_resistors: int | None,
    *,
    parasitics: bool,
    distributed_rc: bool,
) -> None:
    """Up-front validation of ``--resistance-path`` and its limits (issue
    #2765) -- every violation is an :class:`ExtractError`, the same "a flag
    naming something invalid is an error, not a silent no-op" convention
    the other opt-in parasitics flags follow."""
    from .resistance_path import validate_limits

    if not resistance_paths:
        if max_nodes is not None or max_resistors is not None:
            raise ExtractError(
                "--resistance-path-max-nodes/--resistance-path-max-resistors "
                "require --resistance-path"
            )
        return
    if not parasitics:
        raise ExtractError("--resistance-path requires --parasitics")
    if not distributed_rc:
        raise ExtractError("--resistance-path requires --distributed-rc")
    for query in resistance_paths:
        net_id = query[0]
        if not is_int(net_id):
            raise ExtractError(
                f"--resistance-path NET_ID must be an integer "
                f"parasitics.nets[].net_id (got {net_id!r})"
            )
    try:
        validate_limits(
            _resistance_path_limit(max_nodes, "nodes"),
            _resistance_path_limit(max_resistors, "resistors"),
        )
    except ValueError as exc:
        raise ExtractError(
            f"--resistance-path limits: {exc} "
            "(--resistance-path-max-nodes/--resistance-path-max-resistors)"
        ) from exc


def _resistance_path_limit(value: int | None, kind: str) -> int:
    """``value``, or the module default for ``kind`` (``"nodes"``/
    ``"resistors"``) when ``None``."""
    from .resistance_path import DEFAULT_MAX_NODES, DEFAULT_MAX_RESISTORS

    if value is not None:
        return value
    return DEFAULT_MAX_NODES if kind == "nodes" else DEFAULT_MAX_RESISTORS


def _attach_resistance_paths(
    parasitics_report: dict[str, Any],
    resistance_paths: Sequence[tuple[int, str, str]] | None,
    max_nodes: int | None,
    max_resistors: int | None,
) -> None:
    """Answer every ``--resistance-path`` query (issue #2765) over the
    finished ``parasitics_report["nets"]`` and store the results, in request
    order, as ``parasitics_report["resistance_paths"]``. A no-op (the key
    stays absent) when no query was given, so default output is unchanged.

    A malformed emitted ladder would be an internal inconsistency, not a
    caller error -- surfaced as an :class:`ExtractError` rather than an
    answer computed over silently dropped segments.
    """
    if not resistance_paths:
        return
    from .resistance_path import ResistanceGraphError, query_parasitic_net

    nets = parasitics_report["nets"]
    results = []
    for net_id, source, target in resistance_paths:
        try:
            results.append(
                query_parasitic_net(
                    nets,
                    net_id,
                    source,
                    target,
                    max_nodes=_resistance_path_limit(max_nodes, "nodes"),
                    max_resistors=_resistance_path_limit(max_resistors, "resistors"),
                )
            )
        except ResistanceGraphError as exc:
            raise ExtractError(
                f"--resistance-path: net_id {net_id}'s emitted ladder is "
                f"malformed: {exc}"
            ) from exc
    parasitics_report["resistance_paths"] = results
