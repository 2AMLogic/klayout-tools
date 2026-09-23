import json
import subprocess
import sys
from importlib.metadata import version

import pytest

from klayout_tools import __version__, gen_compose
from klayout_tools.cli import main
from klayout_tools.cli.parser import create_parser


def test_version_matches_package():
    assert __version__ == version("klayout-tools")


def test_cli_no_subcommand_prints_scaffold(capsys):
    assert main([]) == 0
    assert "scaffold" in capsys.readouterr().out


def test_cli_version_flag(capsys):
    # argparse's `action="version"` prints and raises SystemExit(0).
    with pytest.raises(SystemExit) as exc_info:
        main(["--version"])
    assert exc_info.value.code == 0
    assert __version__ in capsys.readouterr().out


# Regression coverage for #2395: `jsonschema` -> `referencing` -> `rpds-py`'s
# compiled extension can fail to load (macOS arm64 / Python 3.14 rejects its
# code signature). Because `cli/parser.py` imports every `<verb>_cmd` module
# at startup, a module-scope `import jsonschema` in `kb.py` used to take down
# *every* `klt` invocation. These run in a fresh interpreter (the test
# process has long since imported `klayout_tools.kb` / `jsonschema`) with
# those modules poisoned in `sys.modules`, so any attempt to import them
# raises ImportError exactly as the broken native extension would.
_BROKEN_JSONSCHEMA_PRELUDE = (
    "import sys\n"
    "for _name in ('jsonschema', 'referencing', 'rpds'):\n"
    "    sys.modules[_name] = None\n"
    "from klayout_tools.cli import main\n"
)


def run_klt_with_broken_jsonschema(argv: list[str]) -> subprocess.CompletedProcess:
    """Run ``klt <argv>`` in a subprocess where ``jsonschema`` (and its
    ``referencing``/``rpds`` dependency chain) cannot be imported."""
    code = _BROKEN_JSONSCHEMA_PRELUDE + f"raise SystemExit(main({argv!r}))\n"
    return subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=120,
    )


def test_cli_version_flag_survives_broken_jsonschema_import():
    result = run_klt_with_broken_jsonschema(["--version"])
    assert result.returncode == 0, result.stderr
    assert __version__ in result.stdout
    assert "jsonschema" not in result.stderr


@pytest.mark.parametrize(
    ("argv", "payload_key"),
    [
        (["kb", "list", "--format", "json"], "entries"),
        (["kb", "search", "mirror", "--format", "json"], "entries"),
        (["kb", "show", "beta-multiplier-bias-cell", "--format", "json"], "entry"),
    ],
)
def test_cli_kb_read_verbs_survive_broken_jsonschema_import(argv, payload_key):
    # Only `kb validate` needs jsonschema; the read-only `kb` verbs (served
    # from the same `kb.py` module) must keep working without it -- the fix
    # must not be narrower than "`--version` happens to work".
    result = run_klt_with_broken_jsonschema(argv)
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert "error" not in payload
    assert payload_key in payload


def test_cli_kb_validate_reports_broken_jsonschema_as_structured_error():
    # `kb validate` genuinely needs jsonschema, so it must still fail -- but
    # loudly and attributably through the shared JSON error envelope, not a
    # raw traceback or a silent pass.
    result = run_klt_with_broken_jsonschema(["kb", "validate", "--format", "json"])
    assert result.returncode == 1, result.stderr
    # `emit_error` writes the JSON error envelope to stderr.
    payload = json.loads(result.stderr)
    assert payload["error"]["command"] == "kb validate"
    assert "jsonschema" in payload["error"]["message"]


def test_gen_compose_help_lists_every_supported_placement_strategy():
    # Regression test for #683: `gen_compose_parser`'s description drifted
    # from `gen_compose.SUPPORTED_PLACEMENT_STRATEGIES` (it once claimed
    # "row" was the only strategy after "explicit" landed). Assert every
    # supported strategy name is mentioned so `--help` can't silently go
    # stale again the next time a strategy is added.
    parser = create_parser()
    subparsers_action = next(
        action
        for action in parser._actions
        if getattr(action, "choices", None) and "gen-compose" in action.choices
    )
    description = subparsers_action.choices["gen-compose"].description
    assert description is not None
    for strategy in gen_compose.SUPPORTED_PLACEMENT_STRATEGIES:
        assert f'"{strategy}"' in description, (
            f"gen-compose --help description does not mention placement "
            f"strategy {strategy!r}: {description!r}"
        )
