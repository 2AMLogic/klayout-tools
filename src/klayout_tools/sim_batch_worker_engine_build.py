"""Worker-observed simulator identity out of ``environment.remote`` (issues
#3110, #5566): ``sim.apply_engine_host``'s two readers, kept together; split
out of :mod:`klayout_tools.sim_batch_features` (size ratchet). Also home to
``_compare_engine_builds``, the per-worker banner/digest comparison behind
``worker_environment.engine_build_comparison`` (#3095, #7959), moved here from
:mod:`klayout_tools.sim_batch` for the same reason."""

from __future__ import annotations

from typing import Any

from ._digest import normalize_sha256


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


def _worker_banner(
    simulators: list[dict[str, Any] | None],
) -> tuple[str | None, list[str]]:
    """``(agreed_banner, explaining_probe_codes)``: the ``version_banner``
    every member reported (same agreement rule as
    :func:`worker_ngspice_binary`), plus probe codes when it is missing."""
    banners: set[str | None] = set()
    probes: set[str] = set()
    for simulator in simulators:
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
    the digest is available when every member captured the same binary hash.
    Older captures can still provide a banner. With no usable worker
    banner it is ``None`` and ``probe``/``reason`` say why. Keyed on
    ``engine_build``'s presence (a deck-list report gains nothing) and value
    (``engine: "xyce"`` keeps a null build)."""
    if "engine_build" not in environment:
        return
    client_build = environment["engine_build"]
    environment["client_engine_build"] = client_build
    if client_build is None:
        return
    simulators = _worker_simulators(environment.get("remote"))
    digests = {
        normalize_sha256(simulator.get("binary_sha256")) if simulator else None
        for simulator in simulators
    }
    banner, codes = _worker_banner(simulators)
    probe, reason = ("ok", None) if banner else _no_worker_banner(codes)
    if banner and len(digests - {None}) > 1:
        probe, reason = (
            "disagreement",
            "the sharded members reported different binary digests",
        )
    environment["engine_build"] = {
        "version_banner": banner,
        "binary_sha256": next(iter(digests)) if len(digests) == 1 else None,
        "probe": probe,
        "reason": reason,
    }


def _digest_comparison(
    worker_simulator: dict[str, Any] | None, client_build: dict[str, Any] | None
) -> dict[str, Any]:
    """``{"digest": "match" | "mismatch"}`` when both sides carry a valid
    ``binary_sha256`` (hex case ignored), else ``{}`` (issue #7959)."""
    worker = normalize_sha256((worker_simulator or {}).get("binary_sha256"))
    client = normalize_sha256((client_build or {}).get("binary_sha256"))
    if worker is None or client is None:
        return {}
    return {"digest": "match" if worker == client else "mismatch"}


def _compare_engine_builds(
    worker_simulator: dict[str, Any] | None, client_build: dict[str, Any] | None
) -> dict[str, Any]:
    """Compare the worker's ``--version`` banner with the client's
    ``environment.client_engine_build`` banner (#3095; the off-host probe key, #5566).
    ``not_comparable`` unless both sides have one; never a proven mismatch. Whitespace
    normalised; a match proves the same banner, not the same build options.

    With a valid ``binary_sha256`` on both sides the result also carries ``digest``
    (``"match"`` / ``"mismatch"``): the same binary or not, independent of the banner
    result because one banner covers many builds (#7959). Omitted -- the banner-only
    object, as before -- when either hash is absent, ``null`` or malformed."""
    digest = _digest_comparison(worker_simulator, client_build)
    worker = (worker_simulator or {}).get("version_banner")
    client = (client_build or {}).get("version_banner")
    if not isinstance(worker, str) or not isinstance(client, str):
        return {"status": "not_comparable", **digest}
    if not worker.strip() or not client.strip():
        return {"status": "not_comparable", **digest}
    if " ".join(worker.split()) == " ".join(client.split()):
        return {"status": "match", **digest}
    return {
        "status": "mismatch",
        "warning": (
            "the worker's ngspice --version banner differs from the local "
            "binary's; build options (e.g. FFT linkage) may differ, so "
            "results derived from them can differ from a local run"
        ),
        **digest,
    }


def portable_batch_report(
    report: dict[str, Any], *, enabled: bool, backend: str | None = None
) -> dict[str, Any]:
    """Omit the submitter's binary path only from an opted-in batch report."""
    env = report.get("environment") or {}
    if not enabled or "client_ngspice_binary" not in env:
        return report
    resolved = (env.get("backend") or {}).get("resolved")
    transport = (env.get("remote") or {}).get("backend")
    if (resolved or transport or backend) != "batch":
        return report
    portable_env = dict(env)
    portable_env.pop("client_ngspice_binary")
    return {**report, "environment": portable_env}
