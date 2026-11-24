"""SPEF preserves distributed ladders instead of reading star-only fields (#6218)."""

from __future__ import annotations

import copy
import json

import pytest

from klayout_tools.cli import main
from klayout_tools.extract import run_extract
from klayout_tools.extract_spef import (
    ExtractError,
    _spef_name,
    _unescape_spice_safe_net_name,
    _write_spef,
)
from test_extract import (
    _distributed_rc_internal_net_name,
    _make_series_nmos_layout,
    _resistance_path_ladder_run,
    _write_gds,
)


def _cards(text, net):
    net = _unescape_spice_safe_net_name(net)
    block = text.split(f"*D_NET {_spef_name(net)} ", 1)[1].split("*END", 1)[0]
    total, body = block.split("\n", 1)
    caps, _, resistors = body.split("*CAP\n", 1)[1].partition("*RES\n")
    return (
        float(total),
        [s.split() for s in caps.splitlines()],
        [s.split() for s in resistors.splitlines()],
    )


def _assert_ladder(text, entry):
    total, caps, resistors = _cards(text, entry["net"])
    assert len(caps) == len(entry["terminals"])
    assert len(resistors) == len(entry["segments"]) == len(caps) - 1
    assert all(len(c) == 3 for c in caps)
    assert sum(float(c[2]) for c in caps) == pytest.approx(
        entry["capacitance_ff"], abs=2e-6
    )
    assert total == entry["capacitance_ff"]
    nodes = {}
    for terminal, cap in zip(entry["terminals"], caps, strict=True):
        nodes[terminal["leg_net"]] = cap[1]
        assert float(cap[2]) == terminal["capacitance_ff"]
    assert len(set(nodes.values())) == len(caps)
    for segment, resistor in zip(entry["segments"], resistors, strict=True):
        assert resistor[1:3] == [nodes[segment["net_a"]], nodes[segment["net_b"]]]
        assert float(resistor[3]) == segment["resistance_ohm"]
    # Independently walk the emitted graph: all capacitor nodes must be in
    # one chain, with two endpoints and no extra hub or self-loop.
    degree = {node: 0 for node in nodes.values()}
    reached = {caps[0][1]}
    for _, a, b, _ in resistors:
        assert a != b
        degree[a] += 1
        degree[b] += 1
    for _ in caps:
        for _, a, b, _ in resistors:
            if a in reached or b in reached:
                reached.update((a, b))
    assert reached == set(degree)
    assert sorted(degree.values()) == [1, 1] + [2] * (len(caps) - 2)


def test_distributed_rc_spef_cli_series_nmos(tmp_path, capsys):
    """Run the reported CLI combination against real extracted layout geometry."""
    path = _write_gds(_make_series_nmos_layout(), tmp_path / "series.gds")
    baseline = run_extract(path, "sky130", output=str(tmp_path / "base.spice"))
    net = _distributed_rc_internal_net_name(baseline)
    spef = tmp_path / "series.spef"
    assert (
        main(
            [
                "extract",
                path,
                "--deck",
                "sky130",
                "--parasitics",
                "--spef",
                str(spef),
                "--critical-net",
                net,
                "--distributed-rc",
                "--output",
                str(tmp_path / "series.spice"),
                "--format",
                "json",
            ]
        )
        == 0
    )
    report = json.loads(capsys.readouterr().out)
    entry = next(e for e in report["parasitics"]["nets"] if e["net"] == net)
    assert entry["rc_model"] == "distributed"
    assert all("resistance_ohm" not in t for t in entry["terminals"])
    _assert_ladder(spef.read_text(), entry)


def test_distributed_rc_spef_four_terminal_port(tmp_path):
    spef = tmp_path / "port.spef"
    _, baseline_entry = _resistance_path_ladder_run(tmp_path)
    _, entry = _resistance_path_ladder_run(tmp_path, spef_output=str(spef))
    assert entry == baseline_entry
    assert len(entry["terminals"]) == 4
    _assert_ladder(spef.read_text(), entry)
    _, caps, _ = _cards(spef.read_text(), "OUT")
    assert caps[entry["pin_order"]][1] == "OUT"


@pytest.mark.parametrize("count", [2, 3, 4])
@pytest.mark.parametrize("port", [False, True])
def test_distributed_nodes_and_coupling(tmp_path, count, port):
    """Offline writer cases cover odd/even ladders, escaping and coupling hubs."""
    mid = count // 2
    legs = [r"\$1" if i == mid else rf"\$1__t{i}" for i in range(count)]
    entry = {
        "net": r"\$1",
        "hub_net": r"\$1",
        "rc_model": "distributed",
        "pin_order": mid,
        "capacitance_ff": float(count),
        "terminals": [
            {"leg_net": leg, "order": i, "capacitance_ff": 1.0}
            for i, leg in enumerate(legs)
        ],
        "segments": [
            {"net_a": a, "net_b": b, "resistance_ohm": float(i + 2)}
            for i, (a, b) in enumerate(zip(legs, legs[1:], strict=False))
        ],
        "coupled": [{"net": "other", "capacitance_ff": 0.5}],
    }
    report = {
        "nets": [
            entry,
            {"net": "other", "hub_net": "other", "capacitance_ff": 2.0},
        ]
    }
    original = copy.deepcopy(report)
    path = tmp_path / "nodes.spef"
    _write_spef(
        str(path),
        design_name="top",
        klt_version=None,
        parasitics_report=report,
        port_names=[r"\$1"] if port else [],
    )
    assert report == original
    total, caps, resistors = _cards(path.read_text(), "$1")
    expected_nodes = []
    next_index = 2
    hub = r"\$1" if port else r"\$1:1"
    for i in range(count):
        if i == mid:
            expected_nodes.append(hub)
        else:
            expected_nodes.append(rf"\$1:{next_index}")
            next_index += 1
    assert total == count + 0.5
    assert [c[1] for c in caps[:-1]] == expected_nodes
    assert caps[-1][1:] == [hub, "other:1", "0.500000"]
    assert [r[1:3] for r in resistors] == [
        [a, b] for a, b in zip(expected_nodes, expected_nodes[1:], strict=False)
    ]
    assert [float(r[3]) for r in resistors] == [float(i + 2) for i in range(count - 1)]


def test_unknown_rc_model_is_clean_error(tmp_path):
    path = tmp_path / "unknown.spef"
    with pytest.raises(ExtractError, match="rc_model.*future"):
        _write_spef(
            str(path),
            design_name="top",
            klt_version=None,
            port_names=[],
            parasitics_report={
                "nets": [
                    {
                        "net": "n",
                        "hub_net": "n",
                        "capacitance_ff": 1.0,
                        "rc_model": "future",
                        "terminals": [{"leg_net": "n__t0"}],
                    }
                ]
            },
        )
    assert not path.exists()


@pytest.mark.parametrize("model", [None, "lumped"])
@pytest.mark.parametrize("resistance", [0.0, 2.5])
def test_lumped_and_drop_r_blocks_unchanged(tmp_path, model, resistance):
    entry = {
        "net": "n",
        "hub_net": "n",
        "capacitance_ff": 3.0,
        "terminals": [{"leg_net": "n__t0", "resistance_ohm": resistance}],
    }
    if model is not None:
        entry["rc_model"] = model
    path = tmp_path / "lumped.spef"
    _write_spef(
        str(path),
        design_name="top",
        klt_version=None,
        parasitics_report={"nets": [entry]},
        port_names=[],
    )
    assert path.read_text().split("*D_NET", 1)[1] == (
        f" n 3.000000\n*CAP\n1 n:1 3.000000\n*RES\n1 n:1 n:2 {resistance:.6f}\n*END\n\n"
    )
