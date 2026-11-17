"""``klayout-runset`` engine test fixtures shared by
``tests/test_lvs_klayout_runset.py`` and
``tests/test_lvs_klayout_runset_pins.py`` (issue #5664).

The two test modules split from one file and carried (or would have carried)
byte-identical copies of this preamble; the builders moved here verbatim,
names included -- the existing call sites keep working unchanged. The fake
"runset" is mocked at the ``subprocess.run`` boundary and writes a *real*
``.lvsdb`` (built with the pip ``klayout.db`` API) so the
verdict-from-database path is exercised end to end. A real-binary smoke test
is skipped when ``klayout`` is not on PATH.
"""

import json
import subprocess

import klayout.db as kdb

SPICE_ONE = ".subckt TOP\nM1 c b a d NMOS L=0.2u W=0.4u\n.ends\n"

#: A reference *with* pins (a ``.subckt`` port list): the layout side of the
#: fixture never carries pin labels, so every reference pin pair comes back
#: ``layout=None`` -- exactly the foundry device-recognition shape.
SPICE_PINNED = ".subckt TOP D G S B\nM1 D G S B NMOS L=0.2u W=0.4u\n.ends\n"

#: Same pins, but a compare that genuinely fails (an extra reference device),
#: so the circuit pair is not Match even though its pin pairs are.
SPICE_PINNED_EXTRA_DEVICE = (
    ".subckt TOP D G S B\nM1 D G S B NMOS L=0.2u W=0.4u\n"
    "M2 D G S B NMOS L=0.2u W=0.4u\n.ends\n"
)


def make_gds(path: str, n_dev: int) -> None:
    ly = kdb.Layout()
    ly.dbu = 0.001
    top = ly.create_cell("TOP")
    act, poly, bulk = ly.layer(1, 0), ly.layer(2, 0), ly.layer(9, 0)
    top.shapes(bulk).insert(kdb.Box(-1000, -1000, 100000, 2000))
    for i in range(n_dev):
        x = i * 2000
        top.shapes(act).insert(kdb.Box(x, 0, x + 1000, 400))
        top.shapes(poly).insert(kdb.Box(x + 400, -100, x + 600, 500))
    ly.write(path)


def write_lvsdb(
    gds: str, schem: str, out: str, extract: bool = True, compare: bool = True
) -> None:
    ly = kdb.Layout()
    ly.read(gds)
    l2n = kdb.LayoutVsSchematic(kdb.RecursiveShapeIterator(ly, ly.top_cell(), []))
    act = l2n.make_layer(ly.layer(1, 0), "act")
    poly = l2n.make_layer(ly.layer(2, 0), "poly")
    bulk = l2n.make_layer(ly.layer(9, 0), "bulk")
    gate, sd = act & poly, act - poly
    if extract:
        l2n.extract_devices(
            kdb.DeviceExtractorMOS4Transistor("NMOS"),
            {"SD": sd, "G": gate, "tG": poly, "W": bulk},
        )
    l2n.connect(sd)
    l2n.connect(poly)
    l2n.connect(gate)
    l2n.connect(bulk)
    l2n.extract_netlist()
    ref = kdb.Netlist()
    # The stock reader needs a C-card value the deck's reader doesn't (#4817).
    patched = schem + ".stock.sp"
    with open(schem) as src, open(patched, "w") as dst:
        dst.write(src.read().replace(" sky130_fd_pr__model", " 0 sky130_fd_pr__model"))
    ref.read(patched, kdb.NetlistSpiceReader())
    l2n.reference = ref
    if compare:
        l2n.compare(kdb.NetlistComparer())
    l2n.write(out)


def install_fake_klayout(monkeypatch, tmp_path) -> dict:
    """Replace the subprocess boundary; the returned ``state`` dict controls
    the fake runset. Called by each consumer module's ``fake_klayout``
    fixture."""
    state = {
        "returncode": 0,
        "write_db": True,
        "extract": True,
        "compare": True,
        "stdout": "",
    }
    fake_bin = tmp_path / "klayout"
    fake_bin.write_text("#!/bin/sh\n")
    fake_bin.chmod(0o755)
    monkeypatch.setenv("KLT_KLAYOUT_BINARY", str(fake_bin))
    real_run = subprocess.run

    def fake_run(cmd, *a, **kw):
        if cmd[0] != str(fake_bin):
            return real_run(cmd, *a, **kw)
        if cmd[1:] == ["-v"]:
            return subprocess.CompletedProcess(cmd, 0, "KLayout 0.30.12\n", "")
        args = dict(c.split("=", 1) for c in cmd if "=" in c and c != "-rd")
        state["cmd"] = cmd
        if state["write_db"]:
            write_lvsdb(
                args["input"],
                args["schematic"],
                args["report"],
                state["extract"],
                state["compare"],
            )
        # The driver exits 0 even when the netlists do not match.
        return subprocess.CompletedProcess(
            cmd, state["returncode"], state["stdout"], ""
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    return state


def request(tmp_path, n_dev, spice=SPICE_ONE, **extra):
    gds = tmp_path / "l.gds"
    make_gds(str(gds), n_dev)
    sp = tmp_path / "ref.sp"
    sp.write_text(spice)
    deck = tmp_path / "pdk.lvs"
    deck.write_text("# runset\n")
    request = {
        "engine": "klayout-runset",
        "layout": {"file": str(gds)},
        "reference": {"netlist": str(sp)},
        "deck_file": str(deck),
        **extra,
    }
    return json.dumps(request)
