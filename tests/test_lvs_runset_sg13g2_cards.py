"""Issues #7836 and #7855: SG13G2 bipolar, MOS, ``cap_cmim`` and poly-resistor
``X`` calls through ``klt lvs --engine klayout-runset`` on the **pinned** IHP
``ihp-sg13g2`` runset.

The runset's reader converts no ``X`` card, so a bipolar call used to vanish
from the compare (reference ``npn13G2v`` count 66 against 67 laid out, no
finding) and a MOS call landed under generic ``nfet``/``pfet``. The controls
are the PDK's own ``bjt_devices/npn13G2v`` layout and CDL netlist: 67 bipolars
written as native ``Q`` cards (and, for #7855, its ``cap_devices`` / ``res_devices``
/ ``ind_devices`` counterparts). Everything skips cleanly when the standalone
``klayout`` binary, the IHP install or the pinned runset is absent -- the
hermetic coverage of the same conversion is in
``tests/test_lvs_runset_reference.py``.
"""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

import klayout.db as kdb
import pytest

from klayout_tools.lvs import run_lvs
from klayout_tools.signoff_runset import PINNED_RUNSETS

pytestmark = pytest.mark.skipif(
    shutil.which("klayout") is None, reason="klayout binary absent"
)

_IHP_HASH = next(h for h, e in PINNED_RUNSETS.items() if e.get("pdk") == "ihp-sg13g2")
_UNRESOLVED = "reference.unresolved_subckt_call"


@pytest.fixture(scope="module")
def pdk_lvs() -> Path:
    from klayout_tools import pdk
    from klayout_tools._provenance import sha256_file

    try:
        root = Path(pdk.find_pdk(variant="ihp-sg13g2")["assets"]["klayout"])
    except Exception:  # any resolution failure means "absent"
        pytest.skip("no ihp-sg13g2 PDK install")
    lvs = root / "tech" / "lvs"
    deck = lvs / "sg13g2.lvs"
    if not deck.is_file():
        pytest.skip("ihp-sg13g2 install lacks the sg13g2.lvs runset")
    if sha256_file(str(deck)) != _IHP_HASH.removeprefix("sha256:"):
        pytest.skip(f"installed sg13g2.lvs does not hash to the pin ({_IHP_HASH})")
    if not (lvs / "testing/testcases/unit/bjt_devices/layout/npn13G2v.gds").is_file():
        pytest.skip("ihp-sg13g2 install lacks the bjt_devices unit controls")
    return lvs


def _control(pdk_lvs: Path, tmp_path: Path) -> tuple[str, str]:
    """The npn13G2v layout (top renamed ``TOP``) and CDL text (``top``): the
    case-only top difference the request reconciles."""
    unit = pdk_lvs / "testing/testcases/unit/bjt_devices"
    layout = kdb.Layout()
    layout.read(str(unit / "layout/npn13G2v.gds"))
    layout.top_cell().name = "TOP"
    gds = tmp_path / "npn13G2v_top.gds"
    layout.write(str(gds))
    cdl = (unit / "netlist/npn13G2v.cdl").read_text()
    cdl = re.sub(r"(?im)^\.SUBCKT npn13G2v\s*$", ".SUBCKT top", cdl)
    cdl = re.sub(r"(?im)^\.ENDS.*$", ".ENDS top", cdl)
    return str(gds), cdl


def _lvs(pdk_lvs: Path, tmp_path: Path, gds: str, spice: str, **reference) -> dict:
    ref = tmp_path / f"ref_{abs(hash(spice))}.sp"
    ref.write_text(spice)
    return run_lvs(
        json.dumps(
            {
                "engine": "klayout-runset",
                "deck_file": str(pdk_lvs / "sg13g2.lvs"),
                "layout": {"file": gds, "top": "TOP"},
                "reference": {"netlist": str(ref), "top": "top", **reference},
            }
        )
    )


def _as_calls(cdl: str, only: set[str] | None = None) -> str:
    """Rewrite the control's ``Q`` cards as the xschem simulation form
    ``X.. C B E S npn13G2v Nx= El=`` (``m`` -> ``Nx``, ``le`` metres -> ``El``
    micrometres), for every card or just the named ones."""
    card = re.compile(r"(?i)^(Q\S*)\s+(\S+\s+\S+\s+\S+\s+\S+)\s+npn13G2v\s+(.*)$")
    out = []
    for line in cdl.splitlines():
        m = card.match(line)
        if m and (only is None or m.group(1) in only):
            fields = dict(re.findall(r"(\w+)=(\S+)", m.group(3)))
            le = fields["le"].lower().removesuffix("u")
            nx = f" Nx={fields['m']}" if "m" in fields else ""
            line = f"X{m.group(1)} {m.group(2)} npn13G2v{nx} El={le}"
        out.append(line)
    return "\n".join(out) + "\n"


def _unresolved(report: dict) -> list[dict]:
    return [m for m in report["mismatches"] if m["category"] == _UNRESOLVED]


def test_one_converted_call_restores_the_native_bipolar_count(pdk_lvs, tmp_path):
    gds, cdl = _control(pdk_lvs, tmp_path)
    native = _lvs(pdk_lvs, tmp_path, gds, cdl)
    assert native["device_census"]["layout"] == {"npn13G2v": 67}
    assert native["device_census"]["reference"] == {"NPN13G2V": 67}

    # The one-card substitution from the issue: 66 on the reference side
    # before the fix, with no finding naming the lost call.
    swapped = _lvs(
        pdk_lvs,
        tmp_path,
        gds,
        _as_calls(cdl, {"Q2"}),
        form="subckt-call",
    )
    assert swapped["device_census"]["reference"] == {"NPN13G2V": 67}
    assert swapped["device_census"]["reference_source"] == {"NPN13G2V": 67}
    assert not _unresolved(swapped)

    # Negative control: remove the card entirely and the count falls to 66.
    removed = _lvs(
        pdk_lvs,
        tmp_path,
        gds,
        "\n".join(x for x in cdl.splitlines() if not x.startswith("Q2 ")) + "\n",
    )
    assert removed["device_census"]["reference"] == {"NPN13G2V": 66}


def test_every_converted_call_agrees_with_its_native_q_control(pdk_lvs, tmp_path):
    gds, cdl = _control(pdk_lvs, tmp_path)
    native = _lvs(pdk_lvs, tmp_path, gds, cdl)
    calls = _lvs(pdk_lvs, tmp_path, gds, _as_calls(cdl), form="subckt-call")
    # Terminal order, class and compared parameters (emitter length and
    # multiplicity) all pair exactly as the native cards do: the same device
    # tallies and the same findings, the control's own port findings included.
    assert calls["device_census"]["reference"] == native["device_census"]["reference"]
    assert calls["counts"] == native["counts"]
    assert calls["category_counts"] == native["category_counts"]
    assert not _unresolved(calls)


CHILD = """\
.subckt top a b c d sub
Xchild a b c d sub child
.ends top
.subckt child a b c d sub
XQ1 a b c sub npn13G2v Nx=1 El=1.15
XM1 a b c sub sg13_hv_nmos W=1u L=0.45u
XM2 a b c sub sg13_hv_pmos W=1u L=0.45u
{extra}.ends child
"""


def test_mixed_child_calls_reach_the_runset_classes(pdk_lvs, tmp_path):
    gds, _ = _control(pdk_lvs, tmp_path)
    report = _lvs(pdk_lvs, tmp_path, gds, CHILD.format(extra=""), form="subckt-call")
    # Not the generic ``{"nfet": 1, "pfet": 1}`` plus a dropped bipolar: the
    # MOS classes carry the runset's own names, as the layout side's do.
    assert report["device_census"]["reference"] == {
        "NPN13G2V": 1,
        "sg13_hv_nmos": 1,
        "sg13_hv_pmos": 1,
    }
    assert not _unresolved(report)  # the real ``child`` call is no false positive


def test_unconvertible_call_is_named_while_its_siblings_compare(pdk_lvs, tmp_path):
    gds, _ = _control(pdk_lvs, tmp_path)
    # No ``El``: the symbol's LVS form cannot be written, so the call stays
    # a call and the scan names it -- beside an identical class that converted.
    report = _lvs(
        pdk_lvs,
        tmp_path,
        gds,
        CHILD.format(extra="XQ9 a b c sub npn13G2v Nx=1\n"),
        form="subckt-call",
    )
    assert report["device_census"]["reference"]["NPN13G2V"] == 1
    found = _unresolved(report)
    assert [(m["circuit"]["reference"], m["instance"]["reference"]) for m in found] == [
        ("CHILD", "Q9")
    ]
    assert found[0]["subcircuit"]["reference"] == "NPN13G2V"
    assert found[0]["severity"] == "error"
    assert report["status"] == "mismatch"


# --- Issue #7855: cap_cmim and poly-resistor calls, and the census ---------

_PASSIVES = [
    ("cap_devices", "cap_cmim"),
    ("res_devices", "rppd"),
    ("res_devices", "rhigh"),
    ("res_devices", "rsil"),
]


def _unit_control(pdk_lvs: Path, tmp_path: Path, group: str, name: str):
    unit = pdk_lvs / "testing/testcases/unit" / group
    if not (unit / "layout" / f"{name}.gds").is_file():
        pytest.skip(f"ihp-sg13g2 install lacks the {group}/{name} unit control")
    layout = kdb.Layout()
    layout.read(str(unit / "layout" / f"{name}.gds"))
    layout.top_cell().name = "TOP"
    gds = tmp_path / f"{name}_top.gds"
    layout.write(str(gds))
    cdl = (unit / "netlist" / f"{name}.cdl").read_text()
    cdl = re.sub(rf"(?im)^\.SUBCKT {name}\s*$", ".SUBCKT top", cdl)
    cdl = re.sub(r"(?im)^\.ENDS.*$", ".ENDS top", cdl)
    return str(gds), cdl


def _passives_as_calls(cdl: str, model: str, only: set[str] | None = None) -> str:
    """Rewrite the control's ``C``/``R`` cards of ``model`` as the xschem
    simulation form ``X.. <pins> [<body>] <model> w= l= m= [b= ps=]``, for
    every card or just the named ones (``$SUB=`` and ``$[model]`` spellings
    included; a card's value is the symbol's computed one, which a call lacks)."""
    out = []
    for line in cdl.splitlines():
        tokens = line.split()
        card = (
            tokens
            and tokens[0][:1].upper() in "CR"
            and re.search(rf"(?i)(?<![\w$])\$?\[?{model}\]?(?!\w)", line)
            and (only is None or tokens[0] in only)
        )
        if not card:
            out.append(line)
            continue
        fields = dict(re.findall(r"(\w+)=(\S+)", line))
        nets = [t for t in tokens[1:] if "=" not in t]
        if tokens[0][:1].upper() == "R":
            body = re.search(r"\$SUB=(\S+)", line)
            pins = nets[:2] + [body.group(1) if body else nets[2]]
            keys = ("w", "l", "m", "b", "ps")
        else:
            pins, keys = nets[:2], ("w", "l", "m")
        params = " ".join(f"{k}={fields[k]}" for k in keys if k in fields)
        out.append(f"X{tokens[0]} {' '.join(pins)} {model} {params}")
    return "\n".join(out) + "\n"


def _folded(counts: dict) -> dict:
    return {name.upper(): count for name, count in counts.items()}


@pytest.mark.parametrize(("group", "name"), _PASSIVES, ids=[n for _, n in _PASSIVES])
def test_every_passive_call_agrees_with_its_native_control(
    pdk_lvs, tmp_path, group, name
):
    gds, cdl = _unit_control(pdk_lvs, tmp_path, group, name)
    native = _lvs(pdk_lvs, tmp_path, gds, cdl)
    calls = _lvs(
        pdk_lvs, tmp_path, gds, _passives_as_calls(cdl, name), form="subckt-call"
    )
    # Every device the layout drew pairs with a converted call: none purged
    # by ``min_caps(1e-18)`` (the cap ``w``/``l`` reached the reader) and no
    # resistor read with its terminals or ``b``/``ps`` shifted. The same
    # tallies and the same findings as the native cards, the PDK control's own
    # pin/port findings included.
    devices = calls["counts"]["devices"]
    assert devices["reference"] == devices["layout"] == devices["matched"] > 0
    assert calls["device_census"]["reference"] == native["device_census"]["reference"]
    assert calls["counts"] == native["counts"]
    assert calls["category_counts"] == native["category_counts"]
    assert not _unresolved(calls)
    # The converted cards are counted by the source census, class for class.
    source = calls["device_census"]["reference_source"]
    assert source is not None
    assert _folded(source) == _folded(calls["device_census"]["reference"])


@pytest.mark.parametrize(
    ("group", "name", "card"),
    [
        ("cap_devices", "cap_cmim", "C_pattern_1"),
        ("res_devices", "rppd", "R_pattern_1"),
    ],
    ids=["cap_cmim", "rppd"],
)
def test_one_converted_passive_call_restores_the_native_pairing(
    pdk_lvs, tmp_path, group, name, card
):
    gds, cdl = _unit_control(pdk_lvs, tmp_path, group, name)
    native = _lvs(pdk_lvs, tmp_path, gds, cdl)
    swapped = _lvs(
        pdk_lvs,
        tmp_path,
        gds,
        _passives_as_calls(cdl, name, {card}),
        form="subckt-call",
    )
    # Before the fix one converted card left 68/69 capacitors and 2/84
    # resistors paired (its ``w``/``l`` lost, or its terminals shifted).
    assert swapped["counts"]["devices"] == native["counts"]["devices"]
    assert swapped["category_counts"] == native["category_counts"]
    assert swapped["device_census"]["reference_source"] is not None


def test_unconvertible_call_is_named_beside_a_converted_passive(pdk_lvs, tmp_path):
    gds, _ = _control(pdk_lvs, tmp_path)
    # A converted ``cap_cmim`` card used to make the source census ``null`` and
    # drop this finding: the plain reader raised on the cap before the scan.
    report = _lvs(
        pdk_lvs,
        tmp_path,
        gds,
        ".subckt top a b c sub\nXC1 a b cap_cmim w=6.99u l=6.99u m=1\n"
        "XQ9 a b c sub npn13G2v Nx=1\n.ends top\n",
        form="subckt-call",
    )
    assert report["device_census"]["reference_source"] == {"CAP_CMIM": 1}
    assert [m["instance"]["reference"] for m in _unresolved(report)] == ["Q9"]
    assert report["status"] == "mismatch"


@pytest.mark.parametrize(
    ("group", "name"),
    [
        ("cap_devices", "sg13_hv_svaricap"),
        ("ind_devices", "inductor"),
        ("ind_devices", "inductor3"),
    ],
)
def test_rf_cards_get_a_source_census_that_matches_the_runset(
    pdk_lvs, tmp_path, group, name
):
    gds, cdl = _unit_control(pdk_lvs, tmp_path, group, name)
    report = _lvs(pdk_lvs, tmp_path, gds, cdl)
    source = report["device_census"]["reference_source"]
    assert source is not None
    assert _folded(source) == _folded(report["device_census"]["reference"])
