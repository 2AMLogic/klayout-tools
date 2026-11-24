"""Unknown-top-level-field check shared by request-document verbs.

Lives outside ``_paths`` (per-file size ratchet, issue #2766) so both
``cli._request_document`` and ``post_route_sta`` can import it without a cycle.
"""

from __future__ import annotations

from typing import Any


def check_known_fields(
    request: dict[str, Any],
    known: tuple[str, ...],
    *,
    verb: str,
    error_cls: type[Exception],
) -> None:
    """Reject unknown top-level fields.

    A request document mirrors the command's flag surface one-for-one, so an
    unrecognised key is almost always a typo or a field from a newer build --
    either way, silently ignoring it would drop an input the caller believed
    was applied, which is precisely what the hashable-request contract exists
    to prevent.
    """
    unknown = sorted(key for key in request if key != "schema" and key not in known)
    if unknown:
        raise error_cls(
            f"unknown field(s) in `klt {verb}` request document: "
            f"{', '.join(unknown)} -- known fields: schema, "
            f"{', '.join(sorted(known))}"
        )
