"""Issue #5318: ``.pdk-store.json`` client/worker PDK stamps agree."""

from __future__ import annotations

import json

from klayout_tools import sim_batch as sb
from test_sim_batch import _run_capture_program


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
