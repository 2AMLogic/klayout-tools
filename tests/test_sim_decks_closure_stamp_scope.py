"""The closure-only deck shape documented beside ``stamp_scope`` (issue #7985).

``docs/cli/sim.md`` "Worker-observed environment" carries a known-limit
caveat: a batch deck whose relative ``.include``/``.lib`` stages a file beside
it, with no ``decks[].inputs[]`` declaration, runs the caller's bytes while
``environment.remote.worker_environment.pdk`` still reads
``stamp_scope: "executed"`` and ``models_staged: false``, and the reader is
sent to ``key.includes[<name>]`` in ``<outdir>/<id>/unit.json`` to verify the
staged bytes. A staged include can be a model, a stimulus or a measurement
block, so klt classifies none of them; the caveat is the deliverable, and
these tests keep it true to the code by running its example through the fake
fleet (the real worker deck path, a stub engine, a synthetic worker capture).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path

import pytest

from klayout_tools import sim_batch as sb
from klayout_tools import sim_decks as sd
from test_sim_decks import (
    _FakeFleet,
    batch_deck_request,
    install_fake_batch,
    run_local,
)

DOC = Path(__file__).resolve().parents[1] / "docs" / "cli" / "sim.md"

MODEL = ".lib tt\n* pinned tt models\n.endl tt\n"

IMAGE_LIB = "$PDK_ROOT/gf180mcuD/libs.tech/ngspice/sm141064.ngspice"

#: The worker capture the doc's report example quotes. Its two stamp digests
#: are what the doc leaves as ``<hex>`` / ``<hash>`` placeholders.
_IMAGE_CAPTURE = {
    "schema": 1,
    "simulator": {
        "requested_binary": "ngspice",
        "binary_source": "PATH",
        "selected_path": "/opt/ngspice/bin/ngspice",
        "version_banner": "ngspice-47",
        "version_probe": "ok",
    },
    "pdk": {
        "variant": "gf180mcuD",
        "root": "/opt/pdk",
        "stamp_scheme": "sources-sha256",
        "stamp_sha256": "c" * 64,
        "stamp_text": "open_pdks c6d73a35f524070e85faff4a6a9eef49553ebc2b",
        "models_staged": False,
    },
    "cpu_model": "Fake CPU",
    "omp_num_threads": "1",
    "image_manifest": {"pdk_snapshot": "gf180mcuD-2026.08.03"},
    "diagnostics": [],
}


class _CapturingFleet(_FakeFleet):
    """The fake fleet, plus a worker capture written beside the collected
    report, where the job command's preflight leaves it."""

    def __init__(self, capture):
        super().__init__({"state": "done", "exit_code": "0"})
        self.capture = capture

    def collect(self, config, job_id, local_dir, **kwargs):
        super().collect(config, job_id, local_dir, **kwargs)
        with open(
            os.path.join(local_dir, sb.BATCH_WORKER_ENVIRONMENT_FILENAME),
            "w",
            encoding="utf-8",
        ) as handle:
            json.dump(self.capture, handle)


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _run(tmp_path, monkeypatch, deck, files, **unit):
    """Stage and run one batch unit ``pinned`` over ``files`` beside its deck;
    return the collected report, the staged worker unit and the unit record."""
    (tmp_path / "pinned.spice").write_text(deck, encoding="utf-8")
    for name, body in files.items():
        (tmp_path / name).write_text(body, encoding="utf-8")
    request = batch_deck_request(
        tmp_path, decks=[{"id": "pinned", "deck": "pinned.spice", **unit}]
    )
    _, worker_request = sd.stage_deck_job(request, request_dir=str(tmp_path))
    monkeypatch.delenv("PDK_ROOT", raising=False)
    install_fake_batch(monkeypatch, _CapturingFleet(_IMAGE_CAPTURE))
    report = run_local(tmp_path, request, outdir="out")
    record_path = Path(report["outdir"]) / "pinned" / "unit.json"
    record = json.loads(record_path.read_text(encoding="utf-8"))
    return report, worker_request["decks"][0], record


def _worker_pdk(report):
    return report["environment"]["remote"]["worker_environment"]["pdk"]


def _names(refs):
    return [ref["name"] for ref in refs]


def _caveat() -> str:
    """The caveat, from its lead-in up to the next topic in the doc."""
    text = DOC.read_text(encoding="utf-8")
    start = text.index("**Closure-only deck caveat")
    return text[start : text.index("\nFour sibling fields sit on", start)]


def _fence(block: str, language: str) -> str:
    match = re.search(rf"```{language}\n(.*?)\n```", block, re.DOTALL)
    assert match, f"the caveat carries a ```{language} block"
    return match.group(1)


def _jq_get(expression: str, document):
    """Resolve a ``jq`` path made of ``.field`` and ``["name"]`` steps."""
    for field, bracket in re.findall(r'\.(\w+)|\["([^"]*)"\]', expression):
        document = document[field or bracket]
    return document


class TestClosureOnlyDeck:
    @pytest.mark.parametrize("directive", [".include model.lib", ".lib model.lib tt"])
    def test_report_reads_executed_while_the_library_is_staged(
        self, tmp_path, monkeypatch, directive
    ):
        report, unit, record = _run(
            tmp_path,
            monkeypatch,
            f"* pinned deck\n{directive}\n",
            {"model.lib": MODEL},
        )

        assert report["status"] == "pass"
        assert unit["inputs"] == [] and unit["host_resolved"] == []
        assert _names(unit["includes"]) == ["model.lib"]
        assert _worker_pdk(report) == {
            **_IMAGE_CAPTURE["pdk"],
            "stamp_scope": "executed",
            "install": None,
            "store_layout": None,
        }
        staged = _digest(tmp_path / "model.lib")
        assert record["key"]["inputs"] == {}
        assert record["key"]["includes"] == {"model.lib": staged}

    def test_one_unit_can_stage_a_stimulus_and_read_the_image_pdk(
        self, tmp_path, monkeypatch
    ):
        report, unit, record = _run(
            tmp_path,
            monkeypatch,
            f"* mixed deck\n.include stim.spice\n.lib {IMAGE_LIB} typical\n",
            {"stim.spice": "* local stimulus\nV1 in 0 PWL(0 0 1n 1)\n"},
        )

        assert _names(unit["includes"]) == ["stim.spice"]
        assert unit["host_resolved"] == [IMAGE_LIB]
        assert report["units"][0]["host_resolved"] == [IMAGE_LIB]
        assert _worker_pdk(report)["stamp_scope"] == "executed"
        assert record["key"]["includes"] == {
            "stim.spice": _digest(tmp_path / "stim.spice")
        }

    def test_a_declared_input_is_digest_under_key_inputs(self, tmp_path, monkeypatch):
        _, unit, record = _run(
            tmp_path,
            monkeypatch,
            "* pinned deck\n.include model.lib\n",
            {"model.lib": MODEL},
            inputs=["model.lib"],
        )

        staged = _digest(tmp_path / "model.lib")
        assert _names(unit["inputs"]) == ["model.lib"]
        assert record["key"]["inputs"] == {"model.lib": staged}
        assert record["key"]["includes"] == {"model.lib": staged}


class TestCaveatWording:
    """The caveat beside the field reference, read back out of the doc."""

    def test_example_deck_and_report_match_a_real_run(self, tmp_path, monkeypatch):
        caveat = _caveat()

        assert ".include model.lib" in _fence(caveat, "spice").splitlines()
        assert "The file-bearing `.lib model.lib tt` form" in caveat
        assert '`{"id": "pinned", "deck":' in caveat and '"pinned.spice"}`' in caveat

        report, _, _ = _run(
            tmp_path, monkeypatch, _fence(caveat, "spice") + "\n", {"model.lib": MODEL}
        )

        quoted = json.loads(_fence(caveat, "json"))
        observed = _worker_pdk(report)
        placeholders = {key for key, value in quoted.items() if "<" in str(value)}
        assert placeholders == {"stamp_sha256", "stamp_text"}
        assert set(quoted) == set(observed)
        assert all(observed[key] for key in placeholders)
        assert {
            key: value for key, value in quoted.items() if key not in placeholders
        } == {key: value for key, value in observed.items() if key not in placeholders}

    def test_recipe_resolves_to_the_local_digest(self, tmp_path, monkeypatch):
        caveat = _caveat()
        console = _fence(caveat, "console")

        assert "$ sha256sum model.lib" in console.splitlines()
        match = re.search(r"\$ jq '([^']+)' (\S+)$", console, re.MULTILINE)
        assert match, "the recipe reads the record with jq"
        expression, record_path = match.groups()
        assert record_path == "<outdir>/pinned/unit.json"
        assert "<outdir>/<id>/unit.json" in caveat

        _, _, record = _run(
            tmp_path, monkeypatch, _fence(caveat, "spice") + "\n", {"model.lib": MODEL}
        )

        assert _jq_get(expression, record) == _digest(tmp_path / "model.lib")

    def test_the_limit_and_both_sources_are_stated(self):
        caveat = " ".join(_caveat().split())

        for phrase in (
            "issues/7985)",
            "no `decks[].inputs[]` declaration",
            "identifies the worker image's PDK tree",
            "origin of every model the simulator read",
            "stimulus or a measurement block",
            "`$PDK_ROOT/<variant>/...` reference",
            "`host_resolved`",
            "not that the simulator opened them",
            "issues/7981)",
        ):
            assert phrase in caveat, phrase
