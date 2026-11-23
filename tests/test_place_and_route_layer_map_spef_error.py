"""Report unresolved DEF layer mapping when the routed SPEF step fails."""

import pytest

from klayout_tools import place_and_route as pnr
from test_place_and_route import (
    _setup_success_env,
    _stub_merge_def_to_gds,
    _stub_openroad_success,
)


@pytest.mark.parametrize("resolution", ["none", "exact", "family"])
def test_failed_routed_spef_explains_an_unmapped_merge(
    tmp_path, monkeypatch, resolution
):
    request = _setup_success_env(tmp_path, monkeypatch, post_route_spef=True)
    _stub_openroad_success(monkeypatch)
    _stub_merge_def_to_gds(monkeypatch)
    merge = pnr._merge_def_to_gds

    def merge_with_map(**kwargs):
        result = merge(**kwargs)
        result["resolution"] = resolution
        return result

    monkeypatch.setattr(pnr, "_merge_def_to_gds", merge_with_map)
    message = "request.post_route_spef: extractor refused the routed geometry"
    error = pnr.PlaceAndRouteError(
        message, code="extract_failed", job_id="test-job", details={"stage": "extract"}
    )

    def refuse_spef(**kwargs):
        raise error

    monkeypatch.setattr(pnr, "_post_route_spef_metrics", refuse_spef)
    with pytest.raises(pnr.PlaceAndRouteError) as caught:
        pnr.run_place_and_route(request)

    exc = caught.value
    assert exc.code == error.code and exc.job_id == error.job_id
    assert exc.details == error.details
    if resolution == "none":
        (warning,) = pnr._layer_map_warnings({"resolution": resolution})
        assert message in str(exc)
        assert warning in str(exc)
    else:
        assert str(exc) == message
