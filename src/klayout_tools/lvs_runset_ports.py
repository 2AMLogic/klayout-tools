"""Strict top-port findings and the mirrored-``inductor2`` known limit for
the ``klayout-runset`` LVS engine (issue #6903).

Split into its own module because :mod:`klayout_tools.lvs_runset` sits at
its file-size ratchet ceiling (774 lines) and may not grow; this module
holds the two issue-#6903 additions:

- **Strict top-port findings, made first-class (part B).** A PDK runset's
  strict top-port check (IHP sg13g2's ``flag_missing_ports``, on by that
  runset's own default -- klt passes no ``ignore_top_ports_mismatch``
  ``deck_vars`` entry) logs ``Port mismatch 'LA,VDD' vs. 'VDD'`` style
  findings into the *cross-reference section* of the ``.lvsdb`` database,
  not into the process log (which only says ``Netlists don't match``) and
  not into any ``klayout.db`` API object: the pairs themselves still read
  ``Match``, so a verdict built from the API alone can say ``match`` while
  the wrapped runset failed. :func:`strict_port_findings` parses those
  entries out of the database text (short ``Z( ... M(E B('msg'))`` form as
  written by KLayout's LVS DSL, long ``xref( ... entry(error
  description('msg')))`` form as written by ``LayoutVsSchematic.write``),
  and :func:`augment_verdict` lists them verbatim in ``mismatches[]`` as
  error-severity :data:`CATEGORY_PORT_NAME_MISMATCH` entries -- while any
  stands, ``status`` is ``"mismatch"``. The rule preserved against #5664:
  klt never reports ``match`` where the wrapped runset, in the mode klt
  ran it, reports a failure (the #5664 downgrade lives in
  :mod:`klayout_tools.lvs_runset_findings` and turns one specific
  *unpaired* ``pin.unmatched`` shape into a warning; these findings are
  the runset's own error verdict, never a warning).

- **The mirrored-``inductor2`` terminal-order known limit, named (part C).**
  The pinned IHP runset orders an ``inductor2``'s two winding terminals by
  port-polygon x position (``GeneralNTerminalExtractor#define_and_sort_terminals``
  -> ``sort_polygons``) after joining the ``LA``/``LB``-labelled ports
  (``ind_derivations.lvs``), and compares the order
  (``DeviceCustomInd`` clears equivalent terminal ids) -- so a *mirrored*
  spiral extracts with its ``LB`` terminal first even when it is wired
  correctly, and the compare fails (or, on a symmetric minimal block, the
  comparer absorbs the reversal into a crossed net pairing that the strict
  top-port check then flags). :func:`known_limitation_mismatches` detects
  that case from positive evidence only -- the ``LA``/``LB`` label-to-net
  relation readable from the extracted net names -- and discloses it as an
  in-band ``severity: "warning"`` ``topology.known_limitation`` entry
  carrying :data:`LIMIT_MIRRORED_INDUCTOR2_TERMINAL_ORDER`, the same shape
  as issue #4819's pinned ASAP7 corpus limit. ``status`` is left exactly
  as the runset reported it; ``klt signoff`` turns the token into the
  named refusal reason ``runset_inductor_terminal_order`` instead of a
  bare ``check_failed`` (:mod:`klayout_tools.signoff_runset`).

Detection deliberately requires **both** label relations, because a
genuinely reversed reference (negative control 1) and a correctly wired
mirrored spiral (the defect) produce the same terminal-reversed compare
and only the labels tell them apart: the extracted layout device must have
its first winding terminal on an ``LB``-labelled net and its second on an
``LA``-labelled net (the mirrored extraction), *and* the paired reference
device's first terminal must sit on a net sharing a name with the
``LA``-labelled layout net (the reference follows the PDK's own
``LA`` = terminal 1 convention, so the reversal is the extraction's, not
the reference's). Measured limit (issue #6903, pinned runset, KLayout
0.30.12): ``LA``/``LB`` texts moved *off* the port pins (negative control
2) collapse the extraction entirely -- no inductor device, a plain
``coverage.no_devices_recognized`` refusal, no named limit. Crossed
labels (each text on the other port's net) are **netlist-indistinguishable**
from a mirrored spiral -- identical terminal order, identical names,
identical strict-port findings -- so they necessarily carry the same named
limit; electrically the two are the same report, and the label-ordered
normalisation proposed in #6914 resolves both identically.
"""

from __future__ import annotations

import re
from typing import Any

from .lvs_mismatch import _mismatch, _sort_key

#: Issue #6903: the runset's strict top-port check found a top-level port
#: whose layout net name disagrees with its reference net name.
CATEGORY_PORT_NAME_MISMATCH = "port.name_mismatch"

#: Issue #6903: ``topology.known_limitation`` token for the pinned IHP
#: runset's mirrored-``inductor2`` terminal-order defect. Duplicated as a
#: literal contract value on the signoff side (``signoff_runset.py``), the
#: same way ``RUNSET_ENGINE`` is.
LIMIT_MIRRORED_INDUCTOR2_TERMINAL_ORDER = "mirrored-inductor2-terminal-order"

# Short form (what KLayout's LVS DSL writes): Z( ... M(E B('msg') ...));
# long form (LayoutVsSchematic.write): xref( ... entry(error
# description('msg'))). The message grammar allows backslash-escaped
# quotes and backslashes. Same shape 2AMLogic/sg13g2-vco's
# scripts/lvsdb_summary.py reads (measured against the pinned runset's
# real databases, KLayout 0.30.12).
_MSG_RE = re.compile(
    r"(?:M|entry)\((E|W|I|error|warning|info)\b[^'()]*?"
    r"(?:B|description)\('((?:[^'\\]|\\.)*)'\)"
)
_SEVERITY = {
    "E": "error",
    "W": "warning",
    "I": "info",
    "error": "error",
    "warning": "warning",
    "info": "info",
}

# The strict-port findings' own shape: Port mismatch '<layout>' vs. '<ref>'.
_PORT_MISMATCH_RE = re.compile(r"Port mismatch '([^']*)' vs\. '([^']*)'\Z")

# IHP's runset logs this line when its strict mode is active (sg13g2.lvs:
# "Starting SG13G2 LVS Comparison in strict port mode."). Absent from the
# log -> None ("not observed"), never guessed.
_STRICT_PORT_MODE_RE = re.compile(r"Comparison in strict port mode", re.I)


def _unescape(text: str) -> str:
    return re.sub(r"\\(.)", r"\1", text)


def xref_log_entries(lvsdb_text: str) -> list[dict[str, Any]]:
    """Every log message of the ``.lvsdb`` cross-reference section:
    ``[{"severity", "message"}, ...]`` in file order.

    The section starts at the first line-initial ``Z(`` (DSL short form)
    or ``xref(`` (long form) and runs to the end of the file; the messages
    live in the circuit pairs' ``L(`` lists, which the ``klayout.db`` API
    does not expose (issue #6903 part A: this is where the strict
    top-port findings surface -- the process log carries only the summary
    ``Netlists don't match`` line). ``[]`` when there is no such section.
    """
    match = re.search(r"^(?:Z|xref)\($", lvsdb_text, re.M)
    if match is None:
        return []
    return [
        {"severity": _SEVERITY[sev], "message": _unescape(msg)}
        for sev, msg in _MSG_RE.findall(lvsdb_text[match.end() :])
    ]


def strict_port_findings(lvsdb_text: str) -> list[dict[str, Any]]:
    """The strict top-port findings among :func:`xref_log_entries`:
    ``[{"severity", "message", "layout_name", "reference_name"}, ...]``,
    verbatim. Non-port entries are not reported here (nothing else is
    known to log into this section today; an unknown producer stays
    visible through :func:`xref_log_entries`, which the runset block
    records).
    """
    out = []
    for entry in xref_log_entries(lvsdb_text):
        match = _PORT_MISMATCH_RE.search(entry["message"])
        if match is None:
            continue
        out.append(
            {
                "severity": entry["severity"],
                "message": entry["message"],
                "layout_name": match.group(1),
                "reference_name": match.group(2),
            }
        )
    return out


def strict_top_port_mode(runset_log: str) -> bool | None:
    """Whether the runset's own log says it compared in strict top-port
    mode. ``None`` when the marker is absent (another PDK's runset, or a
    log that never reached the comparison step)."""
    if _STRICT_PORT_MODE_RE.search(runset_log):
        return True
    return None


def port_mismatch_mismatches(
    findings: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """The ``mismatches[]`` entries for ``findings``: one error-severity
    :data:`CATEGORY_PORT_NAME_MISMATCH` entry per finding, its
    ``description`` the runset's own finding text verbatim and the two
    net names in ``net``."""
    return [
        _mismatch(
            CATEGORY_PORT_NAME_MISMATCH,
            f["severity"],
            f["message"],
            "both",
            net={"layout": f["layout_name"], "reference": f["reference_name"]},
        )
        for f in findings
    ]


def _components(net_name: str | None) -> set[str]:
    """A net name's comma/pipe-joined components, upper-cased (KLayout
    joins a multiply-labelled net as ``LA,VDD``). Empty for anonymous
    ``$n`` names."""
    if not net_name:
        return set()
    return {
        part.strip().upper() for part in re.split(r"[,|]", net_name) if part.strip()
    }


#: One layout-side ``inductor2`` as :func:`collect_inductor_rows` sees it:
#: plain data, so the detection rule is testable without klayout objects.
INDUCTOR_ROW_KEYS = (
    "circuit",
    "device",
    "reference_device",
    "pair_ok",
    "t1_net",
    "t2_net",
    "reference_t1_net",
)


def _winding_net(device: Any, suffix: str) -> str | None:
    """Name of the net on ``device``'s winding terminal ``_<suffix>``
    (``inductor_1``/``inductor_2``; ``_sub`` ignored). ``None`` when the
    class does not declare that terminal."""
    cls = device.device_class()
    for td in cls.terminal_definitions():
        if td.name.lower().endswith(f"_{suffix}"):
            net = device.net_for_terminal(td.id())
            return net.name if net is not None else None
    return None


def collect_inductor_rows(xref: Any) -> list[dict[str, Any]]:
    """Walk ``xref`` and return one plain-data row (see
    :data:`INDUCTOR_ROW_KEYS`) per paired circuit pair's layout-side
    ``inductor``-class device: the device labels, whether the device pair
    compared OK, the two winding-terminal net names, and the paired
    reference device's first winding-terminal net name (``None`` when the
    pair carries no reference device -- an unpaired layout device cannot
    be judged against the reference convention and is skipped by the
    detection rule)."""
    from .lvs_runset_findings import _device_label, _name, _paired_ok

    rows: list[dict[str, Any]] = []
    if xref is None:
        return rows
    for cp in xref.each_circuit_pair():
        circuit = {"layout": _name(cp.first()), "reference": _name(cp.second())}
        for dp in xref.each_device_pair(cp):
            d1, d2 = dp.first(), dp.second()
            dev = d1 if d1 is not None else d2
            if dev is None or dev.device_class().name.lower() != "inductor":
                continue
            rows.append(
                {
                    "circuit": circuit,
                    "device": _device_label(d1),
                    "reference_device": _device_label(d2),
                    "pair_ok": _paired_ok(dp),
                    "t1_net": _winding_net(d1, "1") if d1 is not None else None,
                    "t2_net": _winding_net(d1, "2") if d1 is not None else None,
                    "reference_t1_net": (
                        _winding_net(d2, "1") if d2 is not None else None
                    ),
                }
            )
    return rows


def known_limitation_mismatches(
    rows: list[dict[str, Any]],
    port_findings: list[dict[str, Any]],
    status: str,
) -> list[dict[str, Any]]:
    """The ``topology.known_limitation`` warning entries for the
    mirrored-``inductor2`` terminal-order defect, per row of
    :func:`collect_inductor_rows` (see the module docstring for the rule
    and its negative controls). ``[]`` unless ``status`` is
    ``"mismatch"``: the limit explains a failing compare, it never
    creates one."""
    if status != "mismatch":
        return []
    out: list[dict[str, Any]] = []
    for row in rows:
        t1, t2 = _components(row.get("t1_net")), _components(row.get("t2_net"))
        ref_t1 = _components(row.get("reference_t1_net"))
        mirrored = "LB" in t1 and "LA" in t2
        convention = bool(t2 & ref_t1) if ref_t1 else False
        involved = not row.get("pair_ok") or any(
            _components(f.get("layout_name")) & (t1 | t2) for f in port_findings
        )
        if not (mirrored and convention and involved):
            continue
        out.append(
            _mismatch(
                "topology.known_limitation",
                "warning",
                f"inductor '{row.get('device')}' extracts with its LB terminal "
                f"first ('{row.get('t1_net')}' on terminal 1, '"
                f"{row.get('t2_net')}' on terminal 2) while the paired "
                f"reference keeps LA first ('{row.get('reference_t1_net')}') "
                "-- the pinned IHP sg13g2 runset orders an inductor2's "
                "terminals by port x position and compares the order, so a "
                "mirrored spiral compares terminal-reversed however it is "
                "wired (docs/cli/lvs.md, 'Mirrored inductor2 terminal "
                "order'). This names the cause; it is not a pass: the "
                "as-shipped status stands.",
                "layout",
                device={
                    "layout": row.get("device"),
                    "reference": row.get("reference_device"),
                    "class": "inductor",
                },
                details={
                    "limitation": LIMIT_MIRRORED_INDUCTOR2_TERMINAL_ORDER,
                    "layout_terminal_1_net": row.get("t1_net"),
                    "layout_terminal_2_net": row.get("t2_net"),
                    "reference_terminal_1_net": row.get("reference_t1_net"),
                },
                circuit=row.get("circuit"),
            )
        )
    return out


def augment_verdict(verdict: dict[str, Any], lvsdb_path: str) -> None:
    """Fold both issue-#6903 additions into a ``read_lvsdb_verdict``
    result, in place: append the strict top-port findings as
    :data:`CATEGORY_PORT_NAME_MISMATCH` entries (``status`` becomes
    ``"mismatch"`` while any error-severity one stands) and the mirrored
    ``inductor2`` known-limitation warnings for the final status. The
    database is re-read as text here because the API consumed by the
    verdict does not expose the cross-reference's log messages."""
    from .lvs_runset import STATUS_MISMATCH

    try:
        with open(lvsdb_path, encoding="utf-8", errors="replace") as handle:
            text = handle.read()
    except OSError:
        return
    findings = strict_port_findings(text)
    verdict["mismatches"].extend(port_mismatch_mismatches(findings))
    if any(f["severity"] == "error" for f in findings):
        verdict["status"] = STATUS_MISMATCH
    owned = _read_xref(lvsdb_path)
    if owned is not None:
        verdict["mismatches"].extend(
            known_limitation_mismatches(
                collect_inductor_rows(owned[0]), findings, verdict["status"]
            )
        )
    verdict["mismatches"].sort(key=_sort_key)
    verdict["runset_xref_log"] = xref_log_entries(text)


def _read_xref(lvsdb_path: str) -> tuple[Any, Any] | None:
    """``(xref, owner)`` for ``lvsdb_path``, or ``None`` when the database
    cannot be read (a synthetic or partial text: the known-limitation
    detection then simply finds no inductor rows). The owning
    :class:`~klayout.db.LayoutVsSchematic` must stay referenced for as long
    as the cross-reference is walked -- its netlists die with it, and the
    each_* walks then raise internal errors (a KLayout binding lifetime
    quirk, not a verdict fact)."""
    import klayout.db as kdb

    try:
        l2n = kdb.LayoutVsSchematic()
        l2n.read(lvsdb_path)
        _ = l2n.netlist()
        xref = l2n.xref()
        return None if xref is None else (xref, l2n)
    except Exception:
        return None
