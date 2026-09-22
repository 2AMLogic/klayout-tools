"""Entry testbench fixture for the #2097 identity-contract fixtures.

A deliberately tiny synthetic verification command: it never imports
cocotb, so the contract tests can execute it with plain CPython in any
disposable tree (issue #2097 acceptance item: "Execute one small synthetic
Python check in disposable trees containing only its declared files").

The check itself: ``a * MULTIPLIER + b`` must equal ``expected_sum`` where
``a``/``b``/``expected_sum`` come from the declared fixture
``fixtures/vectors.json`` and ``MULTIPLIER`` comes from the declared helper
``expected_counts.py``. Mutating either declared file flips the verdict,
which is exactly the item-9 mutation control.

The undeclared dynamic input (item 10): this file ALSO reads
``runtime_control.json`` -- a file that is deliberately NOT in the declared
inventory. When it exists with ``{"mode": "always_fail"}`` the command
fails even though every declared byte is unchanged, demonstrating why v1
closure is structurally ``partial``.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from expected_counts import MULTIPLIER

TB_DIR = Path(__file__).resolve().parent


def verdict() -> str:
    """Return ``"pass"`` or ``"fail"`` for the synthetic check."""
    vectors = json.loads(
        (TB_DIR / "fixtures" / "vectors.json").read_text(encoding="utf-8")
    )
    actual = vectors["a"] * MULTIPLIER + vectors["b"]
    # Undeclared dynamic input: runtime_control.json is intentionally absent
    # from the declared inventory (issue #2097 checklist item 10).
    control_path = TB_DIR / "runtime_control.json"
    if control_path.exists():
        control = json.loads(control_path.read_text(encoding="utf-8"))
        if control.get("mode") == "always_pass":
            return "pass"
        if control.get("mode") == "always_fail":
            return "fail"
    return "pass" if actual == vectors["expected_sum"] else "fail"


if __name__ == "__main__":
    result = verdict()
    print(result)
    sys.exit(0 if result == "pass" else 1)
