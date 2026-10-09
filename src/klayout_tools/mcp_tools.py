"""Tool derivation and execution for the generic ``klt mcp serve`` bridge.

Issue #2830 (Phase 5, "agent surface"). This module is deliberately free of
any MCP-SDK import: it walks the argparse registry built by
:func:`klayout_tools.cli.parser.create_parser`, derives one tool definition
per leaf verb (``klt_<verb>``, or ``klt_<verb>_<subcommand>`` for the nested
verbs such as ``pdk``/``deck``/``wave``/``kb``), and executes a call by
running ``klt <verb> ... --format json`` in a subprocess. The SDK-facing
glue lives in :mod:`klayout_tools.mcp_server`, so this half is unit-testable
without the optional ``[mcp]`` extra.

Design rules (see ``docs/guides/mcp-server.md``):

* **The CLI is the contract.** There is no in-process import path that could
  fork from ``klt``: a call is exactly the subprocess a human would run, so
  the envelope and exit codes of ``docs/json-contract.md`` hold unchanged.
* **Exit-code mapping follows the contract.** Exit ``1``/``2`` -> MCP
  ``isError`` (no report was written); exit ``0`` and every code ``>= 3`` ->
  a report is on stdout and is returned as the structured result, with the
  raw code carried in ``_meta.klt_exit_code``.
* **Safety defaults.** Fleet-dispatching verbs are hidden unless opted in
  (:data:`HIDDEN_BY_DEFAULT`); ``--backend remote|batch`` is refused unless
  fleet dispatch is allowed; every subprocess runs with the workspace root
  as its cwd and path-like arguments may not escape it.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

TOOL_PREFIX = "klt_"

DOCS_BASE_URL = "https://github.com/2AMLogic/klayout-tools/blob/main/docs/cli"

#: Verbs that dispatch to a paid/remote fleet by design: not listed unless the
#: operator opts in (``--enable-verb`` / ``--allow-fleet``).
HIDDEN_BY_DEFAULT: frozenset[str] = frozenset({"yield-campaign"})

#: ``--backend`` values that provision cloud hosts or submit to the batch
#: fleet. Refused unless fleet dispatch is explicitly allowed.
FLEET_BACKENDS: frozenset[str] = frozenset({"remote", "batch"})

#: Verbs the bridge never exposes (it would recurse into itself).
EXCLUDED_VERBS: frozenset[str] = frozenset({"mcp"})

#: Arguments the bridge owns: ``--format`` is forced to ``json``; colour is
#: meaningless for JSON output.
_BRIDGE_OWNED_DESTS = frozenset({"format", "color", "no_color"})

_PATH_HINT = re.compile(r"\b(path|file|files|directory|dir|folder|gds|oasis)\b", re.I)

DEFAULT_TIMEOUT_S = 900.0


@dataclass
class Tool:
    """One derived MCP tool: a leaf ``klt`` command."""

    name: str
    verb_path: tuple[str, ...]
    description: str
    input_schema: dict[str, Any]
    parser: argparse.ArgumentParser = field(repr=False, compare=False)
    json_supported: bool = True
    path_dests: frozenset[str] = frozenset()

    @property
    def top_verb(self) -> str:
        return self.verb_path[0]


def _subparsers_action(
    parser: argparse.ArgumentParser,
) -> argparse._SubParsersAction | None:
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return action
    return None


def _help_text(parser: argparse.ArgumentParser, action: argparse.Action) -> str:
    if not action.help or action.help is argparse.SUPPRESS:
        return ""
    try:
        return parser._get_formatter()._expand_help(action)
    except Exception:  # noqa: BLE001 -- never let a malformed help string drop a tool
        return str(action.help)


def _scalar_schema(action: argparse.Action) -> dict[str, Any]:
    schema: dict[str, Any]
    if action.type is int:
        schema = {"type": "integer"}
    elif action.type is float:
        schema = {"type": "number"}
    else:
        schema = {"type": "string"}
    if action.choices is not None and not isinstance(action.choices, dict):
        choices = list(action.choices)
        if all(isinstance(c, str) for c in choices):
            schema = {"type": "string", "enum": choices}
        elif all(isinstance(c, int) and not isinstance(c, bool) for c in choices):
            schema = {"type": "integer", "enum": choices}
    return schema


def _is_flag(action: argparse.Action) -> bool:
    return isinstance(
        action,
        (
            argparse._StoreTrueAction,
            argparse._StoreFalseAction,
            argparse._StoreConstAction,
            argparse.BooleanOptionalAction,
        ),
    )


def _action_schema(action: argparse.Action) -> dict[str, Any]:
    if _is_flag(action):
        return {"type": "boolean"}
    if isinstance(action, argparse._CountAction):
        return {"type": "integer", "minimum": 0}
    scalar = _scalar_schema(action)
    multi = isinstance(action, argparse._AppendAction) or action.nargs in (
        "*",
        "+",
    )
    if not multi and isinstance(action.nargs, int) and action.nargs > 1:
        multi = True
    if multi:
        return {"type": "array", "items": scalar}
    return scalar


def _derivable_actions(parser: argparse.ArgumentParser) -> list[argparse.Action]:
    out = []
    for action in parser._actions:
        if isinstance(action, (argparse._HelpAction, argparse._SubParsersAction)):
            continue
        if action.dest is argparse.SUPPRESS or action.dest in _BRIDGE_OWNED_DESTS:
            continue
        if action.default is argparse.SUPPRESS and not action.option_strings:
            continue
        # `--version`-style actions carry SUPPRESS defaults and no value.
        if (
            action.nargs == 0
            and not _is_flag(action)
            and not isinstance(action, argparse._CountAction)
        ):
            continue
        out.append(action)
    return out


def _input_schema(
    parser: argparse.ArgumentParser,
) -> tuple[dict[str, Any], frozenset[str]]:
    properties: dict[str, Any] = {}
    required: list[str] = []
    path_dests: set[str] = set()
    for action in _derivable_actions(parser):
        schema = _action_schema(action)
        help_text = _help_text(parser, action)
        if help_text:
            schema["description"] = help_text
        if (
            action.default is not None
            and action.default is not argparse.SUPPRESS
            and isinstance(action.default, (str, int, float, bool))
            and not _is_flag(action)
        ):
            schema["default"] = action.default
        properties[action.dest] = schema
        is_required = (
            action.required if action.option_strings else action.nargs not in ("?", "*")
        )
        if is_required:
            required.append(action.dest)
        if schema.get("type") == "string" and "enum" not in schema:
            hint = f"{action.dest} {action.metavar or ''} {help_text}"
            if (
                _PATH_HINT.search(hint)
                or not action.option_strings
                and (action.dest in ("file", "path", "request"))
            ):
                path_dests.add(action.dest)
        elif schema.get("type") == "array" and schema["items"].get("type") == "string":
            if _PATH_HINT.search(f"{action.dest} {help_text}"):
                path_dests.add(action.dest)
    schema_doc: dict[str, Any] = {
        "type": "object",
        "properties": properties,
        "additionalProperties": False,
    }
    if required:
        schema_doc["required"] = required
    return schema_doc, frozenset(path_dests)


def _json_supported(parser: argparse.ArgumentParser) -> bool:
    for action in parser._actions:
        if action.dest == "format":
            return action.choices is not None and "json" in action.choices
    return False


def _describe(
    verb_path: tuple[str, ...], parser: argparse.ArgumentParser, help_line: str
) -> str:
    body = (parser.description or help_line or "").strip()
    body = re.sub(r"\s+", " ", body)
    doc = f"{DOCS_BASE_URL}/{verb_path[0]}.md"
    return f"klt {' '.join(verb_path)}: {body} (CLI docs: {doc})"


def _walk(
    parser: argparse.ArgumentParser,
    prefix: tuple[str, ...],
    help_by_name: dict[str, str],
    out: list[Tool],
) -> None:
    sub = _subparsers_action(parser)
    if sub is None:
        schema, path_dests = _input_schema(parser)
        out.append(
            Tool(
                name=TOOL_PREFIX + "_".join(p.replace("-", "_") for p in prefix),
                verb_path=prefix,
                description=_describe(prefix, parser, help_by_name.get(prefix[-1], "")),
                input_schema=schema,
                parser=parser,
                json_supported=_json_supported(parser),
                path_dests=path_dests,
            )
        )
        return
    helps = {a.dest: (a.help or "") for a in sub._choices_actions}
    seen: set[int] = set()
    for name, child in sub.choices.items():
        if id(child) in seen:  # alias of an already-walked parser
            continue
        seen.add(id(child))
        _walk(child, prefix + (name,), helps, out)


def derive_tools() -> list[Tool]:
    """Derive one :class:`Tool` per leaf ``klt`` command from the registry."""
    from .cli.parser import create_parser

    root = create_parser()
    sub = _subparsers_action(root)
    assert sub is not None, "klt registers its verbs through add_subparsers"
    helps = {a.dest: (a.help or "") for a in sub._choices_actions}
    tools: list[Tool] = []
    seen: set[int] = set()
    for name, child in sub.choices.items():
        if name in EXCLUDED_VERBS or id(child) in seen:
            continue
        seen.add(id(child))
        _walk(child, (name,), helps, tools)
    return tools


def registered_verbs() -> list[str]:
    """Top-level verb names, excluding those the bridge never exposes."""
    from .cli.parser import create_parser

    sub = _subparsers_action(create_parser())
    assert sub is not None
    return [n for n in sub.choices if n not in EXCLUDED_VERBS]


class ToolCallRefused(Exception):
    """A call was rejected by a bridge safety rule before any subprocess ran."""


@dataclass
class BridgePolicy:
    """Operator-chosen safety configuration for one ``klt mcp serve`` process."""

    workspace_root: Path
    enabled_verbs: frozenset[str] = frozenset()
    allow_fleet: bool = False
    allow_outside_workspace: bool = False
    timeout_s: float = DEFAULT_TIMEOUT_S

    def verb_visible(self, tool: Tool) -> bool:
        if self.allow_fleet:
            return True
        return tool.top_verb not in HIDDEN_BY_DEFAULT or (
            tool.top_verb in self.enabled_verbs
        )


def visible_tools(tools: list[Tool], policy: BridgePolicy) -> list[Tool]:
    return [t for t in tools if policy.verb_visible(t)]


def _check_path(value: str, policy: BridgePolicy, dest: str) -> None:
    if policy.allow_outside_workspace:
        return
    root = policy.workspace_root.resolve()
    resolved = (root / value).resolve()
    if resolved != root and root not in resolved.parents:
        raise ToolCallRefused(
            f"argument {dest!r}: path {value!r} resolves outside the workspace "
            f"root {str(root)!r} (start the server with --allow-outside-workspace "
            "to permit this)"
        )


def _option_tokens(action: argparse.Action, value: Any) -> list[str]:
    """argv tokens for one optional (``--flag``) argument."""
    flag = max(action.option_strings, key=len)
    if isinstance(action, argparse.BooleanOptionalAction):
        want = bool(value)
        return [
            next(
                (s for s in action.option_strings if s.startswith("--no-") != want),
                flag,
            )
        ]
    if isinstance(action, (argparse._StoreTrueAction, argparse._StoreConstAction)):
        return [flag] if value else []
    if isinstance(action, argparse._StoreFalseAction):
        return [] if value else [flag]
    if isinstance(action, argparse._CountAction):
        return [flag] * int(value)
    if isinstance(action, argparse._AppendAction):
        items = value if isinstance(value, list) else [value]
        return [tok for item in items for tok in (flag, str(item))]
    if isinstance(value, list):
        return [flag, *(str(i) for i in value)]
    return [flag, str(value)]


def _check_safety(
    tool: Tool, dest: str, items: list[Any], policy: BridgePolicy
) -> None:
    if dest in tool.path_dests:
        for item in items:
            if isinstance(item, str):
                _check_path(item, policy, dest)
    if (
        dest == "backend"
        and not policy.allow_fleet
        and any(str(i) in FLEET_BACKENDS for i in items)
    ):
        raise ToolCallRefused(
            f"--backend {items!r} dispatches to a remote/batch fleet; start "
            "the server with --allow-fleet to permit it"
        )


def build_argv(
    tool: Tool, arguments: dict[str, Any], policy: BridgePolicy
) -> list[str]:
    """Translate a tool call's JSON arguments into the ``klt`` argv.

    Raises :class:`ToolCallRefused` for unknown arguments, missing required
    arguments, fleet-dispatching ``--backend`` values, and path escapes.
    """
    actions = {a.dest: a for a in _derivable_actions(tool.parser)}
    unknown = sorted(set(arguments) - set(actions))
    if unknown:
        raise ToolCallRefused(f"unknown argument(s): {', '.join(unknown)}")
    missing = [d for d in tool.input_schema.get("required", []) if d not in arguments]
    if missing:
        raise ToolCallRefused(f"missing required argument(s): {', '.join(missing)}")

    positional: list[str] = []
    optional: list[str] = []
    for dest, value in arguments.items():
        if value is None:
            continue
        action = actions[dest]
        items = value if isinstance(value, list) else [value]
        _check_safety(tool, dest, items, policy)
        if action.option_strings:
            optional.extend(_option_tokens(action, value))
        else:
            positional.extend(str(i) for i in items)
    argv = list(tool.verb_path) + positional + optional
    if tool.json_supported:
        argv += ["--format", "json"]
    return argv


@dataclass
class CallOutcome:
    """Result of one bridged call."""

    is_error: bool
    payload: dict[str, Any]
    exit_code: int | None
    argv: list[str]


def _error_payload(tool: Tool, message: str, exit_code: int | None = None) -> dict:
    error: dict[str, Any] = {"command": " ".join(tool.verb_path), "message": message}
    if exit_code is not None:
        error["exit_code"] = exit_code
    return {"schema_version": 1, "error": error}


def _parse_json_object(text: str) -> dict[str, Any] | None:
    text = text.strip()
    if not text:
        return None
    try:
        value = json.loads(text)
    except ValueError:
        return None
    if isinstance(value, dict):
        return value
    return {"result": value}


def execute(tool: Tool, arguments: dict[str, Any], policy: BridgePolicy) -> CallOutcome:
    """Run ``tool`` as a ``klt`` subprocess and map the result per the contract."""
    try:
        argv = build_argv(tool, arguments, policy)
    except ToolCallRefused as exc:
        return CallOutcome(True, _error_payload(tool, str(exc), 2), 2, [])
    cmd = [sys.executable, "-m", "klayout_tools.cli", *argv]
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(policy.workspace_root),
            capture_output=True,
            text=True,
            timeout=policy.timeout_s,
            stdin=subprocess.DEVNULL,
            check=False,
        )
    except subprocess.TimeoutExpired:
        msg = f"timed out after {policy.timeout_s:g}s"
        return CallOutcome(True, _error_payload(tool, msg), None, argv)
    except OSError as exc:
        return CallOutcome(
            True, _error_payload(tool, f"cannot launch klt: {exc}"), None, argv
        )

    rc = proc.returncode
    if rc in (1, 2):
        payload = _parse_json_object(proc.stderr)
        if payload is None or "error" not in payload:
            message = (proc.stderr or proc.stdout).strip() or f"klt exited {rc}"
            payload = _error_payload(tool, message, rc)
        return CallOutcome(True, payload, rc, argv)
    if rc < 0:
        return CallOutcome(
            True, _error_payload(tool, f"klt killed by signal {-rc}", rc), rc, argv
        )
    payload = _parse_json_object(proc.stdout)
    if payload is None:
        if tool.json_supported:
            msg = "klt produced no JSON on stdout despite --format json"
            return CallOutcome(True, _error_payload(tool, msg, rc), rc, argv)
        payload = {"stdout": proc.stdout}
    return CallOutcome(False, payload, rc, argv)
