"""Lexical helpers for gate-level Verilog text shared by LVS and SDF preflight."""

import re

#: Verilog block (`/* ... */`) and line (`// ...`) comments, stripped before
#: any other parsing. `re.S` so a block comment can span newlines.
_BLOCK_COMMENT_RE = re.compile(r"/\*.*?\*/", re.S)
_LINE_COMMENT_RE = re.compile(r"//[^\n]*")


def strip_verilog_comments(text: str) -> str:
    text = _BLOCK_COMMENT_RE.sub(" ", text)
    return _LINE_COMMENT_RE.sub("", text)


def split_top_level(text: str, seps: str = ",") -> list[str]:
    """Split ``text`` on any character in ``seps`` that is not nested inside
    ``()``/``[]``/`{}` -- used for both a module's port list and one
    instance's `.PORT(NET), .PORT(NET)` connection list, neither of which
    can contain a top-level comma any other way in this grammar."""
    parts: list[str] = []
    depth = 0
    current: list[str] = []
    escaped = False
    for char in text:
        if char == "\\" or (escaped and not char.isspace()):
            escaped = True
            current.append(char)
            continue
        escaped = False
        if char in "([{":
            depth += 1
        elif char in ")]}":
            depth = max(0, depth - 1)
        if char in seps and depth == 0:
            parts.append("".join(current))
            current = []
            continue
        current.append(char)
    parts.append("".join(current))
    return [p.strip() for p in parts if p.strip()]
