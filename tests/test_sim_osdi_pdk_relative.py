"""Resolve declared OSDI model binaries on the host that executes or stages them."""

import hashlib
import json
from pathlib import Path

import pytest

from klayout_tools import sim, sim_batch, sim_osdi_settle, sim_remote, sim_staging
from klayout_tools.sim_errors import SimError
from klayout_tools.sim_osdi_pin import resolve_request_osdi
from test_sim import _abi_probe
from test_sim_batch_osdi_digest import _join, _resolved
from test_sim_osdi_settle import _capture_decks, _osdi_request

REL = "libs.tech/ngspice/device.osdi"
ENTRY = {"pdk_relative": REL}
VARIANT = "ihp-sg13g2"


def _install(root, *, flat=False, content=b"pdk binary"):
    path = (root if flat else root / VARIANT) / REL
    path.parent.mkdir(parents=True)
    path.write_bytes(content)
    return path


@pytest.mark.parametrize("flat", [False, True])
def test_pinned_pdk_and_request_relative_strings_load_different_files(
    tmp_path, monkeypatch, flat
):
    root = tmp_path / VARIANT if flat else tmp_path / "store"
    pinned = _install(root, flat=flat)
    _install(tmp_path / "ambient", content=b"ambient PDK")
    monkeypatch.setenv("PDK_ROOT", str(tmp_path / "ambient"))
    local = tmp_path / REL
    local.parent.mkdir(parents=True)
    local.write_bytes(b"request binary")
    request = {
        "models": {"pdk": VARIANT, "pdk_root": str(root)},
        "options": {"osdi_preload": [ENTRY, REL, ENTRY]},
    }
    paths = resolve_request_osdi(request, str(tmp_path))
    assert paths == (str(pinned), str(local), str(pinned))
    assert [Path(item).read_bytes() for item in paths] == [
        b"pdk binary",
        b"request binary",
        b"pdk binary",
    ]


@pytest.mark.parametrize(
    ("entry", "models", "error"),
    [
        ({}, {"pdk": VARIANT}, "exactly one"),
        ({**ENTRY, "pdk": VARIANT}, {"pdk": VARIANT}, "exactly one"),
        (ENTRY, {}, "requires models.pdk"),
        ({"pdk_relative": ""}, {"pdk": VARIANT}, "non-empty relative"),
        ({"pdk_relative": True}, {"pdk": VARIANT}, "non-empty relative"),
        ({"pdk_relative": "/lib/d.osdi"}, {"pdk": VARIANT}, "within"),
        ({"pdk_relative": r"..\model.osdi"}, {"pdk": VARIANT}, "within"),
        ({"pdk_relative": "foo/../../d.osdi"}, {"pdk": VARIANT}, "within"),
        # Mixed separators: ``\`` is a separator only under Windows rules and an
        # ordinary filename character under POSIX rules, so the two conventions
        # count different directory components. Each of these stays inside the
        # variant under one convention and escapes it under the other.
        ({"pdk_relative": r"models\device/../.."}, {"pdk": VARIANT}, "within"),
        ({"pdk_relative": r"models/..\.."}, {"pdk": VARIANT}, "within"),
        ({"pdk_relative": r"models\device/../../d.osdi"}, {"pdk": VARIANT}, "within"),
        ({"pdk_relative": "a/..\\..\\d.osdi"}, {"pdk": VARIANT}, "within"),
        ({"pdk_relative": r"C:d.osdi"}, {"pdk": VARIANT}, "within"),
    ],
)
def test_invalid_entries_are_reported_even_when_deferred(
    tmp_path, entry, models, error
):
    request = {"models": models, "options": {"osdi_preload": [entry]}}
    with pytest.raises(SimError, match=r"osdi_preload\[0\].*" + error):
        resolve_request_osdi(request, str(tmp_path), frozenset({0}))


@pytest.mark.parametrize(
    "ref",
    [REL, "a/./b.osdi", "a/../b.osdi", r"models\device.osdi", "libs..tech/d.osdi"],
)
def test_contained_references_are_not_over_rejected(tmp_path, ref):
    """The both-conventions containment check still accepts a reference that
    stays inside the variant under each convention."""
    request = {
        "models": {"pdk": VARIANT},
        "options": {"osdi_preload": [{"pdk_relative": ref}]},
    }
    assert resolve_request_osdi(request, str(tmp_path), frozenset({0})) == ()


def test_missing_binary_and_variant_name_the_entry(tmp_path):
    root = tmp_path / "root"
    model = _install(root)
    model.unlink()
    request = {
        "models": {"pdk": VARIANT, "pdk_root": str(root)},
        "options": {"osdi_preload": [ENTRY]},
    }
    with pytest.raises(SimError, match=r"osdi_preload\[0\].*file not found") as caught:
        resolve_request_osdi(request, str(tmp_path))
    assert str(model) in str(caught.value)
    request["models"]["pdk"] = "uninstalled-test-pdk"
    with pytest.raises(SimError, match=r"osdi_preload\[0\].*uninstalled-test-pdk"):
        resolve_request_osdi(request, str(tmp_path))


@pytest.mark.parametrize("backend", ["batch", "remote"])
@pytest.mark.parametrize("stage", [False, True])
def test_worker_request_resolves_only_on_the_responsible_host(
    tmp_path, monkeypatch, backend, stage
):
    client = tmp_path / "client"
    worker = tmp_path / "worker"
    remote = _install(worker, content=b"runner bytes")
    baked = worker / "baked.osdi"
    baked.write_bytes(b"image bytes")
    literal = "$PDK_ROOT/baked.osdi"
    opts = {"osdi_preload": [ENTRY, literal, ENTRY], "stage_model_inputs": stage}
    request = {"models": {"pdk": VARIANT}, "options": opts}
    monkeypatch.setenv("PDK_ROOT", str(client))
    netlist = tmp_path / "dut.cir"
    netlist.write_text("R1 a 0 1k\n")
    if stage:
        _install(client, content=b"submitter bytes")
    deferred = sim_staging.runner_resolved_osdi(request, str(tmp_path))
    local = resolve_request_osdi(request, str(tmp_path), frozenset(deferred))
    _, environment = sim_osdi_settle.finalize_osdi_preload(
        opts, str(tmp_path), True, local, deferred
    )
    assert [e["index"] for e in environment] == ([1] if stage else [0, 1, 2])
    if backend == "batch":
        spec = sim_batch._build_batch_job_spec(
            request,
            str(netlist),
            request_dir=str(tmp_path),
            corner_count=1,
            timeout_s=30,
        )
        inputs = {item.name: item for item in spec.inputs}
    else:
        description = sim_remote._build_remote_job_description(
            request, str(netlist), request_dir=str(tmp_path)
        )
        inputs = {item.remote_name: item for item in description.inputs}
    work = tmp_path / "job"
    work.mkdir()
    for filename, item in inputs.items():
        data = (
            item.content.encode()
            if item.content is not None
            else Path(item.local_path).read_bytes()
        )
        (work / filename).write_bytes(data)
    payload = json.loads((work / "request.json").read_text())
    monkeypatch.setenv("PDK_ROOT", str(worker))
    forwarded = payload["options"]["osdi_preload"]
    paths = resolve_request_osdi(payload, str(work))
    if stage:
        assert forwarded == ["device.osdi", literal, "device.osdi"]
        assert set(inputs) == {"netlist.cir", "request.json", "device.osdi"}
        assert [Path(p).read_bytes() for p in paths] == [
            b"submitter bytes",
            b"image bytes",
            b"submitter bytes",
        ]
    else:
        assert forwarded == [ENTRY, literal, ENTRY]
        assert set(inputs) == {"netlist.cir", "request.json"}
        assert paths == (str(remote), str(baked), str(remote))
        assert all(
            env["reason"] == "pdk_relative" for env in (environment[0], environment[2])
        )


@pytest.mark.usefixtures("real_build_identity_git")
@pytest.mark.parametrize("backend", ["local", "local-parallel", "fallback"])
def test_execution_deck_and_report_use_the_variant_bytes(
    tmp_path, monkeypatch, backend
):
    root = tmp_path / "pdk"
    binary = _install(root)
    request = _osdi_request(
        tmp_path, [ENTRY], models={"pdk": VARIANT, "pdk_root": str(root)}
    )
    decks = _capture_decks(monkeypatch)
    if backend == "fallback":
        monkeypatch.setenv(sim.BACKEND_ENV, "batch")
    result = sim.run_sim(
        str(request), backend=None if backend == "fallback" else backend
    )
    env = result["environment"]
    assert decks and all(f"pre_osdi {binary}" in deck for deck in decks)
    assert "osdi_preload_runner_resolved" not in env
    assert (
        env["osdi_preload"][0]["sha256"]
        == hashlib.sha256(binary.read_bytes()).hexdigest()
    )


@pytest.mark.parametrize("name", ["device.osdi", "wrong.osdi", None])
def test_batch_digest_requires_the_worker_slot_and_matching_basename(name):
    digest = "1" * 64
    worker = {"runner_osdi_preload": [{"name": name, "sha256": digest}]}
    assert _join([_resolved(ENTRY, 0, "pdk_relative")], worker) == [
        digest if name == "device.osdi" else None
    ]


@pytest.mark.parametrize(
    "spelling",
    [
        REL,
        f"{REL}/.",
        "libs.tech/./ngspice//device.osdi",
        "libs.tech/other/../ngspice/device.osdi",
    ],
)
def test_batch_digest_follows_the_resolved_object_not_its_spelling(tmp_path, spelling):
    """Every accepted spelling of one object resolves to the same file, so each
    keeps the worker's digest, while the report still names the declared entry
    as written. A differently named worker file still yields no digest."""
    root = tmp_path / "root"
    binary = _install(root)
    entry = {"pdk_relative": spelling}
    request = {
        "models": {"pdk": VARIANT, "pdk_root": str(root)},
        "options": {"osdi_preload": [entry]},
    }
    assert resolve_request_osdi(request, str(tmp_path)) == (str(binary),)
    digest = "1" * 64
    resolved = [_resolved(entry, 0, "pdk_relative")]
    worker = {"runner_osdi_preload": [{"name": "device.osdi", "sha256": digest}]}
    assert _join(resolved, worker) == [digest]
    assert resolved[0]["entry"] == entry
    other = {"runner_osdi_preload": [{"name": "wrong.osdi", "sha256": digest}]}
    assert _join([_resolved(entry, 0, "pdk_relative")], other) == [None]
    # A string declaration normalizes the same way.
    text = f"$PDK_ROOT/{spelling}"
    assert _join([_resolved(text, 0)], worker) == [digest]
    assert _join([_resolved(text, 0)], other) == [None]


@pytest.mark.parametrize(
    "declared", ["$PDK_ROOT/osdi/device.osdi", None, 7], ids=["string", "none", "int"]
)
@pytest.mark.parametrize(
    "reported",
    [{}, {"name": None}, {"name": 7}],
    ids=["name-absent", "name-null", "name-not-a-string"],
)
def test_batch_digest_is_kept_when_there_is_no_name_to_contradict_it(
    declared, reported
):
    """The basename corroboration only rejects a digest on a proven mismatch.
    A string entry whose worker slot carries no usable ``name`` -- or an entry
    that is neither a string nor a ``pdk_relative`` object -- has nothing to
    disagree with, so the index-joined digest is kept, as it was before the
    object form existed. (The object form itself is stricter: it needs both a
    string path and a string name, see
    ``test_batch_digest_requires_the_worker_slot_and_matching_basename``.)"""
    digest = "1" * 64
    worker = {"runner_osdi_preload": [{**reported, "sha256": digest}]}
    assert _join([_resolved(declared, 0)], worker) == [digest]
    # A string name that does disagree with a string entry is still refused.
    if isinstance(declared, str):
        wrong = {"runner_osdi_preload": [{"name": "wrong.osdi", "sha256": digest}]}
        assert _join([_resolved(declared, 0)], wrong) == [None]


@pytest.mark.usefixtures("real_build_identity_git")
@pytest.mark.parametrize("resume", [False, True])
def test_staged_object_checks_local_abi_after_host_default_yields(
    tmp_path, monkeypatch, resume
):
    root = tmp_path / "pdk"
    _install(root)
    request = _osdi_request(
        tmp_path, [ENTRY], models={"pdk": VARIANT, "pdk_root": str(root)}
    )
    spec = json.loads(request.read_text())
    spec["options"]["stage_model_inputs"] = True
    if resume:
        spec["options"]["resume"] = True
        spec["corners"] = {"temperature_c": [25, 70]}
    request.write_text(json.dumps(spec))
    monkeypatch.setenv(sim.BACKEND_ENV, "batch")
    attempts = _abi_probe(monkeypatch, abi=(0, 4), banner="** ngspice-42 : x\n")
    with pytest.raises(SimError) as err:
        sim.run_sim(str(request))
    assert err.value.code == "osdi_abi_mismatch"
    assert attempts == []


@pytest.mark.parametrize(
    "entry,stage,missing",
    [(ENTRY, False, ["osdi_pdk_relative"]), (ENTRY, True, []), (REL, False, [])],
)
def test_older_worker_identity_distinguishes_preload_forms(
    tmp_path, entry, stage, missing
):
    (tmp_path / sim_batch.BATCH_RUNNER_IDENTITY_FILENAME).write_text(
        json.dumps({"runner_klt_version": "0.9.2"})
    )
    request = {"options": {"osdi_preload": [entry], "stage_model_inputs": stage}}
    report = sim_batch._read_runner_identity(str(tmp_path), request)[
        "runner_request_features"
    ]
    assert report["missing"] == missing
    assert report["status"] == ("unsupported" if missing else "supported")
