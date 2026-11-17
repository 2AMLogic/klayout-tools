"""Focused regression tests for issue #5577: the netlist-not-found error from
``klt place-and-route`` must hint at ``klt synthesize``'s output layout (the
per-run ``.klt/synthesize/<run_id>/`` directory) and at the ``outputs.dir``
alternative that gives the netlist a stable path, so an agent wiring a
committed request pair can self-correct.

These assertions live in their own file rather than in
``test_place_and_route.py`` to stay under that file's per-file size ratchet
(the issue #5577 CI gate); the request helpers are reused from the sibling
suite, following the ``test_extract``-import precedent of
``test_abstract_substrate_pin_lef_merge.py``.
"""

from __future__ import annotations

import pytest

from klayout_tools.place_and_route import PlaceAndRouteError, run_place_and_route
from test_place_and_route import _base_request, _write_request


def test_run_netlist_not_found_hints_synthesize_outputs_dir(tmp_path):
    request_path = _write_request(
        tmp_path / "request.json", _base_request(netlist="nope.v")
    )
    with pytest.raises(
        PlaceAndRouteError, match="netlist not found: nope.v"
    ) as excinfo:
        run_place_and_route(request_path)
    # Issue #5577: the error must name the per-run directory layout and the
    # `outputs.dir` alternative, so the hint cannot silently regress.
    assert ".klt/synthesize/<run_id>/" in str(excinfo.value)
    assert "outputs.dir" in str(excinfo.value)
