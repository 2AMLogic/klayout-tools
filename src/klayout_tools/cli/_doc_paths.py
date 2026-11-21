"""Resolve CLI help references against the pages shipped in the wheel."""

import argparse
import re
from pathlib import Path
from textwrap import TextWrapper
from typing import Any

_DOC_DIR = Path(__file__).resolve().parents[1] / "data" / "cli"
_DOC_POINTER = re.compile(
    r"(?<![\w./\\-])docs/cli/([A-Za-z0-9_-]+\.md)(?![\w/\\-]|\.\w)"
)


def _bundled_page(match: re.Match[str]) -> str:
    page = _DOC_DIR / match[1]
    return str(page) if page.is_file() else match[0]


def resolve_doc_paths(text: str) -> str:
    """Replace standalone CLI doc pointers wherever the installed page exists."""
    return _DOC_POINTER.sub(_bundled_page, text)


class _DocWrapper(TextWrapper):
    def _split(self, text: str) -> list[str]:
        # Some help strings already contain an absolute path. Keep its
        # directory together even when the installation prefix contains spaces.
        chunks = re.findall(rf"(?:{re.escape(str(_DOC_DIR))}|\S)+|\s+", text)
        return [" " if word.isspace() else resolve_doc_paths(word) for word in chunks]


def _wrap_doc_text(text: str, width: int, indent: str = "") -> list[str] | None:
    if "docs/cli/" not in text and str(_DOC_DIR) not in text:
        return None
    if not _DOC_DIR.is_dir():
        return None
    return _DocWrapper(
        width=width,
        initial_indent=indent,
        subsequent_indent=indent,
        expand_tabs=False,
        replace_whitespace=False,
        break_long_words=False,
    ).wrap(text.strip())


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
