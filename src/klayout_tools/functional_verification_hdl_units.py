"""Determine the time units of modules in the Icarus build's source order."""

from __future__ import annotations

import os
import re
import subprocess
from contextlib import nullcontext
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
    r"|(?<![\w$])(?P<end>endmodule)\b|(?<![\w$])(?P<local>timeunit)\b"
)
_LOCAL_DECLARATION = re.compile(rf"\s*(?P<unit>{_UNIT})\s*(?:/\s*{_UNIT}\s*)?;")
# A module-local timeunit declaration precedes other module items. In a
# different scope (e.g. a nested interface), do not attribute it to the module.
_MODULE_HEADER = re.compile(rf"[^;]*;\s*(?:timeprecision\s+{_UNIT}\s*;\s*)?")


def _hdl_code(text: str) -> str:
    return _HDL_LITERAL.sub(lambda m: m[0] if m[0][0] == "\\" else " ", text)


def _check_build_args(args: list[str]) -> None:
    """Accept options that preserve the unit scan, refuse inputs with unmodeled
    compilation-unit semantics, such as -u or an additional command file."""
    operands = iter(args)
    for arg in operands:
        if arg in ("-D", "-I"):
            if next(operands, None) is not None:
                continue
        elif arg.startswith(("-D", "-I", "-W")) or arg in (
            "-g2012",
            "-g2005-sv",
            "-grelative-include",
            "-gno-relative-include",
            "-gstd-include",
            "-gno-std-include",
            "-gspecify",
            "-ginterconnect",
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


def _local_unit(text: str, start: int, token: re.Match[str]) -> str | None:
    header = _MODULE_HEADER.match(text, start)
    value = _LOCAL_DECLARATION.match(text, token.end())
    if header is None or header.end() != token.start() or value is None:
        return None
    return "".join(value["unit"].split())


def _verilog_time_units(
    source_texts: list[str], default_unit: str | None
) -> dict[str, str | None]:
    """An inline timeunit overrides the active directive for that module only."""
    units: dict[str, str | None] = {}
    directive = None
    module = None
    header_start = 0
    for text in source_texts:
        for token in _TOKEN.finditer(text):
            if token["module"] is not None:
                module = token["module"].removeprefix("\\")
                header_start = token.end()
                units[module] = directive or default_unit
            elif token["directive"] is not None:
                directive = "".join(token["directive"].split())
            elif token[0] == "`resetall":
                directive = None
            elif token["end"]:
                module = None
            elif token["local"]:
                if module is None:
                    # An out-of-module declaration might set the compilation
                    # unit default. Only an explicit directive is certain now.
                    default_unit = None
                else:
                    units[module] = _local_unit(text, header_start, token)
    return units
