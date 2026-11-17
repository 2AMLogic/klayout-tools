"""Per-pair mismatch findings for the ``klayout-runset`` LVS engine
(split out of :mod:`klayout_tools.lvs_runset` to keep it under its
file-size ratchet ceiling, issue #5664 -- same shape as the census split,
issue #5407).

``_circuit_findings`` walks one circuit pair of the cross-reference and
delegates to the per-kind ``_device_findings`` / ``_net_findings`` /
``_pin_findings``; the labels and the ``Match``/``MatchWithWarning`` pair
grade live here too. The verdict rules themselves (``_classify``,
``read_lvsdb_verdict``) stay in :mod:`klayout_tools.lvs_runset`, which
imports this module at module scope; this module never imports that one at
module scope (``_circuit_findings`` pulls ``CATEGORY_RUNSET_INCOMPLETE``
from it lazily, mirroring the deferred-import discipline of
``lvs_mismatch.py``).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .lvs_mismatch import _mismatch

if TYPE_CHECKING:
    pass


def _status_name(status: Any) -> str:
    """Stable name of a ``NetlistCrossReference.Status`` value."""
    return str(status)


def _name(obj: Any) -> str | None:
    if obj is None:
        return None
    name = getattr(obj, "name", None)
    if callable(name):
        name = name()
    if name:
        return str(name)
    return None


def _net_label(net: Any) -> str | None:
    if net is None:
        return None
    return _name(net) or f"${net.cluster_id}"


def _device_label(device: Any) -> str | None:
    if device is None:
        return None
    return _name(device) or str(device.id())


def _device_params(device: Any) -> dict[str, float]:
    cls = device.device_class()
    return {
        p.name: device.parameter(p.id())
        for p in cls.parameter_definitions()
        if p.is_primary
    }


def _ok_statuses() -> tuple[Any, Any]:
    import klayout.db as kdb

    x = kdb.NetlistCrossReference
    return (x.Match, x.MatchWithWarning)


def _paired_ok(pair: Any) -> bool:
    return (
        pair.first() is not None
        and pair.second() is not None
        and pair.status() in _ok_statuses()
    )


def _device_findings(xref: Any, cp: Any, cname: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for dp in xref.each_device_pair(cp):
        d1, d2 = dp.first(), dp.second()
        if _paired_ok(dp):
            continue
        dev = d1 if d1 is not None else d2
        dev_echo = {
            "layout": _device_label(d1),
            "reference": _device_label(d2),
            "class": dev.device_class().name,
        }
        if d1 is None or d2 is None:
            side = "reference" if d1 is None else "layout"
            out.append(
                _mismatch(
                    "device.unmatched",
                    "error",
                    f"{side} device '{_device_label(dev)}' (class "
                    f"'{dev.device_class().name}') has no counterpart",
                    side,
                    device=dev_echo,
                    circuit=cname,
                )
            )
        else:
            out.append(
                _mismatch(
                    "device.property",
                    "error",
                    f"devices '{_device_label(d1)}' / '{_device_label(d2)}' "
                    "were paired but do not compare equal",
                    "both",
                    device=dev_echo,
                    details={
                        "layout_parameters": _device_params(d1),
                        "reference_parameters": _device_params(d2),
                    },
                    circuit=cname,
                )
            )
    return out


def _net_findings(xref: Any, cp: Any, cname: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for np_ in xref.each_net_pair(cp):
        n1, n2 = np_.first(), np_.second()
        if _paired_ok(np_):
            continue
        side = (
            "both"
            if n1 is not None and n2 is not None
            else ("reference" if n1 is None else "layout")
        )
        what = (
            f"{side} net '{_net_label(n2 if n1 is None else n1)}' has no counterpart"
            if side != "both"
            else f"nets '{_net_label(n1)}' / '{_net_label(n2)}' were paired but "
            "their connectivity differs"
        )
        out.append(
            _mismatch(
                "net.unmatched",
                "error",
                what,
                side,
                net={"layout": _net_label(n1), "reference": _net_label(n2)},
                circuit=cname,
            )
        )
    return out


def _pin_findings(xref: Any, cp: Any, cname: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for pp in xref.each_pin_pair(cp):
        p1, p2 = pp.first(), pp.second()
        if _paired_ok(pp):
            continue
        side = (
            "both"
            if p1 is not None and p2 is not None
            else ("reference" if p1 is None else "layout")
        )
        # #5664: a pair KLayout's comparer graded Match/MatchWithWarning whose
        # missing side merely means the layout pin is unlabelled (not
        # mismatched) is disclosed as a warning, so it cannot veto the
        # runset's own PASS verdict.
        warn = side != "both" and pp.status() in _ok_statuses()
        what = f"pin '{_name(p1) or _name(p2)}' of circuit '{cname['layout']}' " + (
            "is not matched"
            if not warn
            else f"is present only on the {side} side, but the runset's "
            f"own compare graded the pin pair matched "
            f"({_status_name(pp.status())})"
        )
        out.append(
            _mismatch(
                "pin.unmatched",
                "warning" if warn else "error",
                what,
                side,
                net={"layout": _name(p1), "reference": _name(p2)},
                circuit=cname,
            )
        )
    return out


def _circuit_findings(xref: Any, cp: Any) -> tuple[list[dict[str, Any]], bool]:
    """Findings for one circuit pair and whether the runset left it
    uncompared."""
    import klayout.db as kdb

    from .lvs_runset import CATEGORY_RUNSET_INCOMPLETE

    x = kdb.NetlistCrossReference
    first, second = cp.first(), cp.second()
    cname = {"layout": _name(first), "reference": _name(second)}
    status = cp.status()
    if first is None or second is None:
        side = "layout" if second is None else "reference"
        return [
            _mismatch(
                "topology",
                "error",
                f"circuit '{cname[side]}' exists only on the {side} side",
                side,
                circuit=cname,
            )
        ], False
    if status in (x.Skipped, x.None_):
        return [
            _mismatch(
                CATEGORY_RUNSET_INCOMPLETE,
                "error",
                f"circuit '{cname['layout']}' was not compared by the runset "
                f"(status {_status_name(status)})",
                "both",
                circuit=cname,
            )
        ], True
    found = (
        _device_findings(xref, cp, cname)
        + _net_findings(xref, cp, cname)
        + _pin_findings(xref, cp, cname)
    )
    if (
        not any(f["severity"] == "error" for f in found)
        and status not in _ok_statuses()
    ):
        # The circuit's own status says no-match but no finer-grained pair
        # explains it: still a mismatch, and say so.
        found.append(
            _mismatch(
                "topology",
                "error",
                f"circuit '{cname['layout']}' does not match (status "
                f"{_status_name(status)}) with no finer-grained pair "
                "reporting why",
                "both",
                circuit=cname,
            )
        )
    return found, False
