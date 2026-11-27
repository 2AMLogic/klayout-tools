"""CLI-level check for ``batch_job_interrupted`` (issue #5025): a run whose
units all end interrupted exits 4 and the JSON envelope's diagnostic names the
recovery command. Split out to keep ``test_sim_batch.py`` under its ceiling."""

from __future__ import annotations

import json

from klayout_tools import sim_batch as sb
from klayout_tools.cli import sim_cmd
from test_sim_decks import (
    _FakeFleet,
    argparse_namespace,
    batch_deck_request,
    install_fake_batch,
    write_request,
    write_stub_engine,
)


def test_all_interrupted_run_exits_4_with_recovery_message(
    tmp_path, monkeypatch, capsys
):
    write_stub_engine(tmp_path)
    install_fake_batch(monkeypatch, _FakeFleet({"state": "done"}))

    def interrupted(config, job_id, **kwargs):
        raise sb.BatchJobInterrupted(
            "job klt-sim-1 stayed 'interrupted'; to recover, re-run with the same "
            "-o and --resume (shard map: --attach <batch-shards.json> "
            "--collect-partial, then --resume)",
            interrupted_s=9.0,
        )

    monkeypatch.setattr(sb, "poll_status", interrupted)
    write_request(tmp_path, batch_deck_request(tmp_path))
    args = argparse_namespace(str(tmp_path / "request.json"))
    args.backend = "batch"
    args.format = "json"
    args.outdir = str(tmp_path / "out")

    assert sim_cmd.run(args) == sim_cmd.EXIT_CORNER_ERRORED == 4
    report = json.loads(capsys.readouterr().out)
    report = report.get("data", report)
    diagnostics = [d for u in report["units"] for d in u["diagnostics"]]
    assert diagnostics
    assert {d["code"] for d in diagnostics} == {"batch_job_interrupted"}
    for d in diagnostics:
        assert "--resume" in d["message"] and "--collect-partial" in d["message"]
