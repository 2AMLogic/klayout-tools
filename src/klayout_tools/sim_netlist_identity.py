"""Netlist-identity gate and report stamp for ``klt sim`` (issue #3074).

Split out of :mod:`klayout_tools.sim` (file-size ratchet, issue #6878). The
identity itself is computed by :func:`klayout_tools.sim_staging.netlist_identity`;
the two helpers here act on its ``(identity, error)`` result: one refuses to
simulate when the request's ``expect_netlist_identity`` does not match, the
other records the identity (or why it is unavailable) on the report.
"""

from __future__ import annotations

from typing import Any

from .sim_errors import SimError


def check_expected_netlist_identity(
    expect_identity: Any,
    netlist_identity: str | None,
    netlist_identity_error: str | None,
    netlist_ref: str,
) -> None:
    """Issue #3074: refuse to simulate when the request's
    ``expect_netlist_identity`` does not match the netlist's stamp."""
    if expect_identity is None:
        return
    if not isinstance(expect_identity, str) or not expect_identity:
        raise SimError("expect_netlist_identity must be a non-empty string")
    if netlist_identity is None:
        raise SimError(
            "expect_netlist_identity was given but the netlist identity "
            f"could not be computed: {netlist_identity_error}"
        )
    if expect_identity != netlist_identity:
        raise SimError(
            "netlist identity mismatch: request expects "
            f"{expect_identity} but '{netlist_ref}' has {netlist_identity} "
            "(the referenced record was produced from a different netlist)"
        )


def stamp_netlist_identity(
    environment: dict[str, Any],
    netlist_identity: str | None,
    netlist_identity_error: str | None,
) -> None:
    """Issue #3074: record the netlist identity (or why it is unavailable)."""
    environment["netlist_identity"] = netlist_identity
    if netlist_identity_error is not None:
        environment["netlist_identity_error"] = netlist_identity_error
