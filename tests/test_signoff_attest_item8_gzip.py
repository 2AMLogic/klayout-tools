"""Item-8 citation reads share the gzip-transparent envelope reader (#5688).

The recogniser must accept exactly what `--manifest` grading accepts: a cited
evidence envelope committed gzip-compressed (`klt sim --report-out
report.json.gz` is the documented compact form, issue #5452) and never a file
carrying a nonfinite literal. Split from `tests/test_signoff_attest_item8.py`,
which sits at the per-file size ratchet's 500-line limit (#2766); the
inventory fixtures are imported from it, exactly as
`tests/test_signoff_gzip.py` does for `test_signoff.py`.
"""

from __future__ import annotations

from klayout_tools.cli.output import write_json_file
from test_signoff_attest_item8 import _ENV, _PASS, _analog_inventory, _attest, _repo


def test_gzip_committed_citation_is_recognised(tmp_path):
    repo = _repo(tmp_path)
    write_json_file(
        {**_ENV, "status": "pass", "result": {"fmax": 100, "area": 5, "power": 2}},
        str(repo / "evidence" / "sim.json.gz"),
    )
    inventory = _analog_inventory(
        spec_rows=["gain"],
        citations={"gain": ["evidence/sim.json.gz"]},
        envelope_checks={"evidence/sim.json.gz": _PASS},
    )

    envelope = _attest(repo, inventory)

    assert envelope["status"] == "pass"
    checks = envelope["attestation"]["checks"]
    assert [c for c in checks if c["check"] == "envelope"] == [
        {
            "check": "envelope",
            "role": "evidence",
            "path": "evidence/sim.json.gz",
            "kind": "generic",
            "passed": True,
        }
    ]


def test_cited_nonfinite_json_fails(tmp_path):
    # The shared reader refuses nonstandard numeric constants; the
    # recogniser inherits that via _read_envelope, like `--manifest` grading.
    repo = _repo(tmp_path)
    (repo / "evidence" / "sim_nan.json").write_text(
        '{"schema_version": 1, "kind": "generic", "status": "pass", "x": NaN}'
    )
    inventory = _analog_inventory(
        spec_rows=["gain"],
        citations={"gain": ["evidence/sim_nan.json"]},
        envelope_checks={"evidence/sim_nan.json": _PASS},
    )

    envelope = _attest(repo, inventory)

    assert envelope["status"] == "fail"
    checks = envelope["attestation"]["checks"]
    assert [c for c in checks if c["check"] == "envelope"] == [
        {
            "check": "envelope",
            "role": "evidence",
            "path": "evidence/sim_nan.json",
            "kind": None,
            "passed": False,
        }
    ]
