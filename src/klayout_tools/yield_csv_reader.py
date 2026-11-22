"""Per-draw CSV input with leading harness metadata."""

from __future__ import annotations

import csv
import io
from collections.abc import Iterator, Sequence
from typing import Any


def draw_csv_rows(text: str) -> tuple[Sequence[str] | None, Iterator[dict[str, Any]]]:
    stream = io.StringIO(text, newline="")
    while text.startswith("#", stream.tell()):
        stream.readline()
    reader = csv.DictReader(stream)
    header = reader.fieldnames
    return header, _data_rows(text, stream, reader)


def _data_rows(
    text: str, stream: io.StringIO, reader: csv.DictReader
) -> Iterator[dict[str, Any]]:
    while (start := stream.tell()) < len(text):
        # Consume blank records here so DictReader cannot skip past one into
        # a comment. DictReader itself consumes all lines of a quoted field.
        if text[start] in "\r\n":
            stream.readline()
            continue
        if text[start] == "#":
            raise csv.Error("unexpected comment after the header")
        yield next(reader)
