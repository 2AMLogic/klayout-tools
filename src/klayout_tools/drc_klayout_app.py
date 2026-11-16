"""Standalone KLayout application resolution and version preflight for the
native-deck DRC engine (split out of :mod:`.drc_klayout_engine`, issue #4950,
to keep that module under its file-size ceiling)."""

import os
import re
import subprocess

from ._paths import resolve_tool_binary
from .drc import CODE_KLAYOUT_UNAVAILABLE, CODE_KLAYOUT_VERSION_TOO_OLD, DrcError

_KLAYOUT_EXECUTABLE = "klayout"

KLAYOUT_BINARY_ENV_VAR = "KLT_KLAYOUT_BINARY"
"""Environment override for the standalone KLayout application (issue #4950),
the same variable ``klt lvs --engine klayout-runset`` honors."""


def _resolve_klayout_executable(
    klayout_binary: str | None,
) -> tuple[str, str | None]:
    """Return ``(executable, override_path)`` for the standalone KLayout app.

    Issue #4950: ``klayout_binary`` (CLI ``--klayout-binary``), then
    ``$KLT_KLAYOUT_BINARY``, then ``klayout`` on PATH -- via the shared
    :func:`resolve_tool_binary`, so a named-but-unrunnable override raises
    :class:`DrcError` naming its source and never falls back to PATH.
    ``override_path`` is the resolved absolute path when an override was in
    force, else ``None`` (the executable is then the bare ``klayout``, so a
    no-override run is unchanged).
    """
    if not klayout_binary and not os.environ.get(KLAYOUT_BINARY_ENV_VAR):
        return _KLAYOUT_EXECUTABLE, None
    resolved = resolve_tool_binary(
        "klayout",
        {"klayout_binary": klayout_binary},
        option_key="klayout_binary",
        option_label="--klayout-binary",
        env_var=KLAYOUT_BINARY_ENV_VAR,
        request_dir=os.getcwd(),
        fallback_names=(_KLAYOUT_EXECUTABLE,),
        error_cls=DrcError,
        install_hint="Install the standalone KLayout application.",
    )
    assert resolved is not None
    return resolved, resolved


KLAYOUT_VERSION_PROBE_TIMEOUT_S = 15.0
"""Wall-clock budget for the ``klayout -v`` version probe (issue #2689)."""

_REQUESTED_VERSION_RE = re.compile(r"^\d+(?:\.\d+)*$")
_REPORTED_VERSION_RE = re.compile(r"\d+(?:\.\d+)+")


def _parse_requested_klayout_version(value: str) -> tuple[int, ...]:
    """Validate a caller-declared ``min_klayout_version`` (issue #2689): dotted
    decimal components only (``0.30.12``). Raises :class:`DrcError` otherwise."""
    text = value.strip() if isinstance(value, str) else value
    if not isinstance(text, str) or not _REQUESTED_VERSION_RE.match(text):
        raise DrcError(
            f"invalid minimum KLayout version {value!r}: expected dotted "
            "decimal components such as '0.30.12'"
        )
    return tuple(int(part) for part in text.split("."))


def _klayout_version_at_least(
    found: tuple[int, ...], required: tuple[int, ...]
) -> bool:
    """Numeric component-wise comparison (``0.30.12 > 0.30.9``); missing
    trailing components count as zero."""
    width = max(len(found), len(required))
    pad = lambda v: v + (0,) * (width - len(v))  # noqa: E731
    return pad(found) >= pad(required)


def _preflight_klayout_version(executable: str, min_version: str | None) -> None:
    """Refuse to run a deck on a standalone ``klayout`` application older than
    the caller-declared minimum (issue #2689).

    A no-op when ``min_version`` is ``None`` (existing behaviour: no
    compatibility is claimed or checked). Otherwise probes the *same*
    executable the deck will run with (``<executable> -v``, bounded by
    :data:`KLAYOUT_VERSION_PROBE_TIMEOUT_S`) and raises :class:`DrcError` for
    an invalid minimum, a missing/failed/timed-out/unparseable probe, or a
    detected version below the minimum. Never bypassed by
    ``allow_deck_errors`` or ``allow_missing_host_tools``. Passing proves only
    the version, not that an arbitrary deck is compatible.
    """
    if min_version is None:
        return
    required = _parse_requested_klayout_version(min_version)
    required_text = ".".join(str(c) for c in required)
    try:
        probe = subprocess.run(
            [executable, "-v"],
            capture_output=True,
            text=True,
            timeout=KLAYOUT_VERSION_PROBE_TIMEOUT_S,
        )
    except FileNotFoundError as exc:
        raise DrcError(
            f"cannot verify the minimum KLayout version ({required_text}): "
            f"{executable!r} binary not found on PATH. Install KLayout "
            f">= {required_text} (https://www.klayout.de/build.html) or omit "
            f"the minimum version. ({exc})",
            code=CODE_KLAYOUT_UNAVAILABLE,
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise DrcError(
            f"cannot verify the minimum KLayout version ({required_text}): "
            f"'{executable} -v' did not complete within "
            f"{KLAYOUT_VERSION_PROBE_TIMEOUT_S}s. Check the installation."
        ) from exc
    except OSError as exc:
        raise DrcError(
            f"cannot verify the minimum KLayout version ({required_text}): "
            f"could not launch '{executable} -v' ({exc}). Check that the "
            f"binary is executable and install KLayout >= {required_text}.",
            code=CODE_KLAYOUT_UNAVAILABLE,
        ) from exc
    output = ((probe.stdout or "") + "\n" + (probe.stderr or "")).strip()
    if probe.returncode != 0:
        raise DrcError(
            f"cannot verify the minimum KLayout version ({required_text}): "
            f"'{executable} -v' exited with status {probe.returncode}. "
            f"Reinstall or update KLayout. Output:\n{output}"
        )
    match = _REPORTED_VERSION_RE.search(output)
    if match is None:
        raise DrcError(
            f"cannot verify the minimum KLayout version ({required_text}): "
            f"could not parse a version from '{executable} -v' output. "
            f"Check the installation. Output:\n{output}"
        )
    found = tuple(int(part) for part in match.group(0).split("."))
    if not _klayout_version_at_least(found, required):
        raise DrcError(
            f"the standalone KLayout application on PATH is version "
            f"{match.group(0)}, but this run requires >= {required_text}. "
            f"Install or update the KLayout application to {required_text} "
            "or newer (https://www.klayout.de/build.html); the pip `klayout` "
            "package is a separate install and is not used by --engine "
            "klayout. See docs/cli/drc.md, 'Engine' -> 'klayout'.",
            code=CODE_KLAYOUT_VERSION_TOO_OLD,
        )
