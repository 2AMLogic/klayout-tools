"""Identify caller-supplied SPEF bytes at the scope where STA consumes them."""

import pytest

from klayout_tools import _sta_spef_inputs
from klayout_tools.post_route_sta import PostRouteStaError, run_sta
from test_post_route_sta import (
    _checkout,
    _setup_matrix_env,
    _setup_multi_corner_env,
    _setup_success_env,
    _setup_verilog_success_env,
    _sha256_of,
    _spef_text,
    _stub_openroad_matrix,
    _stub_openroad_multi_corner,
    _stub_openroad_success,
)

pytestmark = pytest.mark.usefixtures("real_build_identity_git")


@pytest.mark.parametrize("shared", [False, True])
def test_scalar_spef_hash_identifies_bytes_at_same_path(tmp_path, monkeypatch, shared):
    _, flows = _checkout(tmp_path)
    spef = flows / "top.spef"
    if shared:
        request = _setup_multi_corner_env(flows, monkeypatch, spef=spef.name)
        _stub_openroad_multi_corner(monkeypatch)
    else:
        request = _setup_success_env(flows, monkeypatch, spef=spef.name)
        _stub_openroad_success(monkeypatch, spef_check=(1, 1, 1, 1))

    for net in ("clk", "reset"):
        spef.write_text(_spef_text(net), encoding="utf-8")
        result = run_sta(request)
        assert result["spef_path"] == {"scope": "repo", "path": "flows/top.spef"}
        assert result["spef_content_hash"] == _sha256_of(spef)
        assert all("spef_content_hash" not in row for row in result.get("corners", []))


def test_matrix_hash_tracks_external_interconnect_input(tmp_path, monkeypatch):
    spefs = {"max": "max.spef", "min": "min.spef"}
    request = _setup_matrix_env(tmp_path, monkeypatch, spefs)
    for key, filename in spefs.items():
        (tmp_path / filename).write_text(_spef_text(key), encoding="utf-8")
    _stub_openroad_matrix(monkeypatch)

    result = run_sta(request)

    assert result["spef_path"] is None and "spef_content_hash" not in result
    assert len(result["corners"]) == 4
    for row in result["corners"]:
        assert row["spef_path"] == {"scope": "external", "path": None}
        assert row["spef_content_hash"] == _sha256_of(
            tmp_path / spefs[row["interconnect_corner"]]
        )


@pytest.mark.parametrize("setup", [_setup_success_env, _setup_verilog_success_env])
def test_no_spef_omits_hash(tmp_path, monkeypatch, setup):
    request = setup(tmp_path, monkeypatch)
    _stub_openroad_success(monkeypatch)
    assert "spef_content_hash" not in run_sta(request)


def test_shared_map_input_is_read_once_for_hash(tmp_path, monkeypatch):
    request = _setup_matrix_env(
        tmp_path, monkeypatch, {"min": "nom.spef", "max": "nom.spef"}
    )
    scripts = _stub_openroad_matrix(monkeypatch)
    hashed = []
    read_digest = _sta_spef_inputs.content_hash_required

    def record_hash(path):
        assert not scripts
        hashed.append(path)
        return read_digest(path)

    monkeypatch.setattr(_sta_spef_inputs, "content_hash_required", record_hash)
    result = run_sta(request)
    assert len(scripts) == 4
    assert hashed == [str(tmp_path / "nom.spef")]
    assert {row["spef_content_hash"] for row in result["corners"]} == {
        _sha256_of(tmp_path / "nom.spef")
    }


def test_external_hash_error_is_scrubbed_before_dispatch(tmp_path, monkeypatch):
    request = _setup_matrix_env(tmp_path, monkeypatch, {"min": "min.spef"})
    scripts = _stub_openroad_matrix(monkeypatch)

    def unreadable(path):
        raise PermissionError(13, "permission denied", path)

    monkeypatch.setattr(_sta_spef_inputs, "content_hash_required", unreadable)
    with pytest.raises(PostRouteStaError, match="could not hash SPEF input") as error:
        run_sta(request)
    assert str(tmp_path) not in str(error.value)
    assert "permission denied" in str(error.value)
    assert scripts == []
