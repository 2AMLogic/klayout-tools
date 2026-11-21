"""``klt extract`` PDK / model-binding resolution (issue #5196).

Moved verbatim out of ``run_extract``: the FinFET option refusal, the
``--device-models`` precondition, the ``--pdk``/``--pdk-root`` lookup and the
per-deck device-model binding resolution, in their original order with their
original messages and error types.

``_require_pdk_for_device_models`` still lives in ``extract.py``, which imports
this module at its top level, so it is imported inside the function body (a
module-level ``from .extract import ...`` would be a circular import).
"""

from __future__ import annotations

from typing import Any

from .decks import (
    InvalidDeckOptionError,
    UnknownExtractionDeckError,
    get_extraction_deck,
)
from .extract_spef import ExtractError
from .pdk import PdkNotFoundError, find_pdk
from .pdk_models import DeviceBinding, ModelBindingError, resolve_device_bindings


def resolve_pdk_and_model_bindings(
    *,
    deck_name: Any,
    deck_options: Any,
    pdk_variant: Any,
    pdk_root: Any,
    device_models: Any,
) -> tuple[dict[str, Any] | None, dict[str, DeviceBinding] | None]:
    """Return ``(pdk_info, model_bindings)``; both ``None`` when no PDK resolves."""
    from .extract import _require_pdk_for_device_models

    # Issue #2761: a FinFET deck has no `--pdk` subcircuit binding; name that
    # before the PDK lookup below reports a less precise error. No-op for
    # other decks.
    from .extract_finfet import refuse_run_options

    refuse_run_options(deck_name, pdk_variant=pdk_variant, pdk_root=pdk_root)
    pdk_info: dict[str, Any] | None = None
    # Populated only when a PDK resolves: `{<deck's device class name>:
    # DeviceBinding}` for every device class this deck extracts that has a
    # curated binding (MOS + resistor + capacitor on both decks, plus sky130's
    # bipolar; gf180mcu's bipolar is a documented carve-out and stays absent --
    # see `klayout_tools.pdk_models`). Drives the `X`-card model-binding writer
    # below -- see the module docstring's "--pdk-triggered model binding" note.
    model_bindings: dict[str, DeviceBinding] | None = None
    _require_pdk_for_device_models(device_models, pdk_variant, pdk_root)
    if pdk_variant is not None or pdk_root is not None:
        try:
            pdk_info = find_pdk(variant=pdk_variant, root=pdk_root)
        except PdkNotFoundError as exc:
            raise ExtractError(str(exc)) from exc

        # No `label_layers` (issue #2656) here on purpose: a label-layer
        # override redirects which GDS purpose net/pin *names* are read from
        # and cannot change which device classes this deck recognises, which
        # is the only thing `resolve_device_bindings` reads off the deck.
        try:
            deck_for_models = get_extraction_deck(deck_name, deck_options)
        except (UnknownExtractionDeckError, InvalidDeckOptionError) as exc:
            raise ExtractError(str(exc)) from exc
        try:
            model_bindings = resolve_device_bindings(
                deck_name,
                pdk_info["variant"],
                deck_for_models,
                device_models=device_models,
            )
        except ModelBindingError as exc:
            raise ExtractError(str(exc)) from exc
    return pdk_info, model_bindings
