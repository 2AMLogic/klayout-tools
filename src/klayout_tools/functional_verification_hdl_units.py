"""Determine the time units of modules in the Icarus build's source order."""

from __future__ import annotations

import os
import re
import subprocess
from collections.abc import Iterable, Iterator
from contextlib import nullcontext
from dataclasses import dataclass, field
from tempfile import TemporaryDirectory

from ._text import read_text
from .functional_verification_base import FunctionalVerificationError

# Keep escaped identifiers and the language names consumed by `begin_keywords.
_LANGUAGE = r"(?:1364|1800)-\d{4}(?:-noconfig)?"
_HDL_LITERAL = re.compile(
    rf'(?P<keep>\\\S+|"{_LANGUAGE}")|"(?:\\.|[^"\\])*"|//[^\n]*|/\*.*?\*/', re.DOTALL
)
_PREPROCESSING = re.compile(r"`(?!(?:timescale|resetall)\b)")
_UNIT = r"(?:100|10|1)\s*(?:ms|us|ns|ps|fs|s)"
_NAME = r"(?:[A-Za-z_][\w$]*|\\\S+)"
_TOKEN = re.compile(
    rf"\\\S+|`timescale\s+(?P<directive>{_UNIT})\s*/\s*{_UNIT}|`resetall\b"
    rf'|`begin_keywords\s+"(?P<language>{_LANGUAGE})"|`end_keywords\b'
    rf"|(?<![\w$])(?:module|macromodule)\s+(?:(?:static|automatic)\s+)?(?P<module>{_NAME})"
    rf"|(?<![\w$])(?P<scope>package|interface|program)\b\s*(?:(?:static|automatic)\s+)?"
    rf"(?P<scope_name>{_NAME})?\s*(?=[;(#]|import\b)"
    r"|(?<![\w$])(?P<scope_end>endpackage|endinterface|endprogram)(?![\w$])"
    r"|(?<![\w$])(?P<end>endmodule)(?![\w$])|(?<![\w$])(?P<local>timeunit)(?![\w$])"
)
_LOCAL_DECLARATION = re.compile(rf"\s*(?P<unit>{_UNIT})\s*(?:/\s*{_UNIT}\s*)?;")
# A module-local timeunit declaration precedes other module items. In a
# different scope (e.g. a nested interface), do not attribute it to the module.
# Header package imports each have their own semicolon before the optional
# parameter and port lists. Escaped identifiers can contain literal semicolons.
_HEADER_FIELD = r"(?:[^;\\]|\\\S+(?=\s))*"
_MODULE_HEADER = re.compile(
    rf"(?:\s*import\b{_HEADER_FIELD};)*{_HEADER_FIELD};\s*(?:timeprecision\s+{_UNIT}\s*;\s*)?"
)


def _hdl_code(text: str) -> str:
    return _HDL_LITERAL.sub(lambda m: m["keep"] or " ", text)


def _hdl_tokens(text: str, system_verilog: bool) -> Iterator[re.Match[str]]:
    """Recognize SystemVerilog keywords only in an active IEEE 1800 region."""
    modes = [system_verilog]
    for token in _TOKEN.finditer(text):
        if token["language"]:
            modes.append(token["language"].startswith("1800-"))
        elif token[0] == "`end_keywords":
            if len(modes) > 1:
                modes.pop()
        elif modes[-1] or not (token["local"] or token["scope"] or token["scope_end"]):
            yield token


# Icarus accepts both forms of each of these feature switches. They leave
# the compilation-unit model intact. Include selection is replayed by -E.
_UNIT_NEUTRAL_SWITCHES = {
    f"-g{prefix}{feature}"
    for prefix in ("", "no-")
    for feature in (
        "assertions",
        "icarus-misc",
        "io-range-error",
        "relative-include",
        "shared-loop-index",
        "std-include",
        "strict-ca-eval",
        "strict-expr-width",
        "xtypes",
    )
} | {
    "-v",
    "-gspecify",
    "-ginterconnect",
    "-gsupported-assertions",
    "-Tmin",
    "-Ttyp",
    "-Tmax",
    "-pfileline=0",
    "-pfileline=1",
}

_SYSTEM_VERILOG_MODE = {
    "-g1995": False,
    "-g2001": False,
    "-g2001-noconfig": False,
    "-g2005": False,
    "-g2005-sv": True,
    "-g2009": True,
    "-g2012": True,
}


def _build_system_verilog(args: list[str]) -> bool:
    """Return the final language mode, refusing unmodeled compilation options."""
    system_verilog = True  # both runners supply -g2012 before user options
    operands = iter(args)
    for option in operands:
        arg = option
        if arg in ("-D", "-I", "-T", "-W", "-g", "-p"):
            arg += next(operands, "")
        if arg in _SYSTEM_VERILOG_MODE:
            system_verilog = _SYSTEM_VERILOG_MODE[arg]
        elif arg.startswith(("-D", "-I", "-W")) and len(arg) > 2:
            continue
        elif arg not in _UNIT_NEUTRAL_SWITCHES:
            raise FunctionalVerificationError(
                f"cannot verify time units with options.build_args entry {option!r}"
            )
    return system_verilog


def _active_sources(
    sources: list[str],
    includes: list[str],
    defines: dict[str, str | None],
    build_args: list[str],
    cocotb_build_dir: str | None,
) -> list[str]:
    code = []
    for source in sources:
        text = read_text(source)
        if text is None:
            raise FunctionalVerificationError(f"cannot read HDL source {source!r}")
        code.append(_hdl_code(text))
    if not any(_PREPROCESSING.search(part) for part in code):
        return code

    # Both dispatch paths compile with -g2012. The Verilog bench runs in the
    # caller's cwd. Cocotb runs inside build_dir, and appends valueless -D
    # flags after user build arguments. Scoped defines are already wrappers.
    valued = [f"-D{k}={v}" for k, v in defines.items() if v is not None]
    valueless = [f"-D{k}" for k, v in defines.items() if v is None]
    flags = [*valued, *(f"-I{path}" for path in includes)]
    flags += (
        [*build_args, *valueless] if cocotb_build_dir else [*valueless, *build_args]
    )
    cmd = [
        "iverilog",
        "-g2012",
        "-gspecify",
        "-ginterconnect",
        *flags,
        "-E",
        "-o",
        "-",
        *sources,
    ]
    # A fresh cocotb build directory is empty. Use an empty sibling so ../
    # resolves identically without leaving a build artifact on refusal.
    try:
        cwd = (
            TemporaryDirectory(dir=os.path.dirname(cocotb_build_dir))
            if cocotb_build_dir and not os.path.isdir(cocotb_build_dir)
            else nullcontext(cocotb_build_dir)
        )
        with cwd as directory:
            output = subprocess.run(
                cmd,
                cwd=directory,
                capture_output=True,
                text=True,
                errors="replace",
                check=True,
            )
    except subprocess.CalledProcessError as exc:
        raise FunctionalVerificationError(
            f"Icarus preprocessing failed: {exc.stderr.strip()}"
        ) from exc
    except OSError as exc:
        raise FunctionalVerificationError(
            f"could not preprocess HDL with Icarus: {exc}"
        ) from exc
    return [_hdl_code(output.stdout)]


def _declared_unit(text: str, start: int) -> str | None:
    value = _LOCAL_DECLARATION.match(text, start)
    return "".join(value["unit"].split()) if value else None


def _compilation_default(
    text: str, compiler_default: str | None, system_verilog: bool
) -> str | None:
    """A compilation-unit timeunit precedes design elements and spans source files."""
    for token in _hdl_tokens(text, system_verilog):
        if token["module"] or token["scope"]:
            return compiler_default
        if token["local"]:
            return _declared_unit(text, token.end())
    return compiler_default


@dataclass
class _Module:
    name: str
    unit: str | None
    body_start: int
    children: dict[str, _Module] = field(default_factory=dict)
    # Direct module items only. Nested declarations have their own bodies.
    body: list[tuple[int, int]] = field(default_factory=list)
    instances: dict[tuple[str, ...], str | None] | None = None


def _collapse_parentheses(text: str) -> str:
    """Hide port connections, parameters and conditions, keeping each outer ()."""
    parts = []
    depth = start = 0
    for token in re.finditer(r"\\\S+|[()]", text):
        if token[0] == "(":
            if depth == 0:
                parts.append(text[start : token.start()] + "()")
            depth += 1
        elif token[0] == ")" and depth:
            depth -= 1
            start = token.end()
    if not depth:
        parts.append(text[start:])
    return "".join(parts)


_INSTANCE = rf"{_NAME}\s*\(\)"
_MODULE_ITEM = re.compile(
    rf"(?<![\w$.\\])(?:"
    rf"(?P<begin>begin)(?![\w$])(?:\s*:\s*(?P<label>{_NAME}))?"
    rf"|(?P<end>end)(?![\w$])(?:\s*:\s*{_NAME})?"
    rf"|(?P<type>{_NAME})(?:\s*#\s*\(\)\s*|\s+)"
    rf"(?P<instances>{_INSTANCE}(?:\s*,\s*{_INSTANCE})*)\s*;)"
    r"|\\\S+"
)
_INSTANCE_NAME = re.compile(rf"({_NAME})\s*\(\)")


def _instance_types(text: str, module: _Module) -> dict[tuple[str, ...], str | None]:
    """Index explicit instances by the named blocks that contain them."""
    scopes: list[str] = []
    types: dict[tuple[str, ...], str | None] = {}
    for start, end in module.body:
        for item in _MODULE_ITEM.finditer(_collapse_parentheses(text[start:end])):
            if item["begin"]:
                scopes.append((item["label"] or "").removeprefix("\\"))
            elif item["end"] and scopes:
                scopes.pop()
            elif item["type"]:
                cell = item["type"].removeprefix("\\")
                for name in _INSTANCE_NAME.findall(item["instances"]):
                    path = (*scopes, name.removeprefix("\\"))
                    if types.setdefault(path, cell) != cell:
                        types[path] = None  # Alternative branches disagree.
    return types


@dataclass
class _ModuleTimeUnits:
    text: str
    top: dict[str, _Module] = field(default_factory=dict)
    nested: dict[str, list[_Module]] = field(default_factory=dict)

    def open_scope(
        self, token: re.Match[str], parent: _Module | None, default_unit: str | None
    ) -> _Module:
        name = (token["module"] or token["scope_name"] or "").removeprefix("\\")
        header = _MODULE_HEADER.match(self.text, token.end())
        body_start = header.end() if header else token.end()
        module = _Module(name, parent.unit if parent else default_unit, body_start)
        if token["module"]:
            if parent is None:
                self.top[name] = module
            else:
                parent.children[name] = module
                self.nested.setdefault(name, []).append(module)
        return module

    def follow(self, module: _Module | None, path: Iterable[str]) -> _Module | None:
        scope: tuple[str, ...] = ()
        for instance in path:
            if module is None:
                return None
            name = instance.removeprefix("\\")
            if not scope and name in module.children:
                # Icarus instantiates a nested declaration under its own name.
                module = module.children[name]
                continue
            if module.instances is None:
                module.instances = _instance_types(self.text, module)
            scope = (*scope, name)
            if scope in module.instances:
                cell = module.instances[scope]
                module = self.top.get(cell) if cell is not None else None
                scope = ()
        return None if scope else module


def _verilog_time_units(
    source_texts: list[str], default_unit: str | None, system_verilog: bool = True
) -> _ModuleTimeUnits:
    """Index declarations by lexical scope, retaining each module's own unit."""
    directive = None
    stack: list[_Module] = []
    text = "\n".join(source_texts)
    default_unit = _compilation_default(text, default_unit, system_verilog)
    units = _ModuleTimeUnits(text)
    start = 0
    for token in _hdl_tokens(text, system_verilog):
        if token.start() < start:
            continue
        if token["module"] or token["scope"]:
            parent = stack[-1] if stack else None
            if stack:
                stack[-1].body.append((start, token.start()))
            module = units.open_scope(token, parent, directive or default_unit)
            stack.append(module)
            start = module.body_start
        elif token["directive"] is not None:
            directive = "".join(token["directive"].split())
        elif token[0] == "`resetall":
            directive = None
        elif (token["end"] or token["scope_end"]) and stack:
            stack.pop().body.append((start, token.start()))
            start = token.end()
        elif token["local"] and stack:
            module = stack[-1]
            declared = _declared_unit(text, token.end())
            if module.body_start == token.start() or declared == module.unit:
                module.unit = declared
            else:
                module.unit = None
    return units
