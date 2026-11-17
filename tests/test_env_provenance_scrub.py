"""Tests for ``env_provenance.scrub_payload`` (#5828)."""

from __future__ import annotations

import copy

import pytest

from klayout_tools import env_provenance as ep


def test_exported() -> None:
    assert "scrub_payload" in ep.__all__


def test_nested_values_and_keys() -> None:
    payload = {"a": ["/tmp/a/x.gds", {"/tmp/a/k": 1, "n": 3}], "b": None}
    out = ep.scrub_payload(payload, [("/tmp/a", "<WORK>")])
    assert out == {"a": ["<WORK>/x.gds", {"<WORK>/k": 1, "n": 3}], "b": None}


def test_path_boundary() -> None:
    out = ep.scrub_payload(
        ["/tmp/ab/x", "/tmp/a/x", "/q/tmp/a/x", "/tmp/a"], [("/tmp/a", "<T>")]
    )
    assert out == ["/tmp/ab/x", "<T>/x", "/q/tmp/a/x", "<T>"]


def test_trailing_slash_root_normalised() -> None:
    assert ep.scrub_payload("/tmp/a/x", [("/tmp/a/", "<T>")]) == "<T>/x"


def test_ordered_substitutions() -> None:
    subs = [("/tmp/a/deep", "<DEEP>"), ("/tmp/a", "<A>")]
    assert ep.scrub_payload(["/tmp/a/deep/f", "/tmp/a/g"], subs) == [
        "<DEEP>/f",
        "<A>/g",
    ]


def test_input_not_mutated_and_scalars_pass_through() -> None:
    payload = {"p": "/tmp/a/x", "n": 1, "f": 1.5, "t": True, "l": ("/tmp/a/y",)}
    snapshot = copy.deepcopy(payload)
    out = ep.scrub_payload(payload, [("/tmp/a", "<T>")])
    assert payload == snapshot
    assert (out["n"], out["f"], out["t"]) == (1, 1.5, True)
    assert out["l"] == ["<T>/y"]


def test_empty_subs_is_identity_copy() -> None:
    payload = {"p": ["/tmp/a"]}
    out = ep.scrub_payload(payload, [])
    assert out == payload and out is not payload


def test_key_collision_raises() -> None:
    with pytest.raises(ValueError, match="collides"):
        ep.scrub_payload({"/tmp/a/x": 1, "<T>/x": 2}, [("/tmp/a", "<T>")])


def test_empty_root_rejected() -> None:
    with pytest.raises(ValueError):
        ep.scrub_payload("x", [("/", "<T>")])


def test_backslash_in_tag_is_literal() -> None:
    assert ep.scrub_payload("/tmp/a/x", [("/tmp/a", r"<\1>")]) == r"<\1>/x"


def test_round_trip_clears_lint() -> None:
    payload = {
        "gds": "/tmp/run1/x.gds",
        "log": ["/tmp/run1/a", "/var/lib/z"],
        "/tmp/run1/k": 1,
    }
    assert ep.find_absolute_path_fields(payload)
    out = ep.scrub_payload(payload, [("/tmp/run1", "<WORK>"), ("/var/lib/z", "<Z>")])
    assert ep.find_absolute_path_fields(out) == []
