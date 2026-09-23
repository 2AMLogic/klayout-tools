"""Drive [magic](http://opencircuitdesign.com/magic/) as an independent
cross-validation oracle for `klt drc` and `klt extract` (issue #2014,
sub-issue of #2007).

`klt drc` and `klt extract` are both backed by **one** geometry engine
(KLayout). `tests/test_lvs.py`'s netgen tier cross-validates LVS
*comparison*, but both of its comparators are fed by the same KLayout
extraction front end, so extraction and DRC themselves are still
single-implementation. magic is a genuinely separate implementation --
different codebase, different language, different geometry model, no
shared code with KLayout -- and its sky130/gf180mcu technology files are
an independent transcription of the same foundry rules. Its DRC and its
`extract` -> `ext2spice` flow are therefore a valid oracle under #2007's
"independent implementation, not a second wrapper on the same engine" bar.

`docs/design/magic-oracle.md` is the methodology document: what is matched
(geometry, units, model, check scope), what the two stacks *do* share, and
which constructs neither this module nor magic's own decks cover. Read it
before changing anything here.

## What this module is (and is not)

It is a **test-only** helper: nothing in `src/klayout_tools/` imports it,
and `klt` never shells out to magic at runtime (the "oracle, not runtime"
call `docs/design/lvs-extraction-spike.md` §1 already made for the
magic+netgen stack). magic is driven exactly the way this repo's
headless-always rule requires -- `magic -dnull -noconsole`, batch Tcl, no
GUI, no X11 (see `scripts/install-magic.sh`'s `--without-x` build).

## Requirements, and how a run is gated

Two things must resolve, and :func:`oracle_skip_reason` reports which is
missing so a test module can skip cleanly (the same real-binary gate
`tests/test_lvs.py` uses for netgen -- absence must never fail CI):

1. a `magic` binary on `$PATH`, new enough to load the PDK decks
   (`scripts/install-magic.sh` pins one; a distro `magic` is typically far
   too old -- see that script's header);
2. a magic technology file for the deck under test, resolved by
   :func:`find_magic_tech` from, in order: an explicit
   `KLT_MAGIC_TECH_<DECK>` environment override, this repo's generated
   `pdks/magic-tech/<variant>.tech` (`scripts/fetch-magic-tech.sh`), or an
   installed open_pdks PDK's own `libs.tech/magic/<variant>.tech` via
   `klt pdk find`.

## Evidence that magic actually ran

Per #2007's acceptance criterion 2, an exit code proves nothing (magic
exits 0 on plenty of no-op runs). Every helper here therefore parses
markers magic itself printed and refuses a run that did not produce them:
the loaded technology's own `tech name`/`tech version`, the loaded cell's
bounding box (which the callers compare against KLayout's own bbox for the
same file -- the "matched geometry" check), the DRC error count *and* each
error rectangle, or the extracted device/net lists. A run that reaches
`quit` without emitting the trailing `done` marker raises
:class:`MagicOracleError` rather than silently reporting "0 violations".
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from klayout_tools._provenance import sha256_file

#: `magic` binary, or ``None`` when it is not installed (the real-binary
#: gate, mirroring `tests/test_lvs.py`'s `HAVE_NETGEN`).
MAGIC_BINARY = shutil.which("magic")

#: Every line this module parses out of magic's log is prefixed with this
#: marker, so nothing is ever scraped out of magic's ordinary chatter.
MARKER = "KLTORACLE"

#: `klt` deck name -> the open_pdks PDK *variant* whose magic technology
#: file is the oracle deck for it. `klt`'s own decks are per-process
#: (`sky130`, `gf180mcu`); magic's are per-variant, and these two are the
#: variants this repo's decks target (5-metal sky130A; gf180mcuC, the
#: 5-metal / 0.9 µm-thick-top gf180mcu variant). See
#: `docs/design/magic-oracle.md`'s matched-scope table.
VARIANT_BY_DECK = {"sky130": "sky130A", "gf180mcu": "gf180mcuC"}

#: Where `scripts/fetch-magic-tech.sh` generates the decks (gitignored).
_REPO_ROOT = Path(__file__).resolve().parents[2]
_GENERATED_TECH_DIR = _REPO_ROOT / "pdks" / "magic-tech"

_TIMEOUT_S = 900

#: Phrases magic prints when a stream/cell did not actually load. magic
#: keeps going (and exits 0) after every one of them -- a `gds read` of a
#: path it cannot open is followed by `load` happily *creating an empty
#: cell* of that name, at which point `drc check` honestly reports zero
#: violations over zero geometry. Hit while developing this module, and
#: exactly the "an exit code is not evidence" failure #2007's criterion 2
#: is about, so a run whose log contains any of these fails closed.
_LOAD_FAILURE_MARKERS = (
    "Cannot open",
    "couldn't be read",
    "Creating new cell",
    "There is nothing here to extract",
)

_ERROR_LINE_RE = re.compile(
    rf"^{MARKER} drc_error (-?\d+) (-?\d+) (-?\d+) (-?\d+) :: (.*)$"
)
_DEVICE_PARAM_RE = re.compile(r"^([a-z_]+)=(-?[0-9.eE+-]+)([a-zA-Z]*)$")

#: open_pdks' magic decks declare their own minimum engine version in
#: their `version` section (`requires magic-8.3.411` for both sky130 and
#: gf180mcu at the pinned commit). An older `magic` does not degrade
#: gracefully -- it rejects the deck outright with "Malformed line for
#: keyword device" and loads nothing -- so :func:`oracle_skip_reason`
#: reads this and skips rather than letting the suite fail on a host
#: whose distro `magic` is too old (Ubuntu 24.04 ships 8.3.105).
_REQUIRES_RE = re.compile(r"^\s*requires\s+magic-([0-9]+(?:\.[0-9]+)*)", re.MULTILINE)

#: SPICE-style engineering suffixes magic may append to an `ext2spice`
#: parameter. magic emits either a bare micron-based number (sky130A) or
#: the SI-suffixed equivalent (gf180mcuC: `w=0.73u`, `ad=0.3212p`); both
#: forms carry the *same* numeric value once the suffix is dropped, since
#: a length in microns and its `u`-suffixed SI form share a mantissa, as
#: do an area in µm² and its `p`-suffixed form. Verified against real
#: output from both decks -- see `docs/design/magic-oracle.md`.
_PARAM_SUFFIXES = {"", "u", "p", "f", "n", "m", "k"}


class MagicOracleError(RuntimeError):
    """magic could not be driven to completion, or printed no usable result."""


def magic_version() -> str | None:
    """magic's own reported version (e.g. ``"8.3.683"``), or ``None``.

    Never raises -- mirrors ``_provenance._yosys_version``'s shape for the
    same "record the engine's version, don't fabricate one" reason.
    """
    if MAGIC_BINARY is None:
        return None
    try:
        completed = subprocess.run(
            [MAGIC_BINARY, "--version"], capture_output=True, text=True, timeout=30
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0:
        return None
    version = (completed.stdout or "").strip().splitlines()
    return version[0].strip() if version else None


def find_magic_tech(deck: str) -> str | None:
    """Resolve the magic technology file that is ``deck``'s oracle deck.

    Resolution order (first hit wins):

    1. ``KLT_MAGIC_TECH_<DECK>`` (e.g. ``KLT_MAGIC_TECH_SKY130``) -- an
       explicit path, for a host that keeps its decks somewhere else.
    2. ``pdks/magic-tech/<variant>.tech`` -- generated from a pinned,
       checksummed open_pdks source by ``scripts/fetch-magic-tech.sh``.
    3. An installed PDK's own ``libs.tech/magic/<variant>.tech``, resolved
       through :func:`klayout_tools.pdk.find_pdk` exactly like
       :func:`klayout_tools.pdk.netgen_setup_file` resolves netgen's setup.

    ``None`` when ``deck`` has no oracle variant, or none of the three
    resolve.
    """
    variant = VARIANT_BY_DECK.get(deck)
    if variant is None:
        return None

    override = os.environ.get(f"KLT_MAGIC_TECH_{deck.upper()}")
    if override:
        return override if os.path.isfile(override) else None

    generated = _GENERATED_TECH_DIR / f"{variant}.tech"
    if generated.is_file():
        return str(generated)

    try:
        from klayout_tools import pdk as pdk_module

        info = pdk_module.find_pdk(variant)
    except Exception:
        return None
    magic_dir = (info.get("assets") or {}).get("magic")
    if not magic_dir:
        return None
    installed = Path(magic_dir) / f"{variant}.tech"
    return str(installed) if installed.is_file() else None


def _version_tuple(version: str) -> tuple[int, ...]:
    parts: list[int] = []
    for token in version.split("."):
        if not token.isdigit():
            break
        parts.append(int(token))
    return tuple(parts)


def required_magic_version(tech_path: str) -> str | None:
    """The minimum magic version ``tech_path`` declares it needs (its
    `requires magic-<version>` line), or ``None`` when it declares none."""
    try:
        with open(tech_path, encoding="utf-8", errors="replace") as handle:
            match = _REQUIRES_RE.search(handle.read())
    except OSError:
        return None
    return match.group(1) if match else None


def oracle_skip_reason(deck: str) -> str | None:
    """A human-readable reason this oracle cannot run for ``deck``, or
    ``None`` when it can.

    Used at module level by the oracle test modules
    (``pytest.skip(..., allow_module_level=True)``). Absence of either
    half must skip, never fail -- CI installs neither by default, and
    ``.github/workflows/magic-oracle.yml`` is the opt-in job that does.
    """
    if deck not in VARIANT_BY_DECK:
        return f"no magic oracle deck is declared for klt deck {deck!r}"
    if MAGIC_BINARY is None:
        return (
            "magic is not installed on this machine -- build the pinned "
            "version with scripts/install-magic.sh and add its bin/ to $PATH "
            "(see docs/design/magic-oracle.md)"
        )
    tech_path = find_magic_tech(deck)
    if tech_path is None:
        variant = VARIANT_BY_DECK[deck]
        return (
            f"no magic technology file for {variant} -- run "
            "scripts/fetch-magic-tech.sh, install an open_pdks PDK, or set "
            f"KLT_MAGIC_TECH_{deck.upper()} (see docs/design/magic-oracle.md)"
        )
    required = required_magic_version(tech_path)
    installed = magic_version()
    if required and installed and _version_tuple(installed) < _version_tuple(required):
        return (
            f"magic {installed} is older than the {required} this deck "
            f"({tech_path}) requires -- it would reject the technology file "
            "outright; build the pinned version with scripts/install-magic.sh"
        )
    return None


@dataclass(frozen=True)
class MagicError:
    """One magic DRC error rectangle: the rule text magic itself reports,
    and the rectangle in microns (converted from magic's internal units by
    the scale magic reported for the loaded technology)."""

    rule: str
    bbox_um: tuple[float, float, float, float]


@dataclass(frozen=True)
class MagicDrcResult:
    """`drc check` over one cell: magic's own total, its per-rule error
    rectangles, and the deck/geometry identity of the run."""

    violation_count: int
    errors: tuple[MagicError, ...]
    tech_name: str
    tech_version: str
    cell_bbox_um: tuple[float, float, float, float]
    um_per_internal_unit: float
    log: str

    @property
    def rule_counts(self) -> dict[str, int]:
        """Error *rectangles* per rule text. Deliberately not compared
        one-for-one against `klt drc`'s `rule_counts`: see
        `docs/design/magic-oracle.md`, "Why violation counts are compared
        as zero/non-zero plus locations"."""
        counts: dict[str, int] = {}
        for error in self.errors:
            counts[error.rule] = counts.get(error.rule, 0) + 1
        return counts


@dataclass(frozen=True)
class MagicDevice:
    """One device line from `ext2spice`'s netlist, normalised to the same
    units `klt extract`'s JSON `devices[].params` uses (µm, µm²)."""

    model: str
    kind: str
    terminals: tuple[str, ...]
    params: dict[str, float]


@dataclass(frozen=True)
class MagicExtractResult:
    """`extract all` + `ext2spice` over one cell."""

    top: str
    ports: tuple[str, ...]
    nets: frozenset[str]
    devices: tuple[MagicDevice, ...]
    netlist_path: str
    netlist_text: str
    tech_name: str
    tech_version: str
    cell_bbox_um: tuple[float, float, float, float]
    log: str

    @property
    def device_count(self) -> int:
        return len(self.devices)

    @property
    def net_count(self) -> int:
        return len(self.nets)

    @property
    def device_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for device in self.devices:
            counts[device.kind] = counts.get(device.kind, 0) + 1
        return counts


def _run_magic(script: str, work_dir: Path, tech_path: str, cell: str) -> str:
    """Run one batch Tcl ``script`` under magic and return its combined
    output. ``work_dir`` is the process cwd, because magic writes its
    ``.ext``/``.mag`` intermediates relative to it.

    Headless and hermetic by construction: ``-dnull`` (no graphics device),
    ``-noconsole`` (no Tk console), ``-rcfile /dev/null`` (ignore any
    per-user ``.magicrc`` that would otherwise change the input style or
    search path out from under the run), ``-T <tech>`` to pin the deck.
    """
    if MAGIC_BINARY is None:  # pragma: no cover - callers gate on this
        raise MagicOracleError("magic is not installed")

    script_path = work_dir / "klt_oracle.tcl"
    script_path.write_text(script, encoding="utf-8")
    command = [
        MAGIC_BINARY,
        "-dnull",
        "-noconsole",
        "-rcfile",
        os.devnull,
        "-T",
        tech_path,
        str(script_path),
    ]
    try:
        completed = subprocess.run(
            command,
            cwd=str(work_dir),
            capture_output=True,
            text=True,
            timeout=_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired as exc:  # pragma: no cover - host-dependent
        raise MagicOracleError(
            f"magic did not finish within {_TIMEOUT_S}s: {' '.join(command)}"
        ) from exc

    log = f"{completed.stdout or ''}\n{completed.stderr or ''}"
    if completed.returncode != 0:
        raise MagicOracleError(
            f"magic exited {completed.returncode} for {' '.join(command)}\n{log}"
        )
    if f"{MARKER} done" not in log:
        # magic exits 0 after plenty of failures (an unreadable stream, a
        # cell that never loaded). The trailing marker is the only proof
        # the script ran to the end -- #2007's "evidence the relevant work
        # actually ran, not an exit code".
        raise MagicOracleError(
            f"magic exited 0 but never reached the end of the oracle script "
            f"(no '{MARKER} done' marker)\n{log}"
        )
    for failure in _LOAD_FAILURE_MARKERS:
        if failure in log:
            raise MagicOracleError(
                f"magic exited 0 but did not load the input ({failure!r} in its "
                f"log) -- any result from this run would be over empty "
                f"geometry\n{log}"
            )
    if f'Reading "{cell}"' not in log:
        raise MagicOracleError(
            f"magic never reported reading cell {cell!r} from the stream\n{log}"
        )
    return log


def _marker_value(log: str, key: str) -> str:
    prefix = f"{MARKER} {key} "
    for line in log.splitlines():
        if line.startswith(prefix):
            return line[len(prefix) :].strip()
    raise MagicOracleError(f"magic printed no '{MARKER} {key}' line\n{log}")


def _preamble(gds_path: str, cell: str) -> str:
    """The Tcl every oracle run starts with: read the *same* stream file
    `klt` was given, load the cell, and report the deck and geometry
    identity of what magic is about to work on.

    The stream path is made absolute: magic runs with the work directory
    as its cwd (it writes `.ext`/`.mag` intermediates there), so a
    caller-relative path would resolve against the wrong directory -- and
    magic's response to a stream it cannot open is to carry on and report
    a clean, empty result (see :data:`_LOAD_FAILURE_MARKERS`).
    """
    absolute = os.path.abspath(gds_path)
    if not os.path.isfile(absolute):
        raise MagicOracleError(f"no such layout stream: {gds_path}")
    return f"""
drc off
gds read {_tcl_quote(absolute)}
load {_tcl_quote(cell)} -dereference
select top cell
puts "{MARKER} tech_name [tech name]"
puts "{MARKER} tech_version [tech version]"
puts "{MARKER} scale [cif scale out]"
puts "{MARKER} bbox [box values]"
"""


def _tcl_quote(value: str) -> str:
    """Brace-quote a path/name for Tcl. Braces suppress every form of
    substitution, so a path with spaces or `$` cannot be re-interpreted;
    paths containing braces are rejected rather than mis-quoted."""
    if "{" in value or "}" in value:
        raise MagicOracleError(f"cannot safely pass {value!r} to magic (braces)")
    return "{" + value + "}"


def _identity(log: str) -> tuple[str, str, float, tuple[float, float, float, float]]:
    tech_name = _marker_value(log, "tech_name")
    tech_version = _marker_value(log, "tech_version")
    scale = float(_marker_value(log, "scale"))
    bbox_units = [int(v) for v in _marker_value(log, "bbox").split()]
    if len(bbox_units) != 4:
        raise MagicOracleError(f"magic reported a malformed cell bbox\n{log}")
    bbox_um = tuple(round(value * scale, 4) for value in bbox_units)
    return tech_name, tech_version, scale, bbox_um  # type: ignore[return-value]


def run_magic_drc(
    gds_path: str, *, deck: str, cell: str, work_dir: str | Path
) -> MagicDrcResult:
    """Run magic's DRC over ``cell`` in ``gds_path`` using ``deck``'s
    oracle technology file.

    ``drc euclidean on`` is set deliberately: KLayout's own
    ``Region.space_check``/``width_check`` measure Euclidean distance by
    default, and magic's default is the Manhattan (square) metric, so
    leaving it unset would compare two different *models* of the same rule
    -- exactly the mismatched-input failure #2007's criterion 1 forbids.
    """
    tech_path = find_magic_tech(deck)
    if tech_path is None:
        raise MagicOracleError(f"no magic technology file resolved for deck {deck!r}")
    work = Path(work_dir)
    work.mkdir(parents=True, exist_ok=True)

    script = (
        _preamble(gds_path, cell)
        + f"""
drc on
drc euclidean on
drc check
drc catchup
puts "{MARKER} drc_total [drc list count total]"
foreach {{why rects}} [drc listall why] {{
    foreach rect $rects {{
        puts "{MARKER} drc_error [join $rect {{ }}] :: $why"
    }}
}}
puts "{MARKER} done"
quit -noprompt
"""
    )
    log = _run_magic(script, work, tech_path, cell)
    tech_name, tech_version, scale, bbox_um = _identity(log)

    errors: list[MagicError] = []
    for line in log.splitlines():
        match = _ERROR_LINE_RE.match(line)
        if match is None:
            continue
        llx, lly, urx, ury = (int(match.group(i)) for i in range(1, 5))
        errors.append(
            MagicError(
                rule=match.group(5).strip(),
                bbox_um=(
                    round(llx * scale, 4),
                    round(lly * scale, 4),
                    round(urx * scale, 4),
                    round(ury * scale, 4),
                ),
            )
        )

    return MagicDrcResult(
        violation_count=int(_marker_value(log, "drc_total")),
        errors=tuple(errors),
        tech_name=tech_name,
        tech_version=tech_version,
        cell_bbox_um=bbox_um,
        um_per_internal_unit=scale,
        log=log,
    )


def run_magic_extract(
    gds_path: str, *, deck: str, cell: str, work_dir: str | Path
) -> MagicExtractResult:
    """Run magic's `extract all` + `ext2spice` over ``cell`` in
    ``gds_path`` and parse the resulting netlist.

    ``ext2spice lvs`` selects magic's own LVS-oriented preset -- devices
    and connectivity only, no extracted parasitic R/C -- which is the
    scope `klt extract` produces by default too (parasitics are `klt
    extract --parasitics`, a separate surface with its own oracle row in
    #2007). Matching the scope is criterion 1 again: a netlist full of
    parasitic devices would not be comparable to `klt`'s device list.
    """
    tech_path = find_magic_tech(deck)
    if tech_path is None:
        raise MagicOracleError(f"no magic technology file resolved for deck {deck!r}")
    work = Path(work_dir)
    work.mkdir(parents=True, exist_ok=True)
    netlist_path = work / f"{cell}.magic.spice"

    script = (
        _preamble(gds_path, cell)
        + f"""
extract all
ext2spice lvs
ext2spice -o {_tcl_quote(str(netlist_path))}
puts "{MARKER} done"
quit -noprompt
"""
    )
    log = _run_magic(script, work, tech_path, cell)
    tech_name, tech_version, _scale, bbox_um = _identity(log)

    if not netlist_path.is_file():
        raise MagicOracleError(f"magic wrote no netlist at {netlist_path}\n{log}")
    netlist_text = netlist_path.read_text(encoding="utf-8")
    top, ports, devices = parse_magic_spice(netlist_text)
    if top != cell:
        raise MagicOracleError(
            f"magic extracted subcircuit {top!r}, expected {cell!r}\n{netlist_text}"
        )

    nets = set(ports)
    for device in devices:
        nets.update(device.terminals)

    return MagicExtractResult(
        top=top,
        ports=ports,
        nets=frozenset(nets),
        devices=devices,
        netlist_path=str(netlist_path),
        netlist_text=netlist_text,
        tech_name=tech_name,
        tech_version=tech_version,
        cell_bbox_um=bbox_um,
        log=log,
    )


def parse_magic_spice(
    text: str,
) -> tuple[str, tuple[str, ...], tuple[MagicDevice, ...]]:
    """Parse one `ext2spice` subcircuit into ``(top, ports, devices)``.

    magic writes subcircuit-instance device lines (``X0 <terminals...>
    <model> <k=v>...``) under `ext2spice lvs`, which is why the model name
    is "the last token before the first ``k=v`` token" rather than a fixed
    column.
    """
    top: str | None = None
    ports: tuple[str, ...] = ()
    devices: list[MagicDevice] = []

    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("*"):
            continue
        lowered = line.lower()
        if lowered.startswith(".subckt"):
            tokens = line.split()
            top = tokens[1]
            ports = tuple(tokens[2:])
            continue
        if lowered.startswith(".ends"):
            break
        if line[0].upper() != "X":
            continue
        tokens = line.split()
        params: dict[str, float] = {}
        first_param = len(tokens)
        for index, token in enumerate(tokens):
            if "=" in token:
                first_param = index
                break
        if first_param < 3:
            raise MagicOracleError(f"unparsable ext2spice device line {line!r}")
        for token in tokens[first_param:]:
            match = _DEVICE_PARAM_RE.match(token)
            if match is None:
                raise MagicOracleError(f"unparsable ext2spice parameter {token!r}")
            name, value, suffix = match.groups()
            if suffix not in _PARAM_SUFFIXES:
                raise MagicOracleError(
                    f"unexpected ext2spice unit suffix {suffix!r} in {token!r}"
                )
            params[name] = float(value)
        model = tokens[first_param - 1]
        terminals = tuple(tokens[1 : first_param - 1])
        devices.append(
            MagicDevice(
                model=model,
                kind=device_kind(model),
                terminals=terminals,
                params=params,
            )
        )

    if top is None:
        raise MagicOracleError(f"ext2spice wrote no .subckt line\n{text}")
    return top, ports, tuple(devices)


def device_kind(model: str) -> str:
    """The `klt`-deck device *class* magic's model name corresponds to.

    `klt`'s curated decks intentionally do not split a MOSFET by voltage
    or threshold flavour (`extract.py`'s own "class per deck" note): they
    emit `nfet`/`pfet`, where magic names the full PDK model
    (`sky130_fd_pr__nfet_01v8`, `sky130_fd_pr__pfet_01v8_hvt`,
    `nfet_05v0`, ...). This is a *declared*, documented scope difference,
    not a disagreement -- see `docs/design/magic-oracle.md`'s
    "Unsupported / deliberately unmatched" section. Anything that is
    neither an n- nor a p-FET keeps magic's own model name, so an
    unexpected device can never be silently folded into a FET count.
    """
    lowered = model.lower()
    if "nfet" in lowered or "nmos" in lowered:
        return "nfet"
    if "pfet" in lowered or "pmos" in lowered:
        return "pfet"
    return lowered


# --------------------------------------------------------------------------- #
# LVS adapter (issue #2316): magic's netlist as `klt lvs`'s *layout* side
# --------------------------------------------------------------------------- #
#
# `klt lvs`'s `request.layout` accepts a pre-extracted SPICE netlist
# (`{"netlist": ..., "top": ...}`) instead of a GDS to extract inline, which
# is the seam this adapter plugs magic into: the verdict pipeline then runs
# with an *independently extracted* layout netlist, closing the one gap
# #2007's analysis flagged (netgen agreement validates comparison; #2014
# validates extraction; neither ran an LVS **verdict** off a non-KLayout
# extraction).
#
# The adapter is a **format** translation, not a filter. magic writes its
# devices as subcircuit-instance cards naming the full PDK model
# (`X0 Y A VGND VNB sky130_fd_pr__nfet_01v8 w=0.65 l=0.15 ...`), which
# KLayout's SPICE reader reads as a call into an abstract circuit rather
# than as a MOS device; `klt`'s own writer uses the `M` card with the deck's
# device-class name (`M$1 Y A VGND vsubs nfet L=0.15U W=0.65U ...`). Every
# extracted parameter magic reported is carried across -- nothing is dropped
# to make a compare easier.


#: Per-`klt`-deck translation of magic's own **bulk** net names onto the
#: names `klt`'s extraction gives the very same nodes.
#:
#: This is the "bulk-net naming" difference `docs/design/magic-oracle.md`
#: already declares (magic names a MOSFET's bulk from its own
#: well/substrate node; `klt` synthesises `vsubs` for the substrate). It is
#: a naming convention on both sides, not a verdict, so translating it here
#: is what stops it producing a *false* mismatch -- the acceptance criterion
#: this adapter exists to satisfy. Every entry is asserted to be live at
#: translation time (see :func:`magic_net_translation`), so a deck whose
#: magic names change fails loudly instead of quietly translating nothing.
BULK_NET_ALIASES_BY_DECK: dict[str, dict[str, str]] = {
    "sky130": {"VNB": "vsubs"},
    "gf180mcu": {"SUB": "vsubs"},
}

#: Prefix for the stable names :func:`magic_net_translation` gives magic's
#: *unlabelled* nets. magic names a net it had to invent from the position
#: of the geometry it found (`a_74_47#`, `w_n86_453#`); `klt` numbers them
#: (`$5`). Neither name means anything, both move when the layout moves, and
#: an LVS comparer pairs such a net topologically -- so they are renamed to
#: something stable and collision-free rather than compared. The second half
#: of the same declared naming difference as :data:`BULK_NET_ALIASES_BY_DECK`.
GENERATED_NET_PREFIX = "magic_unnamed_"

#: Terminal order magic's `ext2spice` writes a MOSFET subcircuit call in,
#: for both PDKs' open_pdks models. Mirrors
#: `tests/test_extract_magic_oracle.py`'s `MAGIC_MOSFET_TERMINALS`, which is
#: where #2014 verified it against real output; a model declaring a
#: different port order fails loudly here (the terminals would be written
#: onto the wrong `M`-card positions and the compare would report a
#: mismatch), never silently.
MAGIC_MOSFET_TERMINAL_ORDER = ("d", "g", "s", "b")

#: `(<ext2spice parameter>, <SPICE card key>, <unit suffix>)` for every
#: parameter `klt extract`'s own SPICE writer emits, in its order. µm for a
#: length/perimeter (`U`), µm² for an area (`P`, i.e. pico-square-metres) --
#: the units `parse_magic_spice` already normalises magic's values to, and
#: the units `klt`'s own writer uses, so both sides state the same numbers.
_SPICE_MOSFET_PARAMS: tuple[tuple[str, str, str], ...] = (
    ("l", "L", "U"),
    ("w", "W", "U"),
    ("as", "AS", "P"),
    ("ad", "AD", "P"),
    ("ps", "PS", "U"),
    ("pd", "PD", "U"),
)

#: Device classes this adapter can express as a SPICE `M` card. Anything
#: else (a resistor, a capacitor, an unrecognised model :func:`device_kind`
#: passed through under magic's own name) raises rather than being dropped:
#: silently omitting a device is exactly how a broken layout reports a clean
#: match.
_MOSFET_KINDS = frozenset({"nfet", "pfet"})

#: Characters that would change how a SPICE reader tokenises a name. A
#: translated name containing any of them is refused rather than quoted --
#: no fixture needs one, and a mangled net name is indistinguishable from a
#: connectivity difference once the comparer has run.
_UNSAFE_NAME_RE = re.compile(r"[\s()=,*;'\"{}]")


def _is_magic_generated_net(name: str) -> bool:
    """Whether ``name`` is one magic invented rather than read off a label.

    magic terminates every such name with ``#`` (``a_74_47#``,
    ``w_n86_453#``); a name taken from a drawn label never carries one.
    """
    return name.endswith("#")


def magic_net_translation(
    result: MagicExtractResult,
    *,
    deck: str,
    aliases: dict[str, str] | None = None,
) -> dict[str, str]:
    """The declared magic -> `klt` net-name translation for ``result``.

    Returns a mapping covering **every** net in ``result.nets``: the bulk
    aliases for ``deck`` (or ``aliases``, when given explicitly), magic's
    unlabelled nets renamed to :data:`GENERATED_NET_PREFIX`-prefixed stable
    names, and every other name passed through unchanged.

    Fails closed, in the same spirit as the rest of this module:

    - an unknown ``deck`` with no explicit ``aliases`` raises, rather than
      translating nothing and letting a bulk-name difference read as a real
      mismatch;
    - an alias whose *source* name is absent from this netlist raises -- a
      stale alias that silently does nothing is how a declared difference
      turns back into a false verdict;
    - two distinct magic names that would translate onto one name raise,
      since merging two nets is a connectivity change, not a rename.
    """
    if aliases is None:
        if deck not in BULK_NET_ALIASES_BY_DECK:
            raise MagicOracleError(
                f"no magic bulk-net alias table is declared for klt deck "
                f"{deck!r} -- pass aliases= explicitly (see "
                "docs/design/magic-oracle.md)"
            )
        aliases = BULK_NET_ALIASES_BY_DECK[deck]

    missing = sorted(name for name in aliases if name not in result.nets)
    if missing:
        raise MagicOracleError(
            f"declared bulk-net alias(es) {missing} are not nets of magic's "
            f"own netlist for {result.top!r} (it has {sorted(result.nets)}) "
            "-- the alias table is stale for this deck/cell and would "
            "silently translate nothing"
        )

    translation: dict[str, str] = {}
    generated = 0
    for name in sorted(result.nets):
        if name in aliases:
            translation[name] = aliases[name]
        elif _is_magic_generated_net(name):
            generated += 1
            translation[name] = f"{GENERATED_NET_PREFIX}{generated}"
        else:
            translation[name] = name

    collisions = sorted(
        {
            translated
            for translated in translation.values()
            if sum(1 for value in translation.values() if value == translated) > 1
        }
    )
    if collisions:
        raise MagicOracleError(
            f"net-name translation would merge distinct magic nets onto "
            f"{collisions} -- that is a connectivity change, not a rename: "
            f"{translation}"
        )
    for original, translated in sorted(translation.items()):
        if not translated or _UNSAFE_NAME_RE.search(translated):
            raise MagicOracleError(
                f"translated net name {translated!r} (from {original!r}) is "
                "not safe to write into a SPICE netlist"
            )
    return translation


def _format_spice_value(value: float) -> str:
    """One device parameter, in the shortest form that round-trips the
    value magic reported (``0.169``, ``1``, ``2.03``)."""
    return f"{value:.10g}"


def magic_lvs_netlist_text(
    result: MagicExtractResult,
    *,
    deck: str,
    aliases: dict[str, str] | None = None,
    extra_ports: tuple[str, ...] | list[str] = (),
) -> str:
    """Render ``result`` as SPICE `klt lvs` can read as ``request.layout.netlist``.

    ``extra_ports`` promotes already-existing nets to top-level ports. The
    one case that needs it is the other half of the bulk-naming difference:
    `klt`'s extraction promotes its synthesized substrate net (``vsubs``) to
    a top-level pin, magic does not declare a port for the substrate node at
    all, and a pin-count difference is a *verdict* (``pin.unmatched``) in
    every LVS comparer. Naming a net that does not exist raises -- an
    invented port is a connectivity claim, not a rename.

    Fails closed on everything that would make a compare meaningless:
    an empty device list (a netlist with no devices matches nothing and
    would report a spurious clean verdict against an equally empty
    reference), a device this adapter cannot express as an `M` card, a
    MOSFET whose terminal count is not magic's documented four, and a
    missing parameter.
    """
    if not result.devices:
        raise MagicOracleError(
            f"magic extracted no devices from {result.top!r} -- refusing to "
            "write a device-less layout netlist for an LVS compare (it would "
            "report a verdict about nothing)\n" + result.netlist_text
        )

    translation = magic_net_translation(result, deck=deck, aliases=aliases)
    ports = [translation[port] for port in result.ports]
    for extra in extra_ports:
        if extra in ports:
            continue
        if extra not in translation.values():
            raise MagicOracleError(
                f"extra_ports names {extra!r}, which is not a net of magic's "
                f"netlist for {result.top!r} (translated nets: "
                f"{sorted(set(translation.values()))})"
            )
        ports.append(extra)

    lines = [
        f"* {result.top} as extracted by magic {magic_version()} "
        f"({result.tech_name} {result.tech_version}), rendered for klt lvs",
        f".SUBCKT {result.top} {' '.join(ports)}",
    ]
    for index, device in enumerate(result.devices):
        if device.kind not in _MOSFET_KINDS:
            raise MagicOracleError(
                f"magic device {index} of {result.top!r} is a "
                f"{device.kind!r} ({device.model!r}), which this LVS adapter "
                "cannot write as a SPICE MOSFET card -- refusing to drop it"
            )
        if len(device.terminals) != len(MAGIC_MOSFET_TERMINAL_ORDER):
            raise MagicOracleError(
                f"magic device {index} of {result.top!r} has terminals "
                f"{device.terminals}, not the "
                f"{MAGIC_MOSFET_TERMINAL_ORDER} an ext2spice MOSFET call is "
                "documented to carry"
            )
        missing_params = [
            name
            for name, _key, _suffix in _SPICE_MOSFET_PARAMS
            if name not in device.params
        ]
        if missing_params:
            raise MagicOracleError(
                f"magic device {index} of {result.top!r} is missing "
                f"parameter(s) {missing_params}: {device.params}"
            )
        terminals = " ".join(translation[net] for net in device.terminals)
        params = " ".join(
            f"{key}={_format_spice_value(device.params[name])}{suffix}"
            for name, key, suffix in _SPICE_MOSFET_PARAMS
        )
        lines.append(f"M{index} {terminals} {device.kind} {params}")
    lines.append(f".ENDS {result.top}")
    return "\n".join(lines) + "\n"


def write_magic_lvs_netlist(
    result: MagicExtractResult,
    path: str | Path,
    *,
    deck: str,
    aliases: dict[str, str] | None = None,
    extra_ports: tuple[str, ...] | list[str] = (),
) -> str:
    """:func:`magic_lvs_netlist_text`, written to ``path``; returns the path."""
    text = magic_lvs_netlist_text(
        result, deck=deck, aliases=aliases, extra_ports=extra_ports
    )
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(text, encoding="utf-8")
    return str(destination)


def oracle_provenance(
    *,
    deck: str,
    gds_path: str,
    klt_report: dict[str, Any],
    tech_version: str | None = None,
) -> dict[str, Any]:
    """The shared provenance block for one oracle comparison, per #2007's
    criterion 4 ("recorded tool/deck versions and input hashes").

    Mirrors the shape of `klt`'s own
    :func:`klayout_tools._provenance.build_provenance` block and reuses its
    hashing implementation, so the two halves are directly diffable::

        {
          "oracle": {"tool": "magic", "version": "8.3.683",
                     "deck": {"name": "sky130A", "path": "...",
                              "version": "1.0.608 {...}",
                              "content_hash": "sha256:..."}},
          "input": {"path": "...", "content_hash": "sha256:..."},
          "klt": {<the report's own provenance block, verbatim>}
        }

    The two ``content_hash`` values for the input are what make a
    disagreement meaningful: if `klt`'s own recorded
    ``provenance.input.content_hash`` matches this block's, both engines
    provably read the same bytes.
    """
    tech_path = find_magic_tech(deck)
    tech_hash = sha256_file(tech_path) if tech_path else None
    input_hash = sha256_file(gds_path)
    return {
        "oracle": {
            "tool": "magic",
            "version": magic_version(),
            "deck": {
                "name": VARIANT_BY_DECK.get(deck),
                "path": tech_path,
                "version": tech_version,
                "content_hash": f"sha256:{tech_hash}" if tech_hash else None,
            },
        },
        "input": {
            "path": gds_path,
            "content_hash": f"sha256:{input_hash}" if input_hash else None,
        },
        "klt": klt_report.get("provenance"),
    }
