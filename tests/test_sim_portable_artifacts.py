"""``options.portable_artifacts`` (issue #3052): the persisted per-corner deck
and engine log carry stable ``$TOKEN`` paths, never host paths."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from helpers.subprocess_fakes import fake_completed
from klayout_tools import _paths as paths_module
from klayout_tools import sim, sim_portable

pytestmark = pytest.mark.usefixtures("real_build_identity_git")


@pytest.fixture(autouse=True)
def _bare_name_ngspice_resolution(monkeypatch):
    real_which = shutil.which

    def fake_which(cmd, *args, **kwargs):
        if cmd == "ngspice":
            return cmd
        return real_which(cmd, *args, **kwargs)

    monkeypatch.setattr(paths_module.shutil, "which", fake_which)


# --------------------------------------------------------------------------- #
# substitute()
# --------------------------------------------------------------------------- #


def test_substitute_earlier_root_wins_and_repeats():
    roots = (("$PDK_ROOT", "/home/u/pdk"), ("$HOME", "/home/u"))
    text = ".lib /home/u/pdk/a.lib tt\n.include /home/u/x.cir /home/u/pdk/b\n"
    assert sim_portable.substitute(text, roots) == (
        ".lib $PDK_ROOT/a.lib tt\n.include $HOME/x.cir $PDK_ROOT/b\n"
    )


def test_substitute_only_whole_path_prefixes():
    roots = (("$HOME", "/home/u"),)
    text = '/home/user2/x /opt/home/u/x /home/u-old/x "/home/u/x" /home/u'
    assert sim_portable.substitute(text, roots) == (
        '/home/user2/x /opt/home/u/x /home/u-old/x "$HOME/x" $HOME'
    )


def test_substitute_regex_metacharacters_in_root():
    roots = (("$REQUEST_DIR", "/tmp/a+b (1)/c.d"),)
    assert (
        sim_portable.substitute("/tmp/a+b (1)/c.d/x.cir", roots) == "$REQUEST_DIR/x.cir"
    )
    assert (
        sim_portable.substitute("/tmp/aab (1)/cxd/x.cir", roots)
        == "/tmp/aab (1)/cxd/x.cir"
    )


def test_substitute_unmatched_path_is_untouched():
    roots = (("$HOME", "/home/u"),)
    assert sim_portable.substitute(".include /opt/other/x.cir", roots) == (
        ".include /opt/other/x.cir"
    )


def test_build_roots_pdk_root_and_symlinked_tree(tmp_path):
    real = tmp_path / "real_pdk"
    (real / "libs").mkdir(parents=True)
    link = tmp_path / "link_pdk"
    link.symlink_to(real)
    lib = link / "libs" / "m.lib"
    lib.write_text("*")
    roots = sim_portable.build_roots(
        request_dir=str(tmp_path / "req"),
        artifacts_dir=str(tmp_path / "req" / ".klt"),
        models_lib=str(lib),
        environ={"PDK_ROOT": str(link), "HOME": str(tmp_path / "home")},
    )
    for path in (str(lib), str(real / "libs" / "m.lib")):
        assert sim_portable.substitute(path, roots) == "$PDK_ROOT/libs/m.lib"


def test_build_roots_without_pdk_root_uses_models_dir(tmp_path):
    lib = tmp_path / "models" / "m.lib"
    lib.parent.mkdir()
    roots = sim_portable.build_roots(
        request_dir=str(tmp_path / "req"),
        artifacts_dir=str(tmp_path / "req" / ".klt"),
        models_lib=str(lib),
        environ={"HOME": str(tmp_path / "home")},
    )
    assert sim_portable.substitute(str(lib), roots) == "$MODELS_DIR/m.lib"


# --------------------------------------------------------------------------- #
# run_sim end to end (engine stubbed: the stub log echoes the deck, like ngspice)
# --------------------------------------------------------------------------- #


def _project(root: Path, *, portable: bool | None, body: str | None = None) -> Path:
    root.mkdir(parents=True)
    (root / "body.spice").write_text(
        body or ".param vdd=1.0\nVdd vdd 0 DC {vdd}\nR1 vdd out 1k\n"
    )
    (root / "corner.lib").write_text(".lib tt\n.endl tt\n.lib ff\n.endl ff\n")
    options: dict = {"keep_artifacts": True}
    if portable is not None:
        options["portable_artifacts"] = portable
    request = root / "request.json"
    request.write_text(
        json.dumps(
            {
                "netlist": "body.spice",
                "models": {"lib": "corner.lib"},
                "corners": {"process": ["tt", "ff"]},
                "analysis": {"kind": "tran", "args": "1n 1u"},
                "measurements": [
                    {"name": "vout", "spice": ".meas tran vout FIND v(out) AT=1u"}
                ],
                "options": options,
            }
        )
    )
    return request


def _stub_engine(monkeypatch):
    def fake_run(cmd, capture_output, text, timeout, cwd=None):
        deck = Path(cmd[cmd.index("-b") + 1])
        Path(cmd[cmd.index("-o") + 1]).write_text(
            "** ngspice-99\nReading deck "
            + str(deck)
            + "\n"
            + deck.read_text()
            + "vout = 5.0e-01\n"
        )
        return fake_completed("** ngspice-99\n")

    monkeypatch.setattr(sim.subprocess, "run", fake_run)


def _artifact_texts(report: dict) -> list[str]:
    out = []
    for corner in report["corners"]:
        for key in ("deck", "log"):
            out.append(Path(corner["artifacts"][key]).read_text())
    return out


def test_default_off_artifacts_keep_host_paths(tmp_path, monkeypatch):
    _stub_engine(monkeypatch)
    request = _project(tmp_path / "a", portable=None)
    report = sim.run_sim(str(request))
    assert "portable_artifacts" not in report["environment"]
    assert all(str(tmp_path) in text for text in _artifact_texts(report))


def test_explicit_false_matches_omitted(tmp_path, monkeypatch):
    _stub_engine(monkeypatch)
    off = sim.run_sim(str(_project(tmp_path / "a", portable=False)))
    assert all(str(tmp_path) in text for text in _artifact_texts(off))
    assert "portable_artifacts" not in off["environment"]


def test_non_boolean_option_is_rejected(tmp_path, monkeypatch):
    _stub_engine(monkeypatch)
    request = _project(tmp_path / "a", portable=None)
    data = json.loads(request.read_text())
    data["options"]["portable_artifacts"] = "yes"
    request.write_text(json.dumps(data))
    with pytest.raises(sim.SimError, match="portable_artifacts"):
        sim.run_sim(str(request))


def test_portable_artifacts_have_no_host_path_and_are_checkout_independent(
    tmp_path, monkeypatch
):
    _stub_engine(monkeypatch)
    monkeypatch.setenv("PDK_ROOT", str(tmp_path / "pdk"))
    # Needs `$HOME`-style expansion in the netlist -> `netlist.expanded.cir`.
    body = ".include $PDK_ROOT/inc.cir\nR1 vdd out 1k\n"
    (tmp_path / "pdk").mkdir()
    (tmp_path / "pdk" / "inc.cir").write_text("* inc\n")
    reports = []
    for name in ("one", "nested/two"):
        request = _project(tmp_path / name, portable=True, body=body)
        reports.append(sim.run_sim(str(request)))
    texts = [_artifact_texts(r) for r in reports]
    assert texts[0] == texts[1]
    for report_texts in texts:
        for text in report_texts:
            assert str(tmp_path) not in text
        assert any("$CORNER_DIR/netlist.expanded.cir" in t for t in report_texts)
        assert any("$MODELS_DIR/corner.lib tt" in t for t in report_texts)
    for report in reports:
        expanded = Path(report["corners"][0]["artifacts"]["deck"]).with_name(
            "netlist.expanded.cir"
        )
        assert "$PDK_ROOT/inc.cir" in expanded.read_text()
        assert str(tmp_path) not in expanded.read_text()
        subs = report["environment"]["portable_artifacts"]["substitutions"]
        assert {"$CORNER_DIR", "$MODELS_DIR", "$PDK_ROOT"} <= {s["token"] for s in subs}
        assert str(tmp_path) not in json.dumps(subs)


def test_xyce_engine_is_covered(tmp_path, monkeypatch):
    def fake_run(cmd, capture_output, text, timeout, cwd=None):
        deck = Path(cmd[cmd.index("-b") + 1])
        Path(cmd[cmd.index("-l") + 1]).write_text(
            "Xyce log\nnetlist " + str(deck) + "\n" + deck.read_text()
        )
        return fake_completed("Xyce Release 7.10.0\n")

    monkeypatch.setattr(sim.subprocess, "run", fake_run)
    request = _project(tmp_path / "x", portable=True)
    data = json.loads(request.read_text())
    data["engine"] = "xyce"
    request.write_text(json.dumps(data))
    report = sim.run_sim(str(request))
    texts = _artifact_texts(report)
    assert texts and all(str(tmp_path) not in t for t in texts)
    assert any("$CORNER_DIR/corner.cir" in t for t in texts)
