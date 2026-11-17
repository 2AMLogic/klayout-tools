"""Shared item 9 attestation fixtures (issues #4848, #5850).

Originally defined in `tests/test_signoff_attest_item9.py`; the
`depends_on` tests (issue #5850) split into
`tests/test_signoff_attest_item9_depends_on.py` now share these
definitions.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

PINS = {
    "pdk": {"name": "sky130A", "revision": "0fe599b2afb6708d281543108caf8310912f54af"},
    "tools": {"ngspice": "42"},
}

OK_ARGV = [sys.executable, "-c", "import sys; sys.exit(0)"]
BAD_ARGV = [sys.executable, "-c", "import sys; sys.exit(1)"]


def make_repo(tmp_path) -> Path:
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    (repo / "tb").mkdir()
    (repo / "attest").mkdir()
    for name in ("a", "b"):
        (repo / "tb" / f"{name}.spice").write_text(f"* {name}\n")
    return repo


def make_inventory(repo: Path, **fields) -> str:
    inventory = {
        "schema_version": 1,
        "kind": "t1-attestation-inventory",
        "t1_item": 9,
        "pins": PINS,
        **fields,
    }
    path = repo / "attest" / "item9.json"
    path.write_text(json.dumps(inventory))
    return str(path)
