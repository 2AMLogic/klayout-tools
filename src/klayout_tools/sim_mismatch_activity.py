"""Per-device-family mismatch activity: detection, ``.include`` scan, report.

Moved verbatim out of ``sim.py`` (issue #8020) so that module stays under
its per-file size ratchet. Holds the issue #355 machinery behind the
``family_mismatch`` report: the curated per-``(pdk_family, device_family)``
activity table (:data:`_MISMATCH_ACTIVITY`), device-family classification
of netlist element lines (:func:`_classify_device_family`,
:func:`_detect_device_families`), the ``.include``-closure scan that finds
them across a wrapper testbench (:func:`_mismatch_scan_closure`, issue
#2928), and :func:`_mismatch_family_report`, which assembles the per-family
rows. The runner-side glue that attaches that report to ``monte_carlo_info``
stays in ``sim.py`` (``_with_family_mismatch``).

A strict leaf: nothing here imports ``sim.py``, which re-exports the names
its own code or an outside caller still reaches so
``klayout_tools.sim.<name>`` keeps working.
"""

from __future__ import annotations

import os
import re
from typing import Any

from . import sim_staging
from .pdk_families import pdk_variant_family

#: Per-``(pdk_family, device_family)`` mismatch-activity table consulted by
#: :func:`_mismatch_family_report` (issue #355). A vendor model deck's global
#: mismatch switch/section does not necessarily enable *every* device
#: family's per-instance variation -- gf180mcu's poly-resistor subcircuits
#: carry the `(1 + mis_r * <switch>)` hook, but `mis_r`'s default is a
#: hardcoded ``0`` and the accompanying Pelgrom-style sigma formula
#: (``var_r = ... / sqrt(par*r_l*r_w)``, ``mis_r = agauss(0, var_r, 1)``)
#: ships **commented out** in the vendored deck, while MOS and BJT ship that
#: formula active under the same switch. A run that turns mismatch on gets
#: MOS/BJT variation and silently gets none for the resistor family, with
#: nothing in the request, deck, log, or (pre-#355) response distinguishing
#: that from "this family contributes negligibly". This table is the tool's
#: side of that gap: curated, documented per-family knowledge, cited to the
#: same vendored decks ``docs/cli/sim.md``'s "Known-good mismatch
#: mechanisms" section already documents. A ``(pdk_family, device_family)``
#: pair absent here -- including an unrecognised ``pdk_family`` -- is
#: reported with ``active: None`` ("not independently verified"), never
#: assumed active.
_MISMATCH_ACTIVITY: dict[tuple[str, str], dict[str, Any]] = {
    ("gf180mcu", "mosfet"): {
        "active": True,
        "note": (
            "MOS per-instance mismatch (the fets_mm include) is gated by "
            "the model-level sw_stat_mismatch switch and scales the "
            "per-device delvto/mulu0 terms -- active whenever that switch "
            "is set (see docs/cli/sim.md's 'Known-good mismatch "
            "mechanisms')."
        ),
    },
    ("gf180mcu", "bipolar"): {
        "active": True,
        "note": (
            "BJT per-instance mismatch (the bjt_mc include) is gated by "
            "the same sw_stat_mismatch switch as MOS -- active whenever "
            "that switch is set (see docs/cli/sim.md's 'Known-good "
            "mismatch mechanisms')."
        ),
    },
    ("gf180mcu", "resistor"): {
        "active": False,
        "note": (
            "The poly-resistor subcircuits' per-instance mismatch hook "
            "(mis_r, scaling body resistance via `(1 + mis_r * <switch>)`) "
            "defaults to a hardcoded 0, and the accompanying Pelgrom-style "
            "sigma formula ships commented out in the vendored deck -- "
            "resistor mismatch is structurally disabled regardless of "
            "sw_stat_mismatch. A run with mismatch enabled samples no "
            "resistor variation. See issue #355."
        ),
    },
    ("gf180mcu", "capacitor"): {
        "active": False,
        "note": (
            "The MIM-capacitor subcircuits (cap_mim_*) reference only the "
            "global sw_stat_global terms (one draw per run shared by every "
            "instance); sw_stat_mismatch is not referenced inside them, so "
            "no per-instance capacitor mismatch is sampled. See issue #2898."
        ),
    },
    ("sky130", "mosfet"): {
        "active": True,
        "note": (
            "MOS per-instance mismatch is active in any _mm-suffixed "
            "process-corner section (e.g. tt_mm) -- select that section "
            "via corners.process to sample it (see docs/cli/sim.md's "
            "'Known-good mismatch mechanisms')."
        ),
    },
    ("sky130", "bipolar"): {
        "active": True,
        "note": (
            "BJT per-instance mismatch is active in any _mm-suffixed "
            "process-corner section, validated by sky130-bandgap's "
            "pnp-mismatch simulation harness (see docs/cli/sim.md's "
            "'Known-good mismatch mechanisms')."
        ),
    },
    ("sky130", "resistor"): {
        "active": True,
        "note": (
            "Poly-resistor subcircuits (e.g. sky130_fd_pr__res_high_po_*) "
            "draw a per-instance AGAUSS term (res_match, rend) scaled by "
            "MC_MM_SWITCH, which any _mm-suffixed process-corner section "
            "(e.g. tt_mm) sets to 1 -- identical instances sample "
            "independent values (see docs/cli/sim.md's 'Known-good "
            "mismatch mechanisms'). Corner-level var_mult is a separate, "
            "shared term. See issue #3032."
        ),
    },
    # Section-dependent: the static entry is only the "section unknown"
    # fallback; `_sg13g2_mosfet_activity` refines it per selected section.
    ("sg13g2", "mosfet"): {
        "active": None,
        "note": (
            "MOS per-instance mismatch (sg13_lv_*/sg13_hv_* subcircuits) is "
            "active only in a *_mismatch process-corner section (e.g. "
            "mos_tt_mismatch); the selected section could not be determined "
            "(set corners.process). See issue #2961."
        ),
    },
}

#: SPICE element-type letter (first character of the instance name) ->
#: canonical device family, for a circuit body written with plain primitive
#: elements rather than PDK subcircuit calls. Used by
#: :func:`_classify_device_family`.
_ELEMENT_TYPE_FAMILY: dict[str, str] = {
    "R": "resistor",
    "C": "capacitor",
    "D": "diode",
    "Q": "bipolar",
    "M": "mosfet",
}

#: Case-insensitive substrings recognised in an ``X`` (subcircuit call)
#: line's own text -- covers a circuit body written against PDK subcircuit
#: calls (e.g. ``XM1 ... nfet_03v3 ...``), where the device family is
#: carried in the subcircuit name rather than the element-type letter.
#: First match wins (order matters only for a name that would otherwise
#: match more than one entry, which the PDK naming conventions this table
#: was built against do not produce).
_SUBCKT_FAMILY_KEYWORDS: tuple[tuple[str, str], ...] = (
    ("nfet", "mosfet"),
    ("pfet", "mosfet"),
    ("mosfet", "mosfet"),
    ("npn", "bipolar"),
    ("pnp", "bipolar"),
    ("bjt", "bipolar"),
    ("moscap", "capacitor"),
    ("mimcap", "capacitor"),
    ("cap_", "capacitor"),
    ("capacitor", "capacitor"),
    # sg13g2 MOS calls (``sg13_lv_nmos``/``sg13_hv_pmos``, issue #2961); after
    # the capacitor keywords so a ``..._nmoscap`` name still wins as capacitor.
    ("nmos", "mosfet"),
    ("pmos", "mosfet"),
    ("res_", "resistor"),
    ("resistor", "resistor"),
    ("diode", "diode"),
)


#: gf180mcu's own resistor subcircuit names (``ppolyf_u_1k``,
#: ``npolyf_s_1k``, ``nplus_u_*``, ``pplus_s_*``, ``rm1``..``rm3``/``tm*``
#: metal resistors) carry no ``res_`` keyword (issue #2898), so they are
#: matched against the X line's subcircuit-name token (the last token
#: before any ``name=value`` parameters) rather than the whole line.
_SUBCKT_NAME_FAMILY_RE: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(?:ppolyf|npolyf|nplus|pplus)_[a-z0-9_]+"), "resistor"),
    (re.compile(r"(?:rm|tm)\d+[a-z0-9_]*"), "resistor"),
)


def _subckt_name_token(line: str) -> str | None:
    """Subcircuit name of an ``X`` call line: the last token before the
    first ``name=value`` parameter (or the last token), lower-cased."""
    tokens = line.split()[1:]
    plain: list[str] = []
    for tok in tokens:
        if "=" in tok:
            break
        plain.append(tok)
    return plain[-1].lower() if len(plain) > 1 else None


def _classify_device_family(element_type: str, line: str) -> str | None:
    """Canonical device family for one netlist element line, or ``None``
    when ``element_type`` never names a PDK device (sources, controlled
    sources, transmission lines, ...). ``element_type`` is the instance
    name's first character, upper-cased; ``line`` is the full stripped
    element line (only consulted for ``X`` subcircuit calls, whose family
    lives in the referenced subcircuit name -- see
    :data:`_SUBCKT_FAMILY_KEYWORDS`). An ``X`` call matching no known
    keyword is classified ``"other"`` rather than dropped -- an
    unrecognised subcircuit may still be a mismatch-relevant device this
    table simply has no PDK-specific knowledge of yet."""
    if element_type in _ELEMENT_TYPE_FAMILY:
        return _ELEMENT_TYPE_FAMILY[element_type]
    if element_type == "X":
        name = _subckt_name_token(line)
        if name is not None:
            for pattern, family in _SUBCKT_NAME_FAMILY_RE:
                if pattern.fullmatch(name):
                    return family
        lowered = line.lower()
        for keyword, family in _SUBCKT_FAMILY_KEYWORDS:
            if keyword in lowered:
                return family
        return "other"
    return None


def _detect_device_families(netlist_text: str) -> list[str]:
    """Distinct device families ``netlist_text`` instantiates, in
    first-seen order. Comment (``*``/``;``), continuation (``+``), and
    dot-command (``.subckt``/``.control``/...) lines never name a device
    and are skipped, along with blank lines."""
    families: list[str] = []
    seen: set[str] = set()
    for raw_line in netlist_text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith(("*", ";", "+", ".")):
            continue
        element_type = line[0].upper()
        family = _classify_device_family(element_type, line)
        if family is not None and family not in seen:
            seen.add(family)
            families.append(family)
    return families


def _mismatch_scan_closure(
    request: dict[str, Any] | None, netlist_path: str, repo_root: str | None
) -> tuple[list[tuple[str, str]], list[dict[str, str]]]:
    """Files ``family_mismatch`` scans (issue #2928): the top-level netlist
    plus its resolved ``.include``/``.inc`` closure (same resolver as
    ``environment.netlist_closure``). Returns ``([(report_path, abs_path)],
    not_scanned)``; ``not_scanned`` lists directives left to the executing
    host (``env_var``/``pdk_root``) or the whole closure when it cannot be
    resolved -- the top-level file is still scanned in that case."""
    from . import env_provenance

    top = (
        env_provenance.repo_relative_path(netlist_path, repo_root=repo_root),
        netlist_path,
    )
    try:
        staged = sim_staging.stage_netlist(
            netlist_path,
            netlist_staged_name="netlist.cir",
            roots=sim_staging.host_resolved_roots(request or {}),
        )
    except sim_staging.IncludeStagingError as exc:
        return (
            [(_scan_label(top[0], netlist_path), netlist_path)],
            [{"target": "<include closure>", "reason": f"unresolved: {exc}"}],
        )
    files: list[tuple[str, str]] = []
    for item in staged.files:
        source = item.source_path or item.local_path
        # stage_netlist always sets one of the two (source_path whenever
        # content rewrote the file, local_path otherwise).
        assert source is not None
        info = env_provenance.repo_relative_path(source, repo_root=repo_root)
        files.append((_scan_label(info, source), source))
    not_scanned = [
        {"target": target, "reason": reason}
        for target, reason in staged.host_resolved_detail
    ]
    return files, not_scanned


def _scan_label(info: dict[str, Any], source: str = "") -> str:
    """Report label for a scanned file: repo-relative path, else
    ``<scope>/<basename>`` (never an absolute external path, like other
    report fields)."""
    return (
        info.get("path")
        or f"<{info.get('scope', 'external')}>/{os.path.basename(source)}"
    )


def _sg13g2_mosfet_activity(sections: list[str] | None) -> dict[str, Any] | None:
    """Section-dependent ``("sg13g2", "mosfet")`` activity (issue #2961), or
    ``None`` to keep the static table entry. IHP's ``cornerMOSlv/hv.lib``
    include the per-instance ``agauss()`` mismatch subcircuits only in
    ``mos_<corner>_mismatch`` sections; plain ``mos_<corner>`` and
    ``mos_tt_stat`` sections carry none."""
    if not sections:
        return None
    flags = {name.lower().endswith("_mismatch") for name in sections}
    if flags == {True}:
        return {
            "active": True,
            "note": (
                "MOS per-instance mismatch is active: every selected "
                "process section is a *_mismatch section "
                f"({', '.join(sorted(set(sections)))}), which includes the "
                "agauss() mismatch subcircuits (see docs/cli/sim.md's "
                "'Known-good mismatch mechanisms')."
            ),
        }
    if flags == {False}:
        return {
            "active": False,
            "note": (
                "No selected process section "
                f"({', '.join(sorted(set(sections)))}) is a *_mismatch "
                "section; plain mos_<corner> and mos_tt_stat sections carry "
                "no per-instance mismatch, so no MOS mismatch is sampled -- "
                "select e.g. mos_tt_mismatch via corners.process."
            ),
        }
    return None


def _mismatch_family_report(
    netlist_path: str,
    pdk_variant: str | None,
    *,
    request: dict[str, Any] | None = None,
    process_sections: list[str] | None = None,
    repo_root: str | None = None,
    scan: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Per-device-family mismatch-activity report for a ``monte_carlo.vary``
    request that includes ``"mismatch"`` (issue #355).

    For every distinct device family :func:`_detect_device_families` finds
    in the netlist, states whether that family's per-instance mismatch is
    structurally active in the selected PDK's model deck, per
    :data:`_MISMATCH_ACTIVITY` -- so a hard-zero-mismatch family (e.g.
    gf180mcu's poly resistor) is never left indistinguishable from a family
    that actually got sampled just because the request's global mismatch
    switch/section was engaged. A family with no curated table entry --
    including when ``pdk_variant`` is absent/unrecognised -- gets
    ``active: None`` ("not independently verified"), never a guessed
    ``True``/``False``.
    """
    # Issue #2928: scan the whole `.include` closure, not just the top-level
    # file, so a wrapper testbench reports the families its included flat
    # netlist actually instantiates. When ``scan`` is passed it is filled
    # with ``{"scanned": [...], "not_scanned": [...]}`` for the caller.
    files, not_scanned = _mismatch_scan_closure(request, netlist_path, repo_root)
    families: list[str] = []
    seen_families: set[str] = set()
    for _label, path in files:
        try:
            with open(path, encoding="utf-8") as handle:
                text = handle.read()
        except (OSError, UnicodeDecodeError):
            not_scanned.append({"target": _label, "reason": "unreadable"})
            continue
        for family in _detect_device_families(text):
            if family not in seen_families:
                seen_families.add(family)
                families.append(family)
    if scan is not None:
        scan["scanned"] = [label for label, _ in files]
        scan["not_scanned"] = not_scanned

    pdk_family = pdk_variant_family(pdk_variant) if pdk_variant else None

    report: list[dict[str, Any]] = []
    for family in families:
        entry = _MISMATCH_ACTIVITY.get((pdk_family, family)) if pdk_family else None
        if (pdk_family, family) == ("sg13g2", "mosfet"):
            entry = _sg13g2_mosfet_activity(process_sections) or entry
        if entry is not None:
            report.append(
                {"family": family, "active": entry["active"], "note": entry["note"]}
            )
        else:
            if pdk_family is None:
                note = (
                    f"PDK family could not be determined for this request "
                    f"(set models.pdk) -- mismatch activity for the "
                    f"'{family}' family could not be verified; treat any "
                    f"sampled spread for it as unconfirmed."
                )
            else:
                note = (
                    f"mismatch activity for the '{family}' family is not "
                    f"independently verified for PDK family '{pdk_family}'; "
                    f"treat any sampled spread for it as unconfirmed."
                )
            report.append({"family": family, "active": None, "note": note})
    return report
