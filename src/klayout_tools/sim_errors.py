"""``SimError`` -- split out of sim.py so sim_validate can raise it without a cycle."""

from __future__ import annotations


class SimError(Exception):
    """Raised when a corner sweep cannot even be attempted: a missing/malformed
    request file, an unresolvable netlist or model library, or an unsupported
    engine.

    The CLI turns this into a clean stderr message + exit code 1, never a
    traceback. Distinct from a *corner* reporting ``status: "error"`` inside
    an otherwise-successful response -- that is a documented outcome, not an
    exception (see this module's docstring and docs/cli/sim.md).

    ``code`` is an optional machine-readable classification (issue #2721)
    the CLI emits as the error envelope's ``error.code`` -- e.g.
    ``"batch_no_capacity"`` for a retryable batch-fleet capacity refusal.
    ``None`` (every other error today) emits no ``code`` field at all.
    """

    def __init__(self, *args: object, code: str | None = None) -> None:
        super().__init__(*args)
        self.code = code


def request_options(request: dict) -> dict:
    """The request's top-level ``options`` object, ``{}`` when omitted or null.

    Issue #7720: any other non-object raises a field-specific :class:`SimError`
    (the message PR #7608 uses for corner requests), so a falsey ``false``/
    ``[]``/``0`` is not run as the defaults and a truthy scalar never reaches a
    later ``.get()`` as an uncaught ``AttributeError``.
    """
    options = request.get("options")
    if options is None:
        return {}
    if not isinstance(options, dict):
        kind = type(options).__name__
        raise SimError(f"request.options must be an object (got {kind})")
    return options
