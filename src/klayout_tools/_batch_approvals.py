"""Shared validation for operator-supplied batch approval files."""

from __future__ import annotations

import json
import os
import posixpath
import re
from collections.abc import Callable, Mapping
from typing import Any, Literal, Protocol, TypeVar

APPROVALS_SCHEMA_VERSION = 1
ID_RE = re.compile(r"^[A-Za-z0-9._-]+$")
INTERPRETER_RE = re.compile(r"^[A-Za-z0-9._+/-]+$")
SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


class _Approval(Protocol):
    @property
    def id(self) -> str: ...


_Entry = TypeVar("_Entry", bound=_Approval)
_MakeError = Callable[[str], Exception]


def load_approvals_file(
    path: str,
    *,
    kind: str,
    parse_entry: Callable[[int, Any], _Entry],
    make_error: _MakeError,
) -> tuple[_Entry, ...]:
    """Read the versioned document and reject duplicate IDs after parsing entries."""
    source = f"{kind} approvals file {path}"
    try:
        with open(path, encoding="utf-8") as handle:
            document = json.load(handle)
    except OSError as exc:
        raise make_error(f"cannot read {source}: {exc}") from exc
    except ValueError as exc:
        raise make_error(f"{source} is not valid JSON: {exc}") from exc
    if (
        not isinstance(document, Mapping)
        or document.get("schema_version") != APPROVALS_SCHEMA_VERSION
        or not isinstance(document.get("approvals"), list)
        or set(document) - {"schema_version", "approvals"}
    ):
        raise make_error(
            f"{source} must be an object "
            f'{{"schema_version": {APPROVALS_SCHEMA_VERSION}, "approvals": [...]}}'
        )
    approvals = tuple(
        parse_entry(i, raw) for i, raw in enumerate(document["approvals"])
    )
    if len({approval.id for approval in approvals}) != len(approvals):
        raise make_error(f"{source} repeats an approval id")
    return approvals


def parse_approval_header(
    index: int, raw: Any, *, allowed_keys: frozenset[str], make_error: _MakeError
) -> tuple[str, str, str]:
    """Return the field label, approval ID and interpreter for a known entry shape."""
    label = f"approvals[{index}]"
    if not isinstance(raw, Mapping):
        raise make_error(f"{label} must be an object")
    unknown = sorted(set(raw) - allowed_keys)
    if unknown:
        raise make_error(f"{label} has unknown keys: {', '.join(unknown)}")
    ident = raw.get("id")
    if not isinstance(ident, str) or not ID_RE.match(ident):
        raise make_error(f"{label}.id must match [A-Za-z0-9._-]+")
    interpreter = raw.get("interpreter")
    if not isinstance(interpreter, str) or not INTERPRETER_RE.match(interpreter):
        raise make_error(
            f"{label}.interpreter must be a bare command name or path without "
            "whitespace or shell metacharacters"
        )
    return label, ident, interpreter


def parse_allowed_arguments(
    raw: Mapping[str, Any], label: str, make_error: _MakeError
) -> tuple[tuple[str, ...], ...]:
    allowed = raw.get("allowed_arguments", [[]])
    if not isinstance(allowed, list) or not all(
        isinstance(item, list) and all(isinstance(arg, str) for arg in item)
        for item in allowed
    ):
        raise make_error(f"{label}.allowed_arguments must be a list of string lists")
    return tuple(tuple(item) for item in allowed)


def validate_relative_posix(
    value: object, what: str, *, scope: Literal["repo", "unit"], make_error: _MakeError
) -> str:
    if not isinstance(value, str) or not value.strip() or "\0" in value:
        raise make_error(f"{what} must be a non-empty string")
    absolute = value.startswith(("/", "\\")) or os.path.isabs(value)
    normal = posixpath.normpath(value.replace("\\", "/"))
    if absolute or normal in ("", ".", "..") or normal.startswith("../"):
        if scope == "unit":
            raise make_error(
                f"{what} must be relative and stay inside the unit ({value!r})"
            )
        if absolute:
            raise make_error(f"{what} must be repo-relative (got {value!r})")
        raise make_error(f"{what} must stay inside the repo ({value!r})")
    return normal
