"""Item-9 reference-form pins and the shared dotted-key JSON walker (#5563).

A pin leaf may name the file that owns its value instead of hand-copying the
value into the inventory: ``{"from": "config/toolchain.json", "key":
"tools.ngspice.commit"}`` resolves to the non-empty string at dotted ``key``
of that JSON file at attest and ``--check`` time, so a pin bump drifts the
envelope the same way a source edit does since #4764. A literal leaf is
checked for shape only, exactly as before.

Split out of :mod:`signoff_attest` for the per-file size ratchet (that module
sits at its baseline ceiling), the same reason :mod:`signoff_attest_item9`
and :mod:`signoff_attest_drift` are separate. Imports nothing from
:mod:`signoff_attest` (which imports this): the repo-path normalizer is
passed in as a callable, the way :mod:`signoff_attest_item9` takes its
helpers.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Mapping
from typing import Any

from .signoff import _finite_json_float, _reject_json_constant


def json_node(root: str, path: str, field: str) -> Any:
    """The raw node at dotted ``field`` (digit segments index lists) of the
    JSON file at repo-relative ``path``, or ``None`` when the file cannot be
    read or parsed, or the key is absent. A JSON ``null`` at the key is
    indistinguishable from an absent one; both callers require a type a
    ``null`` fails (item 8's metric bindings a finite number, item 9's pins a
    non-empty string), so the ambiguity never reaches a caller."""
    try:
        with open(os.path.join(root, path), "rb") as handle:
            node: Any = json.loads(
                handle.read().decode("utf-8"),
                parse_constant=_reject_json_constant,
                parse_float=_finite_json_float,
            )
    except (OSError, UnicodeDecodeError, ValueError):
        return None
    for segment in field.split("."):
        if isinstance(node, Mapping) and segment in node:
            node = node[segment]
        elif isinstance(node, list) and segment.isdigit() and int(segment) < len(node):
            node = node[int(segment)]
        else:
            return None
    return node


def _pin_leaf(
    leaf: Any,
    field: str,
    root: str,
    normalize_path: Callable[[Any, str, str], str],
) -> tuple[Any, dict[str, Any] | None]:
    """One pin leaf: a ``{"from", "key"}`` mapping resolves to the non-empty
    string it names -- or ``None`` when it cannot (missing file, unreadable
    JSON, absent key, non-string or empty value, or a malformed reference),
    which the existing pin validators grade as a failed check. Any other
    value passes through untouched. Returns ``(value, reference)``:
    ``reference`` is the declared ``{"from", "key"}`` object when the leaf was
    a reference, else ``None``. Only a ``from`` that cannot name a file inside
    the repository raises (via ``normalize_path``), the same refusal every
    other inventory path gets."""
    if not (isinstance(leaf, Mapping) and set(leaf) == {"from", "key"}):
        return leaf, None
    path = normalize_path(leaf["from"], f"{field}.from", root)
    key = leaf["key"]
    value: Any = None
    if isinstance(key, str) and key.strip():
        node = json_node(root, path, key)
        if isinstance(node, str) and node:
            value = node
    return value, {"from": leaf["from"], "key": key}


def resolve_pin_refs(
    pins: Any,
    root: str,
    normalize_path: Callable[[Any, str, str], str],
) -> tuple[Any, dict[str, Any]]:
    """Item 9's ``pins`` with every reference-form leaf resolved (issue
    #5563), plus the provenance to record on the checks: a map with ``pdk`` /
    ``tools`` entries, each naming the ``{"from", "key"}`` object that leaf
    declared. Empty when no reference was used, so a literal-only inventory
    grades exactly as before. Leaf positions: ``pins.pdk.name``,
    ``pins.pdk.revision`` and every ``pins.tools.<tool>``."""
    if not isinstance(pins, Mapping):
        return pins, {}
    resolved = dict(pins)
    sources: dict[str, Any] = {}
    pdk = resolved.get("pdk")
    if isinstance(pdk, Mapping):
        new_pdk = dict(pdk)
        pdk_sources: dict[str, Any] = {}
        for leaf in ("name", "revision"):
            if leaf not in new_pdk:
                continue
            value, reference = _pin_leaf(
                new_pdk[leaf], f"pins.pdk.{leaf}", root, normalize_path
            )
            new_pdk[leaf] = value
            if reference is not None:
                pdk_sources[leaf] = reference
        resolved["pdk"] = new_pdk
        if pdk_sources:
            sources["pdk"] = pdk_sources
    tools = resolved.get("tools")
    if isinstance(tools, Mapping):
        new_tools = dict(tools)
        tool_sources: dict[str, Any] = {}
        for tool, leaf in tools.items():
            value, reference = _pin_leaf(
                leaf, f"pins.tools.{tool}", root, normalize_path
            )
            new_tools[tool] = value
            if reference is not None:
                tool_sources[tool] = reference
        resolved["tools"] = new_tools
        if tool_sources:
            sources["tools"] = dict(
                sorted(tool_sources.items(), key=lambda item: str(item[0]))
            )
    return resolved, sources
