"""Issue #6029 companion tests: the ASAP7 PDN preset's accepted cell-library
names, split out of ``tests/test_place_and_route.py`` to keep that file under
its per-file size-ratchet ceiling (scripts/README.md, "per-file size ratchet").

The ASAP7 ``asap7sc7p5t`` PDN recipe is written against the 7.5-track M1-M6
stack, not one cell flavour: ORFS names its library ``asap7sc7p5t_28`` while
lambdapdk ships the same cells as ``asap7sc7p5t_rvt`` -- the name the
post-route SPEF/SDF pass resolves its extract deck for (issue #4559).
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

#: Issue #6029: additional standard-cell library names a preset also accepts
#: -- flavours of the same routing stack the recipe is equally valid for.
#: The ASAP7 grid is written against the 7.5-track M1-M6 stack, which
#: lambdapdk ships under a second cell-flavour name, `asap7sc7p5t_rvt` --
#: the one the post-route SPEF/SDF pass resolves its extract deck for
#: (issue #4559). Accepting only the transcribed `_28` name made the preset
#: and that pass mutually exclusive; presets absent here keep their single
#: accepted name.
_PRESET_ALSO_CELL_LIBRARIES: dict[str, tuple[str, ...]] = {
    "asap7sc7p5t": ("asap7sc7p5t_rvt",),
}


@pytest.mark.parametrize(
    "library",
    [
        _PRESET_CELL_LIBRARIES["asap7sc7p5t"],
        *_PRESET_ALSO_CELL_LIBRARIES["asap7sc7p5t"],
    ],
)
def test_asap7_preset_accepts_both_asap7_library_names(library):
    """Issue #6029: the recipe is written against the ASAP7 7.5-track stack,
    which ORFS and lambdapdk name differently (`asap7sc7p5t_28` vs
    `asap7sc7p5t_rvt`). Either name resolves the same geometry."""
    validated = place_and_route._validate_power({"preset": "asap7sc7p5t"}, library)
    assert validated["preset"] == "asap7sc7p5t"
    assert validated["straps"] == _EXPECTED_PRESET_STRAPS["asap7sc7p5t"]
    assert validated["connects"] == _EXPECTED_PRESET_CONNECTS["asap7sc7p5t"]


def test_asap7_preset_and_post_route_spef_compose_issue_6029():
    """The gap this issue reports: `power.preset: "asap7sc7p5t"` demanded
    `asap7sc7p5t_28` while `post_route_spef`/`post_route_sdf` only resolved
    an extract deck for `asap7sc7p5t_rvt` -- no name satisfied both, so the
    shipped recipe and the real-parasitics post-route pass were mutually
    exclusive. Both gates now pass for one `asap7sc7p5t_rvt` request."""
    validated = place_and_route._validate_power(
        {"preset": "asap7sc7p5t"}, "asap7sc7p5t_rvt"
    )
    assert validated["preset"] == "asap7sc7p5t"
    assert (
        place_and_route._require_extract_deck_for_cell_library("asap7sc7p5t_rvt")
        == "asap7"
    )


def test_asap7_preset_still_rejects_a_non_asap7_cell_library():
    """Accepting the `_rvt` flavour must not open the recipe to unrelated
    stacks; the refusal names every accepted library."""
    with pytest.raises(
        PlaceAndRouteError,
        match=(
            r"request\.power\.preset 'asap7sc7p5t' is a 'asap7sc7p5t_28' "
            r"recipe .* but request\.pdk\.cell_library is 'sky130_fd_sc_hd' "
            r".*accepted: asap7sc7p5t_28, asap7sc7p5t_rvt\)$"
        ),
    ):
        place_and_route._validate_power({"preset": "asap7sc7p5t"}, "sky130_fd_sc_hd")
