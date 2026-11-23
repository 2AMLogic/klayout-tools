"""Issue #6029 companion tests: the ASAP7 PDN preset's accepted cell-library
names, split out of ``tests/test_place_and_route.py`` to keep that file under
its per-file size-ratchet ceiling (scripts/README.md, "per-file size ratchet").

The ASAP7 ``asap7sc7p5t`` PDN recipe is written against the 7.5-track M1-M6
stack, not one cell flavour: ORFS names its library ``asap7sc7p5t_28`` while
lambdapdk ships the same cells as ``asap7sc7p5t_rvt`` -- another RVT spelling.
Descriptor overlays also use ``asap7sc7p5t_28_R`` (issue #7001).
Accepting only the ORFS name made the preset and that pass mutually
exclusive; either name must validate, unrelated stacks must stay refused.
"""

from __future__ import annotations

import pytest

from klayout_tools import place_and_route
from klayout_tools.place_and_route import PlaceAndRouteError
from test_place_and_route import (
    _EXPECTED_PRESET_CONNECTS,
    _EXPECTED_PRESET_STRAPS,
    _PRESET_CELL_LIBRARIES,
)

#: These two RVT library names select the same physical grid and extraction deck.
_PRESET_ALSO_CELL_LIBRARIES: dict[str, tuple[str, ...]] = {
    "asap7sc7p5t": ("asap7sc7p5t_rvt", "asap7sc7p5t_28_R"),
}


@pytest.mark.parametrize(
    "library",
    [
        _PRESET_CELL_LIBRARIES["asap7sc7p5t"],
        *_PRESET_ALSO_CELL_LIBRARIES["asap7sc7p5t"],
    ],
)
def test_asap7_preset_accepts_each_documented_library(library):
    """Issue #6029: the recipe is written against the ASAP7 7.5-track stack,
    which ORFS and lambdapdk name differently (`asap7sc7p5t_28` vs
    `asap7sc7p5t_rvt`). Either name resolves the same geometry."""
    validated = place_and_route._validate_power({"preset": "asap7sc7p5t"}, library)
    assert validated["preset"] == "asap7sc7p5t"
    assert validated["straps"] == _EXPECTED_PRESET_STRAPS["asap7sc7p5t"]
    assert validated["connects"] == _EXPECTED_PRESET_CONNECTS["asap7sc7p5t"]


@pytest.mark.parametrize("library", _PRESET_ALSO_CELL_LIBRARIES["asap7sc7p5t"])
def test_asap7_preset_and_post_route_spef_compose(library):
    """Both RVT library keys resolve the preset and the ASAP7 extraction deck."""
    validated = place_and_route._validate_power({"preset": "asap7sc7p5t"}, library)
    assert validated["preset"] == "asap7sc7p5t"
    assert place_and_route._require_extract_deck_for_cell_library(library) == "asap7"


def test_asap7_preset_still_rejects_a_non_asap7_cell_library():
    """Accepting the `_rvt` flavour must not open the recipe to unrelated
    stacks; the refusal names every accepted library."""
    with pytest.raises(
        PlaceAndRouteError,
        match=(
            r"request\.power\.preset 'asap7sc7p5t' is a 'asap7sc7p5t_28' "
            r"recipe .* but request\.pdk\.cell_library is 'sky130_fd_sc_hd' "
            r".*accepted: asap7sc7p5t_28, asap7sc7p5t_rvt, asap7sc7p5t_28_R\)$"
        ),
    ):
        place_and_route._validate_power({"preset": "asap7sc7p5t"}, "sky130_fd_sc_hd")


@pytest.mark.parametrize("write_sdf", [False, True])
def test_descriptor_request_passes_both_library_gates(tmp_path, monkeypatch, write_sdf):
    from test_place_and_route import _base_request, _write_request

    library = "asap7sc7p5t_28_R"
    (tmp_path / "gcd_synth.v").write_text("module gcd(input clk); endmodule")
    request = _base_request(
        pdk={"cell_library": library, "corner": "TT"},
        power={"preset": "asap7sc7p5t"},
        post_route_spef=True,
        post_route_sdf=write_sdf,
    )
    path = _write_request(tmp_path / "request.json", request)

    def resolve(cell_library, corner, **kwargs):
        assert (cell_library, corner) == (library, "TT")
        raise PlaceAndRouteError("reached PDK asset resolution")

    monkeypatch.setattr(place_and_route, "_resolve_liberty", resolve)
    with pytest.raises(PlaceAndRouteError, match="reached PDK asset resolution"):
        place_and_route.run_place_and_route(path)


def test_nearby_library_spelling_still_requires_its_own_recipe_and_deck():
    with pytest.raises(PlaceAndRouteError):
        place_and_route._validate_power({"preset": "asap7sc7p5t"}, "asap7sc7p5t_28_L")
    for library in ("asap7sc7p5t_28_L", "asap7sc7p5t_28"):
        with pytest.raises(PlaceAndRouteError):
            place_and_route._require_extract_deck_for_cell_library(library)
