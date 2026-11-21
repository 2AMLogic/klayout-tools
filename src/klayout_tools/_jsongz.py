"""Gzip-transparent opening of a JSON file source, shared by the ``klt``
verbs that read a committed report as input.

``klt sim --report-out report.json.gz`` is the documented compact form for a
report committed as evidence (``docs/cli/sim.md``), so a reader that only
understood plain text forced a second, decompressed copy of the same envelope
to be committed beside it -- far larger, and two copies of one envelope to
keep in agreement (issue #5452).

Detection is by the gzip magic bytes, never the ``.gz`` suffix: one rule
covers every caller, and a renamed file still reads. ``klt yield``'s
``yield_analysis._load_json`` applies the same rule inline. Callers keep their
own exception type and message wording, since those are part of each verb's
JSON-contract ``error`` text -- from here they need only :data:`READ_ERRORS`.
Following the precedent set by ``_jsonnum.py`` and ``_text.py``.
"""

from __future__ import annotations

import gzip
import zlib
from typing import IO

GZIP_MAGIC = b"\x1f\x8b"

READ_ERRORS: tuple[type[Exception], ...] = (
    OSError,
    EOFError,
    zlib.error,
    UnicodeDecodeError,
)
"""What :func:`open_text`, and reading from the handle it returns, can raise.

A truncated gzip stream raises ``EOFError`` and a damaged one ``zlib.error``;
neither is an ``OSError``, so a caller catching only ``OSError`` and
``UnicodeDecodeError`` turns a corrupt ``.gz`` into a traceback instead of its
own read-failure verdict. (``gzip.BadGzipFile`` already is an ``OSError``.)
"""


def open_text(path: str) -> IO[str]:
    """Open ``path`` for UTF-8 text reading, transparently decompressing it
    when its first two bytes are the gzip magic number."""
    with open(path, "rb") as probe:
        gzipped = probe.read(2) == GZIP_MAGIC
    if gzipped:
        return gzip.open(path, "rt", encoding="utf-8")
    return open(path, encoding="utf-8")
