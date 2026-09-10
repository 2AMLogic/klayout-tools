"""Shared OpenROAD-subprocess helpers used by the ``klt`` verb modules that
drive OpenROAD as a subprocess: ``place_and_route.py``'s own place/route/CTS/
in-flow-STA stages, and ``post_route_sta.py``'s standalone post-route timing
verb (issue #1099).

``_run_openroad``, ``_count_violations``, and ``_openroad_version`` were
previously defined identically (modulo docstring length, and -- for
``_run_openroad`` -- which ``*Error`` class it raised) in both modules; they
now live here as the single source of truth, following the same precedent
as ``_paths.py`` hosting ``load_request``'s shared file-read/JSON-parse
boilerplate (issue #642).

``_run_openroad`` is the one genuine behavioral difference between the two
former copies: ``place_and_route.py`` needs to raise ``PlaceAndRouteError``
on a launch failure, ``post_route_sta.py`` needs ``PostRouteStaError``. That
is preserved here via a caller-supplied ``error_cls`` parameter, the same
pattern ``_paths.py``'s own ``_load_request_json``/``_load_spec_json`` use
for the identical problem.
"""

from __future__ import annotations

import re
import subprocess

_OPENROAD_VERSION_RE = re.compile(r"OpenROAD\s+(\S+)")


def _run_openroad(
    script_path: str, metrics_path: str, *, error_cls: type[Exception]
) -> subprocess.CompletedProcess:
    """Run ``openroad -no_init -exit -metrics <metrics_path> <script_path>``,
    capturing stdout/stderr as text. Raises ``error_cls`` if the ``openroad``
    binary itself cannot be launched (e.g. not on ``PATH``); a non-zero exit
    from a successfully-launched run is left for the caller to inspect via
    the returned ``CompletedProcess.returncode``."""
    try:
        return subprocess.run(
            ["openroad", "-no_init", "-exit", "-metrics", metrics_path, script_path],
            capture_output=True,
            text=True,
        )
    except OSError as exc:
        raise error_cls(f"could not launch openroad: {exc}") from exc


def _openroad_version() -> str | None:
    """The resolved OpenROAD build string, or ``None`` if unresolvable --
    never raises.

    ``openroad -version`` prints a **bare** version token on its own stdout
    line (e.g. ``26Q3-771-g7cfb2105c9``) -- confirmed live for issue #425's
    own worked example against a real OpenROAD build, distinct from the
    ``OpenROAD <version>`` banner a no-flag/``-no_init`` script invocation
    prints before running. The banner form is matched first (defensively,
    in case a future build changes ``-version``'s own output shape); the
    bare first-token form is the fallback that matches today's real
    behavior.
    """
    try:
        completed = subprocess.run(
            ["openroad", "-version"], capture_output=True, text=True
        )
    except OSError:
        return None
    stdout = completed.stdout.strip()
    if not stdout:
        return None
    banner_match = _OPENROAD_VERSION_RE.search(stdout)
    if banner_match:
        return banner_match.group(1)
    return stdout.split()[0]


def _count_violations(stdout: str, begin: str, end: str) -> int:
    """Count ``"(VIOLATED)"`` lines between ``begin``/``end`` markers in a
    stage's captured stdout -- OpenROAD has no ``*_metric`` proc for
    setup/hold *violation counts* (only the scalar WNS/TNS), so this is the
    documented ``report_*`` stdout-scrape fallback (contract spike section
    5's build/wrap section). Returns ``0`` when the markers aren't found
    (defensive; should not happen for a successful run) or when the report
    found no violating paths."""
    try:
        start_idx = stdout.index(begin) + len(begin)
        stop_idx = stdout.index(end, start_idx)
    except ValueError:
        return 0
    return stdout[start_idx:stop_idx].count("(VIOLATED)")
