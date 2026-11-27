"""Re-attest an item-9 inventory at the running klt version, preserving its bytes."""

from __future__ import annotations

import json
import os
import stat
from contextlib import ExitStack
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any

from . import signoff_attest
from .output_lock import OutputLockError, hold_output_dir
from .signoff_attest_carry import attest_carrying
from .signoff_base import SignoffError


class ToolPinUsageError(SignoffError):
    """The requested pin update cannot be expressed unambiguously."""


def _after_space(text: str, pos: int = 0) -> int:
    while pos < len(text) and text[pos] in " \t\r\n":
        pos += 1
    return pos


def _value_span(text: str, start: int, key: str) -> tuple[int, int]:
    """Find the unique value of key in this object, skipping complete sibling values."""
    decoder = json.JSONDecoder()
    if text[start] != "{":
        raise ToolPinUsageError("--update-tool-pin requires a literal pins.tools.klt")
    pos = _after_space(text, start + 1)
    matches = []
    while text[pos] != "}":
        name, end = decoder.raw_decode(text, pos)
        colon = _after_space(text, end)
        value = _after_space(text, colon + 1)
        _, end = decoder.raw_decode(text, value)
        if name == key:
            matches.append((value, end))
        pos = _after_space(text, end)
        if text[pos] == ",":
            pos = _after_space(text, pos + 1)
    if len(matches) != 1:
        raise ToolPinUsageError(
            "--update-tool-pin requires one unambiguous pins.tools.klt"
        )
    return matches[0]


def _prepare(raw: bytes, version: str) -> tuple[bytes, str]:
    if "+unknown" in version:
        raise ToolPinUsageError("--update-tool-pin requires a known running klt build")
    try:
        text = raw.decode("utf-8")
        json.loads(text)
    except ValueError as exc:
        raise ToolPinUsageError(
            f"invalid inventory JSON for --update-tool-pin: {exc}"
        ) from exc
    start = _after_space(text)
    for key in ("pins", "tools", "klt"):
        start, end = _value_span(text, start, key)
    old = json.loads(text[start:end])
    if isinstance(old, dict) and "from" in old:
        raise ToolPinUsageError(f"update the klt pin in its owner file '{old['from']}'")
    if not isinstance(old, str):
        raise ToolPinUsageError(
            "--update-tool-pin requires a literal string in pins.tools.klt"
        )
    if old == version:
        return raw, old
    return (text[:start] + json.dumps(version) + text[end:]).encode("utf-8"), old


def _stage(files: ExitStack, target: Path, data: bytes) -> Path:
    target.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(dir=target.parent, delete=False) as handle:
        staged = Path(handle.name)
        files.callback(staged.unlink, missing_ok=True)
        if target.exists():
            os.fchmod(handle.fileno(), stat.S_IMODE(target.stat().st_mode))
        handle.write(data)
    return staged


def _publish(
    inventory: Path,
    original: bytes,
    updated: bytes,
    result: dict[str, Any],
    output: Path | None,
) -> None:
    with ExitStack() as files:
        envelope_file = None
        if output:
            envelope = {
                key: value for key, value in result.items() if key != "skipped_checks"
            }
            encoded = signoff_attest.render_envelope(envelope).encode("utf-8")
            if not output.exists() or output.read_bytes() != encoded:
                envelope_file = _stage(files, output, encoded)
        if inventory.read_bytes() != original:
            raise SignoffError(
                "inventory changed during --update-tool-pin. Retry the command."
            )
        restore = None
        if original != updated:
            restore = _stage(files, inventory, original)
            _stage(files, inventory, updated).replace(inventory)
        try:
            if envelope_file and output:
                envelope_file.replace(output)
        except BaseException:
            if restore:
                restore.replace(inventory)
            raise


def update_tool_pin(args: Any, inventory_path: str) -> dict[str, Any]:
    """Check staged inventory bytes, then publish only a passing result."""
    inventory = Path(inventory_path)
    output = Path(args.output).resolve() if args.output else None
    if inventory.is_symlink() or not inventory.is_file():
        raise ToolPinUsageError("--update-tool-pin requires a regular inventory file")
    if output and (
        output == inventory.resolve()
        or (output.exists() and output.samefile(inventory))
    ):
        raise ToolPinUsageError(
            "--output must differ from the inventory for --update-tool-pin"
        )
    targets = {inventory.resolve()}
    if output:
        targets.add(output)
    try:
        with ExitStack() as locks:
            for target in sorted(targets):
                locks.enter_context(hold_output_dir(target))
            original = inventory.read_bytes()
            version = signoff_attest.build_version()
            updated, old = _prepare(original, version)

            def build(**kwargs: Any) -> dict[str, Any]:
                return signoff_attest.build_attestation(
                    9, inventory_path, inventory_bytes=updated, **kwargs
                )

            result = (
                attest_carrying(args.output, build) if args.skip_local_only else build()
            )
            if result["status"] == "pass":
                if updated != original:
                    result["provenance"]["tool_pin_bump"] = {
                        "tool": "klt",
                        "from": old,
                        "to": version,
                    }
                _publish(inventory, original, updated, result, output)
            return result
    except (OSError, OutputLockError) as exc:
        raise SignoffError(
            f"--update-tool-pin could not publish the attestation: {exc}"
        ) from exc
