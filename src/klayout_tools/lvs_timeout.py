"""Deadline and heartbeat for the ``klayout`` engine's netlist compare
(issue #4165).

``NetlistComparer.compare()`` is one blocking native call: on a large flat
design it can run for tens of minutes without output, indistinguishable from
a hang, and it cannot be interrupted from Python in-process. This module
bounds it two ways, both stderr/exit-code only (JSON stdout stays the
contract):

* ``options.compare_timeout_s`` -- the compare runs in a forked child that is
  killed at the deadline; the caller reports ``status: "inconclusive"``
  (never ``match``/``mismatch``).
* a heartbeat line on stderr every :data:`HEARTBEAT_INTERVAL_S` while the
  compare phase runs.

The child only produces the boolean verdict: the compare logger holds native
KLayout objects that cannot cross a process boundary, so when the child
finishes inside the budget the parent repeats the (now known to terminate)
compare in-process to populate the logger. A timed-out run therefore costs at
most ``compare_timeout_s``; a run that finishes costs up to twice its compare
time, which is why the option is opt-in.
"""

from __future__ import annotations

import multiprocessing
import sys
import threading
import time
from collections.abc import Callable, Mapping
from typing import Any

#: Seconds between stderr heartbeat lines.
HEARTBEAT_INTERVAL_S = 30.0

#: Grace period between ``terminate()`` and ``kill()`` of a timed-out child.
_TERMINATE_GRACE_S = 5.0

COMPARE_TIMEOUT_CODE = "compare_timeout"


class CompareTimeout(Exception):
    """The compare did not finish inside ``options.compare_timeout_s``."""

    def __init__(self, timeout_s: float, elapsed_s: float) -> None:
        super().__init__(f"netlist compare exceeded {timeout_s:g}s")
        self.timeout_s = timeout_s
        self.elapsed_s = elapsed_s


def parse_compare_timeout(options: Mapping[str, Any], engine: str) -> float | None:
    """Resolve ``options.compare_timeout_s``; ``None`` when omitted.

    Must be a positive, finite JSON number (not a boolean) and is only
    meaningful for the ``klayout`` engine (netgen has ``netgen_timeout_s``).
    """
    if "compare_timeout_s" not in options or options["compare_timeout_s"] is None:
        return None
    value = options["compare_timeout_s"]
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not 0 < value < float("inf")
    ):
        raise _request_error(
            "options.compare_timeout_s must be a positive number of seconds"
        )
    if engine != "klayout":
        raise _request_error(
            "options.compare_timeout_s is only supported for engine "
            "'klayout' -- use options.netgen_timeout_s for engine 'netgen'"
        )
    return float(value)


def _request_error(message: str) -> Exception:
    from .lvs import LvsError

    return LvsError(message)


def _describe(context: Mapping[str, Any]) -> str:
    return ", ".join(f"{key}={value}" for key, value in context.items())


def _beat(started: float, context: Mapping[str, Any]) -> None:
    print(
        f"klt lvs: comparing netlists, {time.monotonic() - started:.0f}s elapsed "
        f"({_describe(context)})",
        file=sys.stderr,
        flush=True,
    )


class heartbeat:
    """Context manager: a daemon thread writing a stderr line every
    :data:`HEARTBEAT_INTERVAL_S` while the block runs. Best effort -- it
    relies on the native compare releasing the GIL; the timeout path below
    does not (the parent waits in its own process)."""

    def __init__(self, context: Mapping[str, Any]) -> None:
        self._context = context
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._started = time.monotonic()

    def _run(self) -> None:
        while not self._stop.wait(HEARTBEAT_INTERVAL_S):
            _beat(self._started, self._context)

    def __enter__(self) -> heartbeat:
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._stop.set()


def _child(conn: Any, run: Callable[[], bool]) -> None:
    try:
        conn.send(("ok", run()))
    except BaseException as exc:  # noqa: BLE001 - relayed to the parent
        conn.send(("error", f"{type(exc).__name__}: {exc}"))
    finally:
        conn.close()


def run_with_deadline(
    run: Callable[[], bool],
    timeout_s: float,
    context: Mapping[str, Any],
) -> None:
    """Run ``run`` (the blocking compare) in a forked child and wait at most
    ``timeout_s``, emitting heartbeats. Returns ``None`` if the child
    finished; raises :class:`CompareTimeout` at the deadline (child killed
    and reaped) or the library ``LvsError`` if the child failed.

    The child's verdict is discarded -- see the module docstring.
    """
    try:
        mp = multiprocessing.get_context("fork")
    except ValueError as exc:
        raise _request_error(
            "options.compare_timeout_s needs the 'fork' start method, which "
            "this platform does not provide"
        ) from exc
    parent_conn, child_conn = mp.Pipe(duplex=False)
    proc = mp.Process(target=_child, args=(child_conn, run), daemon=True)
    started = time.monotonic()
    proc.start()
    child_conn.close()
    try:
        deadline = started + timeout_s
        next_beat = started + HEARTBEAT_INTERVAL_S
        while True:
            now = time.monotonic()
            if now >= deadline:
                raise CompareTimeout(timeout_s, now - started)
            wait = min(deadline, next_beat) - now
            if parent_conn.poll(wait):
                try:
                    kind, payload = parent_conn.recv()
                except EOFError:
                    raise _request_error(
                        "the netlist compare process exited without a result; "
                        "no LVS verdict was reached"
                    ) from None
                if kind == "error":
                    raise _request_error(
                        "KLayout's netlist comparer failed internally "
                        f"({payload}); no LVS verdict was reached"
                    )
                return None
            if time.monotonic() >= next_beat:
                _beat(started, context)
                next_beat += HEARTBEAT_INTERVAL_S
    finally:
        parent_conn.close()
        if proc.is_alive():
            proc.terminate()
            proc.join(_TERMINATE_GRACE_S)
            if proc.is_alive():
                proc.kill()
        proc.join()
