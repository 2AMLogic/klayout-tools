"""``klt pex --engine klayout-runset`` (issues #6559/#6606): the runset-engine
route.

``klt pex`` always extracted through a curated ``--deck``. For a block whose
core device family only the PDK's own KLayout LVS runset recognises (IHP's
``pnpMPA`` on ``sg13cmos5l``), every ``delta[]`` row of such a run compared
two different circuits. This route extracts the leg with
``klt extract --engine klayout-runset`` instead, so the device set is the
right one.

**What the route is, and is not.** A plain runset leg is a *device-level*
netlist with **no parasitics**: nothing in that leg joins the runset's
devices with the curated deck's parasitics (that needs a net correspondence
between two independently-numbered extractions). ``--deck <curated>`` is
the **merged** route (issue #6606, :mod:`klayout_tools.pex_net_correspondence`):
it also runs that curated deck with parasitics, joins the two extractions by
measured geometric rules, and attaches the curated parasitic network to the
runset devices -- unmatched nets and split/merge disagreements disclosed in
``extraction.net_correspondence``, never silently dropped or merged. A
plain runset leg still does **not** by itself satisfy
``docs/design-evidence-tiers.md`` T1 item 7's intent, and ``klt signoff``
now refuses it for item 7 (``no_parasitics``); every ``extraction`` field
this module adds says which shape the leg is in machine-readable form, and
is present **only** under this engine, so a default-engine envelope is
byte-identical to one produced before the route existed.

What lives here rather than in :mod:`klayout_tools.pex` (size ratchet): the
argument bundle, the ``run_extract`` option mapping, the refusal of the two
``klt pex`` options the extraction-side refusal table cannot see, and the
``extraction`` disclosure fields. Every *other* parasitic-only option is
refused by name inside ``run_extract`` itself (and, on the merged route, by
the correspondence module), from the same
``REFUSED_FLAG_BY_PARAM`` table ``klt extract`` uses -- ``klt pex`` passes
them through unchanged, so the two verbs cannot disagree about what the
runset engine cannot honour.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .extract import run_extract
from .extract_runset_flags import (
    RUNSET_ENGINE,
    parse_deck_vars,
    require_deck_file,
    require_runset_engine_for_deck_vars,
    validate_engine,
)
from .extract_runset_spice import DeviceParams
from .extract_spef import ExtractError
from .pex_errors import PexError


@dataclass(frozen=True)
class PexRunset:
    """The runset-engine arguments of one ``klt pex`` run.

    Its presence on :func:`klayout_tools.pex.run_pex` *is* the engine
    selection; ``None`` (the default) is the curated-deck route, unchanged."""

    deck_file: str
    deck_vars: Mapping[str, str] | None = None
    device_params: DeviceParams | None = None
    runset_timeout: float | None = None


def runset_from_args(
    engine: str,
    deck_file: str | None,
    deck_vars: Mapping[str, str] | None,
    device_params: DeviceParams | None,
    runset_timeout: float | None,
) -> PexRunset | None:
    """The :class:`PexRunset` for ``klt pex``'s ``--engine``/``--deck-file``/
    ``--deck-var``/``--device-param``/``--runset-timeout``, or ``None`` for
    the default engine.

    Spelling, validation and error text are ``klt extract``'s own
    (:mod:`klayout_tools.extract_runset_flags`), so the two verbs cannot drift.
    Raises :class:`ExtractError` for every usage problem -- an unknown engine,
    ``--deck-file`` missing under the runset engine (or given under another),
    a runset-only flag under the default engine, a reserved ``--deck-var``
    name; the CLI turns it into a usage-error envelope.
    """
    validate_engine(engine)
    require_deck_file(engine, deck_file)
    require_runset_engine_for_deck_vars(engine, deck_vars)
    if engine != RUNSET_ENGINE:
        if device_params or runset_timeout is not None:
            raise ExtractError(
                "--device-param/--runset-timeout are only used by engine "
                f"'{RUNSET_ENGINE}' (this run uses '{engine}')"
            )
        return None
    return PexRunset(
        deck_file=deck_file or "",
        deck_vars=parse_deck_vars(dict(deck_vars)) if deck_vars else None,
        device_params=device_params,
        runset_timeout=runset_timeout,
    )


def extract_options(runset: PexRunset | None) -> dict[str, Any]:
    """The ``run_extract`` keyword arguments selecting the runset engine --
    empty for the default route, so that call is exactly the one ``klt pex``
    made before the route existed."""
    if runset is None:
        return {}
    return {
        "engine": RUNSET_ENGINE,
        "deck_file": runset.deck_file,
        "deck_vars": runset.deck_vars,
        "device_params": runset.device_params,
        "runset_timeout": runset.runset_timeout,
    }


def refuse_unsupported(
    runset: PexRunset | None,
    *,
    device_only_leg: bool,
    tie_nets: Sequence[tuple[str, str]] | None,
    resistance_network: bool = False,
) -> None:
    """Refuse, by name, the ``klt pex`` options only a parasitic-annotated
    extraction can serve and ``run_extract``'s own refusal table never sees --
    they are not ``run_extract`` parameters: ``--device-only-leg`` (the
    runset leg *is* already the parasitic-free one) and ``--tie-net`` (it ties
    nets the curated deck reports as unbiased PMOS bodies, which this engine
    never computes), plus ``--resistance-network`` (issue #6899): both runset
    shapes re-emit lumped per-net networks, so a reduced terminal network
    the curated extraction wrote would be silently dropped. Silence would be
    worse than refusal: a caller who believes any of these is in force
    would read the report as if it were.
    """
    if runset is None:
        return
    refused = [
        flag
        for flag, given in (
            ("--device-only-leg", device_only_leg),
            ("--tie-net", tie_nets),
            ("--resistance-network", resistance_network),
        )
        if given
    ]
    if not refused:
        return
    raise PexError(
        f"engine '{RUNSET_ENGINE}' cannot honour {', '.join(refused)} -- "
        f"{'these flags are' if len(refused) > 1 else 'this flag is'} specific "
        "to the curated-deck engine's parasitic-annotated extraction. The "
        "runset engine extracts devices and connectivity only, with no "
        "parasitics; see docs/cli/pex.md, 'Runset engine'"
    )


def extraction_fields(extract_report: Mapping[str, Any]) -> dict[str, Any]:
    """The additive ``extraction`` fields stating what a runset-engine leg is
    and is not -- ``{}`` for any other engine, which keeps that envelope
    byte-identical.

    - ``engine`` -- ``"klayout-runset"``.
    - ``parasitics_carried`` -- ``false`` for a plain runset leg (no R/C
      cards, so a ``delta[]`` over it is a device-representation delta, not
      a parasitic one). ``true`` only for a merged leg
      (``--deck <curated>``, issue #6606) whose correspondence actually
      attached at least one parasitic card; the grading policy this field
      feeds is ``klt signoff``'s ``no_parasitics`` refusal (issue #6606
      AC 4 -- ``signoff_pex_disclosure.parasitic_free_refusal``).
    - ``params_not_carried`` -- per device class, the measured parameters the
      ``X`` cards do **not** carry, so those devices simulate at the PDK
      subcircuit's own defaults. ``{}`` once every parameter is declared with
      ``--device-param``; copied from ``klt extract``'s own report.
    - ``device_counts`` -- per device class, what the runset recognised, so a
      reader can see a family is present (or absent) without the extract JSON.
      Counted over the **instantiated hierarchy** (``klt extract``'s
      ``instantiated_device_counts``): a hierarchical runset's top often holds
      no device itself, and ``klt extract``'s own ``device_counts`` -- the
      selected circuit alone -- would then read ``{}`` for a design full of
      them. Identical to it for a flat runset.
    - ``device_count`` -- overrides the envelope's echo of ``klt extract``'s
      selected-circuit ``device_count``: the total of ``device_counts``, so the
      two never disagree.
    - ``extra_circuits`` -- the circuits written besides the selected one
      (``[]`` for a flat runset). ``net_count`` still describes the selected
      circuit alone; a non-empty list is how a reader knows that.
    - ``net_correspondence`` -- present only on a merged leg (issue #6606):
      ``matched`` / ``unmatched_runset_nets`` / ``unmatched_curated_nets`` /
      ``disagreements`` (``split`` / ``merge``) / ``probe_count`` /
      ``deck`` / ``attached_card_count``. A net that cannot be matched is
      listed here, never silently dropped or silently merged; a connectivity
      disagreement between the two engines is reported, not compared.
    """
    if extract_report.get("engine") != RUNSET_ENGINE:
        return {}
    uncarried = extract_report.get("params_not_carried") or {}
    counts = extract_report.get("instantiated_device_counts")
    if counts is None:  # an extract report predating the hierarchy-aware count
        counts = extract_report.get("device_counts") or {}
    fields: dict[str, Any] = {
        "engine": RUNSET_ENGINE,
        "params_not_carried": {
            name: list(uncarried[name]) for name in sorted(uncarried)
        },
        "device_counts": dict(counts),
        "device_count": sum(counts.values()),
        "extra_circuits": list(extract_report.get("extra_circuits") or []),
    }
    correspondence = extract_report.get("net_correspondence")
    if correspondence is None:
        fields["parasitics_carried"] = False
    else:
        fields["parasitics_carried"] = bool(correspondence.get("attached_card_count"))
        fields["net_correspondence"] = dict(correspondence)
    return fields


def run_extract_leg(
    layout_path: str,
    deck_name: str | None,
    *,
    runset: PexRunset | None,
    output: str | None = None,
    top: str | None = None,
    pdk_variant: str | None = None,
    pdk_root: str | None = None,
    label_layers: Mapping[str, tuple[int, int] | None] | None = None,
    device_models: Mapping[str, str] | None = None,
    deck_options: Mapping[str, str] | None = None,
    declared_pins: frozenset[str] | None = None,
    rename_declared_pins: bool = False,
    tie_declared_pins: bool = False,
    critical_nets: Sequence[str] | None = None,
    critical_net_patterns: Sequence[str] | None = None,
    critical_net_all: bool = False,
    critical_net_excludes: Sequence[str] | None = None,
    distributed_rc: bool = False,
    resistance_graph: bool = False,
    resistance_graph_r_cut_ohm: float | None = None,
    resistance_network: bool = False,
    resistance_graph_caps: Mapping[str, int] | None = None,
    parasitic_variants: Sequence[str] | None = None,
    substrate_tie_ohms: float | None = None,
    mom_rlc_net: str | None = None,
    mom_rlc_resistance_ohm: float | None = None,
    mom_rlc_capacitance_ff: float | None = None,
    mom_rlc_inductance_nh: float | None = None,
) -> dict[str, Any]:
    """``klt pex``'s extracted leg, in whichever of its three shapes this
    run's engine selection asks for (issues #6559/#6606):

    - ``runset is None`` -- the curated-deck engine, ``run_extract`` with
      the parasitics path on: the only shape before the runset route
      existed, and the exact call this module's callers made then.
    - ``runset is not None`` and no ``deck_name`` -- the runset engine's
      device-only leg (``extract_options(runset)``).
    - ``runset is not None`` **and** ``deck_name`` -- the merged route
      (issue #6606): :func:`klayout_tools.pex_merged_leg.merged_runset_extract`
      runs the runset leg *and* a curated parasitic extraction, joins them
      by the geometric net correspondence, and attaches the curated
      parasitic network to the runset devices. ``deck_name``/``label_layers``
      route to the curated leg here; every other option only a richer
      curated parasitic model could serve is refused by name in there.

    Raises :class:`ExtractError` (the caller wraps it into a ``PexError``)
    for every extraction-side failure and usage refusal.
    """
    if runset is not None and deck_name:
        from .pex_merged_leg import merged_runset_extract

        return merged_runset_extract(
            layout_path=layout_path,
            output=output,
            top=top,
            runset=runset,
            deck_name=deck_name,
            pdk_variant=pdk_variant,
            pdk_root=pdk_root,
            label_layers=label_layers,
            device_models=device_models,
            deck_options=deck_options,
            declared_pins=declared_pins,
            rename_declared_pins=rename_declared_pins,
            tie_declared_pins=tie_declared_pins,
            critical_nets=critical_nets,
            critical_net_patterns=critical_net_patterns,
            critical_net_all=critical_net_all,
            critical_net_excludes=critical_net_excludes,
            distributed_rc=distributed_rc,
            resistance_graph=resistance_graph,
            resistance_graph_r_cut_ohm=resistance_graph_r_cut_ohm,
            resistance_graph_caps=resistance_graph_caps,
            parasitic_variants=parasitic_variants,
            substrate_tie_ohms=substrate_tie_ohms,
            mom_rlc_net=mom_rlc_net,
            mom_rlc_resistance_ohm=mom_rlc_resistance_ohm,
            mom_rlc_capacitance_ff=mom_rlc_capacitance_ff,
            mom_rlc_inductance_nh=mom_rlc_inductance_nh,
        )
    return run_extract(
        layout_path,
        deck_name,
        output=output,
        top=top,
        pdk_variant=pdk_variant,
        pdk_root=pdk_root,
        # Issue #6559: the runset engine has no parasitics; every other
        # option passes through and is refused by name by `run_extract`.
        parasitics=runset is None,
        # Issue #1558: `--deck-option`/`--pins` passthrough. `None` for
        # both whenever the flags were never given, which is the exact
        # `run_extract` call this mapping made before they existed.
        deck_options=deck_options,
        declared_pins=declared_pins,
        # Issue #2854: `--rename-declared-pins` passthrough. `False`
        # whenever the flag was never given, which is the exact
        # `run_extract` call this mapping made before it existed.
        rename_declared_pins=rename_declared_pins,
        tie_declared_pins=tie_declared_pins,
        # Issue #2686: `--label-layer` passthrough. `None` when the flag
        # was never given -- the exact `run_extract` call made before it.
        label_layers=label_layers,
        device_models=device_models,
        critical_nets=critical_nets,
        critical_net_patterns=critical_net_patterns,
        critical_net_all=critical_net_all,
        critical_net_excludes=critical_net_excludes,
        distributed_rc=distributed_rc,
        # Issue #4037: `False`/`None` whenever the flags were never given,
        # which is the exact `run_extract` call made before they existed.
        resistance_graph=resistance_graph,
        resistance_graph_r_cut_ohm=resistance_graph_r_cut_ohm,
        resistance_graph_caps=resistance_graph_caps,
        emit_selected_resistance=resistance_graph,
        # Issue #6899: the reduced-network emission (the runset shapes are
        # refused by name in `refuse_unsupported` before reaching here).
        emit_resistance_network=resistance_network,
        parasitic_variants=parasitic_variants,
        substrate_tie_ohms=substrate_tie_ohms,
        mom_rlc_net=mom_rlc_net,
        mom_rlc_resistance_ohm=mom_rlc_resistance_ohm,
        mom_rlc_capacitance_ff=mom_rlc_capacitance_ff,
        mom_rlc_inductance_nh=mom_rlc_inductance_nh,
        **extract_options(runset),
    )
