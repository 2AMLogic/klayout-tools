"""Source-class accounting across the pinned SKY130 subcircuit rename."""

import pytest

from klayout_tools import signoff_runset
from test_signoff_runset import SKY130_RUNSET_HASH, _sky130_envelope


@pytest.mark.parametrize(
    ("digest", "convert", "subcircuits", "passes"),
    [
        (SKY130_RUNSET_HASH, "true", 1, True),
        (SKY130_RUNSET_HASH, "true", 0, False),  # primitive model, no X call
        (SKY130_RUNSET_HASH, "false", 1, False),
        ("sha256:other", "true", 1, False),
        (SKY130_RUNSET_HASH, "true", None, False),  # source form unknown
        (SKY130_RUNSET_HASH, "true", "1", False),  # malformed source-call count
        (SKY130_RUNSET_HASH, "true", 3, False),  # more calls than source cards
    ],
)
def test_declared_alias_is_scoped_to_converted_calls_in_the_pinned_deck(
    tmp_path, digest, convert, subcircuits, passes
):
    special = "SKY130_FD_PR__SPECIAL_NFET_01V8"
    normal = "SKY130_FD_PR__NFET_01V8"
    envelope = _sky130_envelope(tmp_path / "absent.lvs")
    reference = {special: 1, normal: 2}
    census = {
        "layout": reference,
        "reference": reference,
        "reference_source": {special: 2, normal: 1},
    }
    if subcircuits is not None:
        census["reference_source_subcircuits"] = {special.lower(): subcircuits}
    envelope["device_census"] = census
    envelope["provenance"]["runset"] = {
        "content_hash": digest,
        "deck_vars": {"convert_subckts": convert},
    }

    gaps = signoff_runset.unrecognised_device_gaps(envelope)
    assert (gaps == []) == passes


def test_missing_census_cannot_establish_class_correspondence():
    assert signoff_runset.unrecognised_device_gaps({"coverage": {}}) == [
        "report carries no coverage/device_census to prove recognition"
    ]
