"""Identify caller-supplied SPEF bytes at the scope where STA consumes them."""

import os
import shutil
from pathlib import Path

import pytest

from klayout_tools import _sta_spef_inputs, post_route_sta
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


@pytest.mark.parametrize(
    "spef", ["nom.spef", {"nom": "nom.spef"}], ids=["scalar", "map"]
)
def test_replacing_spef_between_liberty_corners_rejects_run(
    tmp_path, monkeypatch, spef
):
    request = _setup_matrix_env(tmp_path, monkeypatch, spef)
    scripts = _stub_openroad_matrix(monkeypatch)
    run_engine = post_route_sta.run_openroad
    replacement = tmp_path / "new.spef"
    replacement.write_text(_spef_text("rst"), encoding="utf-8")

    def replace_after_first_corner(*args, **kwargs):
        result = run_engine(*args, **kwargs)
        if len(scripts) == 1:
            replacement.replace(tmp_path / "nom.spef")
        return result

    monkeypatch.setattr(post_route_sta, "run_openroad", replace_after_first_corner)
    with pytest.raises(PostRouteStaError, match="SPEF input.*changed") as error:
        run_sta(request)
    assert "<outside repo>" in str(error.value)
    assert str(tmp_path) not in str(error.value)


def _touch(path: Path) -> None:
    stat = path.stat()
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns - 10**9))


def _chmod(path: Path) -> None:
    path.chmod(path.stat().st_mode ^ 0o100)


def _rewrite_identical_bytes(path: Path) -> None:
    copy = path.with_name(f"{path.name}.copy")
    shutil.copyfile(path, copy)
    copy.replace(path)


@pytest.mark.parametrize(
    "mutate",
    [_touch, _chmod, _rewrite_identical_bytes],
    ids=["utime", "chmod", "identical-rewrite"],
)
@pytest.mark.parametrize(
    "spef", ["nom.spef", {"nom": "nom.spef"}], ids=["scalar", "map"]
)
def test_metadata_only_change_between_liberty_corners_keeps_run(
    tmp_path, monkeypatch, spef, mutate
):
    request = _setup_matrix_env(tmp_path, monkeypatch, spef)
    scripts = _stub_openroad_matrix(monkeypatch)
    run_engine = post_route_sta.run_openroad
    target = tmp_path / "nom.spef"
    digest = _sha256_of(target)
    moved = []

    def mutate_after_first_corner(*args, **kwargs):
        result = run_engine(*args, **kwargs)
        if len(scripts) == 1:
            before = _sta_spef_inputs._file_state(str(target))
            mutate(target)
            # Guard against a vacuous pass: the stat tuple must really move.
            moved.append(_sta_spef_inputs._file_state(str(target)) != before)
        return result

    monkeypatch.setattr(post_route_sta, "run_openroad", mutate_after_first_corner)
    report = run_sta(request)

    assert moved == [True]
    assert len(scripts) == 2
    assert _sha256_of(target) == digest
    if isinstance(spef, str):
        assert report["spef_content_hash"] == digest
    else:
        assert {row["spef_content_hash"] for row in report["corners"]} == {digest}


def test_switching_spef_symlink_keeps_engine_on_original_target(tmp_path, monkeypatch):
    request = _setup_matrix_env(tmp_path, monkeypatch, "current.spef")
    link = tmp_path / "current.spef"
    link.symlink_to("nom.spef")
    (tmp_path / "max.spef").write_text(_spef_text("rst"), encoding="utf-8")
    scripts = _stub_openroad_matrix(monkeypatch)
    run_engine = post_route_sta.run_openroad

    def switch_target(*args, **kwargs):
        result = run_engine(*args, **kwargs)
        link.unlink()
        link.symlink_to("max.spef" if len(scripts) == 1 else "nom.spef")
        return result

    monkeypatch.setattr(post_route_sta, "run_openroad", switch_target)
    report = run_sta(request)
    target = tmp_path / "nom.spef"
    assert report["spef_content_hash"] == _sha256_of(target)
    assert len(scripts) == 2
    read_spef = f"read_spef {post_route_sta._tcl_literal(str(target))}"
    for script in scripts:
        assert read_spef in Path(script).read_text(encoding="utf-8").splitlines()


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
