"""Regression for a malformed Loom config tier beside an oversized tier (#7966)."""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

RESOLVER = Path(__file__).resolve().parents[1] / ".loom/scripts/lib/config-resolver.sh"


@pytest.mark.skipif(shutil.which("jq") is None, reason="the Bash resolver requires jq")
@pytest.mark.parametrize(
    "malformed",
    [
        "defaults.json",
        ".loom/config.json",
        ".loom-project/project.json",
        ".loom-local/fleet-repo.json",
        ".loom-local/local.json",
    ],
)
def test_two_documents_beside_oversized_tier(tmp_path: Path, malformed: str) -> None:
    defaults = tmp_path / "defaults.json"
    bad = tmp_path / malformed
    good = tmp_path / ".loom/config.json" if bad == defaults else defaults
    expected = {"pad": "x" * (137_977 - len('{"pad":""}'))}
    good.parent.mkdir(parents=True, exist_ok=True)
    good.write_text(json.dumps(expected, separators=(",", ":")))
    bad.parent.mkdir(parents=True, exist_ok=True)
    bad.write_text('{"first":1}\n{"second":2}\n')

    result = subprocess.run(
        [
            "bash",
            "-c",
            'source "$1" && loom_resolve_config "$2"',
            "bash",
            RESOLVER,
            tmp_path,
        ],
        env={**os.environ, "LOOM_CONFIG_DEFAULTS_FILE": str(defaults)},
        check=True,
        capture_output=True,
        text=True,
    )
    assert json.loads(result.stdout) == expected
