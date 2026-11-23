"""JSON document-stream mode for ``klt env-provenance lint-envelope``
(issue #5887).

This is a sibling of :mod:`klayout_tools.env_provenance`, split out of it to
keep that module at its ``file-size-baseline.json`` ratchet ceiling: the
stream mode is a self-contained opt-in reader plus a lint variant, and
``klayout_tools.cli.env_provenance_cmd`` selects it when the CLI's
``--json-stream`` flag is passed. Default (single-document) behaviour lives
in :mod:`klayout_tools.env_provenance` and is unchanged.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any

from klayout_tools import _jsongz
from klayout_tools.env_provenance import (
    ENVELOPE_LINT_RECOMMENDATION,
    SCHEMA_VERSION,
    EnvironmentProvenanceError,
    find_absolute_path_fields,
)

__all__ = ["lint_envelope_files_json_stream", "load_json_stream_documents"]


def load_json_stream_documents(path: str) -> list[Any]:
    """Read ``path`` as one or more whitespace-separated JSON documents.

    The stream counterpart of ``env_provenance._load_json_envelope``
    (issue #5887): a file of concatenated ``json.dumps`` records is a clean
    stream, not an ``Extra data`` parse error. A file that cannot be parsed
    at any point -- a truncated or corrupt document, or a file with no
    document at all (empty / whitespace-only) -- is the same "not valid
    JSON" error as the single-document mode, never a silent "clean" verdict.
    """
    try:
        with _jsongz.open_text(path) as handle:
            text = handle.read()
    except _jsongz.READ_ERRORS as exc:
        raise EnvironmentProvenanceError(f"could not read {path}: {exc}") from exc
    decoder = json.JSONDecoder()
    documents: list[Any] = []
    index = 0
    while True:
        while index < len(text) and text[index].isspace():
            index += 1
        if index >= len(text):
            break
        try:
            document, index = decoder.raw_decode(text, index)
        except json.JSONDecodeError as exc:
            raise EnvironmentProvenanceError(
                f"{path} is not valid JSON (document {len(documents)}): {exc}"
            ) from exc
        documents.append(document)
    if not documents:
        raise EnvironmentProvenanceError(
            f"{path} is not valid JSON: no JSON document found"
        ) from None
    return documents


def lint_envelope_files_json_stream(
    paths: Iterable[str], *, allow_prefixes: Iterable[str] = ()
) -> dict[str, Any]:
    """Run :func:`find_absolute_path_fields` over every document of every
    file in ``paths``, read as whitespace-separated JSON document streams.

    The ``--json-stream`` counterpart of
    :func:`klayout_tools.env_provenance.lint_envelope_files`: the report
    shape is identical (``{schema_version, status, finding_count,
    recommendation, files[]}``), except each finding additionally carries an
    additive 0-based ``document`` index and ``field`` stays relative to its
    own document. Raises :class:`EnvironmentProvenanceError` for a file that
    cannot be read or is not a fully parsable document stream -- an
    unparseable input is an error, never a silent "clean" verdict.
    """
    prefixes = tuple(allow_prefixes)
    entries: list[dict[str, Any]] = []
    total = 0
    for path in paths:
        findings: list[dict[str, Any]] = []
        for index, document in enumerate(load_json_stream_documents(path)):
            for finding in find_absolute_path_fields(document, allow_prefixes=prefixes):
                finding["document"] = index
                findings.append(finding)
        total += len(findings)
        entries.append({"file": str(path), "findings": findings})

    return {
        "schema_version": SCHEMA_VERSION,
        "status": "violations" if total else "clean",
        "finding_count": total,
        "recommendation": ENVELOPE_LINT_RECOMMENDATION,
        "files": entries,
    }
