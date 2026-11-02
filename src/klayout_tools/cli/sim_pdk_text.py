"""Text-mode PDK lines for ``klt sim`` (issues #2564, #3946, #4758).

Split out of :mod:`klayout_tools.cli.sim_cmd` (file-size ratchet).
"""

from __future__ import annotations


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
        print(
            f"pdk: WARNING: variant also installed under "
            f"{', '.join(ambiguous)} -- resolution is first-match-wins; "
            f"pin --pdk-root (or models.pdk_root) to choose deliberately"
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
    comparison = worker.get("pdk_stamp_comparison")
    if isinstance(comparison, dict) and comparison.get("status"):
        line = f"pdk_stamp: {comparison['status']}"
        if comparison.get("reason"):
            line += f" ({comparison['reason']})"
        if comparison.get("basis"):
            line += f" [basis: {comparison['basis']}]"
        print(line)
        if comparison.get("warning"):
            print(f"warning: {comparison['warning']}")
    pdk = worker.get("pdk")
    if isinstance(pdk, dict):
        print(f"models_staged: {str(pdk.get('models_staged') is True).lower()}")
