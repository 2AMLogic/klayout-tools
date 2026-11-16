"""`body_verification` coverage and honesty tests (issue #5749).

Split out of `test_lvs.py` (which is debt-baselined and cannot grow) to
carry the issue-#5749 regression coverage: the body check must count what it
*inspected* (`inspected_count`), must inspect flavoured MOS classes
(`nfet__nvt`-style, `options.compare_mos_flavour`) exactly as it inspects the
generic ones, and must report `unchecked` -- never `verified` -- when zero
devices were eligible for inspection. The determination itself moved into
`klayout_tools.lvs_body` for the same ratchet, together with its unit tests
(formerly `test_lvs.py`'s `test_body_unverified_counts_*` /
`test_body_unverified_message_*` block); they live here now.
"""

from __future__ import annotations

import pytest

from klayout_tools import lvs
from klayout_tools.lvs import run_lvs


@pytest.fixture()
def pdk_root(tmp_path):
    from test_gen import _make_install

    root = tmp_path / "pdk_install"
    _make_install(root, "sky130A")
    return root


def _native_nfet_lvs_report(tmp_path, pdk_root, options):
    """The `tests/test_gen.py:_native_nfet_lvs` shape, inlined: one generated
    `mos_array` native-Vt (`voltage_flavor: "nvt"`) sky130 GDS against a
    `sky130_fd_pr__nfet_05v0_nvt` subckt-call reference, run under the given
    `options`."""
    import json

    from klayout_tools.gen import generate

    gds = tmp_path / "nvt.gds"
    generate(
        {
            "generator": "mos_array",
            "pdk": {"variant": "sky130A", "root": str(pdk_root)},
            "params": {
                "flavor": "nfet",
                "voltage_flavor": "nvt",
                "w_um": 1.0,
                "l_um": 25.0,
                "rows": 1,
                "cols": 1,
                "dummy": 0,
            },
            "options": {"output": str(gds)},
        }
    )
    ref = tmp_path / "ref.spice"
    ref.write_text(
        ".subckt ref d g s b\n"
        "X1 d g s b sky130_fd_pr__nfet_05v0_nvt L=25u W=1u\n.ends\n"
    )
    request = {
        "layout": {"file": str(gds), "deck": "sky130"},
        "reference": {
            "netlist": str(ref),
            "top": "ref",
            "form": "subckt-call",
            "deck": "sky130",
        },
        "options": options,
    }
    return run_lvs(json.dumps(request))


def test_body_verification_inspects_flavoured_classes(tmp_path, pdk_root):
    """Issue #5749 must-bite: with `compare_mos_flavour: true` the flavoured
    class (`nfet__nvt`) is inspected by the body check exactly as the generic
    `nfet` class is under `false`, so the A/B converges instead of reporting
    `verified` with `device_count: 0` on the flavoured arm.

    On the pre-fix code the `True` run reports `status: "verified"`,
    `device_count: 0` -- the vacuous pass this issue exists to close -- so
    this test fails there and passes after the fix."""
    flavoured = _native_nfet_lvs_report(
        tmp_path, pdk_root, {"compare_mos_flavour": True}
    )
    generic = _native_nfet_lvs_report(
        tmp_path, pdk_root, {"compare_mos_flavour": False}
    )

    for report in (flavoured, generic):
        body = report["body_verification"]
        assert body["status"] == lvs.BODY_STATUS_UNVERIFIED, body
        assert body["device_count"] == 1, body
        assert body["inspected_count"] == 1, body
        assert report["status"] == "match"
    # `device_classes` names what was really extracted: the flavoured class
    # on the `True` arm, the generic one on the `False` arm. Everything else
    # about the `False` arm's block is byte-identical to the pre-#5749 shape.
    assert flavoured["body_verification"]["device_classes"] == ["nfet__nvt"]
    assert generic["body_verification"]["device_classes"] == ["nfet"]

    # Same one-source-of-truth discipline as the generic arms: the warning
    # names the flavoured class the block names.
    warnings = {
        m["device"]["class"]: m
        for m in flavoured["mismatches"]
        if m["category"] == lvs.CATEGORY_DEVICE_BODY_UNVERIFIED
    }
    assert set(warnings) == {"nfet__nvt"}
    assert warnings["nfet__nvt"]["severity"] == "warning"


def test_body_verification_unchecked_when_no_mos_device_is_eligible(tmp_path):
    """Issue #5749, flavour-independent instance: a passives-only layout (the
    gf180mcu poly-resistor stream, zero MOS devices) with a deck must report
    `unchecked` with a reason naming the deck's MOS class names -- never the
    vacuous `verified` (with `device_count: 0`) the pre-fix code rendered."""
    from klayout_tools.decks import get_extraction_deck
    from test_lvs import (
        _gf180mcu_poly_res_reference_spice,
        _write,
        _write_gf180mcu_poly_res_gds,
        _write_request,
    )

    gds = _write_gf180mcu_poly_res_gds(tmp_path / "poly_res.gds")
    substrate_net = get_extraction_deck("gf180mcu").substrate_net
    reference_path = _write(
        tmp_path / "ref.spice",
        _gf180mcu_poly_res_reference_spice("RES", 4000.0, substrate_net),
    )
    report = run_lvs(
        _write_request(
            tmp_path / "request.json",
            {
                "layout": {"file": gds, "deck": "gf180mcu"},
                "reference": {"netlist": reference_path, "top": "RES"},
            },
        )
    )

    body = report["body_verification"]
    assert body["status"] == lvs.BODY_STATUS_UNCHECKED, body
    assert body["inspected_count"] == 0, body
    assert body["reason"], body
    # The reason points the reader at the real cause: the deck's MOS class
    # names matched nothing in a passives-only layout.
    assert "'nfet'" in body["reason"] and "'pfet'" in body["reason"]
    assert "no MOS device was eligible" in body["reason"]


# ---------------------------------------------------------------------------
# Unit tests for the determination itself (`klayout_tools.lvs_body`), moved
# here from `test_lvs.py` together with the code (issue #5749 split it out of
# `lvs_mismatch.py`, which is debt-baselined and could not carry them).


def _one_device_circuit(*, body_net_name: str | None):
    """A minimal in-memory KLayout netlist with a single `pfet`
    `DeviceClassMOS4Transistor` whose body terminal reaches `body_net_name`
    (an unnamed net, i.e. KLayout's anonymous placeholder, when `None`).

    Returns `(netlist, circuit)` -- the owning `Netlist` must stay referenced
    by the caller, or KLayout destroys it and every child object with it."""
    import klayout.db as kdb

    netlist = kdb.Netlist()
    pfet_class = kdb.DeviceClassMOS4Transistor()
    pfet_class.name = "pfet"
    netlist.add(pfet_class)
    circuit = kdb.Circuit()
    circuit.name = "TOP"
    netlist.add(circuit)
    device = circuit.create_device(pfet_class, "M1")
    net = (
        circuit.create_net()
        if body_net_name is None
        else circuit.create_net(body_net_name)
    )
    device.connect_terminal("B", net)
    return netlist, circuit


def _gf180mcu_like_deck():
    """A deck stub shaped like gf180mcu: no drawn `tap` layer, but a
    *derivable* one declared via `tap_nplus`/`tap_pplus` (issue #1084) --
    i.e. the exact deck shape whose mere declaration used to suppress the
    PMOS body check entirely."""
    from types import SimpleNamespace

    return SimpleNamespace(
        nfet_class="nfet",
        pfet_class="pfet",
        substrate_net="vsubs",
        tap=None,
        tap_nplus=(32, 0),
        tap_pplus=(31, 0),
    )


def _mixed_arms_circuit():
    """An in-memory circuit mixing the deck's generic `nfet`/`pfet` classes
    with a flavoured `nfet__nvt` one (issue #5749): the two nfet-arm bodies
    reach the deck's synthesized substrate net (the NMOS unverified case),
    the pfet body a named well-tie net."""
    import klayout.db as kdb

    netlist = kdb.Netlist()
    circuit = kdb.Circuit()
    circuit.name = "TOP"
    netlist.add(circuit)
    classes = {}
    for name in ("nfet", "nfet__nvt", "pfet"):
        cls = kdb.DeviceClassMOS4Transistor()
        cls.name = name
        netlist.add(cls)
        classes[name] = cls
    substrate = circuit.create_net("vsubs")
    named = circuit.create_net("VPB")
    m1 = circuit.create_device(classes["nfet"], "M1")
    m2 = circuit.create_device(classes["nfet__nvt"], "M2")
    m3 = circuit.create_device(classes["pfet"], "M3")
    m1.connect_terminal("B", substrate)
    m2.connect_terminal("B", substrate)
    m3.connect_terminal("B", named)
    return netlist, circuit


def test_body_unverified_counts_matches_raw_klayout_anonymous_prefix():
    """Issue #2048's prefix trap, pinned.

    `lvs_body` reads `Net.expanded_name()` straight off the in-memory
    netlist, whose anonymous-net placeholder is a **raw** `"$<n>"`.
    `extract_report.py`'s `_ANONYMOUS_NET_PREFIX` (relocated there out of
    `extract.py` by PR #2071's netlist-report split) is `"\\$"` only because
    every name *that* module reports has already been backslash-escaped by
    `spice_safe_net_name` (issue #1162). Reusing the escaped constant on this
    path would match nothing at all -- silently restoring the bug while every
    other assertion still passed -- so both spellings, and the fact that the
    escaped one does not match a raw name, are asserted here directly.
    """
    from klayout_tools import extract_report as extract_report_mod
    from klayout_tools import lvs_body

    assert lvs_body._RAW_ANONYMOUS_NET_PREFIX == "$"
    assert extract_report_mod._ANONYMOUS_NET_PREFIX == "\\$"
    assert not "$0".startswith(extract_report_mod._ANONYMOUS_NET_PREFIX)

    deck = _gf180mcu_like_deck()

    _netlist, anonymous = _one_device_circuit(body_net_name=None)
    device = next(iter(anonymous.each_device()))
    # KLayout's own raw spelling -- no backslash anywhere in it.
    assert lvs_body._device_body_net_name(device).startswith("$")
    assert "\\" not in lvs_body._device_body_net_name(device)
    inspected, unverified = lvs_body._body_inspected_and_unverified_counts(
        anonymous, deck
    )
    assert inspected == {"pfet": 1}
    assert unverified == {"pfet": 1}

    # A real, named well-tie net is never counted, on the same deck.
    _named_netlist, named = _one_device_circuit(body_net_name="VPB")
    inspected, unverified = lvs_body._body_inspected_and_unverified_counts(named, deck)
    assert inspected == {"pfet": 1}
    assert unverified == {}


def test_body_unverified_counts_flags_pmos_body_reaching_no_net_at_all():
    """A body terminal that reaches no net at all is unverified for the same
    reason an anonymous one is -- nothing biases it. Mirrors the NMOS arm's
    long-standing `None` handling (issue #281)."""
    from klayout_tools import lvs_body

    _netlist, circuit = _one_device_circuit(body_net_name="VPB")
    device = next(iter(circuit.each_device()))
    device.disconnect_terminal("B")

    assert lvs_body._device_body_net_name(device) is None
    inspected, unverified = lvs_body._body_inspected_and_unverified_counts(
        circuit, _gf180mcu_like_deck()
    )
    assert inspected == {"pfet": 1}
    assert unverified == {"pfet": 1}


def test_body_unverified_message_names_unlabeled_tap_cause():
    """Issue #4361: the warning must not claim tap geometry is missing when
    the tap-connected net is merely unnamed (the netlist cannot tell the two
    apart), and must say labeling the conductor resolves it."""
    from klayout_tools import lvs_body

    deck = _gf180mcu_like_deck()
    _netlist, circuit = _one_device_circuit(body_net_name=None)

    (entry,) = lvs_body._body_net_warnings(circuit, deck)
    message = entry["description"]
    assert "no drawn or derived well-tap geometry exists" in message
    assert "carries no name label" in message
    assert "label the tap-connected conductor" in message
    assert entry["severity"] == "warning"


def test_body_unverified_message_nmos_names_both_causes():
    from klayout_tools import lvs_body

    deck = _gf180mcu_like_deck()
    _netlist, circuit = _one_device_circuit(body_net_name="vsubs")
    device = next(iter(circuit.each_device()))
    # reuse the pfet-shaped stub as the deck's nfet class
    deck.nfet_class = device.device_class().name
    deck.pfet_class = "unused"

    (entry,) = lvs_body._body_net_warnings(circuit, deck)
    message = entry["description"]
    assert "no substrate-tap geometry is drawn" in message
    assert "carries no name label" in message
    assert "label the tap-connected conductor" in message


def test_body_check_inspects_flavoured_and_generic_arms_together():
    """Edge case (issue #5749): a layout mixing a flavoured and a generic MOS
    class counts both nfet-arm spellings plus the pfet arm, and the block's
    `device_classes` lists the extracted names. Guards the pre-#5749
    `counts.get(deck.nfet_class, 0)` trap in `_body_net_warnings`: the
    per-class warnings and the block must agree on the flavoured class too,
    or the prose and the JSON would disagree."""
    from klayout_tools import lvs_body

    deck = _gf180mcu_like_deck()
    _netlist, circuit = _mixed_arms_circuit()

    inspected, unverified = lvs_body._body_inspected_and_unverified_counts(
        circuit, deck
    )
    assert inspected == {"nfet": 1, "nfet__nvt": 1, "pfet": 1}
    assert unverified == {"nfet": 1, "nfet__nvt": 1}

    report = lvs_body._body_verification_report(circuit, deck)
    assert report["status"] == lvs.BODY_STATUS_UNVERIFIED
    assert report["device_classes"] == ["nfet", "nfet__nvt"]
    assert report["device_count"] == 2
    assert report["inspected_count"] == 3
    assert report["findings"] == [
        {"class": "nfet", "device_count": 1},
        {"class": "nfet__nvt", "device_count": 1},
    ]

    warnings = {
        entry["device"]["class"]: entry
        for entry in lvs_body._body_net_warnings(circuit, deck)
    }
    assert set(warnings) == {"nfet", "nfet__nvt"}


def test_unchecked_block_always_carries_inspected_count_zero():
    """Issue #5749: `inspected_count` is present in every branch -- including
    the pre-extracted `layout.netlist` unchecked block (issue #1983), which
    runs no determination at all -- so a reader never infers coverage from a
    missing key."""
    from klayout_tools import lvs_body

    block = lvs_body._body_verification_unchecked("no request.layout.deck was given")
    assert block["status"] == lvs.BODY_STATUS_UNCHECKED
    assert block["inspected_count"] == 0
