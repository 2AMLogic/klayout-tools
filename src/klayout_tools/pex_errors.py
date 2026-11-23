"""Shared exception for parasitic extraction and simulation."""


class PexError(Exception):
    """Raised when a `klt pex` run cannot be completed: a bad testbench
    request, a testbench with no `.include`/`.inc` DUT reference, testbenches
    that reference different schematic netlists, or a failure in the
    extraction/simulation steps this command drives (wrapping the original
    :class:`~klayout_tools.extract.ExtractError` /
    :class:`~klayout_tools.sim.SimError`).

    The CLI turns this into a clean stderr message + exit code 1, never a
    traceback -- matching every other `klt` verb's error contract.

    ``code`` is an optional machine-readable classification emitted as the
    error envelope's ``error.code`` (``docs/json-contract.md``); ``None``
    (every pre-#2889 error) keeps the envelope byte-identical.
    """

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        self.code = code
