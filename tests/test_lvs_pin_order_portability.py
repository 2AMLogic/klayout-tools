"""Default ``pin_order_sources`` path tokenisation in ``run_lvs`` (issue
#5812): a pin-order file under the resolved PDK root is recorded as
``$PDK_ROOT/<rel>`` even without ``portable_paths``, while caller-supplied
files outside the PDK root stay absolute. Lives apart from ``test_lvs.py``
to respect the file-size ratchet, like ``test_lvs_repeated_label.py``.
"""

from __future__ import annotations

import os

import pytest

import test_lvs as base
from klayout_tools.lvs import run_lvs

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
