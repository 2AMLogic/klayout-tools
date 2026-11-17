"""``request.pdk`` (issue #5782): the PDK input, define derivation, and
composition behind ``klt functional-verification``'s gate-level
re-simulation -- resolver-unit coverage lives in
``tests/test_functional_verification_pdk_cells.py``, the real-install
end-to-end case in ``tests/test_functional_verification_pdk_live.py``; this
file covers the request/CLI surface, the ``options.from_synthesize``
composition order, the deck cross-check, and the AC5 define policy.

No simulator needed: every case stops at a sentinel standing in for the
cocotb runner import, the convention
``tests/test_functional_verification_from_synthesize.py`` uses.
"""

from __future__ import annotations

import hashlib
import json

import pytest

from helpers.pdk_install_fixtures import isolate_pdk_search_space
from helpers.pdk_install_fixtures import make_install as _make_install
from klayout_tools import functional_verification as fv
from klayout_tools.functional_verification_mutation import (
    _reject_pdk_with_mutations,
)
from klayout_tools.functional_verification_pdk import derive_pdk_defines
from klayout_tools.functional_verification_run_inputs import resolve_run_inputs

NETLIST = b"module gcd(input a, output y); assign y = a; endmodule\n"


class _PastGate(Exception):
    pass


class _Build:
    """Captured ``_run_build`` kwargs (the effective sources + defines)."""

    def __init__(self):
        self.kwargs = None


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _make_verilog_view(variant_dir, lib, *, primitives=True):
    view_dir = variant_dir / "libs.ref" / lib / "verilog"
    view_dir.mkdir(parents=True)
    if primitives:
        (view_dir / "primitives.v").write_text(
            "`celldefine module udp_dff; endmodule `endcelldefine\n",
            encoding="utf-8",
        )
    (view_dir / f"{lib}.v").write_text(
        f"`ifdef FUNCTIONAL\nmodule {lib}__and2; endmodule\n`endif\n", encoding="utf-8"
    )
    return view_dir


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    isolate_pdk_search_space(monkeypatch, tmp_path)


@pytest.fixture
def work(tmp_path, monkeypatch):
    """A request directory beside a one-variant install shipping the library
    (``root`` pinned in every request, so the host's own installs never
    matter)."""
    variant_dir = _make_install(tmp_path / "install", "sky130A", assets=("libs_ref",))
    _make_verilog_view(variant_dir, "sky130_fd_sc_hd")
    (tmp_path / "test_gcd.py").write_text("# testbench\n")
    (tmp_path / "dut.v").write_text(
        "module gcd(input a, output y); assign y = a; endmodule\n"
    )
    build = _Build()

    def _capture_runner():
        return object()

    def _capture_build(_runner, **kwargs):
        build.kwargs = kwargs
        raise _PastGate

    monkeypatch.setattr(fv, "_import_runner", _capture_runner)
    monkeypatch.setattr(fv, "_run_build", _capture_build)
    return tmp_path, build


def _request(work, doc):
    path = work / "request.json"
    path.write_text(json.dumps(doc))
    return str(path)


def _pdk_request(work, **pdk_members):
    doc = {
        "hdl_toplevel": "gcd",
        "testbench": {"module": "test_gcd"},
        "sources": ["dut.v"],
        "pdk": {
            "variant": "sky130A",
            "root": str(work / "install"),
            "cell_library": "sky130_fd_sc_hd",
            **pdk_members,
        },
    }
    return _request(work, doc)


def _view(work):
    return work / "install" / "sky130A" / "libs.ref" / "sky130_fd_sc_hd" / "verilog"


# AC1/AC2: the pdk input, and what it does to the effective sources


def test_pdk_block_prepends_models_in_dependency_order(work):
    """AC2: resolved models precede the caller's sources (and the derived
    ``FUNCTIONAL`` reaches the build) -- no PDK path anywhere in the request
    beyond the install ``root`` pin."""
    tmp, build = work
    request = _pdk_request(tmp)

    with pytest.raises(_PastGate):
        fv.run_functional_verification(request)

    assert build.kwargs["sources"] == [
        str(_view(tmp) / "primitives.v"),
        str(_view(tmp) / "sky130_fd_sc_hd.v"),
        str(tmp / "dut.v"),
    ]
    assert build.kwargs["defines"] == {"FUNCTIONAL": None}


def test_no_pdk_input_leaves_sources_and_defines_untouched(work):
    """AC1: no ``pdk`` block and no CLI flags is byte-identical to today --
    same resolved sources, no derived defines (USE_POWER_PINS stays unset
    and nothing else changes)."""
    tmp, build = work
    doc = {
        "hdl_toplevel": "gcd",
        "testbench": {"module": "test_gcd"},
        "sources": ["dut.v"],
    }

    with pytest.raises(_PastGate):
        fv.run_functional_verification(_request(tmp, doc))

    assert build.kwargs["sources"] == [str(tmp / "dut.v")]
    assert build.kwargs["defines"] == {}


def test_cli_variant_overrides_request_block(tmp_path, monkeypatch):
    """CLI-over-request precedence, the sibling-verb rule: the block names a
    variant the install does not ship the library under; ``--pdk`` names the
    one that does."""
    for variant in ("sky130A", "gf180mcuD"):
        variant_dir = _make_install(tmp_path / "install", variant, assets=("libs_ref",))
        if variant == "gf180mcuD":
            _make_verilog_view(variant_dir, "sky130_fd_sc_hd")
    (tmp_path / "test_gcd.py").write_text("# testbench\n")
    (tmp_path / "dut.v").write_text(
        "module gcd(input a, output y); assign y = a; endmodule\n"
    )

    doc = {
        "hdl_toplevel": "gcd",
        "testbench": {"module": "test_gcd"},
        "sources": ["dut.v"],
        "pdk": {
            "variant": "sky130A",
            "root": str(tmp_path / "install"),
            "cell_library": "sky130_fd_sc_hd",
        },
    }
    monkeypatch.setattr(
        fv,
        "_import_runner",
        lambda: (_ for _ in ()).throw(AssertionError("must not reach the runner")),
    )

    with pytest.raises(fv.FunctionalVerificationError, match="not found under"):
        fv.run_functional_verification(_request(tmp_path, doc))

    sources, inputs = resolve_run_inputs(
        json.loads((tmp_path / "request.json").read_text()),
        str(tmp_path),
        "gcd",
        fv._path_envelope,
        pdk_variant="gf180mcuD",
    )
    assert sources[0].endswith("primitives.v")
    assert inputs.pdk_defines == {"FUNCTIONAL": None}


def test_cli_flags_without_a_pdk_block_are_refused(work):
    """A flag with nothing to apply it to is a request error, never a
    silently-ignored ``--pdk``."""
    tmp, _ = work
    doc = {
        "hdl_toplevel": "gcd",
        "testbench": {"module": "test_gcd"},
        "sources": ["dut.v"],
    }

    with pytest.raises(fv.FunctionalVerificationError, match="no pdk block"):
        fv.run_functional_verification(_request(tmp, doc), pdk_variant="sky130A")
    assert not (tmp / ".klt").exists(), "a build directory was created"


@pytest.mark.parametrize(
    "pdk,match",
    [
        ("sky130A", "request.pdk must be a JSON object"),
        ({"cell_library": "sky130_fd_sc_hd", "corner": "tt"}, "unknown key"),
        ({"variant": "sky130A"}, "cell_library must be a non-empty string"),
        (
            {"cell_library": "sky130_fd_sc_hd", "variant": 7},
            "variant must be a non-empty string or null",
        ),
        (
            {"cell_library": "sky130_fd_sc_hd", "root": ""},
            "root must be a non-empty string or null",
        ),
    ],
)
def test_pdk_block_shape_is_validated(work, pdk, match):
    tmp, _ = work
    doc = {
        "hdl_toplevel": "gcd",
        "testbench": {"module": "test_gcd"},
        "sources": ["dut.v"],
        "pdk": pdk,
    }
    with pytest.raises(fv.FunctionalVerificationError, match=match):
        fv.run_functional_verification(_request(tmp, doc))


def test_relative_root_resolves_against_the_request_dir(tmp_path):
    """AC7: a relative ``pdk.root`` anchors on the request's own directory --
    the same single base every other path field uses (#5673), so the
    committed request stays correct wherever the checkout sits."""
    variant_dir = _make_install(tmp_path / "install", "sky130A", assets=("libs_ref",))
    _make_verilog_view(variant_dir, "sky130_fd_sc_hd")
    req_dir = tmp_path / "req"
    req_dir.mkdir()
    (req_dir / "test_gcd.py").write_text("# testbench\n")
    (req_dir / "dut.v").write_text(
        "module gcd(input a, output y); assign y = a; endmodule\n"
    )
    doc = {
        "hdl_toplevel": "gcd",
        "testbench": {"module": "test_gcd"},
        "sources": ["dut.v"],
        "pdk": {"root": "../install", "cell_library": "sky130_fd_sc_hd"},
    }

    sources, inputs = resolve_run_inputs(doc, str(req_dir), "gcd", fv._path_envelope)

    assert sources == [
        str(_view(tmp_path) / "primitives.v"),
        str(_view(tmp_path) / "sky130_fd_sc_hd.v"),
        str(req_dir / "dut.v"),
    ]
    assert inputs.pdk_defines == {"FUNCTIONAL": None}


# AC4: the three named refusals (resolver-side two live in the _pdk_cells
# module's tests; here: the deck cross-check against from_synthesize)


def _synthesize_report(tmp, *, deck_name="sky130_fd_sc_hd__tt_025C_1v80"):
    report = {
        "hdl_toplevel": "gcd",
        "netlist_path": str(tmp / "gcd_synth.v"),
        "netlist_sha256": _digest(NETLIST),
    }
    if deck_name is not None:
        report["provenance"] = {"deck": {"name": deck_name}}
    path = tmp / "report.json"
    path.write_text(json.dumps(report))
    return path


def _synth_request(tmp, pdk, *, sources=("dut.v",)):
    doc = {
        "hdl_toplevel": "gcd",
        "testbench": {"module": "test_gcd"},
        "options": {"from_synthesize": {"report": "report.json"}},
        "pdk": pdk,
    }
    if sources:
        doc["sources"] = list(sources)
    return doc


def test_from_synthesize_composes_netlist_models_sources_in_order(work):
    """The composition order the docs state: verified netlist, resolved cell
    models, caller sources -- models before the testbench's own files."""
    tmp, _ = work
    (tmp / "gcd_synth.v").write_bytes(NETLIST)
    _synthesize_report(tmp)
    doc = _synth_request(
        tmp,
        {
            "variant": "sky130A",
            "root": str(tmp / "install"),
            "cell_library": "sky130_fd_sc_hd",
        },
    )

    sources, _ = resolve_run_inputs(doc, str(tmp), "gcd", fv._path_envelope)

    assert sources == [
        str(tmp / "gcd_synth.v"),
        str(_view(tmp) / "primitives.v"),
        str(_view(tmp) / "sky130_fd_sc_hd.v"),
        str(tmp / "dut.v"),
    ]


def test_deck_name_mismatch_is_a_named_refusal(work):
    """AC4 refusal 3: the report's deck prefix says the netlist maps
    ``other_lib`` cells; resolving ``sky130_fd_sc_hd`` models would hand it
    cells it never instantiates."""
    tmp, _ = work
    (tmp / "gcd_synth.v").write_bytes(NETLIST)
    _synthesize_report(tmp, deck_name="other_lib__tt_025C_1v80")
    doc = _synth_request(
        tmp,
        {
            "variant": "sky130A",
            "root": str(tmp / "install"),
            "cell_library": "sky130_fd_sc_hd",
        },
    )

    with pytest.raises(
        fv.FunctionalVerificationError, match="does not match the synthesize"
    ) as exc_info:
        resolve_run_inputs(doc, str(tmp), "gcd", fv._path_envelope)
    assert exc_info.value.code == "pdk_cell_library_mismatch"
    assert not (tmp / ".klt").exists(), "a build directory was created"


def test_deck_name_absent_skips_the_cross_check(work):
    """Not every committed synthesize report carries ``provenance.deck.name``
    -- the check refuses only when the name is present."""
    tmp, _ = work
    (tmp / "gcd_synth.v").write_bytes(NETLIST)
    _synthesize_report(tmp, deck_name=None)
    doc = _synth_request(
        tmp,
        {
            "variant": "sky130A",
            "root": str(tmp / "install"),
            "cell_library": "sky130_fd_sc_hd",
        },
    )

    sources, _ = resolve_run_inputs(doc, str(tmp), "gcd", fv._path_envelope)

    assert sources[0] == str(tmp / "gcd_synth.v")


def test_deck_name_exact_library_name_also_matches(work):
    """A deck recorded without the ``__<corner>`` suffix still matches the
    library it was mapped against."""
    tmp, _ = work
    (tmp / "gcd_synth.v").write_bytes(NETLIST)
    _synthesize_report(tmp, deck_name="sky130_fd_sc_hd")
    doc = _synth_request(
        tmp,
        {
            "variant": "sky130A",
            "root": str(tmp / "install"),
            "cell_library": "sky130_fd_sc_hd",
        },
    )

    sources, _ = resolve_run_inputs(doc, str(tmp), "gcd", fv._path_envelope)

    assert sources[1].endswith("primitives.v")


# AC5: the derived define policy


def test_functional_derived_only_without_sdf(work):
    tmp, build = work
    request = _pdk_request(tmp)

    with pytest.raises(_PastGate):
        fv.run_functional_verification(request)

    assert build.kwargs["defines"] == {"FUNCTIONAL": None}


def test_pdk_with_sdf_derives_no_functional_and_never_trips_the_rejection(
    work, monkeypatch
):
    """AC5: with ``options.sdf`` the models must elaborate their *timing*
    branch -- no derived ``FUNCTIONAL`` (and so the request passes the
    ``options.sdf`` + ``FUNCTIONAL`` rejection, which saw caller defines
    only, all the way to the runner)."""
    tmp, _ = work
    (tmp / "delays.sdf").write_text('(DELAYFILE (SDFVERSION "3.0") (DESIGN "gcd"))\n')
    monkeypatch.setattr(fv, "_engine_version", lambda _engine: "13.0")
    doc = {
        "hdl_toplevel": "gcd",
        "testbench": {"module": "test_gcd"},
        "sources": ["dut.v"],
        "pdk": {
            "variant": "sky130A",
            "root": str(tmp / "install"),
            "cell_library": "sky130_fd_sc_hd",
        },
        "options": {"sdf": {"file": "delays.sdf"}},
    }

    with pytest.raises(_PastGate):
        fv.run_functional_verification(_request(tmp, doc))


def test_caller_defines_win_over_the_derived_values(work):
    tmp, build = work
    path = _pdk_request(tmp)
    doc = json.loads((tmp / "request.json").read_text())
    doc["options"] = {"defines": {"FUNCTIONAL": "1", "UNIT_DELAY": None}}
    path = _request(tmp, doc)

    with pytest.raises(_PastGate):
        fv.run_functional_verification(path)

    # Caller FUNCTIONAL keeps its own value; a caller define the derivation
    # would never set (USE_POWER_PINS stays the caller's choice alone)
    # composes through untouched.
    assert build.kwargs["defines"] == {"FUNCTIONAL": "1", "UNIT_DELAY": None}


def test_derive_pdk_defines_unit_cases():
    from klayout_tools.functional_verification_pdk import PdkInput

    pdk_input = PdkInput("sky130A", "/install", "sky130_fd_sc_hd")
    assert derive_pdk_defines(None, {}) == {}
    assert derive_pdk_defines(pdk_input, None) == {"FUNCTIONAL": None}
    assert derive_pdk_defines(pdk_input, {}) == {"FUNCTIONAL": None}
    assert derive_pdk_defines(pdk_input, {"random_seed": 1}) == {"FUNCTIONAL": None}
    assert derive_pdk_defines(pdk_input, {"sdf": None}) == {"FUNCTIONAL": None}
    assert derive_pdk_defines(pdk_input, {"sdf": {"file": "x.sdf"}}) == {}


# edge cases: double-listed models, --mutations


def test_model_already_listed_in_sources_compiles_once(work):
    tmp, _ = work
    doc = {
        "hdl_toplevel": "gcd",
        "testbench": {"module": "test_gcd"},
        "sources": ["install/sky130A/libs.ref/sky130_fd_sc_hd/verilog/primitives.v"],
        "pdk": {
            "variant": "sky130A",
            "root": str(tmp / "install"),
            "cell_library": "sky130_fd_sc_hd",
        },
    }

    sources, _ = resolve_run_inputs(doc, str(tmp), "gcd", fv._path_envelope)

    assert sources.count(str(_view(tmp) / "primitives.v")) == 1
    assert sources == [
        str(_view(tmp) / "primitives.v"),
        str(_view(tmp) / "sky130_fd_sc_hd.v"),
    ]


def test_pdk_input_with_mutations_is_refused(work):
    """The trace/coverage/sdf/from_synthesize refusal posture: per-mutant
    builds do not resolve the pdk block's models, so the combination is a
    request error rather than a silently-diverging mutant build."""
    tmp, _ = work
    doc = {
        "hdl_toplevel": "gcd",
        "testbench": {"module": "test_gcd"},
        "sources": ["dut.v"],
        "pdk": {"cell_library": "sky130_fd_sc_hd"},
    }

    with pytest.raises(fv.FunctionalVerificationError, match="cannot currently"):
        _reject_pdk_with_mutations(doc, None, None)
    with pytest.raises(fv.FunctionalVerificationError, match="cannot currently"):
        _reject_pdk_with_mutations({}, "sky130A", None)
    _reject_pdk_with_mutations({}, None, None)  # no pdk input: unchanged
    _reject_pdk_with_mutations({"pdk": None}, None, None)
