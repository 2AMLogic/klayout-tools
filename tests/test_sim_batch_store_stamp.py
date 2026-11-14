"""Client/worker PDK stamp agreement: ``.pdk-store.json`` bundles (#5318) and
the search-resolved install a host that pins no root uses (#5474)."""

from __future__ import annotations

import hashlib
import json

import pytest

from klayout_tools import pdk, pdk_store
from klayout_tools import sim_batch as sb
from test_sim_batch import _capture, _run_capture_program, _worker_env


def _store_pdk_root(tmp_path, version):
    bundle = tmp_path / "bundle"
    (bundle / "sky130A").mkdir(parents=True)
    (bundle / ".pdk-store.json").write_text(json.dumps({"version": version}))
    root = tmp_path / "pdk"
    root.mkdir()
    (root / "sky130A").symlink_to(bundle / "sky130A")
    return root


def test_store_only_client_and_worker_stamps_match_and_mismatch(tmp_path):
    root = _store_pdk_root(tmp_path, "2026.08.03")
    request = {"models": {"pdk": "sky130A", "pdk_root": str(root)}}
    client = sb._client_pdk_stamp(request)
    assert client["stamp_scheme"] == "store-version-sha256"
    doc = _run_capture_program(
        tmp_path, ngspice_script=None, env={"PDK_ROOT": str(root)}
    )
    assert doc["pdk"]["stamp_scheme"] == "store-version-sha256"
    assert sb._compare_pdk_stamps(doc["pdk"], client) == {"status": "match"}
    other = {**client, "stamp_sha256": "0" * 64}
    assert sb._compare_pdk_stamps(doc["pdk"], other)["status"] == "mismatch"
    sources = {**client, "stamp_scheme": "sources-sha256"}
    assert sb._compare_pdk_stamps(doc["pdk"], sources)["reason"] == "scheme_differs"


# --------------------------------------------------------------------------- #
# Issue #5474: with no root pinned, stamp the install the search order resolves
# --------------------------------------------------------------------------- #


def _make_install(root, variant, *, sources=None):
    """A minimal open_pdks-layout variant under ``root`` (mirrors
    ``tests/test_pdk.py::_make_install``, the shape ``_probe_root`` accepts)."""
    variant_dir = root / variant
    (variant_dir / "libs.tech").mkdir(parents=True)
    if sources is not None:
        (variant_dir / "SOURCES").write_text(sources, encoding="utf-8")
    return variant_dir


@pytest.fixture
def _searchable_home(monkeypatch, tmp_path):
    """An empty, controlled search space: ``$PDK_ROOT``/``$PDK`` scrubbed,
    ``$HOME`` redirected under ``tmp_path`` and only ``~/.ciel`` searched, so
    nothing the host happens to have installed can resolve."""
    monkeypatch.delenv("PDK_ROOT", raising=False)
    monkeypatch.delenv("PDK", raising=False)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(pdk, "STORE_DIRS", ["~/.ciel"])
    monkeypatch.setattr(pdk, "SHARED_ROOTS", [])
    monkeypatch.setattr(pdk, "CONVENTIONAL_PREFIXES", [])
    return home


def test_search_resolved_install_supplies_the_client_stamp(_searchable_home):
    """No ``models.pdk_root``, no ``$PDK_ROOT`` -- the default worker setup --
    still compares against the install `provenance.pdk` names, rather than
    reporting ``client_stamp_unavailable`` (issue #5474)."""
    _make_install(_searchable_home / ".ciel", "gf180mcuD", sources="open_pdks f6ee\n")

    client = sb._client_pdk_stamp({"models": {"pdk": "gf180mcuD"}})

    assert client == {
        "variant": "gf180mcuD",
        "stamp_scheme": "sources-sha256",
        "stamp_sha256": hashlib.sha256(b"open_pdks f6ee").hexdigest(),
    }
    worker = {**client, "root": "/opt/pdk"}
    assert sb._compare_pdk_stamps(worker, client) == {"status": "match"}
    other = {**worker, "stamp_sha256": "0" * 64}
    assert sb._compare_pdk_stamps(other, client)["status"] == "mismatch"


def test_pdk_root_env_still_wins_over_the_search_resolved_install(
    _searchable_home, tmp_path, monkeypatch
):
    """Precedence is unchanged: a pinned root is stamped even when the search
    order would have resolved a different install."""
    _make_install(_searchable_home / ".ciel", "gf180mcuD", sources="search side\n")
    pinned = tmp_path / "pinned"
    _make_install(pinned, "gf180mcuD", sources="pinned side\n")
    monkeypatch.setenv("PDK_ROOT", str(pinned))

    client = sb._client_pdk_stamp({"models": {"pdk": "gf180mcuD"}})

    assert client["stamp_sha256"] == hashlib.sha256(b"pinned side").hexdigest()


def test_models_pdk_root_still_wins_over_pdk_root_env(
    _searchable_home, tmp_path, monkeypatch
):
    """``models.pdk_root`` keeps beating ``$PDK_ROOT``, which keeps beating the
    search order."""
    _make_install(_searchable_home / ".ciel", "gf180mcuD", sources="search side\n")
    env_root = tmp_path / "env"
    _make_install(env_root, "gf180mcuD", sources="env side\n")
    monkeypatch.setenv("PDK_ROOT", str(env_root))
    request_root = tmp_path / "request"
    _make_install(request_root, "gf180mcuD", sources="request side\n")

    client = sb._client_pdk_stamp(
        {"models": {"pdk": "gf180mcuD", "pdk_root": str(request_root)}}
    )

    assert client["stamp_sha256"] == hashlib.sha256(b"request side").hexdigest()


def test_variant_installed_nowhere_is_still_client_stamp_unavailable(
    _searchable_home,
):
    """The fallback never invents a stamp: an uninstalled variant stays
    ``not_comparable``, never a proven mismatch."""
    _make_install(_searchable_home / ".ciel", "sky130A", sources="open_pdks abc\n")

    assert sb._client_pdk_stamp({"models": {"pdk": "gf180mcuD"}}) is None
    worker = {
        "variant": "gf180mcuD",
        "stamp_scheme": "sources-sha256",
        "stamp_sha256": "0" * 64,
    }
    assert sb._compare_pdk_stamps(worker, None) == {
        "status": "not_comparable",
        "reason": "client_stamp_unavailable",
    }


def test_search_resolved_bundle_install_uses_the_store_version_scheme(
    _searchable_home,
):
    """A search-resolved bundle install (no ``SOURCES``) falls back to
    ``.pdk-store.json`` exactly as a pinned root does (issue #5318)."""
    store = _searchable_home / ".ciel"
    bundle = _searchable_home / "bundle"
    (bundle / "gf180mcuD" / "libs.tech").mkdir(parents=True)
    (bundle / ".pdk-store.json").write_text(json.dumps({"version": "2026.08.03"}))
    store.mkdir(parents=True)
    (store / "gf180mcuD").symlink_to(bundle / "gf180mcuD")

    client = sb._client_pdk_stamp({"models": {"pdk": "gf180mcuD"}})

    assert client == {
        "variant": "gf180mcuD",
        "stamp_scheme": "store-version-sha256",
        "stamp_sha256": hashlib.sha256(b"2026.08.03").hexdigest(),
    }


def test_search_resolved_install_without_either_stamp_source(_searchable_home):
    """Resolvable but unstampable (no ``SOURCES``, no ``.pdk-store.json``)
    keeps reporting ``client_stamp_unavailable``."""
    _make_install(_searchable_home / ".ciel", "gf180mcuD")

    assert sb._client_pdk_stamp({"models": {"pdk": "gf180mcuD"}}) is None


def test_flat_single_pdk_install_is_its_own_variant_directory(_searchable_home):
    """A flat, single-PDK install (issue #522) *is* the variant directory, and a
    descriptor names a variant unrelated to that directory's basename -- so the
    stamp must come from the resolved root itself, not ``<root>/<variant>``."""
    flat = _searchable_home / ".ciel"
    flat.mkdir()
    (flat / "klt-pdk.json").write_text(
        json.dumps({"schema_version": 1, "variant": "asap7"}), encoding="utf-8"
    )
    (flat / "SOURCES").write_text("asap7 v1.0\n", encoding="utf-8")

    assert pdk_store.resolved_variant_dir("asap7") == str(flat)
    client = sb._client_pdk_stamp({"models": {"pdk": "asap7"}})
    assert client["stamp_sha256"] == hashlib.sha256(b"asap7 v1.0").hexdigest()


def test_staged_model_comparison_carries_the_search_resolved_client_stamp(
    _searchable_home,
):
    """The staged-models path (`_compare_staged_model_inputs`) gets the same
    resolved stamp, so a staged batch run can still pin the PDK revision."""
    _make_install(_searchable_home / ".ciel", "gf180mcuD", sources="open_pdks f6ee\n")
    remote_env = {
        "worker_environment": {
            "pdk": {"variant": "gf180mcuD", "models_staged": True},
            "model_inputs": [{"name": "models.spice", "sha256": "a" * 64}],
        }
    }

    sb.attach_staged_model_comparison(
        remote_env,
        [{"name": "models.spice", "sha256": "a" * 64}],
        {"models": {"pdk": "gf180mcuD"}},
    )

    comparison = remote_env["worker_environment"]["pdk_stamp_comparison"]
    assert comparison["status"] == "match"
    assert comparison["client_stamp"] == {
        "variant": "gf180mcuD",
        "stamp_scheme": "sources-sha256",
        "stamp_sha256": hashlib.sha256(b"open_pdks f6ee").hexdigest(),
    }


def test_batch_report_without_any_install_is_client_stamp_unavailable(
    _searchable_home, tmp_path, monkeypatch
):
    """End to end through a collected job: with nothing pinned *and* no install
    of the variant in the search order -- the only `client_stamp_unavailable`
    case left since #5474 -- the report still says so rather than guessing.
    (Moved here from ``test_sim_batch.py``: it now needs a scrubbed search
    space, which the sibling client-stamp tests already set up.)"""
    env = _worker_env(
        tmp_path,
        monkeypatch,
        {"worker_environment.json": _capture()},
        request={"netlist": "body.spice", "models": {"pdk": "sky130A"}},
    )

    assert env["worker_environment"]["pdk_stamp_comparison"] == {
        "status": "not_comparable",
        "reason": "client_stamp_unavailable",
    }
