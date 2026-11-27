"""Default ``pin_order_sources`` path tokenisation in ``run_lvs`` (issue
#5812): a pin-order file under the resolved PDK root is recorded as
``$PDK_ROOT/<rel>`` even without ``portable_paths``, while caller-supplied
files outside the PDK root stay absolute. Lives apart from ``test_lvs.py``
to respect the file-size ratchet, like ``test_lvs_repeated_label.py``.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

import test_lvs as base
from klayout_tools.cli import main
from klayout_tools.lvs import LvsError, rerun_lvs_report, run_lvs

pytestmark = pytest.mark.usefixtures("real_build_identity_git")


def test_run_lvs_portable_paths_default_tokenises_pdk_rooted_pin_order_sources(
    tmp_path,
):
    """Issue #5812 (narrow exception to #3798's byte-identical default):
    without `portable_paths`, a pin-order file under the PDK root is recorded
    as `$PDK_ROOT/<rel>` -- no absolute home path leaks -- and an explicit
    `portable_paths: false` is identical."""
    from klayout_tools.env_provenance import find_absolute_path_fields

    request, _ = base._portable_macro_request(tmp_path)
    explicit_false, _ = base._portable_macro_request(tmp_path, portable_paths=False)
    report = run_lvs(request)
    sources = report["provenance"]["pin_order_sources"]
    assert [entry["path"] for entry in sources] == [
        "$PDK_ROOT/sky130A/libs.ref/sky130_fd_sc_hd/spice/sky130_fd_sc_hd.spice",
        "$PDK_ROOT/sky130A/libs.ref/sky130_sram_macros/spice/"
        "sky130_sram_1kbyte_1rw1r_32x256_8.spice",
        "$PDK_ROOT/sky130A/libs.ref/sky130_sram_macros/spice/"
        "sky130_sram_2kbyte_1rw1r_32x512_8.spice",
    ]
    assert not [
        hit
        for hit in find_absolute_path_fields(report)
        if "pin_order_sources" in str(hit)
    ]
    assert run_lvs(explicit_false)["provenance"]["pin_order_sources"] == sources


def test_run_lvs_default_keeps_non_pdk_pin_order_files_absolute(tmp_path):
    """Issue #5812: only PDK-rooted sources are tokenised. A caller-supplied
    `reference.pin_order_files` entry outside the PDK root stays absolute by
    default, including a sibling dir whose name merely prefix-matches the PDK
    root."""
    root = base._make_fake_macro_pdk(tmp_path)
    lookalike = tmp_path / "pdk_install_extra"
    lookalike.mkdir()
    macro_path = base._write(lookalike / "macro.spice", base._SRAM_MACRO_SPICE)
    request = base._macro_request(
        tmp_path,
        {
            "library": "sky130_fd_sc_hd",
            "pdk": "sky130A",
            "pdk_root": root,
            "pin_order_files": [macro_path],
        },
    )
    sources = run_lvs(request)["provenance"]["pin_order_sources"]
    assert sources[0]["path"].startswith("$PDK_ROOT/")
    assert sources[-1]["library"] is None
    assert os.path.isabs(sources[-1]["path"])
    assert os.path.basename(sources[-1]["path"]) == "macro.spice"


def test_pin_order_tokenisation_preserves_existing_tokens_and_relative_paths(
    tmp_path, monkeypatch
):
    from klayout_tools.lvs_report import _tokenise_pdk_pin_order_sources

    monkeypatch.chdir(tmp_path)
    sources = [
        {"path": "$PDK_ROOT/lib.spice", "cells": 2},
        {"path": "relative/lib.spice", "cells": 1},
    ]
    assert _tokenise_pdk_pin_order_sources(sources, str(tmp_path)) == sources


@pytest.mark.parametrize("source", ["libraries", "repo", "pdk", "repo-only"])
def test_portable_gate_level_report_replays_from_relocated_checkout(
    tmp_path, monkeypatch, capsys, source
):
    repo = tmp_path / "producer"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    monkeypatch.chdir(repo)
    if source == "libraries":
        raw, _ = base._portable_macro_request(repo)
    else:
        raw = base._gate_level_replay_request(repo, with_library=source != "repo-only")
    request = json.loads(raw)
    reference = request["reference"]
    if source == "pdk":
        # Leave variant selection to the producer environment and move the
        # explicit file under the installation so it gets a PDK token too.
        monkeypatch.setenv("PDK", reference.pop("pdk"))
        target = Path(reference["pdk_root"]) / "custom.spice"
        Path(reference["pin_order_files"][0]).rename(target)
        reference["pin_order_files"] = [str(target)]
    request["portable_paths"] = True
    original = run_lvs(json.dumps(request))
    assert original["status"] == "match"
    evidence = repo / "evidence"
    evidence.mkdir()
    report = Path(base._commit_report(evidence, original))
    saved = report.read_bytes()
    checkout = tmp_path / "verifier"
    monkeypatch.chdir(tmp_path)
    repo.rename(checkout)
    report = checkout / report.relative_to(repo)
    monkeypatch.setenv("PDK_ROOT", str(checkout / "pdk_install"))
    monkeypatch.setenv("PDK", "different-variant")
    args = ["lvs", "--check", str(report), "--rerun", "--format", "json"]
    assert main(args) == 0
    replay = json.loads(capsys.readouterr().out)
    assert replay["status"] == "match" and replay["drift"] == []
    assert replay["fresh"]["reference_conversion"] == original["reference_conversion"]
    assert report.read_bytes() == saved

    if source == "libraries":
        library = next((checkout / "pdk_install").rglob("sky130_fd_sc_hd.spice"))
        with library.open("a") as handle:
            handle.write("\n.subckt changed A B\n.ends changed\n")
        assert main(args) == 3
        drift = json.loads(capsys.readouterr().out)["drift"]
        assert any("pin_order_sources" in item["field"] for item in drift)


def test_portable_replay_identifies_unlocatable_explicit_pin_order_file(
    tmp_path, monkeypatch
):
    repo = tmp_path / "project"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    monkeypatch.chdir(repo)
    request = json.loads(base._gate_level_replay_request(repo, with_library=False))
    source = Path(request["reference"]["pin_order_files"][0])
    external = tmp_path / source.name
    source.rename(external)
    request["reference"]["pin_order_files"] = [str(external)]
    request["portable_paths"] = True
    report = run_lvs(json.dumps(request))
    with pytest.raises(
        LvsError, match=r"reference_conversion\.pin_order_files\[0\].*external"
    ):
        rerun_lvs_report(base._commit_report(repo, report))
