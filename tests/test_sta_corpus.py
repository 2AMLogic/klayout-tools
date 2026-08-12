"""Reproduces issue #809's own STA-spike accuracy comparison
(`native/statime/README.md` "Results -- accuracy") through the
**integrated** path issue #925 adds, rather than the standalone
`klt-statime critical-path` binary `tests/corpus/statime/compare.py`
drives.

Requires the built `klt_statime_native` extension (`maturin develop
--release` in `native/statime/`) and a real, volare-fetched `sky130A` PDK
install (`klt pdk find` must resolve it) -- every test in this module is
skipped with a clear reason (never silently passed) when either is
unavailable, mirroring `tests/test_mom.py`'s and `tests/test_synthesize.py`'s
own real-PDK integration tests.

Does **not** need Docker/OpenROAD -- `oracle_results.json` is a checked-in
snapshot of a real OpenSTA run (see `tests/corpus/statime/README.md`).
"""

from __future__ import annotations

import importlib.util
import json
import os

import pytest

from klayout_tools.pdk import PdkNotFoundError, find_pdk
from klayout_tools.sta import compute_critical_path

_HAVE_STATIME_NATIVE = importlib.util.find_spec("klt_statime_native") is not None

_CORPUS_DIR = os.path.join(os.path.dirname(__file__), "corpus", "statime")
_DESIGNS = ("gcd", "mult8", "modexp")

#: The spike's own worst-case measured delta was 1.34% (`gcd`, always an
#: *under*-estimate -- see `native/statime/README.md`'s "Results --
#: accuracy" table and "Known simplifications" #5). This integrated-path
#: test uses a slightly looser bound to absorb any liberty-install/toolchain
#: drift between the machine that captured `oracle_results.json` and the one
#: running this test, while still gating on "within the documented
#: tolerance" per issue #925's acceptance criterion.
_MAX_DELTA_PCT = 2.0


def _resolve_liberty() -> str | None:
    try:
        pdk = find_pdk(variant="sky130A")
    except PdkNotFoundError:
        return None
    libs_ref = pdk["assets"]["libs_ref"]
    path = os.path.join(
        libs_ref, "sky130_fd_sc_hd", "lib", "sky130_fd_sc_hd__tt_025C_1v80.lib"
    )
    return path if os.path.isfile(path) else None


_LIBERTY_PATH = _resolve_liberty() if _HAVE_STATIME_NATIVE else None


pytestmark = [
    pytest.mark.skipif(
        not _HAVE_STATIME_NATIVE,
        reason=(
            "klt_statime_native is not built -- run `maturin develop "
            "--release` in native/statime/"
        ),
    ),
    pytest.mark.skipif(
        _HAVE_STATIME_NATIVE and _LIBERTY_PATH is None,
        reason="no real sky130A install (with sky130_fd_sc_hd) resolves via find_pdk()",
    ),
]


@pytest.mark.parametrize("design", _DESIGNS)
def test_integrated_path_matches_opensta_oracle_within_tolerance(design):
    """`compute_critical_path` (the function `klt synthesize`'s `sta` field
    calls) against each corpus design's checked-in mapped netlist, diffed
    against the OpenSTA oracle snapshot -- same fixtures, same boundary
    condition, same comparison `tests/corpus/statime/compare.py` runs
    against the standalone CLI binary, now exercised through the pyo3
    extension `klt synthesize` actually calls."""
    netlist_path = os.path.join(_CORPUS_DIR, f"{design}_netlist.v")
    with open(os.path.join(_CORPUS_DIR, "oracle_results.json"), encoding="utf-8") as fh:
        oracle = json.load(fh)["designs"][design]

    result = compute_critical_path(netlist_path, _LIBERTY_PATH, design)

    rust_ns = result["worst_path"]["delay_ns"]
    oracle_ns = oracle["worst_path_delay_ns"]
    delta_pct = abs(oracle_ns - rust_ns) / oracle_ns * 100.0

    assert delta_pct <= _MAX_DELTA_PCT, (
        f"{design}: integrated klt_statime_native path ({rust_ns:.4f} ns) diverged "
        f"{delta_pct:.2f}% from the OpenSTA oracle ({oracle_ns:.4f} ns) -- exceeds "
        f"the {_MAX_DELTA_PCT}% tolerance"
    )


def test_worst_reg_to_reg_path_present_only_for_sequential_designs():
    """`mult8` is this corpus's purely combinational design (issue #809's
    own README) -- its worst path is input-port-to-output-port, so
    `worst_reg_to_reg_path` must be `None`. `gcd`/`modexp` are sequential --
    theirs must be populated."""
    for design, expect_reg_to_reg in (
        ("gcd", True),
        ("mult8", False),
        ("modexp", True),
    ):
        netlist_path = os.path.join(_CORPUS_DIR, f"{design}_netlist.v")
        result = compute_critical_path(netlist_path, _LIBERTY_PATH, design)
        if expect_reg_to_reg:
            assert result["worst_reg_to_reg_path"] is not None, design
        else:
            assert result["worst_reg_to_reg_path"] is None, design
