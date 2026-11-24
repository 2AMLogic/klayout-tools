"""Tests for the request-level validation `klt place-and-route --backend batch`
runs before any upload (issue #7583, `place_and_route_batch._preflight`).

`_preflight` used to gather only what the *upload* needs, so a request defect
that `--backend local` rejects (issue #7538) was packaged, submitted and only
caught inside each remote job -- N per-unit failures, paid for in fleet
capacity. It now runs the shared `_validate_request_level` first.

Reuses `test_place_and_route_batch`'s fake transport (`aws` and the launch
script are replaced, OpenROAD on this host fails the test). The invariant
every refusal asserts: **no transport call was made**.
"""

# ruff: noqa: F401, F811  (the `fake` fixture is imported from the sibling test module)

from __future__ import annotations

import json
from pathlib import Path

import pytest

from klayout_tools import place_and_route, place_and_route_request, sim_batch
from klayout_tools import place_and_route_batch as par_batch
from klayout_tools.place_and_route import PlaceAndRouteError, run_place_and_route
from test_place_and_route_batch import _FakeRunner, fake
from test_place_and_route_corners import CORNERS

pytestmark = pytest.mark.usefixtures("real_build_identity_git")

#: The three request shapes `--backend batch` serves: a rebuild-corner sweep
#: (the fixture's own request), a plain single unit, and the one-element
#: ``rebuild_corners`` list that takes the single-unit path.
SHAPES = {
    "sweep": None,
    "single_unit": {"corner": CORNERS[0]},
    "one_element_list": {"rebuild_corners": [CORNERS[1]]},
}

#: ``(request fields, expected diagnostic)``. The engine is checked first by
#: the validator; the others are reached only by validators *after* it, so
#: together they prove the whole validator runs, not just its head.
DEFECTS = {
    "engine": ({"engine": "siemens-tool"}, "unsupported engine 'siemens-tool'"),
    "target_stage": ({"target_stage": "nope"}, "request.target_stage"),
    "floorplan": ({"floorplan": "square"}, "request.floorplan must be a JSON object"),
    "constraints": (
        {"constraints": "fast"},
        "request.constraints must be a JSON object",
    ),
    "output_dbu_um": ({"output_dbu_um": 0}, "request.output_dbu_um"),
    "post_route_sdf": ({"post_route_sdf": True}, "post_route_sdf requires"),
}


def _rewrite(fake: _FakeRunner, pdk: dict | None = None, **fields) -> str:
    """Rewrite the fixture's request: ``pdk`` (when given) replaces the
    sweep's ``rebuild_corners`` selector, ``fields`` override top-level keys."""
    path = Path(fake.request_path)
    request = json.loads(path.read_text(encoding="utf-8"))
    if pdk is not None:
        request["pdk"] = {"cell_library": "sky130_fd_sc_hd", **pdk}
    request["batch"] = {"poll_interval_s": 0}
    request.update(fields)
    path.write_text(json.dumps(request), encoding="utf-8")
    return str(path)


def _refusal(path: str, backend: str) -> PlaceAndRouteError:
    with pytest.raises(PlaceAndRouteError) as excinfo:
        run_place_and_route(path, backend=backend)
    return excinfo.value


@pytest.mark.parametrize("shape", SHAPES)
@pytest.mark.parametrize("defect", DEFECTS)
def test_request_defect_is_one_error_before_the_first_transport_call(
    fake, defect, shape
):
    fields, message = DEFECTS[defect]
    path = _rewrite(fake, SHAPES[shape], **fields)

    local = _refusal(path, "local")
    batch = _refusal(path, "batch")

    assert message in str(batch)
    # The same diagnostic the local backend gives, not a batch-specific one.
    assert str(batch) == str(local)
    assert fake.calls == []
    assert fake.launched == []


def test_request_defect_wins_over_an_unresolvable_fleet_config(fake, monkeypatch):
    # Error precedence (decided in #7583): the request defect is the more
    # fundamental problem and the one fixed in the request file, so it is
    # reported before any batch-config or packaging problem.
    monkeypatch.delenv(sim_batch.BUCKET_ENV)
    path = _rewrite(fake, target_stage="nope")

    error = _refusal(path, "batch")

    assert "request.target_stage" in str(error)
    assert "job bucket" not in str(error)
    assert fake.calls == []


@pytest.mark.parametrize("shape", ["single_unit", "one_element_list"])
def test_corner_and_rebuild_corners_together_are_still_refused(fake, shape):
    pdk = {**SHAPES[shape], "corner": CORNERS[0], "rebuild_corners": [CORNERS[1]]}
    path = _rewrite(fake, pdk)

    error = _refusal(path, "batch")

    assert "mutually exclusive" in str(error)
    assert fake.calls == []


@pytest.mark.parametrize("shape", SHAPES)
def test_valid_request_submits_without_resolving_the_pdk_on_the_submitter(
    fake, monkeypatch, shape
):
    # The validator must not need anything only the worker has: with every
    # liberty/LEF lookup failing here (the `fake` fixture resolved liberties
    # at setup, so this must be patched explicitly) a valid request submits.
    path = _rewrite(fake, SHAPES[shape])

    def unavailable(*args, **kwargs):
        raise PlaceAndRouteError("PDK resolution attempted on the submitter")

    for module in (place_and_route, place_and_route_request, par_batch):
        monkeypatch.setattr(module, "_resolve_liberty", unavailable, raising=False)
    for module in (place_and_route, place_and_route_request):
        monkeypatch.setattr(module, "_resolve_lef", unavailable, raising=False)

    report = run_place_and_route(path, backend="batch")

    if shape == "sweep":
        assert len(fake.launched) == len(CORNERS)
        assert [unit["status"] for unit in report["corner_runs"]] == ["ok"] * 2
    else:
        assert len(fake.launched) == 1
        assert report["status"] == "ok"
        assert report["environment"]["backend"] == "batch"
