"""Fake-`PyNEC` contract tests for `scripts/mom_nec_reference.py`'s bent-path
driving-point mode (issue #3037, child of #2730).

These run without NEC2++ (or `klt_mom_native`) installed: a recording fake
`PyNEC.nec_context` is injected into `sys.modules`, so the tests pin the
adapter's translation (wire tags, endpoints, source placement, segment
counts) and its malformed-input diagnostics. The live NEC2++ coverage is in
`tests/test_mom_cross_validation.py`.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import types
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "mom_nec_reference.py"


class _FakeGeometry:
    def __init__(self, calls):
        self._calls = calls

    def wire(self, *args):
        self._calls.append(("wire", args))


class _FakeContext:
    calls: list = []

    def __init__(self):
        type(self).calls = []

    def get_geometry(self):
        return _FakeGeometry(type(self).calls)

    def geometry_complete(self, *a):
        type(self).calls.append(("geometry_complete", a))

    def gn_card(self, *a):
        type(self).calls.append(("gn_card", a))

    def fr_card(self, *a):
        type(self).calls.append(("fr_card", a))

    def ex_card(self, *a):
        type(self).calls.append(("ex_card", a))

    def xq_card(self, *a):
        type(self).calls.append(("xq_card", a))

    def get_impedance_real(self, _):
        return 12.5

    def get_impedance_imag(self, _):
        return -3.25


@pytest.fixture
def nec(monkeypatch):
    fake = types.ModuleType("PyNEC")
    fake.nec_context = _FakeContext
    monkeypatch.setitem(sys.modules, "PyNEC", fake)
    _FakeContext.calls = []
    spec = importlib.util.spec_from_file_location(
        "mom_nec_reference_under_test", _SCRIPT
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _request(**overrides):
    request = {
        "mode": "bent_path_driving_point",
        "signal_path_um": [[0, 0, 0], [100, 0, 0], [100, 50, 0]],
        "return_path_um": [[100, 70, 0], [80, 70, 0], [80, 20, 0], [0, 20, 0]],
        "radius_um": 1.0,
        "frequency_hz": 1.0e9,
        "segment_size_um": 20.0,
    }
    request.update(overrides)
    return request


def _wires():
    return [args for kind, args in _FakeContext.calls if kind == "wire"]


def test_legs_translated_in_traversal_order(nec):
    result = nec.solve(_request())
    wires = _wires()
    um = 1e-6

    # signal legs, far connector, return legs, near feed stub -- tags 1..7
    assert [w[0] for w in wires] == [1, 2, 3, 4, 5, 6, 7]
    expected = [
        ((0, 0, 0), (100, 0, 0)),
        ((100, 0, 0), (100, 50, 0)),
        ((100, 50, 0), (100, 70, 0)),  # far connector
        ((100, 70, 0), (80, 70, 0)),
        ((80, 70, 0), (80, 20, 0)),
        ((80, 20, 0), (0, 20, 0)),
        ((0, 20, 0), (0, 0, 0)),  # near feed stub: return end -> signal start
    ]
    for w, (a, b) in zip(wires, expected, strict=True):
        assert w[2:5] == pytest.approx(tuple(c * um for c in a))
        assert w[5:8] == pytest.approx(tuple(c * um for c in b))
        assert w[8] == pytest.approx(1.0 * um)
    assert [w["role"] for w in result["wires"]][2] == "far_connector"
    assert result["wires"][-1]["role"] == "near_feed_stub"


def test_segment_counts_odd_and_derived_from_segment_size(nec):
    result = nec.solve(_request())
    # lengths 100, 50 (-> 5, 3 after odd bump of 3), connector/stub = 1, 20, 50, 80
    counts = {w["role"]: w["segments"] for w in result["wires"]}
    assert counts["signal_path_um[0]"] == 5
    assert counts["signal_path_um[1]"] == 3
    assert counts["far_connector"] == 1
    assert counts["return_path_um[0]"] == 1
    assert counts["return_path_um[1]"] == 3
    assert counts["return_path_um[2]"] == 5
    assert counts["near_feed_stub"] == 1
    for w in _wires():
        assert w[1] % 2 == 1


def test_source_on_near_feed_stub(nec):
    result = nec.solve(_request())
    ex = [args for kind, args in _FakeContext.calls if kind == "ex_card"]
    assert len(ex) == 1
    # (type, tag, segment, ...): voltage source on the stub's only segment.
    assert ex[0][0] == 0
    assert ex[0][1] == 7 and ex[0][2] == 1
    assert ex[0][4] == 1.0
    assert result["source"] == {"tag": 7, "segment": 1, "voltage_v": 1.0}
    fr = [args for kind, args in _FakeContext.calls if kind == "fr_card"][0]
    assert fr[2] == pytest.approx(1000.0)  # MHz


def test_reports_driving_point_only(nec):
    result = nec.solve(_request())
    assert result["z_in_real_ohm"] == 12.5
    assert result["z_in_imag_ohm"] == -3.25
    assert result["frequency_hz"] == 1.0e9
    assert result["tool"]["name"] == "nec2++"
    for key in (
        "z0_real_ohm",
        "attenuation_np_per_m",
        "s11_real",
        "s21_real",
        "z_oc_real_ohm",
    ):
        assert key not in result


def test_explicit_segments_per_leg_validated(nec):
    nec.solve(_request(segments_per_leg=[5, 3, 1, 1, 3, 5, 1]))
    with pytest.raises(nec.SpecError, match=r"segments_per_leg.*odd"):
        nec.solve(_request(segments_per_leg=4))
    with pytest.raises(
        nec.SpecError, match=r"segments_per_leg.*exceed segment_size_um"
    ):
        nec.solve(_request(segments_per_leg=1))


@pytest.mark.parametrize(
    ("overrides", "match"),
    [
        ({"signal_path_um": [[0, 0, 0]]}, "signal_path_um"),
        (
            {"signal_path_um": [[0, 0, 0], [0, 0, 0]]},
            r"signal_path_um\[0\]: zero-length",
        ),
        (
            {"signal_path_um": [[0, 0, 0], [float("nan"), 0, 0]]},
            r"signal_path_um\[1\]: non-finite",
        ),
        ({"signal_path_um": [[0, 0, 0], [1, 0]]}, r"signal_path_um\[1\]"),
        ({"return_path_um": [[100, 50, 0], [0, 20, 0]]}, "far_connector: zero-length"),
        ({"return_path_um": [[100, 70, 0], [0, 0, 0]]}, "near_feed_stub: zero-length"),
        ({"return_path_um": [[100, 70, 0], [100, 0, 0], [0, 20, 0]]}, "coincident"),
        ({"radius_um": 0}, "radius_um"),
        ({"radius_um": -1.0}, "radius_um"),
        ({"frequency_hz": 0}, "frequency_hz"),
        ({"frequency_hz": float("inf")}, "frequency_hz"),
        ({"segment_size_um": 0}, "segment_size_um"),
        ({"mode": "nope"}, "mode"),
    ],
)
def test_malformed_requests_rejected_with_field(nec, overrides, match):
    with pytest.raises(nec.SpecError, match=match):
        nec.solve(_request(**overrides))
    assert not _wires()  # rejected before any NEC geometry is built


def test_cli_reports_invalid_request_without_traceback():
    proc = subprocess.run(
        [sys.executable, str(_SCRIPT)],
        input=json.dumps(_request(radius_um=0)),
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 2
    assert "radius_um" in proc.stderr
    assert "Traceback" not in proc.stderr
    assert proc.stdout == ""
