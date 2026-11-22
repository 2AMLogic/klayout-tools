"""Model digests in a sim report describe the inputs at dispatch (#6508)."""

import hashlib

import pytest

from helpers.subprocess_fakes import _stub_subprocess_run
from klayout_tools import sim
from test_sim import (
    _write_body,
    _write_corner_lib,
    _write_family_lib,
    _write_osdi,
    _write_request,
)

pytestmark = pytest.mark.usefixtures(
    "real_build_identity_git", "bare_name_ngspice_resolution"
)


@pytest.mark.parametrize("delete", [False, True], ids=["edited", "deleted"])
def test_model_digests_survive_midrun_changes(tmp_path, monkeypatch, delete):
    _write_body(tmp_path)
    models = _write_corner_lib(tmp_path, "models.lib")
    section = _write_family_lib(tmp_path, "family.lib")
    osdi = _write_osdi(tmp_path)
    paths = (models, osdi, section)
    before = [hashlib.sha256(path.read_bytes()).hexdigest() for path in paths]
    request = _write_request(
        tmp_path,
        {
            "netlist": "body.spice",
            "analysis": {"kind": "tran", "args": "1n 1u"},
            "models": {"lib": models.name},
            "corners": {
                "process": [
                    {
                        "name": "tt",
                        "sections": ["tt", {"lib": section.name, "section": "tt"}],
                    }
                ]
            },
            "options": {
                "osdi_preload": [osdi.name],
                "keep_artifacts": True,
                "portable_artifacts": True,
            },
        },
    )
    _stub_subprocess_run(monkeypatch)
    run_local = sim._BACKENDS["local"]

    def run_and_change_files(**kwargs):
        result = run_local(**kwargs)
        for path in paths:
            if delete:
                path.unlink()
            else:
                with path.open("ab") as handle:
                    handle.write(b"\n* edited during the run\n")
        return result

    monkeypatch.setitem(sim._BACKENDS, "local", run_and_change_files)
    report = sim.run_sim(
        str(request), backend="local", artifacts_dir=str(tmp_path / "out")
    )

    for path, digest in zip(paths, before, strict=True):
        if delete:
            assert not path.exists()
        else:
            assert hashlib.sha256(path.read_bytes()).hexdigest() != digest

    env = report["environment"]
    assert [
        env["models_lib_sha256"],
        env["osdi_preload"][0]["sha256"],
        env["corner_section_libs"][0]["sha256"],
    ] == before
    portable = env["portable_artifacts"]["substitutions"]
    models_dir = next(entry for entry in portable if entry["token"] == "$MODELS_DIR")
    assert models_dir["models_lib_sha256"] == before[0]
    assert report["provenance"]["deck"]["content_hash"] == f"sha256:{before[0]}"
