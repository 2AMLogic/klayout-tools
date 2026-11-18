"""Content dedup of an off-host deck job's staged closure (issue #5539).

A grid sweeps corners against one model set, so every unit of a deck-list
request stages the *same* include/model closure: N units x M identical
includes used to ship ``N * M`` objects, one transfer process each (1872
units x 8 model files was 17,000 objects and over three hours of upload).
:func:`klayout_tools.sim_decks.stage_deck_job` now ships identical bytes
once, through :class:`klayout_tools.sim_decks_staging.DeckStager`.

The fixtures and the stub engine come from ``test_sim_decks``, so a unit
"run" here is the same deterministic stand-in the rest of that suite uses.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from klayout_tools import sim_decks as sd
from klayout_tools import sim_decks_staging as sds
from test_sim_decks import deck_request


class TestStagedContentDedup:
    """A grid sweeps corners against one model set, so every unit stages the
    same closure. ``stage_deck_job`` ships those bytes once."""

    def _grid(self, tmp_path: Path, unit_count: int, body: str) -> dict:
        """A ``unit_count``-unit request whose units each run their own deck
        (distinct bytes per unit) with ``body`` appended, so only the shared
        closure can be deduplicated."""
        decks = []
        for index in range(unit_count):
            name = f"deck{index}.spice"
            (tmp_path / name).write_text(f"* unit {index}\n{body}", encoding="utf-8")
            decks.append({"id": f"u{index}", "deck": name})
        return deck_request(tmp_path, decks=decks, backend="batch")

    def test_identical_includes_are_staged_once_for_the_whole_grid(self, tmp_path):
        (tmp_path / "models.lib").write_text("* models\n", encoding="utf-8")
        (tmp_path / "corner.spice").write_text("* corner\n", encoding="utf-8")
        request = self._grid(
            tmp_path, 3, ".include models.lib\n.include corner.spice\n"
        )

        files, worker = sd.stage_deck_job(request, request_dir=str(tmp_path))

        # 3 decks + 2 includes, not 3 x (1 + 2).
        assert len(files) == 5
        assert len({item.staged_name for item in files}) == 5
        per_unit = [
            {entry["name"]: entry["staged"] for entry in unit["includes"]}
            for unit in worker["decks"]
        ]
        assert per_unit[0] == per_unit[1] == per_unit[2]
        assert set(per_unit[0]) == {"models.lib", "corner.spice"}
        # Every shared name is a real staged file, and the in-unit
        # destinations are untouched.
        staged_names = {item.staged_name for item in files}
        assert set(per_unit[0].values()) <= staged_names
        assert [unit["deck"]["name"] for unit in worker["decks"]] == [
            "deck0.spice",
            "deck1.spice",
            "deck2.spice",
        ]
        assert len({unit["deck"]["staged"] for unit in worker["decks"]}) == 3

    def test_same_basename_with_different_bytes_stays_distinct(self, tmp_path):
        """Dedup is on bytes, never on name: two units whose include shares a
        basename but differs in content get a staged file each."""
        for index in (0, 1):
            (tmp_path / f"d{index}").mkdir()
            (tmp_path / f"d{index}" / "deck.spice").write_text(
                f"* unit {index}\n.include inc.spice\n", encoding="utf-8"
            )
            (tmp_path / f"d{index}" / "inc.spice").write_text(
                f"* inc {index}\n", encoding="utf-8"
            )
        request = deck_request(
            tmp_path,
            backend="batch",
            decks=[
                {"id": "u0", "deck": "d0/deck.spice"},
                {"id": "u1", "deck": "d1/deck.spice"},
            ],
        )

        files, worker = sd.stage_deck_job(request, request_dir=str(tmp_path))

        includes = [unit["includes"][0] for unit in worker["decks"]]
        assert [entry["name"] for entry in includes] == ["inc.spice", "inc.spice"]
        assert includes[0]["staged"] != includes[1]["staged"]
        by_name = {item.staged_name: item for item in files}
        assert Path(by_name[includes[0]["staged"]].local_path).read_text() == (
            "* inc 0\n"
        )
        assert Path(by_name[includes[1]["staged"]].local_path).read_text() == (
            "* inc 1\n"
        )

    def test_two_units_running_the_same_deck_bytes_stage_one_deck(self, tmp_path):
        """The default request's two units share ``deck.spice`` and
        ``extra.spice``: 4 staged entries before #5539, 2 now."""
        request = deck_request(tmp_path, backend="batch")

        files, worker = sd.stage_deck_job(request, request_dir=str(tmp_path))

        assert len(files) == 2
        first, second = worker["decks"]
        assert first["deck"]["staged"] == second["deck"]["staged"]
        assert first["inputs"][0]["staged"] == second["inputs"][0]["staged"]
        # Each unit's own in-unit destination still differs.
        assert first["inputs"][0]["name"] == "extra.spice"
        assert second["inputs"][0]["name"] == "bench/extra.spice"

    def test_shared_staged_names_are_transport_safe_and_cannot_collide(self, tmp_path):
        (tmp_path / "models.lib").write_text("* models\n", encoding="utf-8")
        request = self._grid(tmp_path, 2, ".include models.lib\n")

        files, _worker = sd.stage_deck_job(request, request_dir=str(tmp_path))

        names = [item.staged_name for item in files]
        # Every name is shared-tagged here (each is used by >= 1 unit and no
        # two units' files collide), and none can escape the staging root.
        assert len(names) == 3
        assert all(name.startswith("sha-") for name in names)
        assert all("/" not in name and ".." not in name for name in names)

    def test_an_unhashable_source_falls_back_to_per_unit_names(
        self, tmp_path, monkeypatch
    ):
        """The documented fallback: a source the digest helper cannot read
        (it cannot happen after `plan_deck_units`' existence check) stages
        under the pre-#5539 per-unit name instead of a shared one, so a
        request still ships rather than failing."""
        (tmp_path / "models.lib").write_text("* models\n", encoding="utf-8")
        request = self._grid(tmp_path, 2, ".include models.lib\n")
        monkeypatch.setattr(sds._provenance, "sha256_file", lambda path: None)

        files, worker = sd.stage_deck_job(request, request_dir=str(tmp_path))

        names = [item.staged_name for item in files]
        assert not any(name.startswith("sha-") for name in names)
        assert names == [
            "u0-deck-deck0.spice",
            "u0-inc0-models.lib",
            "u1-deck-deck1.spice",
            "u1-inc0-models.lib",
        ]
        # Each unit references its own copy, and the destinations still hold.
        assert [unit["includes"][0]["name"] for unit in worker["decks"]] == [
            "models.lib",
            "models.lib",
        ]

    def test_the_worker_copies_a_shared_include_into_every_unit(self, tmp_path):
        """The property the dedup rests on: the deck runner *copies* each
        staged file, so every unit directory still holds its own copy of a
        file that was uploaded once."""
        (tmp_path / "models.lib").write_text("* models\n", encoding="utf-8")
        request = self._grid(tmp_path, 2, ".include models.lib\n")
        files, worker = sd.stage_deck_job(request, request_dir=str(tmp_path))

        # Materialize the flat staged set the way a worker host receives it.
        staging = tmp_path / "staging"
        staging.mkdir()
        for item in files:
            shutil.copyfile(item.local_path, staging / item.staged_name)
        report = sd.run_deck_request(
            worker,
            request_dir=str(staging),
            backend="local-parallel",
            artifacts_dir=str(tmp_path / "worker-out"),
        )

        assert report["status"] == "pass"
        outdir = Path(report["outdir"])
        for unit in worker["decks"]:
            copied = outdir / unit["id"] / "models.lib"
            assert copied.read_text(encoding="utf-8") == "* models\n"
