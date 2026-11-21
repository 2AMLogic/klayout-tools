"""Locating cargo-built native binaries (`klt-techmap`, `klt-wave`) wherever
cargo put them -- issue #3189.

Cargo builds into ``<crate>/target`` only by default: ``CARGO_TARGET_DIR``
or a cargo config's ``build.target-dir`` redirect it, and a hardcoded
``<crate>/target/...`` then reported a built binary as missing.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from klayout_tools import _native


@pytest.fixture(autouse=True)
def _isolated(monkeypatch):
    monkeypatch.delenv("CARGO_TARGET_DIR", raising=False)
    cached = _native._cargo_reported_target_dir
    cached.cache_clear()
    yield
    cached.cache_clear()


def _touch_binary(target_dir: Path, profile: str, name: str = "klt-x") -> str:
    path = target_dir / profile / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n")
    return str(path)


def _no_cargo_metadata(monkeypatch, reported: str | None = None) -> None:
    monkeypatch.setattr(_native, "_cargo_reported_target_dir", lambda _crate: reported)


def test_crate_local_release_preferred_over_debug(tmp_path, monkeypatch):
    _no_cargo_metadata(monkeypatch)
    crate = tmp_path / "crate"
    _touch_binary(crate / "target", "debug")
    release = _touch_binary(crate / "target", "release")
    assert _native.find_cargo_binary(str(crate), "klt-x") == release


def test_cargo_target_dir_env_is_honoured(tmp_path, monkeypatch):
    _no_cargo_metadata(monkeypatch)
    crate = tmp_path / "crate"
    crate.mkdir()
    shared = tmp_path / "shared-target"
    debug = _touch_binary(shared, "debug")
    monkeypatch.setenv("CARGO_TARGET_DIR", str(shared))
    assert _native.find_cargo_binary(str(crate), "klt-x") == debug


def test_relative_cargo_target_dir_resolves_against_the_crate(tmp_path, monkeypatch):
    _no_cargo_metadata(monkeypatch)
    crate = tmp_path / "crate"
    crate.mkdir()
    debug = _touch_binary(tmp_path / "out", "debug")
    monkeypatch.setenv("CARGO_TARGET_DIR", os.path.join("..", "out"))
    assert _native.find_cargo_binary(str(crate), "klt-x") == os.path.normpath(debug)


def test_cargo_config_target_dir_found_via_cargo_metadata(tmp_path, monkeypatch):
    crate = tmp_path / "crate"
    crate.mkdir()
    configured = tmp_path / "configured-target"
    release = _touch_binary(configured, "release")
    _no_cargo_metadata(monkeypatch, reported=str(configured))
    assert _native.find_cargo_binary(str(crate), "klt-x") == release


def test_missing_everywhere_returns_none(tmp_path, monkeypatch):
    _no_cargo_metadata(monkeypatch, reported=str(tmp_path / "nowhere"))
    assert _native.find_cargo_binary(str(tmp_path), "klt-x") is None


def test_cargo_metadata_absent_cargo_is_none(tmp_path, monkeypatch):
    monkeypatch.setattr(_native.shutil, "which", lambda _name: None)
    assert _native._cargo_reported_target_dir(str(tmp_path)) is None


def test_cargo_metadata_parses_target_directory(tmp_path, monkeypatch):
    class _Completed:
        returncode = 0
        stdout = '{"target_directory": "/somewhere/target", "packages": []}'

    monkeypatch.setattr(_native.shutil, "which", lambda _name: "/bin/cargo")
    monkeypatch.setattr(_native.subprocess, "run", lambda *_a, **_k: _Completed())
    assert _native._cargo_reported_target_dir(str(tmp_path)) == "/somewhere/target"
