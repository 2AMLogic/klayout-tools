"""Offline tests for `scripts/tt_corpus_fetch.py` (issue #5081, Epic #520
Phase 1.A). All network access is a fake transport; no real requests."""

from __future__ import annotations

import importlib.util
import io
import json
import tarfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "tt_corpus_fetch", REPO_ROOT / "scripts" / "tt_corpus_fetch.py"
)
tt = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tt)

INDEX = json.loads(
    (REPO_ROOT / "tests/fixtures/tt_corpus/tt06_index_recorded.json").read_text()
)
APACHE = "Apache License\nVersion 2.0, January 2004\n"
MIT = "MIT License\n\nPermission is hereby granted, free of charge, to any person\n"


def _tar(files: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, text in files.items():
            data = text.encode()
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


class FakeTransport:
    def __init__(self, routes: dict[str, bytes]) -> None:
        self.routes = routes
        self.calls: list[str] = []

    def __call__(self, url: str) -> bytes:
        self.calls.append(url)
        if url not in self.routes:
            raise tt.FetchError(f"{url}: 404")
        return self.routes[url]


def _routes() -> dict[str, bytes]:
    a = "https://github.com/{}/archive/{}.tar.gz"
    return {
        a.format("beaprog/tt06-LFSR", "6bffe5f606707446700dc5dc43db8fa4d6a74394"): _tar(
            {"r-1/LICENSE": APACHE, "r-1/src/p.v": "module m; endmodule"}
        ),
        a.format(
            "EdricOngKhaiJieh/8bitALU_verilog",
            "f9bcfd7d3cb6da2972d1678d9ec5e9402ae9eecb",
        ): _tar({"r-2/LICENSE.md": MIT}),
        a.format("example/nolicense", "1" * 40): _tar(
            {"r-3/src/p.v": "module m; endmodule"}
        ),
    }


def test_walk_filters_by_pdk_and_flags_unfetchable():
    recs = tt.walk_index(INDEX, shuttle="tt06", pdk="sky130A")
    assert [r["macro"] for r in recs] == [
        "tt_um_LFSR_shivam",
        "tt_um_8bitALU",
        "tt_um_nolicense",
        "tt_um_nocommit",
    ]
    assert recs[-1]["unfetchable"] == "index entry has no pinned commit"
    assert [
        r["macro"] for r in tt.walk_index(INDEX, shuttle="tt06", pdk="gf180mcuD")
    ] == ["tt_um_gf"]
    assert len(tt.walk_index(INDEX, shuttle="tt06")) == 5


def test_manifest_fields_and_license_exclusion(tmp_path):
    cache = tt.BlobCache(tmp_path, FakeTransport(_routes()))
    out = tt.run(INDEX, cache, shuttle="tt06", pdk="sky130A")
    by = {d["macro"]: d for d in out["designs"]}
    ok = by["tt_um_LFSR_shivam"]
    assert (ok["repo"], ok["commit"], ok["shuttle"], ok["pdk"]) == (
        "https://github.com/beaprog/tt06-LFSR",
        "6bffe5f606707446700dc5dc43db8fa4d6a74394",
        "tt06",
        "sky130A",
    )
    assert ok["license"] == {"spdx": "Apache-2.0", "file": "LICENSE"}
    assert ok["fetched_at"] and not ok["excluded"]
    assert by["tt_um_8bitALU"]["license"]["spdx"] == "MIT"
    assert by["tt_um_nolicense"]["excluded"] is True
    assert "license" in by["tt_um_nolicense"]["exclusion_reason"]
    assert by["tt_um_nocommit"]["excluded"] is True
    assert (out["included"], out["excluded"]) == (2, 2)
    assert (tmp_path / "manifests/tt06/tt_um_LFSR_shivam.json").is_file()


def test_second_run_makes_zero_network_requests(tmp_path):
    first = FakeTransport(_routes())
    tt.run(INDEX, tt.BlobCache(tmp_path, first), shuttle="tt06", pdk="sky130A")
    assert len(first.calls) == 3
    second = FakeTransport({})  # any request would raise
    out = tt.run(INDEX, tt.BlobCache(tmp_path, second), shuttle="tt06", pdk="sky130A")
    assert second.calls == [] and out["network_requests"] == 0
    assert out["included"] == 2


def test_gds_fetched_via_template_and_failure_recorded(tmp_path):
    routes = _routes()
    routes["https://g/tt_um_LFSR_shivam.gds"] = b"GDS"
    cache = tt.BlobCache(tmp_path, FakeTransport(routes))
    out = tt.run(
        INDEX,
        cache,
        shuttle="tt06",
        pdk="sky130A",
        limit=2,
        gds_url_template="https://g/{macro}.gds",
    )
    by = {d["macro"]: d for d in out["designs"]}
    assert by["tt_um_LFSR_shivam"]["artifacts"]["gds"]["bytes"] == 3
    assert "error" in by["tt_um_8bitALU"]["artifacts"]["gds"]
    assert not by["tt_um_8bitALU"]["excluded"]


def test_pacing_floor_and_user_agent():
    sleeps: list[float] = []
    t = [0.0]
    transport = tt.PacedHttpTransport(2.0, clock=lambda: t[0], sleep=sleeps.append)
    assert transport.user_agent == tt.USER_AGENT
    assert transport.user_agent.startswith("klayout-tools")
    transport._last = 0.0
    t[0] = 0.5
    with pytest.raises(tt.FetchError):
        transport("http://127.0.0.1:9/")  # unreachable; pacing runs first
    assert sleeps == [pytest.approx(1.5)]


def test_cli_with_index_file(tmp_path, capsys):
    idx = tmp_path / "idx.json"
    idx.write_text(json.dumps(INDEX))
    rc = tt.main(
        [
            "--shuttle",
            "tt06",
            "--pdk",
            "sky130A",
            "--index-file",
            str(idx),
            "--cache-dir",
            str(tmp_path / "c"),
        ],
        transport=FakeTransport(_routes()),
    )
    assert rc == 0
    assert json.loads(capsys.readouterr().out)["included"] == 2
