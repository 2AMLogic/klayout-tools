"""Worker-observed simulator identity out of ``environment.remote`` (issues
#3110, #5566): ``sim.apply_engine_host``'s two readers, kept together; split
out of :mod:`klayout_tools.sim_batch_features` (size ratchet)."""

from __future__ import annotations

from typing import Any


def _worker_simulators(remote: Any) -> list[dict[str, Any] | None]:
    """One entry per member of ``environment.remote``: its
    ``worker_environment.simulator`` block when ``capture`` is ``"ok"``, else
    ``None`` (one per member; ``[None]`` for a pre-#2834 runner or SSH)."""
    if not isinstance(remote, dict):
        return [None]
    members = remote.get("fleet")
    blocks = members if isinstance(members, list) and members else [remote]
    simulators: list[dict[str, Any] | None] = []
    for block in blocks:
        worker = block.get("worker_environment") if isinstance(block, dict) else None
        simulator = worker.get("simulator") if isinstance(worker, dict) else None
        ok = isinstance(worker, dict) and worker.get("capture") == "ok"
        simulators.append(simulator if ok and isinstance(simulator, dict) else None)
    return simulators


def worker_ngspice_binary(remote: Any) -> str | None:
    """The worker-observed ngspice path out of ``environment.remote`` (issue #3110):
    ``worker_environment.simulator.selected_path`` when its capture is ``"ok"``.
    A sharded run reports a value only when every member captured the same
    path; ``None`` for a pre-#2834 runner, SSH ``remote``, or disagreement."""
    paths: set[str | None] = set()
    for simulator in _worker_simulators(remote):
        path = simulator.get("selected_path") if simulator else None
        paths.add(path if isinstance(path, str) and path else None)
    return next(iter(paths)) if len(paths) == 1 else None


def _worker_banner(remote: Any) -> tuple[str | None, list[str]]:
    """``(agreed_banner, explaining_probe_codes)``: the ``version_banner``
    every member reported (same agreement rule as
    :func:`worker_ngspice_binary`), plus probe codes when it is missing."""
    banners: set[str | None] = set()
    probes: set[str] = set()
    for simulator in _worker_simulators(remote):
        raw = simulator.get("version_banner") if simulator else None
        banner = raw.strip() if isinstance(raw, str) and raw.strip() else None
        banners.add(banner)
        probe = simulator.get("version_probe") if simulator else None
        if banner is None and isinstance(probe, str) and probe:
            probes.add(probe)
    if len(banners) != 1:
        return None, ["disagreement"]
    return next(iter(banners)), sorted(probes)


def _no_worker_banner(codes: list[str]) -> tuple[str, str]:
    """The ``(probe, reason)`` pair explaining a missing worker banner."""
    if codes == ["disagreement"]:
        return "disagreement", "the sharded members did not report the same banner"
    if codes == ["ok"]:
        # A clean probe with no banner is what the client-side probe reports as "error".
        return "error", "the worker's --version probe reported 'ok' but no banner"
    if len(codes) == 1:
        return codes[0], f"the worker's --version probe reported {codes[0]!r}"
    if len(codes) > 1:
        # Several members each recorded a concrete reason for the missing banner,
        # and the reasons differ (#6260): "absent" would read as "no capture at all".
        named = ", ".join(repr(code) for code in codes)
        return (
            "error",
            f"the sharded members' --version probes failed for different "
            f"reasons: {named}",
        )
    return "absent", "the worker reported no --version banner"


def apply_worker_engine_build(environment: dict[str, Any]) -> None:
    """Issue #5566: make ``environment.engine_build`` describe the *worker* on
    an off-host report, in place, keeping the submitter's probe verbatim under
    ``client_engine_build`` -- the same relocation #3110 made for
    ``ngspice_binary``, which left this field behind.

    The banner is the one every member reported (:func:`_worker_banner`);
    ``binary_sha256`` is always ``None`` (the worker capture records no digest
    and the client's would describe the wrong machine). With no usable worker
    banner it is ``None`` and ``probe``/``reason`` say why. Keyed on
    ``engine_build``'s presence (a deck-list report gains nothing) and value
    (``engine: "xyce"`` keeps a null build)."""
    if "engine_build" not in environment:
        return
    client_build = environment["engine_build"]
    environment["client_engine_build"] = client_build
    if client_build is None:
        return
    banner, codes = _worker_banner(environment.get("remote"))
    probe, reason = ("ok", None) if banner else _no_worker_banner(codes)
    environment["engine_build"] = {
        "version_banner": banner,
        "binary_sha256": None,
        "probe": probe,
        "reason": reason,
    }
