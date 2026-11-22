"""GF180 native registry selections and the starts observed in one deck run."""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Any

from .coverage import work_id
from .drc_klayout_abort import _klayout_deck_error_lines, deck_starts

# Use the same KLayout Ruby as the real run. Register the installed rules
# without opening a layout or invoking any registered block.
_REGISTRY = """
require 'json'
require 'set'
require File.join($directory, 'rule_framework')
require File.join($directory, 'options')
Object.const_set(:GF180_DRC_REGISTRY, RuleFramework::Registry.new)
files = Dir[File.join($directory, 'rule_decks', '*.rb')]
raise 'No GF180 rule registrations' if files.empty?
files.each { |file| load file }
options = GF180DRC::Options
default = options::DEFAULTS.fetch(:decks)
ids = ->(tag) { options.resolve_decks(tag, GF180_DRC_REGISTRY).map(&:id) }
File.write($output, JSON.generate([ids.call(default), ids.call($selector || default)]))
"""


def _registry(
    exe: str, deck_file: str, requested: str | None, work_dir: str, timeout_s: float
) -> tuple[list[str], list[str]] | None:
    script = Path(work_dir, "registry.rb")
    result = Path(work_dir, "registry.json")
    cmd = [
        exe,
        "-b",
        "-r",
        str(script),
        "-rd",
        f"directory={Path(deck_file).resolve().parent}",
        "-rd",
        f"output={result}",
    ]
    if requested is not None:
        cmd.extend(["-rd", f"selector={requested}"])
    try:
        script.write_text(_REGISTRY)
        subprocess.run(cmd, capture_output=True, check=True, timeout=min(timeout_s, 30))
        registry = json.loads(result.read_text())
        if not isinstance(registry, list) or len(registry) != 2:
            return None
        default, selected = registry
        if not all(
            isinstance(ids, list) and all(isinstance(i, str) for i in ids)
            for ids in (default, selected)
        ):
            return None
        return default, selected
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def _not_reached(
    selected: list[str] | None, started: list[str], text: str, failed: bool
) -> list[str] | None:
    """Establish complete start evidence, including a sequential abort prefix."""
    if selected is None:
        return None
    header = re.search(
        r"Starting DRC( in isolated parallel mode)?: executing (\d+) deck\(s\)", text
    )
    if not header or int(header[2]) != len(selected):
        return None
    if set(started) == set(selected):
        return []
    if failed and header[1] is None and started and started == selected[: len(started)]:
        return [name for name in selected if name not in started]
    return None


def native_selection(
    exe: str,
    deck_file: str,
    deck_vars: dict[str, str] | None,
    completed: subprocess.CompletedProcess[str],
    work_dir: str,
    timeout_s: float,
) -> dict[str, Any] | None:
    if Path(deck_file).name != "gf180mcu.drc":
        return None
    requested = (deck_vars or {}).get("decks")
    registry = _registry(exe, deck_file, requested, work_dir, timeout_s)
    text = "\n".join([completed.stdout or "", completed.stderr or ""])
    started = [name for name, _ in deck_starts(text)]
    selected = omitted = None
    if registry is not None:
        default, selected = registry
        omitted = sorted(set(default) - set(selected))
    failed = completed.returncode != 0 or bool(_klayout_deck_error_lines(text))
    not_reached = _not_reached(selected, started, text, failed)
    return {
        "requested": requested,
        "selected": selected,
        "omitted": omitted,
        "narrowed": bool(omitted) if omitted is not None else None,
        "started": started,
        "not_reached": not_reached,
    }


def selection_coverage(
    selection: dict[str, Any] | None, unknown: list[dict[str, str]]
) -> list[dict[str, str]]:
    if selection is None:
        return []
    if selection["omitted"] is None or selection["not_reached"] is None:
        unknown.append(
            {
                "id": work_id("native_deck", "gf180_registry"),
                "reason": "unmeasured_deck_scope",
            }
        )
    return [
        {"id": work_id("native_deck", name), "reason": reason}
        for field, reason in (
            ("omitted", "not_selected"),
            ("not_reached", "not_reached"),
        )
        for name in selection[field] or []
    ]


def selection_fields(selection: dict[str, Any] | None) -> dict[str, Any]:
    if selection is None:
        return {}
    fields: dict[str, Any] = {"native_deck": {"deck_selection": selection}}
    if selection["narrowed"] is not False:
        fields["rule_selection"] = {
            "selectors": [selection["requested"]]
            if selection["requested"] is not None
            else [],
            "subset": selection["narrowed"],
            "omitted": selection["omitted"],
            "selected": selection["selected"],
        }
    return fields
