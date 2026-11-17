"""Gzip-compressed JSON evidence files for `klt signoff` (issue #5452).

`klt sim --report-out report.json.gz` is the documented compact form for a
report committed as evidence; before this, `klt signoff` could not read it
and the citation graded `unmet`/`unreadable_evidence`, so a second
decompressed copy had to be committed next to it. Detection is by the gzip
magic bytes, not the `.gz` suffix -- the same rule `klt yield`'s
`yield_analysis._load_json` already applies, so one rule covers both verbs.

A separate file rather than more of `tests/test_signoff.py`, which is
already far over the per-file size ratchet's hard limit (#2766); the
envelope fixtures and manifest helpers are imported from it, exactly as
`tests/test_signoff_fv_pin.py` does.
"""

from __future__ import annotations

import gzip
import json
from pathlib import Path

from klayout_tools.cli.output import write_json_file
from klayout_tools.signoff import build_signoff, build_tier_report
from test_signoff import (
    DRC_CLEAN_ENVELOPE,
    SIM_PASS_ENVELOPE,
    _item5,
    _manifest,
    _write,
)


def _write_gz(tmp_path, name: str, payload: dict) -> str:
    """Write ``payload`` gzip-compressed exactly as `klt sim --report-out
    *.json.gz` does -- through the same `write_json_file` helper."""
    path = tmp_path / name
    write_json_file(payload, str(path))
    return str(path)


def test_gzip_evidence_grades_identically_to_the_plain_json_twin(tmp_path):
    plain = _write(tmp_path, "sim.json", SIM_PASS_ENVELOPE)
    gzipped = _write_gz(tmp_path, "sim.json.gz", SIM_PASS_ENVELOPE)

    plain_item = _item5({"5": plain})
    gz_item = _item5({"5": gzipped})

    assert plain_item["status"] == "met"
    assert gz_item["citation"]["file"] == gzipped
    # Equal apart from the file name itself: same status, same reason, same
    # citation fields.
    gz_item["citation"]["file"] = plain_item["citation"]["file"]
    assert gz_item == plain_item


def test_gzip_evidence_in_the_rows_list_form_grades_met(tmp_path):
    # The `{"file", "rows"}` list form the original reproduction used
    # (`"5.analog": [{"file": ..., "rows": [...]}]`) reaches the same
    # `_resolve_evidence` read path -- covered so it is not assumed.
    gzipped = _write_gz(tmp_path, "sim.json.gz", SIM_PASS_ENVELOPE)

    manifest = _manifest(
        evidence={"5.analog": [{"file": gzipped, "rows": ["tdec"]}]},
        spec_rows={"5.analog": [{"id": "tdec"}]},
    )
    item = next(i for i in build_tier_report(manifest)["items"] if i["id"] == 5)

    assert item["status"] == "met"
    assert item["row_coverage"]["uncovered_rows"] == []


def test_gzip_detection_is_by_magic_bytes_not_by_the_gz_suffix(tmp_path):
    # Plain JSON under a `.json.gz` name still reads (suffix lies, bytes do
    # not), and a gzip stream under a bare `.json` name is still decompressed.
    lying_suffix = tmp_path / "plain-named.json.gz"
    lying_suffix.write_text(json.dumps(SIM_PASS_ENVELOPE), encoding="utf-8")

    lying_name = tmp_path / "gz-named.json"
    write_json_file(SIM_PASS_ENVELOPE, str(tmp_path / "tmp.json.gz"))
    lying_name.write_bytes((tmp_path / "tmp.json.gz").read_bytes())

    assert _item5({"5": str(lying_suffix)})["status"] == "met"
    assert _item5({"5": str(lying_name)})["status"] == "met"


def test_truncated_gzip_evidence_is_unreadable_evidence_not_a_traceback(tmp_path):
    gzipped = Path(_write_gz(tmp_path, "sim.json.gz", SIM_PASS_ENVELOPE))
    raw = gzipped.read_bytes()
    truncated = tmp_path / "truncated.json.gz"
    truncated.write_bytes(raw[: len(raw) // 2])

    item = _item5({"5": str(truncated)})

    assert item["status"] == "unmet"
    assert item["reason"] == "unreadable_evidence"
    assert item["citation"] is None


def test_corrupt_gzip_evidence_is_unreadable_evidence_not_a_traceback(tmp_path):
    # A damaged deflate stream raises `zlib.error`, which is not an
    # `OSError` -- distinct from the truncation case above (`EOFError`).
    gzipped = Path(_write_gz(tmp_path, "sim.json.gz", SIM_PASS_ENVELOPE))
    raw = bytearray(gzipped.read_bytes())
    for index in range(10, min(len(raw) - 8, 24)):
        raw[index] ^= 0xFF
    corrupt = tmp_path / "corrupt.json.gz"
    corrupt.write_bytes(bytes(raw))

    item = _item5({"5": str(corrupt)})

    assert item["status"] == "unmet"
    assert item["reason"] == "unreadable_evidence"
    assert item["citation"] is None


def test_gzip_evidence_is_refused_for_nonfinite_numbers_like_its_plain_twin(tmp_path):
    # The `_reject_json_constant`/`_finite_json_float` guards apply to a
    # gzip-read document exactly as to a plain one -- `NaN` never becomes a
    # passing verdict just because it arrived compressed.
    body = json.dumps(SIM_PASS_ENVELOPE).replace('"failed": 0', '"failed": NaN')

    plain = tmp_path / "nan.json"
    plain.write_text(body, encoding="utf-8")
    gzipped = tmp_path / "nan.json.gz"
    with gzip.open(gzipped, "wt", encoding="utf-8") as handle:
        handle.write(body)

    for path in (plain, gzipped):
        item = _item5({"5": str(path)})
        assert item["status"] == "unmet"
        assert item["reason"] == "unreadable_evidence"


def test_gzip_envelope_reads_in_envelope_aggregation_mode(tmp_path):
    # Positional envelopes go through the same `_read_json_source`, so they
    # accept gzip too (issue #5452) -- intended, and asserted so it is not
    # silently lost.
    gzipped = _write_gz(tmp_path, "drc.json.gz", DRC_CLEAN_ENVELOPE)

    report = build_signoff([gzipped])

    assert report["status"] == "pass"
    assert report["check_count"] == 1
