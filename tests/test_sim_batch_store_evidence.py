"""Compare the shipped worker capture and the client library on the same PDK
installs, then check how each host's evidence appears in batch reports (#8007).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

from helpers.subprocess_fakes import fake_completed, uploaded_request_of
from klayout_tools import _provenance, pdk
from klayout_tools import sim_batch as sb
from test_sim_batch import (
    FAKE_BUCKET,
    _capture,
    _corner_points,
    _FakeRunner,
    _install_fake_batch_transport,
    _measurements_spec,
    _pin_client_version,
    _provision_script,
    _run_capture_program,
    _sim_report,
    _status,
    _worker_env,
    _write_body,
)

_STORE_REV = "f6eeac7dad085ffcc829ccfd721f7b4ce39edcf7"
_INSTALL = {
    "sha256": "9f2c0a5b" * 8,
    "version": _STORE_REV,
    "via": "artifact-broker",
    "synced": "2026-11-23T06:48:48Z",
}
_NO_FACTS = dict.fromkeys(_INSTALL)
_FAMILY = f"ciel/gf180mcu/versions/{_STORE_REV}/gf180mcuD"
_LEGACY = f"gf180mcu/versions/{_STORE_REV}/gf180mcuD"
_PDK_REQUEST = {"models": {"pdk": "gf180mcuD"}}


def _store_tree(parent, digest=_INSTALL["sha256"], relative=_FAMILY):
    """``<parent>/pdk/<relative>`` carrying ``SOURCES``, the fleet marker beside
    it, and the enabled link ``<parent>/pdk/gf180mcuD``."""
    root = parent / "pdk"
    variant = root / relative
    (variant / "libs.tech").mkdir(parents=True)
    (variant / "SOURCES").write_text(f"open_pdks {_STORE_REV}\n", encoding="utf-8")
    (variant.parent / ".pdk-store.json").write_text(
        json.dumps({**_INSTALL, "sha256": digest}), encoding="utf-8"
    )
    if variant != root / "gf180mcuD":
        (root / "gf180mcuD").symlink_to(os.path.relpath(variant, root))
    return root


def _capture_request(tmp_path, env, models, *, staged=False):
    """Exercise the preflight command, including its quoted request root."""
    request = {"models": models, "options": {"stage_model_inputs": staged}}
    subprocess.run(
        ["/bin/sh", "-c", sb._worker_capture_command(request)],
        env=env
        | {"PATH": os.path.dirname(sys.executable), "EDA_OUTPUT_DIR": str(tmp_path)},
        check=True,
        timeout=30,
    )
    return json.loads((tmp_path / sb.BATCH_WORKER_ENVIRONMENT_FILENAME).read_text())


def _captured(parent, root, staged="0", variant="gf180mcuD"):
    return _capture_request(
        parent,
        {"PDK_ROOT": str(root)},
        {"pdk": variant, "pdk_root": str(root)},
        staged=staged == "1",
    )


def _observed(document):
    """The ``(install, store_layout)`` pair a capture or a report carries."""
    return (document["install"], document["store_layout"])


def test_worker_capture_reports_the_store_beside_a_sources_stamp(tmp_path):
    """The capture read the marker only when ``SOURCES`` was unreadable, and
    then only one level above the tree: a tree carrying both now reports its
    revision stamp *and* what placed it."""
    block = _captured(tmp_path, _store_tree(tmp_path))["pdk"]

    assert block["stamp_scheme"] == "sources-sha256"
    assert _observed(block) == (_INSTALL, "manager_family")


@pytest.mark.parametrize(
    "relative, layout",
    [(_FAMILY, "manager_family"), (_LEGACY, "legacy_manager"), ("gf180mcuD", "flat")],
    ids=["manager-family", "legacy-manager", "flat"],
)
def test_worker_capture_agrees_with_the_library_on_each_layout(
    tmp_path, relative, layout
):
    root = _store_tree(tmp_path, relative=relative)
    (root / "unused").symlink_to("unused")  # An unrelated loop must not hide the PDK.

    report = pdk.find_pdk(variant="gf180mcuD", root=str(root))

    assert _observed(report) == (_INSTALL, layout)
    assert _observed(_captured(tmp_path, root)["pdk"]) == (_INSTALL, layout)


def test_worker_requires_the_requested_variant_for_store_evidence(tmp_path):
    root = _store_tree(tmp_path, relative="gf180mcuD")
    # Only gf180mcuD is installed, even if a namesake directory exists.
    for directory in (root, root / "gf180mcuD"):
        block = _captured(tmp_path, directory, staged="1", variant="sky130A")["pdk"]
        assert _observed(block) == (None, None)
    (root / "sky130A").mkdir()
    block = _captured(tmp_path, root, staged="1", variant="sky130A")["pdk"]
    assert _observed(block) == (None, None)


@pytest.mark.parametrize("layout", ["open_pdks", "descriptor", "asap7"])
def test_worker_reads_the_parent_marker_of_a_matching_flat_root(tmp_path, layout):
    root = _store_tree(tmp_path, relative="gf180mcuD") / "gf180mcuD"
    if layout != "open_pdks":
        (root / "libs.tech").rmdir()
    if layout == "descriptor":
        root = root.rename(root.with_name("checkout"))
        (root / "klt-pdk.json").write_text(
            json.dumps({"schema_version": 1, "variant": "gf180mcuD"})
        )
    if layout == "asap7":
        for marker in ("base/setup/klayout", "base/apr", "libs"):
            (root / marker).mkdir(parents=True)

    client = pdk.find_pdk(variant="gf180mcuD", root=str(root))
    worker = _captured(tmp_path, root)["pdk"]

    assert _observed(client) == _observed(worker) == (_INSTALL, "flat")
    assert worker["stamp_scheme"] is None  # revision probing still uses root/<variant>


@pytest.fixture
def descriptor_store(tmp_path):
    root = _store_tree(tmp_path, relative="gf180mcuD")
    installed = root / "gf180mcuD"
    (installed / "libs.tech").rmdir()
    (installed / "cells.lib").touch()
    corners = {f"corner_{i}": "cells.lib" for i in range(3000)}
    descriptor = installed / "klt-pdk.json"
    descriptor.write_text(
        json.dumps(
            {"schema_version": 1, "variant": "gf180mcuD", "liberty": {"cells": corners}}
        )
    )
    assert descriptor.stat().st_size > 65536
    return root


@pytest.mark.parametrize("direct", [False, True], ids=["parent", "flat"])
def test_large_descriptor_preserves_selected_bundle(tmp_path, descriptor_store, direct):
    root = descriptor_store / "gf180mcuD" if direct else descriptor_store
    client = pdk.find_pdk(variant="gf180mcuD", root=str(root))
    worker = _captured(tmp_path, root)["pdk"]
    assert _observed(client) == _observed(worker) == (_INSTALL, "flat")


@pytest.mark.parametrize("malformed", [False, True], ids=["large", "malformed"])
def test_nested_descriptor_rules_out_outer_bundle(
    tmp_path, descriptor_store, malformed
):
    root = descriptor_store
    (root / "klt-pdk.json").write_text(
        json.dumps({"schema_version": 1, "variant": "sky130A"})
    )
    if malformed:
        (root / "gf180mcuD" / "klt-pdk.json").write_text("{")
    with pytest.raises(pdk.PdkNotFoundError):
        pdk.find_pdk(variant="sky130A", root=str(root))
    worker = _captured(tmp_path, root, variant="sky130A")["pdk"]
    assert _observed(worker) == (None, None)


@pytest.mark.parametrize("env_root", ["nested", "flat", "unset"])
def test_worker_finds_fallback_store_unless_root_is_pinned(
    tmp_path, monkeypatch, env_root
):
    store = _store_tree(tmp_path).rename(tmp_path / ".ciel")
    other = tmp_path / "other root"
    sky = other / "sky130A"
    (sky / "libs.tech").mkdir(parents=True)
    (sky / ".pdk-store.json").write_text(json.dumps({"version": "sky130-rev"}))
    env = {"HOME": str(tmp_path)}
    if env_root != "unset":
        env["PDK_ROOT"] = str(sky if env_root == "flat" else other)
    monkeypatch.delenv("PDK_ROOT", raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    models = {"pdk": "gf180mcuD"}
    client = pdk.find_pdk(variant=models["pdk"])
    worker = _capture_request(tmp_path, env, models)["pdk"]
    assert client["root"] == str(store)
    assert _observed(client) == _observed(worker) == (_INSTALL, "manager_family")
    expected_stamp = (
        ("store-version-sha256", "store:sky130-rev")
        if env_root == "flat"
        else (None, None)
    )
    assert (worker["stamp_scheme"], worker["stamp_text"]) == expected_stamp
    pin = models | {"pdk_root": str(other)}
    assert _observed(_capture_request(tmp_path, env, pin)["pdk"]) == (None, None)
    pinned = _store_tree(tmp_path / "pinned", "b" * 64, relative="gf180mcuD")
    env["PDK_ROOT"] = str(store)
    worker = _capture_request(tmp_path, env, models | {"pdk_root": str(pinned)})["pdk"]
    assert _observed(worker) == (_INSTALL | {"sha256": "b" * 64}, "flat")


@pytest.mark.parametrize("direct", [False, True], ids=["nested", "flat"])
def test_worker_requires_valid_descriptor_before_using_marker(tmp_path, direct):
    # A later, valid store cannot replace a rejected descriptor.
    _store_tree(tmp_path).rename(tmp_path / ".ciel")
    root = _store_tree(tmp_path / "first", relative="gf180mcuD")
    variant = root / "gf180mcuD"
    search = variant if direct else root
    env = {"HOME": str(tmp_path), "PDK_ROOT": str(search)}
    (variant / "klt-pdk.json").write_text(
        json.dumps({"schema_version": 1, "liberty": {"cells": {"tt": "timing.lib"}}})
    )
    with pytest.raises(pdk.PdkNotFoundError, match="timing.lib"):
        pdk.find_pdk(variant="gf180mcuD", root=str(search))
    models = _PDK_REQUEST["models"]
    rejected = _capture_request(tmp_path, env, models)["pdk"]
    assert _observed(rejected) == (None, None)
    (variant / "timing.lib").touch()
    client = pdk.find_pdk(variant="gf180mcuD", root=str(search))
    worker = _capture_request(tmp_path, env, models)["pdk"]
    assert _observed(client) == _observed(worker) == (_INSTALL, "flat")


def test_install_alias_resolves_the_same_tree_on_the_worker(tmp_path):
    root = _store_tree(tmp_path, relative="gf180mcuD")
    installed = (root / "gf180mcuD").rename(root / "ihp-sg13g2")
    for directory in (root, installed):
        client = pdk.find_pdk(variant="sg13g2", root=str(directory))
        worker = _captured(tmp_path, directory, variant="sg13g2")["pdk"]
        assert _observed(client) == _observed(worker) == (_INSTALL, "flat")


def test_custom_version_tree_searched_at_its_revision_reports_unknown_layout(tmp_path):
    root = _store_tree(tmp_path)
    for directory, expected in (
        (root, "manager_family"),
        ((root / _FAMILY).parent, None),
    ):
        client = pdk.find_pdk(variant="gf180mcuD", root=str(directory))
        worker = _captured(tmp_path, directory)["pdk"]
        assert _observed(client) == _observed(worker) == (_INSTALL, expected)


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="requires a FIFO")
@pytest.mark.parametrize("location", ["variant", "parent"])
def test_worker_skips_nonregular_markers_before_reading(tmp_path, location):
    root = _store_tree(tmp_path)
    variant = root / _FAMILY
    (variant / "SOURCES").unlink()  # also exercise the independent stamp fallback
    if location == "parent":
        marker = variant.parent / ".pdk-store.json"
        marker.unlink()
    else:
        marker = variant / ".pdk-store.json"  # shadows the valid parent for install
    os.mkfifo(marker)

    worker = _captured(tmp_path, root)["pdk"]
    assert _observed(worker) == (None, "manager_family")
    assert worker["stamp_scheme"] == (
        "store-version-sha256" if location == "variant" else None
    )


@pytest.mark.parametrize(
    "beside, inside, expected",
    [
        (_INSTALL, None, _INSTALL),
        (None, _INSTALL, _INSTALL),  # the variant root, where the provisioner writes
        (_INSTALL, "{not json", None),  # the nearer marker is *the* marker
        (_INSTALL, json.dumps(_INSTALL) + " " * 65536, None),
        ({"version": "v1"}, None, _NO_FACTS | {"version": "v1"}),
        ({"sha256": "short"}, None, None),  # no valid fact is no evidence
        ({"sha256": " " + _INSTALL["sha256"]}, None, None),
        (None, None, None),
    ],
    ids=[
        "beside",
        "inside",
        "shadowed",
        "oversized",
        "partial",
        "invalid-digest",
        "padded-digest",
        "unmarked",
    ],
)
def test_worker_capture_reads_the_marker_as_the_library_does(
    tmp_path, beside, inside, expected
):
    root = _store_tree(tmp_path)
    variant = root / _FAMILY
    (variant.parent / ".pdk-store.json").unlink()
    for where, payload in ((variant.parent, beside), (variant, inside)):
        if payload is not None:
            text = payload if isinstance(payload, str) else json.dumps(payload)
            (where / ".pdk-store.json").write_text(text, encoding="utf-8")

    library = pdk.find_pdk(variant="gf180mcuD", root=str(root))["install"]

    assert (library, _captured(tmp_path, root)["pdk"]["install"]) == (
        expected,
        expected,
    )


@pytest.mark.parametrize("with_sources", [True, False], ids=["sources", "store-only"])
def test_deeply_nested_marker_preserves_pdk_report(tmp_path, with_sources):
    root = _store_tree(tmp_path)
    variant = root / _FAMILY
    # A nearer unreadable marker shadows the valid parent even with SOURCES.
    marker_dir = variant if with_sources else variant.parent
    if not with_sources:
        (variant / "SOURCES").unlink()
    (marker_dir / ".pdk-store.json").write_text(
        '{"unused":' + "[" * 20000 + "]" * 20000 + "}"
    )

    client = pdk.find_pdk(variant="gf180mcuD", root=str(root))
    worker = _captured(tmp_path, root)["pdk"]
    assert _observed(client) == _observed(worker) == (None, "manager_family")
    if with_sources:
        assert client["version"] == f"open_pdks {_STORE_REV}"
        assert worker["stamp_scheme"] == "sources-sha256"
    else:
        assert worker["stamp_scheme"] is None


def test_worker_capture_without_a_pdk_root_reports_no_store_evidence(tmp_path):
    block = _run_capture_program(tmp_path, ngspice_script=None, args=("", "", "0"))
    assert _observed(block["pdk"]) == (None, None)


def test_client_and_worker_each_report_their_own_bundle_at_one_revision(
    tmp_path, monkeypatch
):
    """Two hosts, one revision, two bundles: each side names its own digest,
    and the revision comparison reads exactly as it did."""
    monkeypatch.setenv("PDK_ROOT", str(_store_tree(tmp_path / "client", "1" * 64)))
    document = _captured(
        tmp_path / "worker", _store_tree(tmp_path / "worker", "2" * 64)
    )
    request = {"netlist": "body.spice", **_PDK_REQUEST}

    client = _provenance._pdk_block(pdk.find_pdk(variant="gf180mcuD"))
    files = {"worker_environment.json": json.dumps(document)}
    env = _worker_env(tmp_path, monkeypatch, files, request)["worker_environment"]

    assert (client["install"]["sha256"], env["pdk"]["install"]["sha256"]) == (
        "1" * 64,
        "2" * 64,
    )
    assert _observed(env["pdk"]) == (_INSTALL | {"sha256": "2" * 64}, "manager_family")
    assert env["pdk_stamp_comparison"] == {"status": "match"}


def test_sharded_fleet_members_each_report_their_own_bundle(tmp_path, monkeypatch):
    """A sharded run collects every ``fleet[]`` member from that shard's own
    capture: two workers at one revision show their respective bundles."""
    _pin_client_version(monkeypatch)
    runner = _FakeRunner()
    runner.default = fake_completed(_status("done"))
    _install_fake_batch_transport(monkeypatch, runner=runner)
    workers = {}
    for name, digest, relative in (("a", "1" * 64, _FAMILY), ("b", "2" * 64, _LEGACY)):
        directory = tmp_path / name
        root = _store_tree(directory, digest, relative)
        workers[name] = json.dumps(_captured(directory, root))

    def _register(argv, timeout_s):
        captured = uploaded_request_of(argv)
        if captured is not None:
            document, job_id = captured
            points = document["_explicit_points"]
            first = int(points[0]["temperature_c"])
            names = tuple(f"tt_1.800_{int(p['temperature_c'])}" for p in points)
            files = {"report.json": _sim_report(corner_ids=names)}
            files["worker_environment.json"] = workers["a" if first == 0 else "b"]
            runner.outputs_by_job[job_id] = files
        return runner(argv, timeout_s)

    monkeypatch.setattr(sb, "_run_subprocess", _register)
    batch = {
        "bucket": FAKE_BUCKET,
        "provision_script_path": str(_provision_script(tmp_path)),
        "poll_interval_s": 0.0,
    }
    _, _, environment = sb._run_batch_fleet(
        corner_points=_corner_points(3),
        netlist_path=str(_write_body(tmp_path)),
        timeout_s=30.0,
        keep_artifacts=False,
        want_waveforms=False,
        artifacts_dir=str(tmp_path / "artifacts"),
        request={"netlist": "body.spice", "batch": batch},
        hosts=2,
        measurements_spec=_measurements_spec(),
    )

    blocks = [entry["worker_environment"]["pdk"] for entry in environment["fleet"]]
    assert [block["install"]["sha256"] for block in blocks] == ["1" * 64, "2" * 64]
    assert [block["store_layout"] for block in blocks] == [
        "manager_family",
        "legacy_manager",
    ]


def test_capture_without_store_evidence_keeps_the_install_identity_unknown(
    tmp_path, monkeypatch
):
    """An older worker's capture carries neither field: it stays readable, and
    the install identity is unknown rather than borrowed from the client."""
    assert "install" not in json.loads(_capture())["pdk"]

    files = {"worker_environment.json": _capture()}
    pdk_block = _worker_env(tmp_path, monkeypatch, files)["worker_environment"]["pdk"]

    assert _observed(pdk_block) == (None, None)
    assert pdk_block["stamp_sha256"] == "a" * 64


@pytest.mark.parametrize(
    "install, layout, expected",
    [
        (
            {"sha256": "short", "version": "v1"},
            "manager_family",
            (_NO_FACTS | {"version": "v1"}, "manager_family"),
        ),
        (
            {"sha256": "9" * 70, "version": "v1"},
            None,
            (_NO_FACTS | {"version": "v1"}, None),
        ),
        ("artifact-broker", "elsewhere", (None, None)),
        ({"via": "x" * 400}, None, (_NO_FACTS | {"via": "x" * 300}, None)),
        ({"via": "  ", "version": ""}, "flat", (None, "flat")),
        ({"via": " store ", "synced": " "}, None, (_NO_FACTS | {"via": "store"}, None)),
        ({}, "flat", (None, "flat")),
    ],
    ids=[
        "short-digest",
        "long-digest",
        "not-an-object",
        "bounded",
        "blanks",
        "partial-blanks",
        "no-fact",
    ],
)
def test_collected_store_evidence_is_revalidated(
    tmp_path, monkeypatch, install, layout, expected
):
    """The capture file is untrusted input: each field is validated against
    the closed shapes again on collection."""
    document = json.loads(_capture())
    document["pdk"].update(install=install, store_layout=layout)
    files = {"worker_environment.json": json.dumps(document)}

    env = _worker_env(tmp_path, monkeypatch, files)["worker_environment"]

    assert _observed(env["pdk"]) == expected


def test_staged_model_capture_keeps_the_image_scope_beside_its_install(
    tmp_path, monkeypatch
):
    """Staging marks the stamp as image-scoped and preserves the install
    evidence collected for the resolved worker variant."""
    document = _captured(tmp_path, _store_tree(tmp_path), staged="1")
    request = {"netlist": "body.spice", **_PDK_REQUEST}
    files = {"worker_environment.json": json.dumps(document)}

    env = _worker_env(tmp_path, monkeypatch, files, request)
    pdk_block = env["worker_environment"]["pdk"]

    assert (pdk_block["stamp_scope"], pdk_block["models_staged"]) == ("image", True)
    assert _observed(pdk_block) == (_INSTALL, "manager_family")
