"""Portable diagnostics remain supported when power audit messages omit host paths."""

import pytest

from klayout_tools.place_and_route_portable import portable_response


@pytest.mark.parametrize("use_pdk", [False, True])
def test_diagnostic_with_embedded_repo_pdk_and_external_paths(tmp_path, use_pdk):
    repo = tmp_path / "design"
    repo.mkdir()
    (repo / ".git").mkdir()
    pdk = tmp_path / "pdk"
    reason = (
        f"Reading {repo}/route/top.def with {pdk}/libs/cells.lib from {pdk} "
        f"while writing {tmp_path}/outside/report.json"
    )
    response = {"reasons": [reason]}

    pdk_source = (
        "$PDK_ROOT/libs/cells.lib from $PDK_ROOT"
        if use_pdk
        else "<external> from <external>"
    )
    assert portable_response(
        response, request_dir=str(repo), pdk_root=str(pdk) if use_pdk else None
    ) == {
        "reasons": [f"Reading route/top.def with {pdk_source} while writing <external>"]
    }
    assert response == {"reasons": [reason]}
