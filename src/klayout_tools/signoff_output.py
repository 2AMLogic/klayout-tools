"""``-o``/``--output`` for ``klt signoff`` renders (issue #5867): the
``--manifest``/``--fleet`` twin of
:func:`~.signoff_attest.write_attestation`.

The bytes written are exactly what ``--format json`` prints, a committed
file is never rewritten when the render is unchanged (mtime included),
and the write itself is atomic -- temp file in the target directory plus
``os.replace`` -- so a death mid-write can never leave a truncated
verdict-of-record behind. Only called after a successful render, so a
committed report is never replaced by an error envelope."""

from __future__ import annotations

import argparse
import json
import os
import tempfile


def write_report_file(payload: dict, output: str) -> bool:
    """Write ``payload``'s JSON envelope to ``output`` **only if its bytes
    would change**; return whether the file was written. Raises ``OSError``
    on a failed write, for the caller to translate into a ``signoff``
    error envelope."""
    rendered = (json.dumps(payload, indent=2) + "\n").encode("utf-8")
    try:
        with open(output, "rb") as handle:
            if handle.read() == rendered:
                return False
    except OSError:
        pass
    directory = os.path.dirname(os.path.abspath(output))
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(
        dir=directory, prefix=os.path.basename(output) + ".", suffix=".tmp"
    )
    try:
        with open(fd, "wb") as handle:
            handle.write(rendered)
        os.replace(tmp, output)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return True


def non_attest_problem(
    args: argparse.Namespace, manifest_source: str | None, fleet_source: str | None
) -> str | None:
    """The misuse message for a non-``--attest`` invocation (issues #2844
    and #5867), or ``None`` when the flags are allowed: the attest-only
    flags (``--inventory``/``--input``/``--generator``/
    ``--regenerated-hash``/``--hash-kind``/``--skip-local-only``/
    ``--require-build-match``) never apply without ``--attest``, and
    ``--output`` is refused with ``--check`` (which verifies a committed
    report and never writes) and without a ``--manifest``/``--fleet``
    render behind it."""
    stray = [
        flag
        for flag, attr in (
            ("--inventory", "inventory"),
            ("--input", "attest_input"),
            ("--generator", "generator"),
            ("--regenerated-hash", "regenerated_hash"),
            ("--skip-local-only", "skip_local_only"),
            ("--require-build-match", "require_build_match"),
        )
        if getattr(args, attr, None)
    ]
    if stray or getattr(args, "hash_kind", None) is not None:
        return (
            "--inventory/--input/--generator/--regenerated-hash/"
            "--hash-kind/--skip-local-only/--require-build-match only apply "
            "to --attest"
        )
    output = getattr(args, "output", None)
    if not output:
        return None
    if getattr(args, "check", None):
        return (
            "--check and --output are mutually exclusive: --check verifies "
            "a committed report and never writes"
        )
    if not (manifest_source or fleet_source):
        return (
            "--output only applies to --attest, --manifest, or --fleet "
            "(it writes the JSON envelope to a file; see docs/cli/signoff.md)"
        )
    return None


def write_output_or_message(payload: dict, output: str | None) -> str | None:
    """Write ``payload``'s envelope to ``output`` (issue #5867) and return
    ``None``, or return the error message on ``OSError``; a falsy
    ``output`` is a no-op."""
    if not output:
        return None
    try:
        write_report_file(payload, output)
    except OSError as exc:
        return f"could not write --output '{output}': {exc}"
    return None
