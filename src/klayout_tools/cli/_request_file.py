"""Shared reader for the ``klt`` request JSON files (issue #5540).

Four verbs (``klt route``, ``klt gen-compose``, ``klt layout-plan
execute``/``check``) take a request document as a file argument and read it
through :func:`load_request_file`, so their CLI reads cannot drift from the
shared loader in :mod:`klayout_tools._paths` again -- each verb's
hand-rolled copy had dropped ``UnicodeDecodeError`` and escaped as a raw
traceback past the error envelope on a non-UTF-8 request.

Private to the :mod:`klayout_tools.cli` package -- not part of the public
``klayout_tools`` API.
"""

from __future__ import annotations

from typing import Any

from .._paths import _load_request_json


class RequestFileError(Exception):
    """A ``klt`` request JSON file could not be read or parsed (issue #5540).

    :func:`load_request_file` raises this for a missing file, a directory,
    an unreadable or non-UTF-8 file, or invalid JSON; the command modules
    catch it and translate the message into :func:`.output.emit_error` so
    it reaches the caller through the shared error envelope.
    """


def load_request_file(path: str) -> Any:
    """Read and decode a ``klt`` request JSON file.

    Delegates to :func:`klayout_tools._paths._load_request_json` so the CLI
    request reads cannot drift from the library's loader.

    Raises :class:`RequestFileError` for every failure mode the read can
    hit; returns whatever :func:`json.load` produced, unvalidated.
    """
    return _load_request_json(path, RequestFileError)
