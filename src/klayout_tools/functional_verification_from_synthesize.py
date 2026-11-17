"""``request.options.from_synthesize`` for ``klt functional-verification``
(issue #5684): re-simulate the netlist a ``klt synthesize`` run produced,
with the report's own ``netlist_sha256`` as a mandatory, fail-closed gate.

Also home of :func:`_resolve_sources` (moved out of the oversized
``functional_verification`` module, which re-exports it), since the
effective source list is where the verified netlist is prepended.

The hash gate is the point: ``klt synthesize`` claims a fresh directory per
run, so a stale netlist left in the build tree by an earlier run would
otherwise re-simulate cleanly -- a pass on gates nobody produced. Every
refusal here is a request error (exit 1), never ``status: "fail"``: a netlist
that failed the gate never ran.
"""

from __future__ import annotations

import os
from typing import Any

from ._paths import _resolve_request_path
from ._provenance import sha256_file
from ._report_verify import hash_check, load_committed_report, resolve_committed_path

FROM_SYNTHESIZE_KEYS = ("report", "netlist")


def _err(message: str) -> Exception:
    from .functional_verification import FunctionalVerificationError

    return FunctionalVerificationError(message)


def _request_relative(entry: str, request_dir: str, field: str = "request path") -> str:
    """The one place a request-relative path is anchored (#5673 constraint).

    ``$VAR`` / ``~`` are expanded first (issue #5678), the same convention
    ``klt lvs`` / ``drc`` / ``extract`` / ``sim`` document.
    """
    return os.path.abspath(
        _resolve_request_path(entry, request_dir, error_cls=_err, field=field)
    )


def _resolve_sources(
    sources: Any, request_dir: str, *, optional: bool = False
) -> list[str]:
    """Validate ``request.sources`` and resolve each entry to an absolute,
    readable path (relative to ``request_dir`` -- the same convention every
    other request-taking verb uses). ``optional`` (set only when
    ``options.from_synthesize`` is present) lets it be absent or empty."""
    if sources is None and not optional:
        raise _err("request is missing required field: sources")
    if optional and (sources is None or sources == []):
        return []
    if not isinstance(sources, list) or not sources:
        raise _err("request.sources must be a non-empty array of paths")
    if not all(isinstance(entry, str) and entry for entry in sources):
        raise _err("request.sources entries must be non-empty strings")

    resolved: list[str] = []
    for entry in sources:
        path = _request_relative(entry, request_dir, "request.sources entry")
        if not os.path.isfile(path):
            raise _err(f"RTL source not found: {entry}")
        try:
            with open(path, "rb"):
                pass
        except OSError as exc:
            raise _err(f"could not read RTL source '{entry}': {exc}") from exc
        resolved.append(path)
    return resolved


def has_from_synthesize(options: Any) -> bool:
    return isinstance(options, dict) and "from_synthesize" in options


def _validate_option(raw: Any) -> tuple[str, str | None]:
    if not isinstance(raw, dict):
        raise _err(
            "request.options.from_synthesize must be a JSON object "
            '{"report": <path>, "netlist": <path>|null}'
        )
    unknown = sorted(set(raw) - set(FROM_SYNTHESIZE_KEYS))
    if unknown:
        raise _err(
            f"request.options.from_synthesize has unknown key(s): {', '.join(unknown)}"
        )
    report = raw.get("report")
    if not isinstance(report, str) or not report:
        raise _err("request.options.from_synthesize.report must be a non-empty path")
    netlist = raw.get("netlist")
    if netlist is not None and (not isinstance(netlist, str) or not netlist):
        raise _err(
            "request.options.from_synthesize.netlist must be a non-empty path or null"
        )
    return report, netlist


def resolve_from_synthesize(
    options: Any, request_dir: str, hdl_toplevel: str, envelope
) -> tuple[str, dict[str, Any]] | None:
    """Resolve and verify ``options.from_synthesize``.

    Returns ``(netlist_path, response_block)`` or ``None`` when the key is
    absent. Raises ``FunctionalVerificationError`` on any failure to verify;
    runs before any build.
    """
    if not has_from_synthesize(options):
        return None
    report_rel, netlist_rel = _validate_option(options["from_synthesize"])
    report_path = _request_relative(
        report_rel, request_dir, "options.from_synthesize.report"
    )
    report = load_committed_report(report_path, _err_cls())
    if "netlist_path" not in report:
        raise _err(
            f"options.from_synthesize.report '{report_rel}' is not a klt "
            "synthesize report (no netlist_path)"
        )
    report_top = report.get("hdl_toplevel")
    if report_top != hdl_toplevel:
        raise _err(
            f"options.from_synthesize.report synthesized hdl_toplevel "
            f"{report_top!r}, but the request's hdl_toplevel is {hdl_toplevel!r}"
        )
    override = (
        _request_relative(netlist_rel, request_dir, "options.from_synthesize.netlist")
        if netlist_rel
        else None
    )
    netlist = resolve_committed_path(
        report["netlist_path"], report_path=report_path, override=override
    )
    if netlist is None:
        raise _err(
            "options.from_synthesize.report records no resolvable netlist_path "
            "(scope 'external'/'absent'); set options.from_synthesize.netlist "
            "to the netlist file"
        )
    netlist = os.path.abspath(netlist)
    if not os.path.isfile(netlist):
        raise _err(f"options.from_synthesize netlist not found: {netlist}")
    expected = report.get("netlist_sha256")
    check = hash_check("netlist_sha256", expected, sha256_file(netlist))
    if not check["match"]:
        why = (
            "the report records no netlist_sha256"
            if expected is None
            else "the netlist is not the one that run produced"
        )
        raise _err(
            f"options.from_synthesize netlist_sha256 mismatch ({why}): "
            f"expected {check['expected']}, actual {check['actual']}"
        )
    return netlist, {
        "report": envelope(report_path),
        "netlist": envelope(netlist),
        "netlist_sha256": check["actual"],
        "hash_check": check,
    }


def _err_cls() -> type[Exception]:
    from .functional_verification import FunctionalVerificationError

    return FunctionalVerificationError


def resolve_sources_with_synthesize(
    request_doc: dict[str, Any], request_dir: str, hdl_toplevel: str, envelope
) -> tuple[list[str], dict[str, Any] | None]:
    """The effective ``sources`` (verified netlist first) and the response
    block, or ``(sources, None)`` when ``options.from_synthesize`` is unused."""
    synth = resolve_from_synthesize(
        request_doc.get("options"), request_dir, hdl_toplevel, envelope
    )
    sources = _resolve_sources(
        request_doc.get("sources"), request_dir, optional=synth is not None
    )
    if synth is None:
        return sources, None
    return [synth[0], *sources], synth[1]


def with_from_synthesize(
    report: dict[str, Any], block: dict[str, Any] | None
) -> dict[str, Any]:
    """Attach the additive ``from_synthesize`` response block (in place)."""
    if block is not None:
        report["from_synthesize"] = block
    return report
