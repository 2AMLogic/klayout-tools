"""``hints.short_nets`` disclosure builder for ``klt lvs``.

Split out of ``lvs.py`` (issue #4746) to keep that module under its per-file
size ceiling. Pure move: re-exported from ``lvs`` unchanged; ``lvs`` is
imported only inside the function so the import graph stays acyclic.
"""

from __future__ import annotations

import fnmatch
from typing import Any


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
    from .lvs import CATEGORY_HINTS_SHORT_NETS_APPLIED
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


def _short_nets_drop_devices(
    reference_circuit: Any,
    device_patterns: Any,
) -> list[dict[str, str]]:
    """Remove every ``hints.short_nets.devices`` match from
    ``reference_circuit``, returning one ``{"name", "class"}`` record per
    dropped device for the disclosure entry (issue #2653).

    Split out of :func:`~klayout_tools.lvs._apply_short_nets_hint` purely
    to keep each half of the hint's apply step under the repo's
    cyclomatic-complexity ratchet (`scripts/check_complexity_baseline.py`);
    it has no independent caller -- issue #5632 moved it here with the rest
    of the hint's machinery to hold ``lvs`` at its file-size ceiling (a pure
    move; ``lvs`` re-imports it unchanged). See that function's docstring
    for the device-name spelling (the reader-stripped, upper-cased
    ``expanded_name()`` form) and for why a pattern that matches nothing is
    an error rather than a no-op.
    """
    from .lvs import LvsError

    if not device_patterns:
        return []
    if not isinstance(device_patterns, list) or not all(
        isinstance(pattern, str) and pattern for pattern in device_patterns
    ):
        raise LvsError(
            "hints.short_nets.devices must be a non-empty list of "
            "reference device names or glob patterns"
        )
    all_devices = list(reference_circuit.each_device())
    dropped: list[dict[str, str]] = []
    for pattern in device_patterns:
        matched = [
            device
            for device in all_devices
            if fnmatch.fnmatchcase(device.expanded_name().upper(), pattern.upper())
        ]
        if not matched:
            present = sorted({device.expanded_name() for device in all_devices}) or [
                "none"
            ]
            raise LvsError(
                f"hints.short_nets.devices: pattern {pattern!r} matched "
                f"no device on reference circuit "
                f"'{reference_circuit.name}' (devices present: "
                f"{', '.join(present)})"
            )
        for device in matched:
            dropped.append(
                {
                    "name": device.expanded_name(),
                    "class": device.device_class().name,
                }
            )
            reference_circuit.remove_device(device)
    return dropped


def _short_nets_merge_nets(
    reference_circuit: Any,
    net_groups: Any,
) -> list[dict[str, Any]]:
    """Join every ``hints.short_nets.nets`` group into one net on
    ``reference_circuit``, returning one ``{"members", "merged_net"}`` record
    per group for the disclosure entry (issue #2653).

    Split out of :func:`~klayout_tools.lvs._apply_short_nets_hint` for the
    same complexity-ratchet reason as :func:`_short_nets_drop_devices`; no
    independent caller -- issue #5632 moved it here with the rest of the
    hint's machinery to hold ``lvs`` at its file-size ceiling (a pure move;
    ``lvs`` re-imports it unchanged). See that function's docstring for the
    merged-name spelling (``klt extract``'s own pipe-joined
    ``merged_net_labels`` form) and for why an unresolvable net name is an
    error.
    """
    from .extract import spice_safe_net_name
    from .lvs import LvsError

    if not net_groups:
        return []
    if not isinstance(net_groups, list):
        raise LvsError(
            "hints.short_nets.nets must be a list of reference net-name "
            "groups, each with two or more entries"
        )
    merged: list[dict[str, Any]] = []
    for group in net_groups:
        if (
            not isinstance(group, list)
            or len(group) < 2
            or not all(isinstance(name, str) and name for name in group)
        ):
            raise LvsError(
                "hints.short_nets.nets entries must be a list of two or "
                "more reference net names to merge into one net -- got "
                f"{group!r}"
            )
        nets = []
        for name in group:
            net = reference_circuit.net_by_name(name)
            if net is None:
                raise LvsError(
                    f"hints.short_nets.nets: reference net '{name}' not "
                    f"found on circuit '{reference_circuit.name}'"
                )
            nets.append(net)
        primary = nets[0]
        for other in nets[1:]:
            # Issue #2653: a group naming the same already-merged net twice
            # (e.g. a later group's own join already folded it in) is a
            # harmless no-op, not a double-join error.
            if other is not primary:
                reference_circuit.join_nets(primary, other)
        merged.append(
            {
                "members": list(group),
                "merged_net": spice_safe_net_name(primary.expanded_name()),
            }
        )
    return merged
