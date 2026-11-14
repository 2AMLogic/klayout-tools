"""``monte_carlo.mismatch_scale`` resolution (issues #3942, #5486).

Two shapes of PDK mismatch magnitude, one request field:

- **A global multiplicative switch** -- sky130's ``mc_mm_switch``, gf180mcu's
  ``sw_stat_mismatch`` (:data:`sim_mismatch_gate.MISMATCH_SWITCH`). One
  ``.param <switch>=<scale>`` card replaces the switch, whose active value is
  ``1``, so the deck's own ``AGAUSS``/``delvto`` terms scale linearly.
- **Per-sigma library ``.param``s** -- ihp-sg13g2 (issue #5486), which has no
  global switch at all. Its MOS mismatch magnitudes are plain ``.param``s
  defined by the libraries a ``mos_<corner>_mismatch`` section pulls in (e.g.
  ``sg13g2_lv_nmos_delvto_mm = 0.0039``), consumed as
  ``agauss(0, <name>/sqrt(m*l*w*1e12), ...)``. Scaling them means emitting one
  ``.param <name>=<nominal * scale>`` card per sigma, after the ``.lib`` cards
  so ngspice's last-definition-wins rule makes the request's value stick.

The names and nominals are **read from the resolved libraries at request
time**, never hardcoded here, so a PDK pin bump cannot leave a stale name or
a stale nominal behind: it either reads the new values or refuses.

Split out of ``sim.py`` so that file stays under its size ratchet.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

from . import sim_mismatch_gate
from ._spice_number import parse as _spice_literal
from .pdk_families import pdk_variant_family
from .sim_errors import SimError

#: ``diagnostics[].code`` for a selected ``*_mismatch`` section whose device
#: family the scale does not reach (issue #5486).
UNREACHED_FAMILY_CODE = "mismatch_scale_unreached_family"

#: PDK families whose mismatch magnitudes are scanned out of the model
#: libraries rather than applied through a single switch. Only the
#: ``<prefix>_*_mismatch`` section names listed here are scanned; every other
#: selected section belongs to another device family and is reported by
#: :func:`unreached_families` instead of silently scaled.
SCANNED_SECTION_PREFIX: dict[str, str] = {"sg13g2": "mos"}

#: Suffix that marks a mismatch-sigma ``.param`` in a scanned library.
SIGMA_SUFFIX = "_mm"

#: Section-name prefix -> the device family :func:`unreached_families` names
#: it by, for the families ``klt`` already classifies (``_MISMATCH_ACTIVITY``
#: in ``sim.py``). A prefix absent here is reported by its section name.
_SECTION_FAMILY: dict[str, str] = {
    "mos": "mosfet",
    "res": "resistor",
    "cap": "capacitor",
    "hbt": "bipolar",
    "dio": "diode",
}

_LIB_START_RE = re.compile(r"^\s*\.lib\s+(\S+)(?:\s+(\S+))?\s*$", re.IGNORECASE)
_LIB_END_RE = re.compile(r"^\s*\.endl\b", re.IGNORECASE)
_INCLUDE_RE = re.compile(r"^\s*\.inc(?:lude)?\s+(\S+)\s*$", re.IGNORECASE)
_PARAM_RE = re.compile(r"^\s*\.param\s+(.*)$", re.IGNORECASE)
_SUBCKT_RE = re.compile(r"^\s*\.subckt\b", re.IGNORECASE)
_SUBCKT_END_RE = re.compile(r"^\s*\.ends\b", re.IGNORECASE)
#: One ``<name> = <value>`` assignment of a ``.param`` card; a quoted or
#: braced right-hand side is consumed as one unit, so an ``agauss(...)``
#: expression that *mentions* a sigma is never rescanned as an assignment.
_ASSIGN_RE = re.compile(
    r"([A-Za-z_][A-Za-z0-9_]*)\s*=\s*('[^']*'|\"[^\"]*\"|\{[^}]*\}|[^\s=]+)"
)
_COMMENT_RE = re.compile(r"'[^']*'|\"[^\"]*\"|\{[^}]*\}|([$;])")
_MISMATCH_SECTION_RE = re.compile(r"^([a-z0-9]+)_.*_mismatch$")


@dataclass(frozen=True)
class MismatchScale:
    """A resolved ``monte_carlo.mismatch_scale``: the factor plus the deck
    ``.param`` cards that apply it.

    ``param`` is the single multiplicative switch for a switch family and
    ``None`` for a scanned family; ``cards`` is the ``(name, value)`` pairs
    actually written, one entry for a switch family.
    """

    scale: float
    param: str | None
    cards: tuple[tuple[str, float], ...]

    def seed_fields(self) -> dict[str, Any]:
        """The ``mc_seed``/``environment.monte_carlo`` keys this scale adds.

        A switch family emits exactly the pre-#5486 ``mismatch_scale`` +
        ``mismatch_param`` pair, in that insertion order, so sky130/gf180mcu
        responses stay byte-for-byte unchanged. A scanned family emits
        ``mismatch_scale`` + ``mismatch_params`` (name -> value written).
        """
        fields: dict[str, Any] = {"mismatch_scale": self.scale}
        if self.param is not None:
            fields["mismatch_param"] = self.param
        else:
            fields["mismatch_params"] = dict(self.cards)
        return fields


def seed_cards(mc_seed: dict[str, Any] | None) -> list[tuple[str, float]]:
    """The ``(param, value)`` cards one sample's ``mc_seed`` declares, in deck
    order (``[]`` when the request declared no scale)."""
    if not mc_seed or "mismatch_scale" not in mc_seed:
        return []
    if "mismatch_param" in mc_seed:
        return [(mc_seed["mismatch_param"], mc_seed["mismatch_scale"])]
    return sorted(mc_seed.get("mismatch_params", {}).items())


def deck_lines(
    mc_seed: dict[str, Any] | None, format_value: Callable[[Any], str]
) -> list[str]:
    """The ``.param`` cards this sample's deck carries for its scale."""
    return [
        f".param {name}={format_value(value)}" for name, value in seed_cards(mc_seed)
    ]


def wire_uses_scanned_cards(request: dict[str, Any]) -> bool:
    """Whether a pushed request's ``_explicit_points`` carry scanned cards
    (issue #5486) -- the batch/remote runner-feature detector. A runner that
    predates the field would drop the cards and run the sweep *unscaled*, so
    the feature is mandatory rather than warn-and-run."""
    points = request.get("_explicit_points")
    if not isinstance(points, list):
        return False
    return any(
        isinstance(point, dict)
        and isinstance(point.get("mc_seed"), dict)
        and "mismatch_params" in point["mc_seed"]
        for point in points
    )


def _validate_scale(mc_spec: dict[str, Any], field: str) -> float:
    value = mc_spec["mismatch_scale"]
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or value != value
        or value in (float("inf"), float("-inf"))
        or value < 0
    ):
        raise SimError(f"{field} must be a finite non-negative number (got {value!r})")
    return float(value)


def resolve(
    mc_spec: dict[str, Any],
    pdk: Any,
    *,
    corner_points: Iterable[Any] = (),
    models: dict[str, Any] | None = None,
    request_dir: str = "",
    resolve_lib_ref: Callable[[str, dict[str, Any], str], str] | None = None,
) -> MismatchScale | None:
    """Validate ``request.monte_carlo.mismatch_scale`` and resolve the deck
    cards that apply it, or ``None`` when the request omits the field.

    A family with a verified multiplicative switch resolves without touching
    the filesystem (its answer does not depend on the corners). A scanned
    family (:data:`SCANNED_SECTION_PREFIX`) reads the names and nominals out
    of the libraries its selected sections pull in -- so the keyword
    arguments are required for it, and anything that leaves the magnitudes
    undeterminable (no mismatch section selected, an unreadable library, no
    sigma ``.param`` found) is a :class:`SimError` here, before any corner is
    dispatched, never a silently unscaled run.
    """
    if "mismatch_scale" not in mc_spec:
        return None
    field = "request.monte_carlo.mismatch_scale"
    scale = _validate_scale(mc_spec, field)
    family = pdk_variant_family(pdk) if pdk else None
    param = sim_mismatch_gate.MISMATCH_SWITCH.get(family or "")
    if param is not None:
        return MismatchScale(scale, param, ((param, scale),))
    prefix = SCANNED_SECTION_PREFIX.get(family or "")
    if prefix is None:
        known = ", ".join(
            sorted({*sim_mismatch_gate.MISMATCH_SWITCH, *SCANNED_SECTION_PREFIX})
        )
        raise SimError(
            f"{field} is only supported for PDK families whose mismatch "
            f"magnitudes klt can resolve ({known}); models.pdk {pdk!r} "
            f"resolves to family {family!r}"
        )
    nominals = _scan_nominals(
        prefix,
        family or "",
        field,
        selected_sections(corner_points, models or {}),
        models or {},
        request_dir,
        resolve_lib_ref,
    )
    cards = tuple((name, nominal * scale) for name, nominal in sorted(nominals.items()))
    return MismatchScale(scale, None, cards)


def selected_sections(
    corner_points: Iterable[Any], models: dict[str, Any]
) -> list[tuple[str, str | None]]:
    """``(section_name, library_ref_as_declared)`` for every process section
    the run's points select, de-duplicated in first-appearance order.

    The ref is the point's own per-section library (issue #2522) when it
    declared one, else ``models.lib`` -- the same precedence
    ``_corner_lib_lines`` writes into the deck, so the scan reads exactly the
    file the deck's ``.lib`` card will name.
    """
    pairs: list[tuple[str, str | None]] = []
    default = models.get("lib")
    for point in corner_points:
        sections = getattr(point, "process_sections", None)
        if sections is None:
            process = getattr(point, "process", None)
            if process:
                pairs.append((process, default))
            continue
        libs = getattr(point, "process_section_libs", None) or [None] * len(sections)
        for index, section in enumerate(sections):
            ref = libs[index] if index < len(libs) else None
            pairs.append((section, ref or default))
    return list(dict.fromkeys(pairs))


def _scan_nominals(
    prefix: str,
    family: str,
    field: str,
    sections: list[tuple[str, str | None]],
    models: dict[str, Any],
    request_dir: str,
    resolve_lib_ref: Callable[[str, dict[str, Any], str], str] | None,
) -> dict[str, float]:
    """The sigma ``.param`` name -> nominal map of every selected
    ``<prefix>_*_mismatch`` section, or a :class:`SimError` naming what was
    missing."""
    wanted = [(name, ref) for name, ref in sections if _is_scanned(name, prefix)]
    if not wanted:
        selected = ", ".join(name for name, _ in sections) or "(none)"
        raise SimError(
            f"{field} on PDK family {family!r} scales the mismatch-sigma "
            f".params of a {prefix}_*_mismatch process section, and none is "
            f"selected (selected: {selected}); select one via corners.process "
            "(e.g. mos_tt_mismatch) or drop the field"
        )
    nominals: dict[str, float] = {}
    for name, ref in wanted:
        path = _resolve_section_lib(
            field, name, ref, models, request_dir, resolve_lib_ref
        )
        nominals.update(_section_sigma_params(field, path, name))
    if not nominals:
        read = ", ".join(sorted({name for name, _ in wanted}))
        raise SimError(
            f"{field} found no '.param <name>{SIGMA_SUFFIX} = <number>' "
            f"mismatch-sigma parameter in the libraries the selected section(s) "
            f"{read} pull in -- refusing rather than running an unscaled sweep"
        )
    return nominals


def _is_scanned(section: str, prefix: str) -> bool:
    low = section.lower()
    return low.startswith(f"{prefix}_") and low.endswith("_mismatch")


def _resolve_section_lib(
    field: str,
    section: str,
    ref: str | None,
    models: dict[str, Any],
    request_dir: str,
    resolve_lib_ref: Callable[[str, dict[str, Any], str], str] | None,
) -> str:
    if ref is None or resolve_lib_ref is None:
        raise SimError(
            f"{field} needs the model library that defines process section "
            f"{section!r} to read its mismatch-sigma parameters, and the "
            "request resolves none (set models.lib, or the section's own lib)"
        )
    path = resolve_lib_ref(ref, models, request_dir)
    if not os.path.isfile(path):
        raise SimError(
            f"{field} cannot read the model library for process section "
            f"{section!r}: {path} is not a readable file on this host"
        )
    return path


def _read(field: str, path: str) -> list[str]:
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            return handle.read().splitlines()
    except OSError as exc:
        raise SimError(f"{field} cannot read model library {path}: {exc}") from exc


def _logical_lines(raw: list[str]) -> list[str]:
    """``raw`` with SPICE ``+`` continuations folded onto their own line."""
    out: list[str] = []
    for line in raw:
        if line.lstrip().startswith("+") and out:
            out[-1] += " " + line.lstrip()[1:]
        else:
            out.append(line)
    return out


def _section_sigma_params(field: str, path: str, section: str) -> dict[str, float]:
    """Every ``.param <name>_mm = <number>`` the ``.lib <section>`` block of
    ``path`` defines, following its ``.include`` / nested ``.lib`` closure."""
    found: dict[str, float] = {}
    _walk_section(field, path, section, found, set())
    return found


def _walk_section(
    field: str, path: str, section: str, found: dict[str, float], seen: set[Any]
) -> None:
    key = (os.path.abspath(path), section.lower())
    if key in seen:
        return
    seen.add(key)
    lines = _logical_lines(_read(field, path))
    inside = False
    for line in lines:
        start = _LIB_START_RE.match(line)
        if start is not None and start.group(2) is None:
            # `.lib <section>` opens a named block of this same file.
            inside = start.group(1).lower() == section.lower()
            continue
        if _LIB_END_RE.match(line):
            inside = False
            continue
        if inside:
            _walk_line(field, path, line, found, seen)


def _walk_line(
    field: str, path: str, line: str, found: dict[str, float], seen: set[Any]
) -> None:
    """One line inside a selected section: recurse into what it pulls in, or
    record the sigma ``.param``s it defines."""
    include = _INCLUDE_RE.match(line)
    if include is not None:
        _walk_file(field, _join(path, include.group(1)), found, seen)
        return
    nested = _LIB_START_RE.match(line)
    if nested is not None and nested.group(2) is not None:
        _walk_section(field, _join(path, nested.group(1)), nested.group(2), found, seen)
        return
    found.update(_sigma_params(field, path, line))


def _walk_file(field: str, path: str, found: dict[str, float], seen: set[Any]) -> None:
    """A whole ``.include``d file: its top-level sigma ``.param``s, plus the
    files it includes in turn. ``.subckt`` bodies are skipped -- a parameter
    declared there is subcircuit-local, not the deck-level sigma."""
    key = (os.path.abspath(path), None)
    if key in seen:
        return
    seen.add(key)
    if not os.path.isfile(path):
        raise SimError(
            f"{field} cannot read model library {path} (included by the "
            "selected mismatch section) on this host"
        )
    depth = 0
    for line in _logical_lines(_read(field, path)):
        if _SUBCKT_RE.match(line):
            depth += 1
            continue
        if _SUBCKT_END_RE.match(line):
            depth = max(0, depth - 1)
            continue
        if depth:
            continue
        include = _INCLUDE_RE.match(line)
        if include is not None:
            _walk_file(field, _join(path, include.group(1)), found, seen)
            continue
        found.update(_sigma_params(field, path, line))


def _join(from_path: str, ref: str) -> str:
    ref = ref.strip().strip("'\"")
    return ref if os.path.isabs(ref) else os.path.join(os.path.dirname(from_path), ref)


def _strip_comment(body: str) -> str:
    """``body`` up to its first ngspice ``$``/``;`` comment outside a quoted
    or braced term, so a commented-out sigma is neither scaled nor refused."""
    for match in _COMMENT_RE.finditer(body):
        if match.group(1):
            return body[: match.start(1)]
    return body


def _sigma_params(field: str, path: str, line: str) -> dict[str, float]:
    """The ``<name>_mm = <number>`` assignments of one ``.param`` card.

    A sigma whose value is **not** a plain numeric literal raises rather
    than being skipped (issue #5486): skipping it would scale its siblings
    and leave it at nominal -- a partially scaled run that
    ``mismatch_scale: 0`` no longer makes a sigma=0 control, and that nothing
    downstream flags, ``mismatch_params`` listing only what was written.
    """
    match = _PARAM_RE.match(line)
    if match is None:
        return {}
    out: dict[str, float] = {}
    for name, value in _ASSIGN_RE.findall(_strip_comment(match.group(1))):
        if not name.lower().endswith(SIGMA_SUFFIX):
            continue
        number = _spice_literal(value)
        if number is None:
            raise SimError(
                f"{field} cannot scale mismatch-sigma parameter {name!r}, "
                f"defined as {value.strip()} (not a plain numeric literal) in "
                f"{path} -- refusing rather than scaling only its siblings"
            )
        out[name] = number
    return out


def attach_unreached_families(
    report: dict[str, Any],
    monte_carlo_info: dict[str, Any] | None,
    corner_points: Iterable[Any],
) -> None:
    """Append :func:`unreached_families`' warning to ``report["diagnostics"]``
    when there is one (the ``attach_process_leg_check`` pattern)."""
    diagnostic = unreached_families(monte_carlo_info, corner_points)
    if diagnostic:
        report.setdefault("diagnostics", []).append(diagnostic)


def unreached_families(
    monte_carlo_info: dict[str, Any] | None, corner_points: Iterable[Any]
) -> dict[str, Any] | None:
    """The ``mismatch_scale_unreached_family`` warning, or ``None``.

    Fires when a scanned-family run selected a ``*_mismatch`` section for a
    device family the scale does **not** reach (sg13g2's resistor, capacitor
    and HBT corner files are separate from its MOS ones), and the netlist
    actually instantiates that family -- so "the scale covered everything
    that varies" is never assumed by a caller reading a clean report.
    """
    if not monte_carlo_info or "mismatch_params" not in monte_carlo_info:
        return None
    present = {
        entry.get("family")
        for entry in monte_carlo_info.get("family_mismatch") or []
        if isinstance(entry, dict)
    }
    if not present:
        return None
    unreached: dict[str, str] = {}
    for section, _ in selected_sections(corner_points, {}):
        match = _MISMATCH_SECTION_RE.match(section.lower())
        if match is None:
            continue
        family = _SECTION_FAMILY.get(match.group(1), section.lower())
        # `mosfet` is what the scanned sections cover by construction. A
        # non-MOS sigma that happens to live in a scanned library (sg13g2
        # puts the `svaricap` geometry sigmas in `sg13g2_moshv_mismatch.lib`)
        # IS scaled, and its section is a `mos_*` one, so it never lands here.
        if family != "mosfet" and family in present:
            unreached.setdefault(family, section)
    if not unreached:
        return None
    named = ", ".join(f"{f} ({s})" for f, s in sorted(unreached.items()))
    return {
        "code": UNREACHED_FAMILY_CODE,
        "severity": "warning",
        "message": (
            "monte_carlo.mismatch_scale scaled only the mismatch-sigma "
            ".params of the selected MOS mismatch section(s); these selected "
            f"sections vary a family the scale did not reach: {named}. Their "
            "magnitudes are unscaled, so this run is not a clean control for "
            "them -- select that family's plain (non-_mismatch) section via "
            "corners.process to switch it off instead."
        ),
        "families": sorted(unreached),
        "sections": sorted(unreached.values()),
    }
