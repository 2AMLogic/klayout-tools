"""``"klayout-runset"`` engine for ``klt extract`` (issue #5481): run a PDK's
own KLayout LVS runset in batch and take the **extracted netlist** out of the
``.lvsdb`` it writes.

This is the ``klt extract`` sibling of ``klt lvs --engine klayout-runset``
(:func:`klayout_tools.lvs_runset.run_lvs_klayout_runset`, issue #2849), and it
drives the very same launcher: binary resolution, ``-rd`` construction,
timeout, "no database written" and non-zero-exit error text all come from
``lvs_runset`` itself, never from a copy. Those helpers are still private
names there (``_run_runset`` and friends): ``lvs_runset.py`` sits exactly on
its file-size ceiling, so this module reaches them through
``import-boundaries.json``'s ``shared_private_permissions`` -- the convention
``lvs_runset_reference`` already uses for ``netlist_normalize`` -- and they
should be promoted to public when that file is next split. Calling them
*through the module* (never ``from ... import``) is also what keeps the
existing tests that monkeypatch those names covering both engines.

**Why this engine exists.** ``klt extract``'s default engine runs a *curated*
deck (``decks/``), so a device family that deck declares no class for is simply
not extracted: IHP's ``pnpMPA`` on ``sg13cmos5l`` comes back
``device_count: 0``. ``klt lvs`` could already reach such a family by running
the PDK's own runset -- but a *verdict* is not a netlist, so there was no way
to get a simulatable extracted netlist for a block whose core devices live in
such a family. This engine closes exactly that gap.

**What it does NOT do.** No parasitics (``--parasitics`` and everything built
on it is refused by name, not silently ignored), so ``klt pex`` cannot use this
engine: joining runset devices with curated-deck parasitics would need a
geometric net correspondence between two independent, independently-numbered
extractions, which nothing in this repo provides. See ``docs/cli/extract.md``'s
"Engine" section.

How a netlist is obtained
-------------------------

1. A PDK runset is written to run a *complete* LVS flow and hard-requires a
   schematic (it reaches ``align``/``compare`` unconditionally unless its own
   ``net_only`` switch is set). This engine therefore synthesizes a trivial
   empty ``.SUBCKT <top> / .ENDS <top>`` stub purely to satisfy that
   requirement, and **discards the compare verdict entirely** -- a stub never
   matches, and matching is `klt lvs`'s job, not this verb's.

   Measured against IHP's ``sg13cmos5l.lvs`` for issue #5481 (the open question
   the issue asked to settle first): the stub does **not** cost devices. The
   runset's own log reports ``Devices to compare: layout=2 schematic=0`` after
   ``align``, its default ``SIMPLIFY`` pass keeps both, and the written
   ``.lvsdb`` carries them. Its ``net_only=true`` switch -- which skips the
   compare and would need no stub at all -- was also measured and **rejected**:
   on that runset it writes neither the ``.lvsdb`` nor a side netlist, so there
   is nothing to read back.

2. The netlist is read from the ``.lvsdb`` with
   ``kdb.LayoutVsSchematic.read()`` + ``.netlist()`` -- the same database
   ``lvs_runset`` already reads for its census and power-connectivity blocks --
   and **never** from a runset's own ``<top>_extracted.cir`` side file. That
   file's location is runset-specific (``sg13cmos5l.lvs`` derives it from the
   *layout's* directory, not from the process cwd), and depending on it would
   make this engine runset-specific too.

3. SPICE is written by ``klt``'s own writer, through the delegate below.

Device cards
------------

Every extracted device is written as an ``X`` subcircuit call onto its own
class name -- see :mod:`klayout_tools.extract_runset_spice` for why KLayout's
default ``Q``/``R`` cards do not bind against a PDK model library, and why no
measured parameter rides the call. What this engine accepts and refuses is
:mod:`klayout_tools.extract_runset_flags`; both are re-exported here.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from collections.abc import Mapping
from typing import Any

from . import env_provenance
from ._provenance import build_provenance, sha256_file

# This module is the engine's public face: `extract.py` and `cli/extract_cmd.py`
# import every one of these from here, so the two sibling modules below stay an
# internal split (file-size ratchet, `scripts/README.md`) rather than a second
# import surface callers have to know about.
from .extract_runset_flags import (
    DEFAULT_ENGINE as DEFAULT_ENGINE,
)
from .extract_runset_flags import (
    ENGINES as ENGINES,
)
from .extract_runset_flags import (
    REFUSED_FLAG_BY_PARAM as REFUSED_FLAG_BY_PARAM,
)
from .extract_runset_flags import (
    RESERVED_RUNSET_VARS as RESERVED_RUNSET_VARS,
)
from .extract_runset_flags import (
    RUNSET_ENGINE as RUNSET_ENGINE,
)
from .extract_runset_flags import (
    parse_deck_vars as parse_deck_vars,
)
from .extract_runset_flags import (
    refuse_unsupported as refuse_unsupported,
)
from .extract_runset_flags import (
    refused_flags as refused_flags,
)
from .extract_runset_flags import (
    require_deck_file as require_deck_file,
)
from .extract_runset_flags import (
    require_runset_engine_for_deck_vars as require_runset_engine_for_deck_vars,
)
from .extract_runset_flags import (
    validate_engine as validate_engine,
)
from .extract_runset_spice import (
    _dropped_parameter_warnings,
    _runset_spice_writer_delegate,
)
from .extract_spef import ExtractError

#: Default wall-clock budget for the runset, shared with ``klt lvs``.
RUNSET_DEFAULT_TIMEOUT_S = 600.0


def _resolve_path(path: str, base_dir: str) -> str:
    expanded = os.path.expanduser(os.path.expandvars(path))
    return expanded if os.path.isabs(expanded) else os.path.join(base_dir, expanded)


def _sha_prefixed(path: str | None) -> str | None:
    digest = sha256_file(path)
    return f"sha256:{digest}" if digest is not None else None


def _stub_schematic(work_dir: str, top: str) -> str:
    """Write the empty ``.SUBCKT``/``.ENDS`` stub the runset's ``align`` step
    needs, and return its path. See this module's docstring for why a stub is
    sound here and what was measured to establish that."""
    path = os.path.join(work_dir, "klt-extract-stub.cir")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(
            "* synthesized by klt extract --engine klayout-runset: an empty\n"
            "* stub purely to satisfy the runset's schematic requirement. The\n"
            "* compare verdict it produces is discarded -- use klt lvs for a\n"
            "* real comparison.\n"
            f".SUBCKT {top}\n"
            f".ENDS {top}\n"
        )
    return path


def _read_runset_netlist(lvsdb_path: str) -> tuple[Any, Any]:
    """``(owner, netlist)`` for the layout side of the runset's ``.lvsdb``.

    ``LayoutVsSchematic.netlist()`` hands back a *borrowed* reference into the
    database object, not an owned copy: let the ``LayoutVsSchematic`` be
    collected and the next call on the netlist raises ``Object has been
    destroyed already``. The owner is returned alongside so the caller can
    keep it alive for as long as the netlist is used.
    """
    import klayout.db as kdb

    owner = kdb.LayoutVsSchematic()
    try:
        owner.read(lvsdb_path)
    except Exception as exc:  # noqa: BLE001 - any read failure is one error
        raise ExtractError(
            f"could not read the runset's .lvsdb database '{lvsdb_path}': {exc}"
        ) from exc
    netlist = owner.netlist()
    # `read()` does not raise on a file that is not a database at all -- it
    # hands back an empty one -- so "no circuits" is the real unreadable /
    # never-extracted signal, not an exception.
    if netlist is None or not any(True for _ in netlist.each_circuit()):
        raise ExtractError(
            f"could not read the runset's .lvsdb database '{lvsdb_path}': it "
            "carries no extracted layout netlist (no circuits). The runset "
            "wrote a file but never extracted the layout, or what it wrote is "
            "not a KLayout LVS database"
        )
    return owner, netlist


def _select_circuit(netlist: Any, top: str) -> Any:
    """The circuit the netlist was extracted for: ``top`` by name, else the
    netlist's sole top circuit."""
    circuit = netlist.circuit_by_name(top)
    if circuit is not None:
        return circuit
    tops = list(netlist.top_circuits())
    if len(tops) == 1:
        return tops[0]
    names = ", ".join(sorted(c.name for c in netlist.each_circuit())) or "(none)"
    raise ExtractError(
        f"the runset's extracted netlist has no circuit named '{top}' and no "
        f"single top circuit to fall back on (circuits: {names}). The runset "
        "may have flattened or renamed the top cell during alignment"
    )


def _resolve_binary(klayout_binary: str | None, base_dir: str) -> str:
    """The standalone ``klayout`` application, through ``klt lvs``'s own
    resolver (and its own error text, re-raised as an ``ExtractError``)."""
    from . import lvs_runset
    from .lvs import LvsError

    options: dict[str, Any] = {}
    if klayout_binary is not None:
        options["klayout_binary"] = klayout_binary
    try:
        return lvs_runset._resolve_klayout_binary(options, base_dir)
    except LvsError as exc:
        raise ExtractError(str(exc)) from exc


def _launch_runset(
    *,
    binary: str,
    deck_file: str,
    layout: str,
    top: str,
    deck_vars: dict[str, str],
    timeout_s: float,
    work_dir: str,
) -> tuple[str, Any]:
    """Synthesize the stub schematic and run the runset; return
    ``(lvsdb_path, completed)``."""
    from . import lvs_runset
    from .extract_progress import stage
    from .lvs import LvsError

    stub = _stub_schematic(work_dir, top)
    try:
        with stage("runset"):
            return lvs_runset._run_runset(
                binary=binary,
                deck_file=deck_file,
                layout=layout,
                reference=stub,
                top=top,
                deck_vars=deck_vars,
                timeout_s=timeout_s,
                work_dir=work_dir,
            )
    except LvsError as exc:
        # The launcher's text is `klt lvs`'s (deliberately shared), but its
        # one knob reference names that verb's request field; point it at
        # this verb's own flag instead.
        raise ExtractError(
            str(exc).replace(
                "raise options.runset_timeout_s to allow more time",
                "raise --runset-timeout to allow more time",
            )
        ) from exc


def _write_netlist(
    *,
    netlist: Any,
    path: str,
    output: str | None,
    no_netlist: bool,
    deck_basename: str,
    models: Mapping[str, str],
    dropped: dict[str, set[str]],
) -> tuple[str | None, str | None]:
    """Write ``netlist`` as SPICE through the X-call delegate; return
    ``(reported netlist_path, netlist_sha256)``, both ``None`` under
    ``no_netlist``. ``dropped`` is filled in by the delegate."""
    import klayout.db as kdb

    from .extract import _finish_netlist_write, _resolve_netlist_target
    from .extract_progress import stage

    netlist_path, scratch_dir = _resolve_netlist_target(path, output, no_netlist)
    writer = kdb.NetlistSpiceWriter(_runset_spice_writer_delegate(kdb, models, dropped))
    writer.use_net_names = True
    description = (
        f"extracted by klt extract --engine {RUNSET_ENGINE} --deck-file "
        f"{deck_basename}\n"
        "* no parasitics: this engine extracts devices and connectivity only"
    )
    try:
        with stage("netlist_write"):
            netlist.write(netlist_path, writer, description)
    except Exception as exc:  # noqa: BLE001 - one error envelope
        if scratch_dir is not None:
            shutil.rmtree(scratch_dir, ignore_errors=True)
        raise ExtractError(f"could not write netlist '{netlist_path}': {exc}") from exc
    with stage("netlist_hash"):
        return _finish_netlist_write(netlist_path, scratch_dir)


def run_extract_klayout_runset(
    path: str,
    *,
    deck_file: str,
    output: str | None = None,
    top: str | None = None,
    deck_vars: Mapping[str, str] | None = None,
    device_models: Mapping[str, str] | None = None,
    no_netlist: bool = False,
    runset_timeout_s: float | None = None,
    klayout_binary: str | None = None,
    request_dir: str | None = None,
) -> dict[str, Any]:
    """Extract ``path`` by running the PDK runset ``deck_file`` and return the
    ``klt extract`` report. See this module's docstring for the mechanism and
    ``docs/cli/extract.md``'s "Engine" section for the CLI contract.

    Raises :class:`ExtractError` for every failure -- a missing ``klayout``
    binary, a runset that wrote no database, a timeout, an unreadable
    database -- reusing ``klt lvs``'s own message text wherever the shared
    launcher produces it.
    """
    from . import lvs_runset
    from .extract import _build_metrics, _normalized_netlist_sha256, _staged
    from .extract_progress import stage
    from .extract_report import _describe_devices, _describe_nets

    base_dir = request_dir or os.getcwd()
    resolved_deck = _resolve_path(deck_file, base_dir)
    if not os.path.isfile(resolved_deck):
        raise ExtractError(f"--deck-file not found: {resolved_deck}")
    if not os.path.isfile(path):
        raise ExtractError(f"file not found: {path}")

    vars_ = parse_deck_vars(dict(deck_vars) if deck_vars else {})
    models = dict(device_models) if device_models else {}
    timeout_s = float(
        runset_timeout_s if runset_timeout_s is not None else RUNSET_DEFAULT_TIMEOUT_S
    )

    # The top cell has to be known before the runset runs: the stub schematic
    # is named after it, and the runset is told which cell to extract.
    with stage("layout_load"):
        top_cell_name, dbu_um = _layout_identity(path, top)

    binary = _resolve_binary(klayout_binary, base_dir)

    work_dir = tempfile.mkdtemp(prefix="klt-extract-runset-")
    try:
        lvsdb_path, completed = _launch_runset(
            binary=binary,
            deck_file=resolved_deck,
            layout=path,
            top=top_cell_name,
            deck_vars=vars_,
            timeout_s=timeout_s,
            work_dir=work_dir,
        )

        with stage("lvsdb_read"):
            # `lvsdb` is the owner of `netlist`; it must stay referenced for
            # as long as the netlist is read or written below.
            lvsdb, netlist = _read_runset_netlist(lvsdb_path)
            circuit = _select_circuit(netlist, top_cell_name)
            # Every circuit the runset produced is written, not just `circuit`:
            # dropping the others would orphan any `X` call the top makes into
            # them. A runset in its default flat mode (and IHP's `align` step,
            # which flattens unmatched sub-circuits into their callers) leaves
            # exactly one, which is the shape the curated engine also has; more
            # than one is disclosed below rather than silently summarised away.
            # Both read out as plain strings here, not held as live KLayout
            # objects: `circuit`/`netlist` are borrowed references into
            # `lvsdb` and are invalid once it is collected, which happens
            # before the report below is assembled.
            circuit_name = circuit.name
            extra_circuits = sorted(
                c.name for c in netlist.each_circuit() if c.name != circuit_name
            )

        dropped: dict[str, set[str]] = {}
        reported_netlist_path, netlist_sha256 = _write_netlist(
            netlist=netlist,
            path=path,
            output=output,
            no_netlist=no_netlist,
            deck_basename=os.path.basename(resolved_deck),
            models=models,
            dropped=dropped,
        )

        devices, device_counts = _staged("describe_devices", _describe_devices, circuit)
        nets = _staged("describe_nets", _describe_nets, circuit)
        runset_block = {
            "deck_file": deck_file,
            "deck_vars": vars_,
            "content_hash": _sha_prefixed(resolved_deck),
            "klayout_application_version": lvs_runset._application_version(binary),
            "exit_status": completed.returncode,
            "engine_error_lines": lvs_runset._error_lines(
                (completed.stdout or "") + (completed.stderr or "")
            ),
        }
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)

    warnings = _dropped_parameter_warnings(dropped, models)
    if extra_circuits:
        warnings.append(
            f"the runset's netlist holds {len(extra_circuits) + 1} circuits "
            f"({', '.join([circuit_name] + extra_circuits)}); all of them are "
            f"written, but devices[]/nets[] and every count describe "
            f"'{circuit_name}' alone. Re-run the runset in flat mode (for "
            "sg13cmos5l.lvs: --deck-var run_mode=flat) for the single-circuit "
            "shape the curated engine always produces"
        )
    if not devices:
        warnings.append(
            "the runset recognised no devices in this layout -- an empty "
            "extraction is not evidence. Check that the runset matches this "
            "PDK and that --top names the cell the devices are drawn in"
        )

    provenance = build_provenance(input_path=path)
    provenance["deck"] = {
        "name": os.path.basename(resolved_deck),
        "content_hash": _sha_prefixed(resolved_deck),
        "released": None,
    }
    provenance["runset"] = runset_block

    result: dict[str, Any] = {
        "schema_version": _schema_version(),
        # Present only under this engine, so a curated-deck report is
        # byte-identical to one produced before this engine existed.
        "engine": RUNSET_ENGINE,
        "file": env_provenance.repo_relative_path(
            path, repo_root=env_provenance.repo_root_for(path)
        ),
        # No curated deck ran: the runset *is* the deck, named by `deck_file`.
        "deck": None,
        "deck_file": deck_file,
        "deck_vars": vars_,
        "top": top_cell_name,
        "dbu_um": dbu_um,
        "netlist_path": reported_netlist_path,
        "netlist_sha256": netlist_sha256,
        # Same contract as the curated engine (issue #3407): `null` exactly
        # when `netlist_sha256` is, otherwise a numbering-independent digest
        # of the flat `devices[]`/`nets[]` connectivity -- so two runs of the
        # same layout compare equal despite KLayout's unstable `$N` names.
        "netlist_normalized_sha256": (
            None
            if netlist_sha256 is None
            else _normalized_netlist_sha256(devices, nets)
        ),
        "status": "extracted",
        "device_count": len(devices),
        "net_count": len(nets),
        "pin_count": sum(1 for net in nets if net["pin"]),
        "device_counts": dict(sorted(device_counts.items())),
        "device_classes": sorted(device_counts),
        "devices": devices,
        "nets": nets,
        "device_models": dict(sorted(models.items())) if models else None,
        # This engine extracts devices and connectivity only; `klt pex`
        # therefore cannot use it (docs/cli/extract.md, "Engine").
        "parasitics": None,
        "pdk": None,
        "warnings": warnings,
        "provenance": provenance,
    }
    result["metrics"] = _build_metrics(result, None)
    return result


def _layout_identity(path: str, top: str | None) -> tuple[str, float]:
    """``(top cell name, dbu in um)`` for ``path``, resolving ``top`` the same
    way the curated engine does."""
    from ._layout import load_layout, resolve_top_cell

    layout = load_layout(path, ExtractError)
    cell = resolve_top_cell(layout, top, ExtractError, path=path)
    return cell.name, layout.dbu


def _schema_version() -> int:
    from .extract import SCHEMA_VERSION

    return SCHEMA_VERSION
