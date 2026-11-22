"""Build an editable ERC supply spec from one routed Sky130 HD response."""

from __future__ import annotations

import os
from typing import Any

from .decks.sky130 import EXTRACTION_DECK as _DECK
from .decks.sky130 import LAYER_NAMES
from .erc_spec import ErcError

_HD = "sky130_fd_sc_hd"
_METALS = tuple(LAYER_NAMES[layer].removesuffix(".drawing") for layer in _DECK.metals)
_LEVELS = ("poly", *_METALS)
_CUTS = ("licon1", "mcon", "via1", "via2", "via3", "via4")


def _object(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ErcError(f"{field} must be a JSON object")
    return value


def _check_profile(report: dict[str, Any]) -> None:
    if report.get("stage_reached") != "route" or report.get("status") != "ok":
        raise ErcError(
            "--pnr-response requires a successful run with stage_reached 'route'"
        )
    if report.get("macros", []) != []:
        raise ErcError("--pnr-response supports one standard-cell block without macros")
    provenance = _object(report.get("provenance"), "provenance")
    variant = _object(provenance.get("pdk"), "provenance.pdk").get("name")
    deck = _object(provenance.get("deck"), "provenance.deck").get("name")
    if variant not in ("sky130A", "sky130B"):
        raise ErcError(
            f"unsupported provenance.pdk.name {variant!r}; expected sky130A or sky130B"
        )
    if (
        not isinstance(deck, str)
        or not deck.startswith(f"{_HD}__")
        or not deck[len(_HD) + 2 :]
    ):
        raise ErcError(
            f"unsupported provenance.deck.name {deck!r}; expected {_HD}__<corner>"
        )
    layer_map = _object(report.get("layer_map"), "layer_map")
    expected_map = {"exact": f"{variant}.map", "family": "sky130.map"}
    resolution = layer_map.get("resolution")
    path = layer_map.get("path")
    if (
        not isinstance(resolution, str)
        or not isinstance(path, str)
        or expected_map.get(resolution) != os.path.basename(path)
    ):
        raise ErcError(
            f"layer_map must identify the resolved {variant}.map or sky130.map"
        )


def _supplies(power: dict[str, Any]) -> list[dict[str, str]]:
    aliases = {}
    for label, field in (("VPWR", "power_net"), ("VGND", "ground_net")):
        name = power.get(field)
        if (
            not isinstance(name, str)
            or not name.strip()
            or name.strip() != name
            or "," in name
        ):
            raise ErcError(f"power.{field} must be a single non-empty layout label")
        aliases[label] = name
    if (
        len(set(aliases.values())) != 2
        or aliases["VPWR"] == "VGND"
        or aliases["VGND"] == "VPWR"
    ):
        raise ErcError(
            "power.power_net and power.ground_net collide with each other "
            "or the opposite library rail"
        )
    native = {"VPWR": "VPWR", "VGND": "VGND"}
    if power.get("library_rail_labels", native) != aliases:
        raise ErcError(
            "power.library_rail_labels must confirm VPWR and VGND map to the "
            "configured nets. Regenerate the response or write the aliases "
            "explicitly in your spec."
        )
    nets = [{"name": name, "kind": "supply"} for name in aliases.values()]
    nets.extend(
        {"name": label, "kind": "supply", "same_net_as": name}
        for label, name in aliases.items()
        if label != name
    )
    return nets


def _check_power(power: dict[str, Any]) -> None:
    if power.get("pdn") is not True or power.get("global_connect") is not True:
        raise ErcError(
            "power.pdn and power.global_connect must be true. "
            "Route with an explicit power grid."
        )
    if power.get("tapcell_master") != f"{_HD}__tapvpwrvgnd_1":
        raise ErcError(
            f"unsupported power.tapcell_master; expected {_HD}__tapvpwrvgnd_1"
        )
    straps = power.get("straps")
    if not isinstance(straps, list) or not straps:
        raise ErcError("power.straps must be a non-empty array of applied straps")
    for index, strap in enumerate(straps):
        layer = _object(strap, f"power.straps[{index}]").get("layer")
        if layer not in _METALS:
            raise ErcError(
                f"power.straps[{index}].layer {layer!r} is outside the Sky130 ladder"
            )


def _layer(pair: tuple[int, int] | None) -> str:
    # These layers are all present in the supported Sky130 extraction deck.
    assert pair is not None
    return f"{pair[0]}/{pair[1]}"


def supply_spec_from_pnr(response: Any) -> dict[str, Any]:
    report = _object(response, "--pnr-response")
    _check_profile(report)
    power = _object(report.get("power"), "power")
    _check_power(power)
    nets = _supplies(power)
    stackup = [
        {
            "name": "poly",
            "role": "gate",
            "layer": _layer(_DECK.poly),
            "active_layer": _layer(_DECK.active),
            "label_layer": _layer(_DECK.poly_label),
        }
    ]
    for name, metal, label in zip(
        _METALS, _DECK.metals, _DECK.metal_labels, strict=True
    ):
        stackup.append(
            {"name": name, "layer": _layer(metal), "label_layer": _layer(label)}
        )
    cuts = (_DECK.contact, *_DECK.vias)
    vias = [
        {"name": name, "layer": _layer(cut), "between": [lower, upper]}
        for name, cut, lower, upper in zip(
            _CUTS, cuts, _LEVELS[:-1], _LEVELS[1:], strict=True
        )
    ]
    return {
        "stackup": stackup,
        "vias": vias,
        "nets": nets,
        "ties": [
            {
                "name": "nwell_tap",
                "well_layer": _layer(_DECK.nwell),
                "tap_layer": _layer(_DECK.tap),
                "tap_is_dedicated": True,
                "connect_to": "li1",
                "through": "licon1",
                "net": power["power_net"],
            }
        ],
    }
