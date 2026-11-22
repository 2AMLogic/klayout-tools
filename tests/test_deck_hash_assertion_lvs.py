"""``klt lvs --expect-deck-hash`` (issue #5806) -- the lvs half of the
deck-pin assertion's CLI-level coverage; the unit tests and the ``klt drc``
CLI level live in ``tests/test_deck_hash_assertion.py``.

These run the tiny in-process deck compare ``tests/test_lvs.py`` established
(no subprocess stub), which still produces a real ``provenance.deck`` to
assert against: a matching run is silent and byte-identical, a mismatching
one (or an assertion against a run that recorded no deck hash at all, e.g.
a pre-extracted ``layout.netlist``) is an error envelope on exit 1.
"""

from __future__ import annotations

import json
from pathlib import Path

import klayout.db as kdb
import pytest

from klayout_tools.cli import main

# The lvs runs stub nothing, but the deck run still probes Git for the
# provenance build identity; keep those probes on the real runner (conftest),
# exactly as this block ran before the split from test_deck_hash_assertion.py.
pytestmark = pytest.mark.usefixtures("real_build_identity_git")

_PAD_RING_SPICE = ".SUBCKT pad_ring A B\n.ENDS pad_ring\n"


def _pad_ring_gds(path: Path) -> str:
    layout = kdb.Layout()
    layout.dbu = 0.001
    top = layout.create_cell("pad_ring")
    m1 = layout.layer(68, 20)
    m1_label = layout.layer(68, 5)
    top.shapes(m1).insert(kdb.Box(0, 0, 1000, 1000))
    top.shapes(m1_label).insert(kdb.DText("A", kdb.DTrans(kdb.DPoint(0.5, 0.5))))
    top.shapes(m1).insert(kdb.Box(5000, 0, 6000, 1000))
    top.shapes(m1_label).insert(kdb.DText("B", kdb.DTrans(kdb.DPoint(5.5, 0.5))))
    layout.write(str(path))
    return str(path)


def _lvs_deck_request(tmp_path: Path) -> Path:
    gds = _pad_ring_gds(tmp_path / "pad_ring.gds")
    reference = tmp_path / "pad_ring.spice"
    reference.write_text(_PAD_RING_SPICE, encoding="utf-8")
    request = tmp_path / "request.json"
    request.write_text(
        json.dumps(
            {
                "layout": {"file": gds, "deck": "sky130", "top": "pad_ring"},
                "reference": {"netlist": str(reference), "top": "pad_ring"},
            }
        ),
        encoding="utf-8",
    )
    return request


def test_lvs_expect_deck_hash_match_is_byte_identical(tmp_path, capsys):
    request = _lvs_deck_request(tmp_path)

    assert main(["lvs", str(request), "--format", "json"]) == 0
    baseline = capsys.readouterr().out
    observed = json.loads(baseline)["provenance"]["deck"]["content_hash"]
    assert observed  # a deck run records its hash

    assert (
        main(["lvs", str(request), "--expect-deck-hash", observed, "--format", "json"])
        == 0
    )
    assert capsys.readouterr().out == baseline


def test_lvs_expect_deck_hash_mismatch_fails_loudly(tmp_path, capsys):
    request = _lvs_deck_request(tmp_path)

    assert main(["lvs", str(request), "--format", "json"]) == 0
    observed = json.loads(capsys.readouterr().out)["provenance"]["deck"]["content_hash"]
    tampered = observed[:-1] + ("0" if observed[-1] != "0" else "1")

    code = main(
        ["lvs", str(request), "--expect-deck-hash", tampered, "--format", "json"]
    )
    assert code == 1
    envelope = json.loads(capsys.readouterr().err)
    message = envelope["error"]["message"]
    assert "deck-hash assertion failed" in message
    assert "sky130" in message
    assert tampered in message
    assert observed in message


def test_lvs_expect_deck_hash_null_deck_fails_never_passes(tmp_path, capsys):
    """``layout.netlist`` (pre-extracted) involves no deck, so there is no
    hash to assert against -- the assertion must fail, not pass silently."""
    layout = tmp_path / "layout.spice"
    reference = tmp_path / "ref.spice"
    layout.write_text(_PAD_RING_SPICE, encoding="utf-8")
    reference.write_text(_PAD_RING_SPICE, encoding="utf-8")
    request = tmp_path / "request.json"
    request.write_text(
        json.dumps(
            {
                "layout": {"netlist": str(layout), "top": "pad_ring"},
                "reference": {"netlist": str(reference), "top": "pad_ring"},
            }
        ),
        encoding="utf-8",
    )

    code = main(
        [
            "lvs",
            str(request),
            "--expect-deck-hash",
            "sha256:" + "0a1b" * 16,
            "--format",
            "json",
        ]
    )

    assert code == 1
    assert "no deck content hash" in capsys.readouterr().err


def test_lvs_expect_deck_hash_malformed_is_a_usage_error(tmp_path, capsys):
    request = _lvs_deck_request(tmp_path)

    with pytest.raises(SystemExit) as exc:
        main(["lvs", str(request), "--expect-deck-hash", "beef"])

    assert exc.value.code == 2
    assert "sha256" in capsys.readouterr().err


def test_lvs_expect_deck_hash_rejected_with_check(tmp_path, capsys):
    request = _lvs_deck_request(tmp_path)
    report_path = tmp_path / "committed.json"
    assert main(["lvs", str(request), "-o", str(report_path), "--format", "json"]) == 0
    capsys.readouterr()

    code = main(
        [
            "lvs",
            "--check",
            str(report_path),
            "--expect-deck-hash",
            "sha256:" + "0a1b" * 16,
            "--format",
            "json",
        ]
    )

    assert code == 1
    assert (
        "--expect-deck-hash cannot be combined with --check" in capsys.readouterr().err
    )
