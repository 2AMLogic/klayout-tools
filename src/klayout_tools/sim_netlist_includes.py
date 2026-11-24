"""Resolve include paths in an executing simulation corner's netlist body."""

from __future__ import annotations

import os
import re

_PDK_ROOT_VAR = re.compile(r"\$(?:\{PDK_ROOT\}|PDK_ROOT(?![a-zA-Z0-9_]))")

_NETLIST_INCLUDE_RE = re.compile(
    r"^(?P<lead>\s*\.inc(?:lude)?\s+)(?P<target>\"[^\"]*\"|'[^']*'|\S+)(?P<tail>.*)$",
    re.IGNORECASE,
)


def _split_include_target(match: re.Match[str]) -> tuple[str, str]:
    """Return ``(quote, target)`` for an ``_NETLIST_INCLUDE_RE`` match."""
    raw = match.group("target")
    quote = raw[0] if raw[0] in "\"'" else ""
    return quote, (raw[1:-1] if quote else raw)


def _unset_include_diagnostic(
    netlist_path: str, number: int, target: str, expanded: str
) -> dict[str, str]:
    unset = sorted(set(re.findall(r"\$\{?(\w+)\}?", expanded)))
    return {
        "severity": "error",
        "code": "missing_include",
        "message": (
            f"{netlist_path} line {number}: .include target "
            f"{target!r} names environment variable(s) "
            f"{', '.join(unset) or '?'} that are not set on this "
            "host, so ngspice cannot load it"
        ),
    }


def _expand_include_line(
    line: str, number: int, netlist_path: str, pdk_root: str | None
) -> tuple[str | None, dict[str, str] | None]:
    """Expand ``$VAR``/``~`` in one line's ``.include`` target.

    Returns ``(rewritten_line, diagnostic)``: ``rewritten_line`` is ``None``
    when the line is left as written (not an include, nothing to expand, or
    an unset variable -- the latter also yields a diagnostic)."""
    match = _NETLIST_INCLUDE_RE.match(line)
    if match is None:
        return None, None
    quote, target = _split_include_target(match)
    if "$" not in target and not target.startswith("~"):
        return None, None
    # Expand other variables from the environment before inserting the pinned
    # root, which may itself contain a literal dollar sign.
    parts = _PDK_ROOT_VAR.split(target) if pdk_root is not None else [target]
    expanded_parts = [os.path.expandvars(part) for part in parts]
    unresolved = "".join(expanded_parts)
    if "$" in unresolved:
        return None, _unset_include_diagnostic(netlist_path, number, target, unresolved)
    expanded = os.path.expanduser((pdk_root or "").join(expanded_parts))
    if not quote and any(ch.isspace() for ch in expanded):
        quote = '"'
    return (
        f"{match.group('lead')}{quote}{expanded}{quote}{match.group('tail')}",
        None,
    )


def absolutize_relative_include(line: str, base: str) -> str:
    """Rewrite a relative ``.include`` target that resolves beside the
    original netlist (``base``) to its absolute path."""
    match = _NETLIST_INCLUDE_RE.match(line)
    if match is None:
        return line
    quote, target = _split_include_target(match)
    if not target or os.path.isabs(target):
        return line
    candidate = os.path.join(base, target)
    if not os.path.isfile(candidate):
        return line
    q = quote or ('"' if any(c.isspace() for c in candidate) else "")
    return f"{match.group('lead')}{q}{candidate}{q}{match.group('tail')}"


def expand_netlist_includes(
    netlist_path: str, corner_dir: str, pdk_root: str | None = None
) -> tuple[str, list[dict[str, str]]]:
    """Expand ``$VAR``/``~`` in the netlist body's own ``.include``/``.inc``
    targets on the *executing* host (issue #2926).

    ngspice does not expand environment variables in include paths, so a
    ``$PDK_ROOT/...`` include (which off-host backends deliberately leave
    verbatim for the executing host) would otherwise fail to load. Uses the
    same ``os.path.expandvars``/``expanduser`` semantics as
    ``sim_staging`` (an unset variable stays as ``$``). Returns
    ``(netlist_path_to_include, diagnostics)``: the original path unchanged
    when no include needs expanding; otherwise a rewritten copy under
    ``corner_dir`` (relative targets that resolve beside the original
    netlist are made absolute so the copy keeps their meaning). A target
    whose variable is unset is left as written and yields a
    ``missing_include`` diagnostic naming the file, line and variable.
    """
    try:
        with open(netlist_path, encoding="utf-8", errors="surrogateescape") as fh:
            text = fh.read()
    except OSError:
        return netlist_path, []
    if "$" not in text and "~" not in text:
        return netlist_path, []
    out: list[str] = []
    diagnostics: list[dict[str, str]] = []
    changed = False
    for number, line in enumerate(text.split("\n"), start=1):
        rewritten_line, diagnostic = _expand_include_line(
            line, number, netlist_path, pdk_root
        )
        if diagnostic is not None:
            diagnostics.append(diagnostic)
        if rewritten_line is None:
            out.append(line)
            continue
        out.append(rewritten_line)
        changed = True
    if not changed:
        return netlist_path, diagnostics
    # Keep other relative includes meaning what they meant beside the original.
    base = os.path.dirname(os.path.abspath(netlist_path))
    final = [absolutize_relative_include(line, base) for line in out]
    rewritten = os.path.join(corner_dir, "netlist.expanded.cir")
    with open(rewritten, "w", encoding="utf-8", errors="surrogateescape") as fh:
        fh.write("\n".join(final))
    return rewritten, diagnostics
