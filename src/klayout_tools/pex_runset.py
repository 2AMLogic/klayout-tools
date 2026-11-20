"""``klt pex --engine klayout-runset`` (issue #6559): the runset-engine route.

``klt pex`` always extracted through a curated ``--deck``. For a block whose
core device family only the PDK's own KLayout LVS runset recognises (IHP's
``pnpMPA`` on ``sg13cmos5l``), every ``delta[]`` row of such a run compared
two different circuits. This route extracts the leg with
``klt extract --engine klayout-runset`` instead, so the device set is the
right one.

**What the route is, and is not.** The extracted leg is a *device-level*
netlist with **no parasitics**: nothing here joins the runset's devices with
the curated deck's parasitics (that needs a net correspondence between two
independently-numbered extractions, which nothing in this repo provides --
issue #6606). A runset-engine ``klt pex`` report therefore does **not** by
itself satisfy ``docs/design-evidence-tiers.md`` T1 item 7's intent; it makes
the gap visible in the envelope instead of unreachable. Every ``extraction``
field this module adds says so in machine-readable form, and is present
**only** under this engine, so a default-engine envelope is byte-identical to
one produced before the route existed.

What lives here rather than in :mod:`klayout_tools.pex` (size ratchet): the
argument bundle, the ``run_extract`` option mapping, the refusal of the two
``klt pex`` options the extraction-side refusal table cannot see, and the
``extraction`` disclosure fields. Every *other* parasitic-only option is
refused by name inside ``run_extract`` itself, from the same
``REFUSED_FLAG_BY_PARAM`` table ``klt extract`` uses -- ``klt pex`` passes
them through unchanged, so the two verbs cannot disagree about what the
runset engine cannot honour.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .extract_runset_flags import (
    RUNSET_ENGINE,
    parse_deck_vars,
    require_deck_file,
    require_runset_engine_for_deck_vars,
    validate_engine,
)
from .extract_runset_spice import DeviceParams
from .extract_spef import ExtractError


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
) -> None:
    """Refuse, by name, the ``klt pex`` options only a parasitic-annotated
    extraction can serve and ``run_extract``'s own refusal table never sees --
    they are not ``run_extract`` parameters: ``--device-only-leg`` (the
    runset leg *is* already the parasitic-free one) and ``--tie-net`` (it ties
    nets the curated deck reports as unbiased PMOS bodies, which this engine
    never computes). Silence would be worse than refusal: a caller who
    believes either is in force would read the report as if it were.
    """
    if runset is None:
        return
    refused = [
        flag
        for flag, given in (
            ("--device-only-leg", device_only_leg),
            ("--tie-net", tie_nets),
        )
        if given
    ]
    if not refused:
        return
    from .pex import PexError  # lazy: `pex` imports this module

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
    - ``parasitics_carried`` -- always ``false`` here: the extracted leg has no
      R/C cards, so a ``delta[]`` over it is a device-representation delta,
      not a parasitic one. Reported, never graded on (policy for a
      parasitic-free leg is issue #6606's).
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
    """
    if extract_report.get("engine") != RUNSET_ENGINE:
        return {}
    uncarried = extract_report.get("params_not_carried") or {}
    counts = extract_report.get("instantiated_device_counts")
    if counts is None:  # an extract report predating the hierarchy-aware count
        counts = extract_report.get("device_counts") or {}
    return {
        "engine": RUNSET_ENGINE,
        "parasitics_carried": False,
        "params_not_carried": {
            name: list(uncarried[name]) for name in sorted(uncarried)
        },
        "device_counts": dict(counts),
        "device_count": sum(counts.values()),
        "extra_circuits": list(extract_report.get("extra_circuits") or []),
    }
