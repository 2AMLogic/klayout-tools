"""Determine the time units of modules in the Icarus build's source order."""

from __future__ import annotations

import os
import re
import subprocess
from collections.abc import Iterable
from contextlib import nullcontext
from dataclasses import dataclass, field
from tempfile import TemporaryDirectory

from ._text import read_text
from .functional_verification_base import FunctionalVerificationError

# Keep escaped names intact even if they contain a quote or a comment opener.
_HDL_LITERAL = re.compile(r'\\\S+|"(?:\\.|[^"\\])*"|//[^\n]*|/\*.*?\*/', re.DOTALL)
_PREPROCESSING = re.compile(r"`(?!(?:timescale|resetall)\b)")
_UNIT = r"(?:100|10|1)\s*(?:ms|us|ns|ps|fs|s)"
_NAME = r"(?:[A-Za-z_][\w$]*|\\\S+)"
_TOKEN = re.compile(
    rf"\\\S+|`timescale\s+(?P<directive>{_UNIT})\s*/\s*{_UNIT}|`resetall\b"
    rf"|(?<![\w$])(?:module|macromodule)\s+(?:(?:static|automatic)\s+)?(?P<module>{_NAME})"
    r"|(?<![\w$])(?P<scope>package|interface|program)(?![\w$])"
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
    return _HDL_LITERAL.sub(lambda m: m[0] if m[0][0] == "\\" else " ", text)


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
}


def _check_build_args(args: list[str]) -> None:
    """Accept options that preserve the unit scan, refuse inputs with unmodeled
    compilation-unit semantics, such as -u or an additional command file."""
    operands = iter(args)
    for arg in operands:
        if arg in ("-D", "-I"):
            if next(operands, None) is not None:
                continue
        elif arg == "-T":
            if next(operands, None) in ("min", "typ", "max"):
                continue
        elif arg in _UNIT_NEUTRAL_SWITCHES or arg.startswith(("-D", "-I", "-W")):
            continue
        elif arg in (
            "-v",
            "-g2012",
            "-g2005-sv",
            "-gspecify",
            "-ginterconnect",
            "-gsupported-assertions",
            "-Tmin",
            "-Ttyp",
            "-Tmax",
        ):
            continue
        raise FunctionalVerificationError(
            f"cannot verify time units with options.build_args entry {arg!r}"
        )


def _active_sources(
    sources: list[str],
    includes: list[str],
    defines: dict[str, str | None],
    build_args: list[str],
    cocotb_build_dir: str | None,
) -> list[str]:
    _check_build_args(build_args)
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


def _compilation_default(texts: list[str], compiler_default: str | None) -> str | None:
    """A compilation-unit timeunit precedes design elements and spans source files."""
    for text in texts:
        for token in _TOKEN.finditer(text):
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


def _instance_type(
    text: str, body: Iterable[tuple[int, int]], instance: str
) -> str | None:
    """Find an explicit instantiation, declining an ambiguous name."""
    name = re.escape(instance.removeprefix("\\"))
    pattern = re.compile(
        rf"(?<![\w$.\\])({_NAME})\s*(?:#\s*\([^;]*?\)\s*)?"
        rf"(?<![\w$])(?:\\{name}\s+|{name}\s*)\("
    )
    types = {
        m[1].removeprefix("\\")
        for start, end in body
        for m in pattern.finditer(text, start, end)
    }
    return types.pop() if len(types) == 1 else None


@dataclass
class _ModuleTimeUnits:
    text: str
    top: dict[str, _Module] = field(default_factory=dict)
    nested: dict[str, list[_Module]] = field(default_factory=dict)

    def follow(self, module: _Module | None, path: Iterable[str]) -> _Module | None:
        for instance in path:
            if module is None:
                return None
            name = instance.removeprefix("\\")
            if name in module.children:
                # Icarus instantiates a nested declaration under its own name.
                module = module.children[name]
            else:
                cell = _instance_type(self.text, module.body, name)
                module = self.top.get(cell) if cell else None
        return module


def _verilog_time_units(
    source_texts: list[str], default_unit: str | None
) -> _ModuleTimeUnits:
    """Index declarations by lexical scope, retaining each module's own unit."""
    default_unit = _compilation_default(source_texts, default_unit)
    directive = None
    stack: list[_Module] = []
    text = "\n".join(source_texts)
    units = _ModuleTimeUnits(text)
    start = 0
    for token in _TOKEN.finditer(text):
        if token["module"] is not None:
            name = token["module"].removeprefix("\\")
            header = _MODULE_HEADER.match(text, token.end())
            body_start = header.end() if header else token.end()
            if stack:
                parent = stack[-1]
                parent.body.append((start, token.start()))
                module = _Module(name, parent.unit, body_start)
                parent.children[name] = module
                units.nested.setdefault(name, []).append(module)
            else:
                module = _Module(name, directive or default_unit, body_start)
                units.top[name] = module
            stack.append(module)
            start = body_start
        elif token["directive"] is not None:
            directive = "".join(token["directive"].split())
        elif token[0] == "`resetall":
            directive = None
        elif token["end"] and stack:
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
