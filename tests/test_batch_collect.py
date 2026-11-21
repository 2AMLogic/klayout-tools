"""Direct tests for the shared batch collect half ``_batch_collect`` (the
collected-report reader, ``runner_error`` and the remote stderr tail) that
``extract_batch`` / ``netlist_batch`` now share (issue #5711)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from klayout_tools import _batch_collect, _batch_common, sim_batch
from klayout_tools import extract_batch as eb
from klayout_tools import netlist_batch as nb
from klayout_tools.extract import ExtractError
from klayout_tools.netlist import NetlistError


def _write_report(collected: Path, payload) -> None:
    text = payload if isinstance(payload, str) else json.dumps(payload)
    (collected / sim_batch.BATCH_REPORT_FILENAME).write_text(text, encoding="utf-8")


def test_batch_collect_imports_standalone():
    """``sim_batch`` imports ``_batch_common``; ``_batch_collect`` must not
    pull it in at module level (circular at import time)."""
    import subprocess
    import sys

    out = subprocess.run(
        [sys.executable, "-c", "import klayout_tools._batch_collect"],
        capture_output=True,
        text=True,
        env={
            **__import__("os").environ,
            "PYTHONPATH": str(Path(_batch_collect.__file__).parents[1]),
        },
        check=False,
    )
    assert out.returncode == 0, out.stderr


@pytest.mark.parametrize(
    "reader, err_cls, noun",
    [
        (
            lambda c: eb._read_report(c, "job1"),
            ExtractError,
            "not a `klt extract` report",
        ),
        (
            lambda c: nb._read_runner_report(c, "job1"),
            NetlistError,
            "not a `klt netlist` generate report",
        ),
    ],
    ids=["extract", "netlist"],
)
def test_each_command_rejects_a_wrong_shape_report_with_its_own_error(
    tmp_path, reader, err_cls, noun
):
    _write_report(tmp_path, {"unrelated": True})
    with pytest.raises(err_cls, match=noun) as err:
        reader(str(tmp_path))
    assert err.value.code == _batch_common.BATCH_JOB_FAILED_CODE


@pytest.mark.parametrize(
    "payload",
    [
        {"status": "failed", "xschem": {}, "netlist": {}},
        {"status": nb.STATUS_GENERATED, "xschem": "x", "netlist": {}},
        {"status": nb.STATUS_GENERATED, "xschem": {}, "netlist": None},
    ],
    ids=["wrong-status", "non-dict-xschem", "non-dict-netlist"],
)
def test_netlist_reader_rejects_each_malformed_generate_report(tmp_path, payload):
    _write_report(tmp_path, payload)
    with pytest.raises(NetlistError, match="not a `klt netlist` generate report"):
        nb._read_runner_report(str(tmp_path), "job1")


def test_shared_reader_returns_a_report_the_predicate_accepts(tmp_path):
    _write_report(tmp_path, {"ok": 1})
    report = _batch_collect.read_collected_unit_report(
        str(tmp_path),
        "job1",
        err_cls=ExtractError,
        no_fallback="",
        validate=lambda r: "ok" in r,
        what="a thing",
    )
    assert report == {"ok": 1}


def test_runner_error_prefers_report_json_then_stderr_envelope(tmp_path):
    _write_report(
        tmp_path,
        {
            "schema_version": 1,
            "error": {"command": "x", "message": "boom", "code": "c"},
        },
    )
    error, note = _batch_collect.runner_error(str(tmp_path))
    assert error["message"] == "boom" and note == ""


def test_remote_stderr_tail_is_sanitized_and_capped(tmp_path):
    raw = b"x" * 5000 + b"\n\x1b[31mred\x1b[0m\x07\xff ok\r\n"
    (tmp_path / sim_batch.BATCH_STDERR_FILENAME).write_bytes(raw)
    tail = _batch_collect.remote_stderr_tail(str(tmp_path))
    assert "\x1b" not in tail and "\x07" not in tail
    assert tail.endswith("red\ufffd ok")
    assert len(tail.encode()) <= _batch_collect.REMOTE_STDERR_TAIL_BYTES
    assert _batch_collect.remote_stderr_tail(str(tmp_path / "missing")) == ""
