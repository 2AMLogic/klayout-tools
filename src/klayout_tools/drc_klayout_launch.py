"""Launching a PDK-native KLayout DRC deck script (issue #5444).

Split out of :mod:`klayout_tools.drc_klayout_engine` so the verdict run
(:func:`~klayout_tools.drc_klayout_engine.run_drc_klayout_engine`) and the
``--list-rule-categories`` listing
(:func:`~klayout_tools.drc_klayout_list.list_klayout_deck_rule_categories`)
share **one** ``klayout -b -r`` invocation shape rather than two copies that
can drift -- the deck-var ordering, the ``input``/``report`` pair and the
three launch failure modes are a contract, not an implementation detail.
"""

from __future__ import annotations

import subprocess

from .drc import CODE_KLAYOUT_UNAVAILABLE, DrcError


def launch_klayout_deck(
    klayout_exe: str,
    deck_file: str,
    *,
    input_path: str,
    report_path: str,
    deck_vars: dict[str, str] | None = None,
    timeout_s: float,
) -> subprocess.CompletedProcess[str]:
    """Run ``klayout -b -r <deck_file> -rd input=... -rd report=...`` and
    return the finished process.

    ``deck_vars`` (issue #1302, CLI ``--deck-var name=value``) appends extra
    ``-rd`` script globals *after* the ``input``/``report`` pair, in
    insertion order.

    Raises :class:`DrcError` for the three ways the launch itself fails --
    a missing binary (``code="klayout_unavailable"``), any other ``OSError``
    starting it (same code), and a wall-clock overrun. Judging the *result*
    is the caller's job (see
    :func:`~klayout_tools.drc_klayout_engine._check_klayout_run`): this
    helper never inspects the exit status or the report file.
    """
    cmd = [
        klayout_exe,
        "-b",
        "-r",
        deck_file,
        "-rd",
        f"input={input_path}",
        "-rd",
        f"report={report_path}",
    ]
    for name, value in (deck_vars or {}).items():
        cmd.extend(["-rd", f"{name}={value}"])
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_s)
    except FileNotFoundError as exc:
        raise DrcError(
            "could not launch klayout: binary not found on PATH. "
            "Install KLayout (https://www.klayout.de/build.html) or "
            "omit --engine klayout to use the curated deck instead. "
            f"({exc})",
            code=CODE_KLAYOUT_UNAVAILABLE,
        ) from exc
    except OSError as exc:
        raise DrcError(
            f"could not launch klayout: {exc}. Check that a system "
            "KLayout (https://www.klayout.de/build.html) is installed "
            "and executable, or omit --engine klayout to use the "
            "curated deck instead.",
            code=CODE_KLAYOUT_UNAVAILABLE,
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise DrcError(
            f"klayout did not complete within {timeout_s}s (raise "
            "--timeout-s to allow more time)"
        ) from exc
