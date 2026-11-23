"""``hints.short_nets`` disclosure builder for ``klt lvs``.

The runner re-exports this helper. Its disclosure category is shared
through ``lvs_base``.
"""

from __future__ import annotations

from typing import Any

from .lvs_base import CATEGORY_HINTS_SHORT_NETS_APPLIED


def _short_nets_disclosure(
    merged: list[dict[str, Any]],
    dropped: list[dict[str, str]],
) -> dict[str, Any]:
    """Build the single :data:`CATEGORY_HINTS_SHORT_NETS_APPLIED` entry
    disclosing what ``hints.short_nets`` resolved (issue #2653).

    Split out of :func:`_apply_short_nets_hint` for the same
    complexity-ratchet reason as the two apply helpers above; no independent
    caller. Always ``severity: "warning"`` -- see :func:`_apply_short_nets_hint`
    for why the disclosure, not the transform, is the point.
    """
    from .lvs_mismatch import _mismatch

    description_parts = []
    if merged:
        groups_text = "; ".join(
            f"{entry['members']} -> '{entry['merged_net']}'" for entry in merged
        )
        description_parts.append(
            f"merged {len(merged)} reference net group(s) ({groups_text})"
        )
    if dropped:
        names_text = ", ".join(f"'{entry['name']}'" for entry in dropped)
        description_parts.append(
            f"dropped {len(dropped)} reference device(s) ({names_text})"
        )

    return _mismatch(
        CATEGORY_HINTS_SHORT_NETS_APPLIED,
        "warning",
        "request.hints.short_nets resolved the reference to one concrete "
        "mask-option state before comparing: "
        + " and ".join(description_parts)
        + " -- this resolution was asserted by the request, not read "
        "from the reference netlist's own device values, so this "
        "dimension of the compare is not independently verified (see "
        "docs/cli/lvs.md, 'hints.short_nets_applied')",
        "reference",
        device=None,
        net=None,
        details={
            "merged_nets": merged,
            "dropped_devices": dropped,
        },
    )
