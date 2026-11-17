"""Issue #5671: repeated layers in ``floorplan.tracks``.

OpenROAD's repeated ``make_tracks`` on one layer accumulates track patterns,
which is how e.g. the ASAP7 7.5-track row is expressed; only a byte-identical
repeated entry is an error. These cases live in their own module rather than
``test_place_and_route.py`` to stay under that file's size-ratchet ceiling.
"""

from __future__ import annotations

import pytest

from klayout_tools import place_and_route
from klayout_tools.place_and_route import PlaceAndRouteError
from test_place_and_route import (
    _GOOD_TRACK,
    _TRACKS_FLOORPLAN,
    _tech_lef_with_offset,
)

# Issue #5671: a layer may be named by several entries (OpenROAD accumulates
# repeated `make_tracks` on one layer, which is how e.g. the ASAP7 7.5-track
# row is expressed); only a byte-identical repeated entry is an error.

_ASAP7_M2_75_TRACKS = [
    {
        "layer": "M2",
        "x_offset_um": 0.009,
        "x_pitch_um": 0.036,
        "y_offset_um": y_offset,
        "y_pitch_um": 0.270,
    }
    for y_offset in (0.045, 0.081, 0.117, 0.153, 0.189, 0.225, 0.270)
]


def test_seven_entry_repeated_layer_tracks_validate_and_emit_issue_5671():
    from klayout_tools.place_and_route_script import _make_tracks_lines

    floorplan = place_and_route._validate_floorplan(
        {**_TRACKS_FLOORPLAN, "tracks": _ASAP7_M2_75_TRACKS}
    )
    assert _make_tracks_lines(floorplan) == [
        "make_tracks M2 -x_offset 0.009 -x_pitch 0.036 -y_offset 0.045 -y_pitch 0.27",
        "make_tracks M2 -x_offset 0.009 -x_pitch 0.036 -y_offset 0.081 -y_pitch 0.27",
        "make_tracks M2 -x_offset 0.009 -x_pitch 0.036 -y_offset 0.117 -y_pitch 0.27",
        "make_tracks M2 -x_offset 0.009 -x_pitch 0.036 -y_offset 0.153 -y_pitch 0.27",
        "make_tracks M2 -x_offset 0.009 -x_pitch 0.036 -y_offset 0.189 -y_pitch 0.27",
        "make_tracks M2 -x_offset 0.009 -x_pitch 0.036 -y_offset 0.225 -y_pitch 0.27",
        "make_tracks M2 -x_offset 0.009 -x_pitch 0.036 -y_offset 0.27 -y_pitch 0.27",
    ]


def test_repeated_layer_interleaved_with_other_layer_emits_in_order_issue_5671():
    from klayout_tools.place_and_route_script import _make_tracks_lines

    tracks = [
        _ASAP7_M2_75_TRACKS[0],
        {**_GOOD_TRACK, "layer": "M3"},
        _ASAP7_M2_75_TRACKS[6],
    ]
    floorplan = place_and_route._validate_floorplan(
        {**_TRACKS_FLOORPLAN, "tracks": tracks}
    )
    assert _make_tracks_lines(floorplan) == [
        "make_tracks M2 -x_offset 0.009 -x_pitch 0.036 -y_offset 0.045 -y_pitch 0.27",
        "make_tracks M3 -x_offset 0.0 -x_pitch 0.048 -y_offset 0.0 -y_pitch 0.048",
        "make_tracks M2 -x_offset 0.009 -x_pitch 0.036 -y_offset 0.27 -y_pitch 0.27",
    ]


def test_byte_identical_track_entry_names_both_indices_issue_5671():
    with pytest.raises(PlaceAndRouteError) as exc:
        place_and_route._validate_floorplan(
            {**_TRACKS_FLOORPLAN, "tracks": [_GOOD_TRACK, _GOOD_TRACK]}
        )
    message = str(exc.value)
    assert "request.floorplan.tracks[1]" in message
    assert "request.floorplan.tracks[0]" in message


def test_non_adjacent_byte_identical_track_entry_names_indices_issue_5671():
    tracks = [_GOOD_TRACK, {**_GOOD_TRACK, "layer": "M3"}, _GOOD_TRACK]
    with pytest.raises(PlaceAndRouteError) as exc:
        place_and_route._validate_floorplan({**_TRACKS_FLOORPLAN, "tracks": tracks})
    message = str(exc.value)
    assert "request.floorplan.tracks[2]" in message
    assert "request.floorplan.tracks[0]" in message


def test_same_layer_differing_numbers_accepted_issue_5671():
    tracks = [_GOOD_TRACK, {**_GOOD_TRACK, "x_pitch_um": 0.05}]
    out = place_and_route._validate_floorplan({**_TRACKS_FLOORPLAN, "tracks": tracks})
    assert out["tracks"] == tracks


def test_negative_offset_preflight_skips_multi_entry_tracks_issue_5671(tmp_path):
    tech_lef = _tech_lef_with_offset(tmp_path, "-0.27")
    floorplan = {**_TRACKS_FLOORPLAN, "tracks": _ASAP7_M2_75_TRACKS}
    place_and_route.validate_tech_lef_track_offsets(floorplan, tech_lef)
