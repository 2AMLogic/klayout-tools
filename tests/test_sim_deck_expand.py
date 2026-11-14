"""Tests for `klt sim` deck-unit ``pre_osdi`` classification and runner-side
``$VAR``/``~`` expansion (issue #4881, ``src/klayout_tools/sim_deck_expand.py``).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from klayout_tools import sim
from klayout_tools import sim_deck_expand as sde
from klayout_tools import sim_decks as sd
from test_sim_decks import deck_request


class TestPreOsdiAndRunnerExpansion:
    def _deck(self, tmp_path: Path, body: str) -> dict:
        request = deck_request(tmp_path, decks=[{"id": "u0", "deck": "deck.spice"}])
        (tmp_path / "deck.spice").write_text(body, encoding="utf-8")
        return request

    def test_env_var_pre_osdi_left_to_the_executing_host(self, tmp_path, monkeypatch):
        monkeypatch.delenv("PDK_ROOT", raising=False)
        lib = "$PDK_ROOT/sky130A/libs.tech/ngspice/sky130.lib.spice"
        osdi = "${PDK_ROOT}/sky130A/osdi/bsim.osdi"
        request = self._deck(
            tmp_path,
            f'* deck\n.lib {lib} tt\n.control\npre_osdi {osdi}\npre_osdi "{osdi}"\n'
            ".endc\n",
        )
        unit = sd.plan_deck_units(request, request_dir=str(tmp_path))[0]
        assert unit["includes"] == []
        assert unit["host_resolved"] == [lib, osdi, osdi]

    def test_relative_pre_osdi_staged_not_scanned(self, tmp_path):
        (tmp_path / "bsim.osdi").write_bytes(b"\xff\x00.include nothing.spice\n")
        request = self._deck(tmp_path, "* deck\n.control\npre_osdi bsim.osdi\n.endc\n")
        unit = sd.plan_deck_units(request, request_dir=str(tmp_path))[0]
        assert unit["includes"] == [
            {"staged": str(tmp_path / "bsim.osdi"), "name": "bsim.osdi"}
        ]

    def test_absolute_pre_osdi_rejected_naming_pdk_root_spelling(self, tmp_path):
        request = self._deck(tmp_path, f"* deck\npre_osdi {tmp_path}/x.osdi\n")
        with pytest.raises(sim.SimError, match=r"`pre_osdi` target.*\$PDK_ROOT/<rel"):
            sd.plan_deck_units(request, request_dir=str(tmp_path))

    def test_unresolved_pre_osdi_rejected(self, tmp_path):
        request = self._deck(tmp_path, "* deck\npre_osdi missing.osdi\n")
        with pytest.raises(sim.SimError, match="does not resolve to a readable"):
            sd.plan_deck_units(request, request_dir=str(tmp_path))

    def test_runner_expands_env_without_touching_deck_bytes(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("PDK_ROOT", "/opt/pdk")
        deck = (
            "* deck\n.lib $PDK_ROOT/a.lib tt\n.include ${PDK_ROOT}/b.spice\n"
            "pre_osdi $PDK_ROOT/c.osdi\n.lib tt\n.endl tt\n"
        )
        unit_dir = tmp_path / "unit"
        unit_dir.mkdir()
        (unit_dir / "deck.spice").write_text(deck, encoding="utf-8")
        unit = {"deck": {"name": "deck.spice"}, "includes": []}
        run_name = sde.prepare_run_deck(unit, unit_dir=str(unit_dir))
        assert run_name == ".klt-expanded-deck.spice"
        assert (unit_dir / "deck.spice").read_text(encoding="utf-8") == deck
        assert (unit_dir / run_name).read_text(encoding="utf-8") == (
            "* deck\n.lib /opt/pdk/a.lib tt\n.include /opt/pdk/b.spice\n"
            "pre_osdi /opt/pdk/c.osdi\n.lib tt\n.endl tt\n"
        )

    def test_runner_leaves_plain_deck_alone(self, tmp_path):
        (tmp_path / "deck.spice").write_text("* deck\n.end\n", encoding="utf-8")
        unit = {"deck": {"name": "deck.spice"}, "includes": []}
        assert sde.prepare_run_deck(unit, unit_dir=str(tmp_path)) == "deck.spice"
