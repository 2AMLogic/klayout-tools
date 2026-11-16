"""Re-check a finished ``klt sim`` report's netlist evidence (issue #5821).

``environment.netlist_closure`` / ``environment.netlist_identity`` were
write-only: nothing could tell, after the fact, that a pinned netlist had been
edited. :func:`check_sim_report` is the reader half behind ``klt sim --check``
-- no engine run, no network, no PDK, only text hashing over the files the
report already names -- built on the shared ``--check`` core in
:mod:`klayout_tools._report_verify` (as ``klt drc``/``lvs``/``extract`` do).

Two independent signals are compared, because replaying the recorded paths
alone can return a false ``match`` (``_normalized_netlist_text`` replaces an
``.include`` *target* with the bare directive, so a repointed include leaves
the top file's normalized text unchanged):

1. each entry's recorded raw ``sha256`` vs. the file on disk now;
2. the recorded ``netlist_identity`` vs. one recomputed with the writer's own
   :func:`~klayout_tools.sim_staging._normalized_netlist_text` (imported, not
   re-implemented).

``diagnosis`` in the payload reads them together: ``intact``, ``cosmetic``
(bytes moved, identity did not), ``electrical`` (both moved),
``normalization_changed`` (bytes identical, identity moved -- the tool's
normalization changed, not the netlist; see #5438) or ``indeterminate``.
"""

from __future__ import annotations

import hashlib
import os
from typing import Any

from ._digest import sha256_file
from ._report_verify import (
    build_check_result,
    get_path,
    hash_check,
    load_committed_report,
    resolve_committed_path,
)
from .sim_errors import SimError
from .sim_staging import IncludeStagingError, _normalized_netlist_text, _read_text


def _entry_path(
    entry: dict[str, Any], *, report_path: str, root: str | None
) -> str | None:
    """The on-disk path of one closure entry, or ``None`` when unresolvable."""
    if root and entry.get("scope") == "repo":
        relative = entry.get("path")
        return (
            os.path.join(root, relative)
            if isinstance(relative, str) and relative
            else None
        )
    return resolve_committed_path(entry, report_path=report_path)


def _recomputed_identity(paths: list[str]) -> str | None:
    digest = hashlib.sha256()
    for path in paths:
        try:
            text, _ = _read_text(path)
        except IncludeStagingError:
            return None
        digest.update(_normalized_netlist_text(text).encode("utf-8"))
        digest.update(b"\0")
    return f"sha256:{digest.hexdigest()}"


def _diagnose(raw_ok: bool, identity_ok: bool, complete: bool) -> str:
    if not complete:
        return "indeterminate"
    if raw_ok and identity_ok:
        return "intact"
    if identity_ok:
        return "cosmetic"
    return "electrical" if not raw_ok else "normalization_changed"


def check_sim_report(report_path: str, *, root: str | None = None) -> dict[str, Any]:
    """``klt sim --check``: verify a committed report's netlist closure and
    identity against the working tree. Raises :class:`SimError` for a
    missing/unparseable report."""
    report = load_committed_report(report_path, SimError)
    env = get_path(report, ("environment",))
    env = env if isinstance(env, dict) else {}
    closure = env.get("netlist_closure")
    checks: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []

    if not isinstance(closure, list) or not closure:
        check = hash_check("environment.netlist_closure", None, None)
        check["error"] = env.get("netlist_closure_error") or (
            "report records no netlist_closure (predates #2799); cannot verify"
        )
        checks.append(check)
        return {
            **build_check_result(
                report_path=report_path, checks=checks, skipped=skipped
            ),
            "diagnosis": "indeterminate",
        }

    resolved: list[str] = []
    raw_ok = True
    complete = True
    for index, entry in enumerate(closure):
        field = f"environment.netlist_closure[{index}].sha256"
        if not isinstance(entry, dict) or entry.get("sha256") is None:
            target = entry.get("target") if isinstance(entry, dict) else None
            reason = entry.get("unhashed_reason") if isinstance(entry, dict) else None
            skipped.append(
                {
                    "field": field,
                    "reason": f"include {target!r} is resolved by the executing host "
                    f"({reason}); its content was never recorded",
                }
            )
            complete = False
            continue
        path = _entry_path(entry, report_path=report_path, root=root)
        if path is None:
            skipped.append(
                {
                    "field": field,
                    "reason": f"scope {entry.get('scope')!r} has no resolvable "
                    "path here; pass --root or check where the file lives",
                }
            )
            complete = False
            continue
        actual = sha256_file(path)
        check = hash_check(field, entry["sha256"], actual)
        if actual is None:
            check["error"] = f"closure file not found: {path}"
            complete = False
        raw_ok = raw_ok and check["match"]
        checks.append(check)
        resolved.append(path)

    recorded = env.get("netlist_identity")
    actual_identity = _recomputed_identity(resolved) if complete else None
    identity_check = hash_check(
        "environment.netlist_identity", recorded, actual_identity
    )
    if env.get("netlist_identity_error"):
        identity_check["error"] = env["netlist_identity_error"]
    checks.append(identity_check)

    result = build_check_result(report_path=report_path, checks=checks, skipped=skipped)
    if skipped:
        result["status"] = "drifted"
    diagnosis = _diagnose(
        raw_ok, identity_check["match"], complete and recorded is not None
    )
    if diagnosis == "normalization_changed":
        result["note"] = (
            "every recorded file hash matches but the recomputed identity differs: "
            "the normalization changed since this report was written, not the netlist"
        )
    result["diagnosis"] = diagnosis
    return result
