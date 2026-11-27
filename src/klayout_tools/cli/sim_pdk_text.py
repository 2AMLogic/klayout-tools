"""Text-mode PDK lines for ``klt sim`` (issues #2564, #3946, #4758, #5449).

Split out of :mod:`klayout_tools.cli.sim_cmd` (file-size ratchet).
"""

from __future__ import annotations

from ..sim_pdk_ambiguity import text_remedy_for


def _resolved_backend(report: dict) -> str | None:
    """Which backend actually ran, per ``environment.backend.resolved``
    (issue #2977), or ``None`` for a report carrying no such block."""
    backend = (report.get("environment") or {}).get("backend")
    return backend.get("resolved") if isinstance(backend, dict) else None


def _print_pdk(report: dict) -> None:
    """Echo the resolved PDK beside `models_lib` in text output (issue #2564).

    Which PDK *build* the models actually came from used to be JSON-only
    (`provenance.pdk`), so an interactive run could not see that the resolved
    install was not the one the caller had pinned. Print it by its
    reproducible identity -- variant, `open_pdks` stamp, and the search-order
    step that won -- never the absolute root, keeping the same path-privacy
    posture `render_path_field` gives `netlist`/`models_lib` above. When more
    than one install held the variant, flag it outright: the absolute roots
    are in the accompanying stderr warning
    (`klayout_tools.pdk.ambiguity_warning`). No-op for a run that resolved no
    PDK (the usual case for a request with no process axis).
    """
    pdk_block = (report.get("provenance") or {}).get("pdk")
    if pdk_block is None:
        return
    print(
        f"pdk: {pdk_block['name']} {pdk_block['version'] or '-'} "
        f"(via {pdk_block['source']})"
    )
    ambiguous = pdk_block.get("ambiguous_sources")
    if ambiguous:
        # Issue #5449: on `batch` both pins this line used to name are
        # refused with `models_pdk_root_host_path`, so name the one that
        # works there -- $PDK_ROOT, which this client reads and the runner
        # never sees. Every other backend keeps the #2564 wording.
        print(
            f"pdk: WARNING: variant also installed under "
            f"{', '.join(ambiguous)} -- resolution is first-match-wins; "
            f"{text_remedy_for(_resolved_backend(report))}"
        )


def _print_worker_pdk(report: dict) -> None:
    """Surface the batch runner's PDK stamp comparison in text output (issue
    #3946). Which library the simulator read used to be JSON-only
    (`environment.remote.worker_environment`), so a run on a different PDK
    revision than the one the client resolved read like a run on the client's.
    No-op for a report without a worker capture (non-batch backends)."""
    remote = (report.get("environment") or {}).get("remote") or {}
    worker = remote.get("worker_environment")
    if not isinstance(worker, dict):
        return
    _print_comparison("pdk_stamp", worker.get("pdk_stamp_comparison"))
    _print_comparison("models_lib", remote.get("models_lib_comparison"))
    # Issue #5457: the identity line for a request built out of per-section
    # corner libraries, whose `models_lib:` line can only ever read
    # `not_comparable (no_models_lib)`.
    _print_comparison(
        "corner_section_libs", remote.get("corner_section_libs_comparison")
    )
    pdk = worker.get("pdk")
    if isinstance(pdk, dict):
        print(f"models_staged: {str(pdk.get('models_staged') is True).lower()}")


def _print_comparison(label: str, comparison: object) -> None:
    """One ``<label>: <status> (<reason>) [basis: <basis>]`` line, plus any
    ``warning`` on the line after -- the shape every batch provenance
    comparison on ``environment.remote`` reports (``pdk_stamp`` #3946,
    ``models_lib`` #5321, ``corner_section_libs`` #5457). No-op for a
    comparison that is absent or carries no ``status``, i.e. a non-batch
    backend or a runner that reported nothing to compare. A mismatch's
    ``names``/``differing`` list stays JSON-only; the warning text names what
    a reader needs.
    """
    if not isinstance(comparison, dict) or not comparison.get("status"):
        return
    line = f"{label}: {comparison['status']}"
    if comparison.get("reason"):
        line += f" ({comparison['reason']})"
    if comparison.get("basis"):
        line += f" [basis: {comparison['basis']}]"
    print(line)
    if comparison.get("warning"):
        print(f"warning: {comparison['warning']}")
