"""Behavioural cell models for a descriptor-declared PDK root (issue #7695).

``klt-pdk.json`` ``views.<cell_library>.verilog`` is an ordered file list that
``request.pdk`` prepends exactly as it does the ``libs.ref/<lib>/verilog/``
convention's files (issue #5782). Every tree is fabricated under ``tmp_path``
-- no vendored PDK data -- and, like ``test_functional_verification_pdk.py``,
nothing needs a simulator: the cases stop at ``resolve_run_inputs``.

Library names follow the ASAP7 assembler (``asap7sc7p5t_<vt lowercase>``).
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from helpers.pdk_install_fixtures import isolate_pdk_search_space
from klayout_tools import functional_verification as fv
from klayout_tools import pdk, pdk_cell_models
from klayout_tools._artifact_envelope import path_envelope
from klayout_tools.functional_verification_run_inputs import resolve_run_inputs
from klayout_tools.pdk_cell_models import resolve_cell_models_for_cell_library
from klayout_tools.pdk_descriptor import DescriptorError, load_descriptor

LIB = "asap7sc7p5t_rvt"
# Deliberately not alphabetical: the declared order is the contract.
MODELS = ["SEQ", "AO", "SIMPLE"]
NETLIST = b"module gcd(input a, output y); assign y = a; endmodule\n"


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    isolate_pdk_search_space(monkeypatch, tmp_path)


def _descriptor_root(tmp_path, *, views=None, assets=None):
    """A descriptor root with the models under ``models/`` and no ``libs.ref``
    convention directory at all."""
    root = tmp_path / "asap7"
    (root / "models").mkdir(parents=True)
    for group in MODELS:
        (root / "models" / f"{group}.v").write_text(
            f"module {group}x1_rvt; endmodule\n", encoding="utf-8"
        )
    descriptor = {
        "schema_version": 1,
        "variant": "asap7",
        "assets": {} if assets is None else assets,
        "views": (
            {LIB: {"verilog": [f"models/{group}.v" for group in MODELS]}}
            if views is None
            else views
        ),
    }
    (root / "klt-pdk.json").write_text(json.dumps(descriptor), encoding="utf-8")
    return root


def _paths(root):
    return [str(root / "models" / f"{group}.v") for group in MODELS]


def _resolve(root, cell_library=LIB):
    return resolve_cell_models_for_cell_library(
        cell_library, fv.FunctionalVerificationError, variant="asap7", root=str(root)
    )


# --- descriptor parsing -----------------------------------------------------


def test_verilog_view_is_parsed_in_declared_order(tmp_path):
    root = _descriptor_root(tmp_path)
    views = load_descriptor(str(root))["views"]
    assert views[LIB]["verilog"] == _paths(root)


@pytest.mark.parametrize(
    "value, match",
    [
        ("models/SEQ.v", "non-empty list"),
        ([], "non-empty list"),
        ({"a": "models/SEQ.v"}, "non-empty list"),
        ([1], r"expected a non-empty relative path string"),
        (["models/SEQ.v", ""], r"verilog'\[1\]"),
        (["models/missing.v"], r"verilog'\[0\].*does not exist"),
        (["/abs/SEQ.v"], "must be relative"),
        (["../outside.v"], "escapes the PDK root"),
        (["models"], r"verilog'\[0\].*does not exist"),  # a directory, not a file
    ],
)
def test_malformed_verilog_view_names_file_and_field(tmp_path, value, match):
    root = _descriptor_root(tmp_path, views={LIB: {"verilog": value}})
    with pytest.raises(DescriptorError, match=match) as exc_info:
        load_descriptor(str(root))
    assert f"klt-pdk.json: field 'views.{LIB}.verilog'" in str(exc_info.value)


# --- the resolver -----------------------------------------------------------


def test_resolver_returns_the_descriptor_list_verbatim(tmp_path):
    root = _descriptor_root(tmp_path)
    models, info = _resolve(root)
    assert models == _paths(root)
    assert info["variant"] == "asap7" and info["layout"] == "descriptor"


def test_resolver_does_not_probe_primitives_or_libs_ref(tmp_path):
    """A ``libs.ref/<lib>/verilog/`` convention view beside the descriptor
    list is ignored: the descriptor wins and nothing is reordered or added."""
    root = _descriptor_root(tmp_path, assets={"libs_ref": "libs"})
    view = root / "libs" / LIB / "verilog"
    view.mkdir(parents=True)
    (view / "primitives.v").write_text("module p; endmodule\n", encoding="utf-8")
    (view / f"{LIB}.v").write_text("module q; endmodule\n", encoding="utf-8")
    models, _ = _resolve(root)
    assert models == _paths(root)


def test_library_without_verilog_key_falls_through_to_the_convention(tmp_path):
    root = _descriptor_root(tmp_path, assets={"libs_ref": "libs"})
    other = "asap7sc7p5t_lvt"
    # Mapped in ``views`` for other keys only: still the convention's answer.
    desc = json.loads((root / "klt-pdk.json").read_text())
    desc["views"][other] = {"cell_lef": "models/SEQ.v"}
    (root / "klt-pdk.json").write_text(json.dumps(desc), encoding="utf-8")
    view = root / "libs" / other / "verilog"
    view.mkdir(parents=True)
    (view / f"{other}.v").write_text("module m; endmodule\n", encoding="utf-8")

    models, _ = _resolve(root, other)
    assert models == [str(view / f"{other}.v")]


@pytest.mark.parametrize("library", ["asap7sc7p5t_lvt", "unmapped"])
def test_unmapped_library_still_refuses_with_pdk_no_verilog_view(tmp_path, library):
    root = _descriptor_root(tmp_path)
    with pytest.raises(fv.FunctionalVerificationError) as exc_info:
        _resolve(root, library)
    assert exc_info.value.code == "pdk_no_verilog_view"


def test_bad_descriptor_is_error_cls_not_pdk_not_found(tmp_path):
    root = _descriptor_root(tmp_path, views={LIB: {"verilog": []}})
    with pytest.raises(fv.FunctionalVerificationError, match="non-empty list"):
        _resolve(root)


def test_descriptor_failure_after_find_pdk_is_error_cls(tmp_path, monkeypatch):
    """The ``descriptor_views`` call re-reads the descriptor; a failure there
    is wrapped like ``find_pdk``'s, never leaked as ``PdkNotFoundError``."""
    root = _descriptor_root(tmp_path)

    def _fail(_info, _cell_library):
        raise pdk.PdkNotFoundError("descriptor vanished")

    monkeypatch.setattr(pdk_cell_models, "descriptor_views", _fail)
    with pytest.raises(fv.FunctionalVerificationError, match="descriptor vanished"):
        _resolve(root)


# --- composition into the run's sources --------------------------------------


def _request(root, **extra):
    return {
        "hdl_toplevel": "gcd",
        "testbench": {"module": "test_gcd"},
        "options": {"from_synthesize": {"report": "report.json"}},
        "sources": ["dut.v"],
        "pdk": {"variant": "asap7", "root": str(root), "cell_library": LIB},
        **extra,
    }


def _write_report(tmp_path, deck_name="asap7sc7p5t_rvt__TT"):
    (tmp_path / "gcd_synth.v").write_bytes(NETLIST)
    (tmp_path / "dut.v").write_text("module tb; endmodule\n", encoding="utf-8")
    (tmp_path / "report.json").write_text(
        json.dumps(
            {
                "hdl_toplevel": "gcd",
                "netlist_path": str(tmp_path / "gcd_synth.v"),
                "netlist_sha256": hashlib.sha256(NETLIST).hexdigest(),
                "provenance": {"deck": {"name": deck_name}},
            }
        )
    )


def test_models_reach_environment_sources_in_composition_order(tmp_path):
    """``[netlist, *models, *caller sources]``, each echoed into
    ``environment.sources`` with a repo-relative path and its hash; the
    composition code is unchanged."""
    root = _descriptor_root(tmp_path)
    _write_report(tmp_path)

    def envelope(path):
        return path_envelope(path, repo_root=str(tmp_path))

    sources, inputs = resolve_run_inputs(_request(root), str(tmp_path), "gcd", envelope)

    expected = [str(tmp_path / "gcd_synth.v"), *_paths(root), str(tmp_path / "dut.v")]
    assert sources == expected
    assert inputs.sources == [  # what ``environment.sources`` carries
        {
            "path": os.path.relpath(path, tmp_path),
            "scope": "repo",
            "sha256": hashlib.sha256(Path(path).read_bytes()).hexdigest(),
        }
        for path in expected
    ]


def test_model_listed_by_the_caller_compiles_once_in_declared_position(tmp_path):
    root = _descriptor_root(tmp_path)
    _write_report(tmp_path)
    doc = _request(root, sources=["asap7/models/AO.v", "dut.v"])

    sources, _ = resolve_run_inputs(doc, str(tmp_path), "gcd", fv._path_envelope)

    assert sources == [
        str(tmp_path / "gcd_synth.v"),
        *_paths(root),
        str(tmp_path / "dut.v"),
    ]
    assert sources.count(str(root / "models" / "AO.v")) == 1


def test_cell_library_mismatch_refuses_before_any_model_resolves(tmp_path, monkeypatch):
    """Ask 3 on the descriptor route: a netlist synthesized against the LVT
    library must not be paired with RVT models, and the refusal happens
    before the descriptor's model list is ever consulted."""
    root = _descriptor_root(tmp_path)
    _write_report(tmp_path, deck_name="asap7sc7p5t_lvt__TT")

    def _never(*_args, **_kwargs):
        raise AssertionError("models resolved before the deck cross-check")

    monkeypatch.setattr(pdk_cell_models, "resolve_cell_models_for_cell_library", _never)

    with pytest.raises(
        fv.FunctionalVerificationError, match="does not match the synthesize"
    ) as exc_info:
        resolve_run_inputs(_request(root), str(tmp_path), "gcd", fv._path_envelope)
    assert exc_info.value.code == "pdk_cell_library_mismatch"
    assert "asap7sc7p5t_lvt" in str(exc_info.value)
