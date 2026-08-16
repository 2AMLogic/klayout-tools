"""Shared thread-safe fake AWS CLI runner for fleet-related tests.

Extracted from the byte-identical (aside from docstring/type-hint deltas)
``_FakeAws`` classes previously duplicated in ``test_remote_fleet.py`` and
``test_digital_fleet.py`` (issue #1021).

Note: ``test_remote_launcher.py`` defines its own, genuinely different
``_FakeAws`` (single-threaded, no lock, takes a ``manifest_path`` constructor
arg) -- that one is *not* this class and is intentionally left alone.
"""

from __future__ import annotations

import threading


class FakeAws:
    """Thread-safe fake AWS CLI runner: records every call, returns a
    canned response (or raises) keyed by ``argv[:2]``. A queued list of
    responses is consumed FIFO across *all* callers (matching
    `test_remote_launcher.py`'s convention) -- guarded by a lock since
    fleet operations dispatch across threads.
    """

    def __init__(self):
        self.calls: list[list[str]] = []
        self._responses: dict[tuple[str, str], object] = {}
        self._lock = threading.Lock()

    def respond(self, verb: str, subverb: str, value):
        self._responses[(verb, subverb)] = value

    def __call__(self, args: list[str]) -> str:
        with self._lock:
            self.calls.append(args)
            key = tuple(args[:2])
            value = self._responses.get(key, "")
            if isinstance(value, Exception):
                raise value
            if isinstance(value, list):
                item = value.pop(0)
                if isinstance(item, Exception):
                    raise item
                return item
            return value
