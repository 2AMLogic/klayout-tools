"""Resolve CLI help references against the pages shipped in the wheel."""

import argparse
import re
from pathlib import Path
from textwrap import TextWrapper
from typing import Any

import klayout_tools

_DOC_POINTER = re.compile(
    r"(?<![\w./\\-])docs/cli/([A-Za-z0-9_-]+\.md)(?![\w/\\-]|\.\w)"
)


def _doc_dir() -> Path:
    return Path(klayout_tools.__file__).parent / "data" / "cli"


def _bundled_page(match: re.Match[str]) -> str:
    page = _doc_dir() / match[1]
    return str(page) if page.is_file() else match[0]


def resolve_doc_paths(text: str) -> str:
    """Replace standalone CLI doc pointers wherever the installed page exists."""
    return _DOC_POINTER.sub(_bundled_page, text)


class _DocWrapper(TextWrapper):
    def _split(self, text: str) -> list[str]:
        # Expand after tokenizing so even an install path containing spaces
        # is measured and wrapped as a single word.
        return [resolve_doc_paths(word) for word in super()._split(text)]


def _wrap_doc_text(text: str, width: int, indent: str = "") -> list[str] | None:
    if "docs/cli/" not in text or not _doc_dir().is_dir():
        return None
    return _DocWrapper(
        width=width,
        initial_indent=indent,
        subsequent_indent=indent,
        break_on_hyphens=False,
        break_long_words=False,
    ).wrap(" ".join(text.split()))


class DocHelpFormatter(argparse.HelpFormatter):
    def _fill_text(self, text: str, width: int, indent: str) -> str:
        lines = _wrap_doc_text(text, width, indent)
        if lines is None:
            return super()._fill_text(text, width, indent)
        return "\n".join(lines)

    def _split_lines(self, text: str, width: int) -> list[str]:
        lines = _wrap_doc_text(text, width)
        if lines is None:
            return super()._split_lines(text, width)
        return lines


class RawDescriptionDocHelpFormatter(
    argparse.RawDescriptionHelpFormatter, DocHelpFormatter
):
    def _fill_text(self, text: str, width: int, indent: str) -> str:
        return super()._fill_text(resolve_doc_paths(text), width, indent)


class DocArgumentParser(argparse.ArgumentParser):
    """Subparsers inherit their parent's parser class at every depth."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        kwargs.setdefault("formatter_class", DocHelpFormatter)
        super().__init__(*args, **kwargs)
