"""PDK install evidence on both hosts of a batch run (issue #8007).

The worker's capture program is stdlib-only source shipped to the runner, so
it cannot import :mod:`klayout_tools.pdk_store`: these tests run the real
program over the same trees the library reads and hold the two to one answer
-- the bundle a store marker records (``install``) and the layout the tree
sits in (``store_layout``), beside the revision stamp of
``test_sim_batch_store_stamp.py``.
"""

from __future__ import annotations

import json
import os

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


def _captured(parent, root, staged="0"):
    """The real capture program's document for ``PDK_ROOT=root``."""
    return _run_capture_program(
        parent,
        ngspice_script=None,
        args=("", "gf180mcuD", staged),
        env={"PDK_ROOT": str(root)},
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

    report = pdk.find_pdk(variant="gf180mcuD", root=str(root))

    assert _observed(report) == (_INSTALL, layout)
    assert _observed(_captured(tmp_path, root)["pdk"]) == (_INSTALL, layout)


@pytest.mark.parametrize(
    "beside, inside, expected",
    [
        (_INSTALL, None, _INSTALL),
        (None, _INSTALL, _INSTALL),  # the variant root, where the provisioner writes
        (_INSTALL, "{not json", None),  # the nearer marker is *the* marker
        ({"version": "v1"}, None, _NO_FACTS | {"version": "v1"}),
        ({"sha256": "short"}, None, None),  # no valid fact is no evidence
        (None, None, None),
    ],
    ids=["beside", "inside", "shadowed", "partial", "invalid-digest", "unmarked"],
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
        ({}, "flat", (None, "flat")),
    ],
    ids=["short-digest", "long-digest", "not-an-object", "bounded", "no-fact"],
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
    """Under staged models ``stamp_*`` describes the image tree, not the files
    that ran. The install evidence is read from that same tree and rides along
    unchanged; the image/executed distinction stays where it was."""
    document = _captured(tmp_path, _store_tree(tmp_path), staged="1")
    request = {"netlist": "body.spice", **_PDK_REQUEST}
    files = {"worker_environment.json": json.dumps(document)}

    env = _worker_env(tmp_path, monkeypatch, files, request)
    pdk_block = env["worker_environment"]["pdk"]

    assert (pdk_block["stamp_scope"], pdk_block["models_staged"]) == ("image", True)
    assert _observed(pdk_block) == (_INSTALL, "manager_family")
