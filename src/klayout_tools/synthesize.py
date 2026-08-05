"""Synthesize RTL against a standard-cell liberty via Yosys, headless.

Pure library: :func:`run_synthesize` returns plain Python data (a ``dict`` of
JSON-serialisable primitives) and never prints, mirroring ``lvs.py``/
``sim.py``. Serialisation and human-readable formatting live in the CLI
command module (``cli/synthesize_cmd.py``).

This is Phase 2 of [Epic #391](https://github.com/2AMLogic/klayout-tools/issues/391)
("adopt the digital engine class -- Yosys + OpenROAD -- RTL->GDS as a first-
class ``klt`` flow"), the build carried by the two accepted Phase 1 spikes --
read them first:

- ``docs/design/yosys-synthesis-spike.md`` (#396) settles the invocation
  shape (a generated ``.ys`` script, never the ``-S`` shortcut or an
  ever-growing ``-p`` string) and the output-parsing recipe
  (``stat -liberty <lib> -json -top <top>`` captured via ``tee -q -o
  <path>``, never re-derived by parsing ``write_verilog``'s netlist).
- ``docs/design/digital-flow-contracts-spike.md`` section 4 (#399) settles
  the request/response JSON contract and exit-code table this module
  implements.

Like ``klt lvs``/``klt sim``, ``klt synthesize`` takes a **request document**
(RTL sources plus PDK/liberty selection plus optional constraints is richer
than a flag line carries cleanly), not positional file args.

Engine: ``"yosys"`` (only value implemented today; ``request.engine`` exists
from day one so a later engine is an additive enum value, per the contract
spike section 2). Invoked as a subprocess (``yosys -s <script>``), never
in-process -- there is no Python binding for Yosys the way ``klayout.db``
already is for ``klt lvs``/``klt extract``.

Liberty resolution reuses ``klayout_tools.pdk.find_pdk``/
``list_cell_libraries`` -- the same ``libs_ref`` discovery ``klt pdk``/
``klt cells`` already use (Yosys survey section 0) -- **no new PDK-fetch
mechanism**. ``request.pdk`` carries only ``cell_library``/``corner`` (no
``variant``/``root``): the resolved PDK install is whatever ``find_pdk()``'s
own default search order (``$PDK_ROOT``/``$PDK``, then the ciel/volare
stores, then the conventional prefixes) finds, exactly as ``klt pdk find``
with no flags would. When that install has no matching liberty at all, this
raises a clear "liberty not found for deck" :class:`SynthesizeError` --
matching ``klt drc``'s existing "deck requires an asset the resolved install
doesn't ship" posture (the Yosys survey's own open question, resolved here).
When ``request.pdk.corner`` is omitted, the nominal (typical-process,
room-temperature) corner ``klt pdk cells``' own ``nominal_corner`` selection
already picks is used, via :func:`klayout_tools.pdk.list_cell_libraries`.

``timing`` is always ``null`` in this contract, deliberately -- the Yosys
survey section 3.5/3.6 found no working recipe for a Yosys-native timing
report against a liberty-mapped netlist (every mapped standard-cell type is
reported "not recognised" by ``sta``); deferred to Phase 4's OpenROAD/OpenSTA
step, which already must run STA for place-and-route signoff.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from typing import Any

from ._provenance import build_provenance, sha256_file
from .pdk import PdkNotFoundError, find_pdk, list_cell_libraries

#: Bumped only on a non-additive (breaking) change to this command's own
#: JSON shape -- see docs/json-contract.md.
SCHEMA_VERSION = 1

#: ``"yosys"`` is the only engine implemented today; the field is present in
#: the request from day one (contract spike section 2) so a later backend is
#: an additive enum value, never a contract-shape change.
SUPPORTED_ENGINES = ("yosys",)

_YOSYS_VERSION_RE = re.compile(r"Yosys\s+(\S+)")


class SynthesizeError(Exception):
    """Raised when a synthesis run cannot even be attempted: a missing/
    malformed request file, an unresolvable/unreadable RTL source, an
    unresolvable ``pdk.cell_library``/``corner`` (no matching liberty via
    ``find_pdk()``), an elaboration/hierarchy error, or a Yosys/ABC engine
    error.

    The CLI turns this into a clean stderr message + exit code 1, never a
    traceback -- synthesis has no "ran but found problems" outcome of its
    own (contract spike section 4, "Exit codes"): it either produces a
    netlist or it fails.
    """


def load_request(request_path: str) -> dict[str, Any]:
    """Read and minimally validate a ``klt synthesize`` request JSON file.

    Raises :class:`SynthesizeError` if the file is missing/unreadable, not
    valid JSON, or missing a required top-level field (``sources``,
    ``hdl_toplevel``, ``pdk``). Does not require a ``schema`` field,
    matching ``klt lvs``/``klt sim``'s ``load_request`` (user-authored
    input, never emitted by this tool).
    """
    if not os.path.exists(request_path):
        raise SynthesizeError(f"file not found: {request_path}")
    if os.path.isdir(request_path):
        raise SynthesizeError(f"not a file: {request_path}")

    try:
        with open(request_path, encoding="utf-8") as handle:
            request = json.load(handle)
    except (OSError, UnicodeDecodeError) as exc:
        raise SynthesizeError(f"could not read request file: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise SynthesizeError(f"request file is not valid JSON: {exc}") from exc

    if not isinstance(request, dict):
        raise SynthesizeError("request file must contain a JSON object")

    for field in ("sources", "hdl_toplevel", "pdk"):
        if field not in request:
            raise SynthesizeError(f"request is missing required field: {field}")

    return request


def run_synthesize(request_path: str) -> dict[str, Any]:
    """Run the Yosys synthesis declared by the request at ``request_path``.

    Returns a dict matching the documented JSON schema (see
    ``docs/cli/synthesize.md`` / ``docs/design/digital-flow-contracts-spike.md``
    section 4). Raises :class:`SynthesizeError` for anything that prevents a
    netlist from being produced at all -- bad request, unreadable RTL
    source, elaboration/hierarchy error, unresolvable ``pdk.cell_library``/
    ``corner``, or a Yosys/ABC engine error.

    The generated ``.ys`` script and mapped netlist are written to
    ``.klt/synthesize/`` next to the request file (the same "next to the
    input" default ``klt sim``'s ``.klt/sim/`` artifacts directory already
    uses) and kept as debuggable artifacts, never deleted.
    """
    request = load_request(request_path)
    request_dir = os.path.dirname(os.path.abspath(request_path))

    engine = request.get("engine", "yosys")
    if engine not in SUPPORTED_ENGINES:
        raise SynthesizeError(
            f"unsupported engine '{engine}' (supported: {', '.join(SUPPORTED_ENGINES)})"
        )

    resolved_sources = _resolve_sources(request["sources"], request_dir)

    hdl_toplevel = request["hdl_toplevel"]
    if not isinstance(hdl_toplevel, str) or not hdl_toplevel:
        raise SynthesizeError("request.hdl_toplevel must be a non-empty string")

    pdk_spec = request["pdk"]
    if not isinstance(pdk_spec, dict):
        raise SynthesizeError("request.pdk must be a JSON object")
    cell_library = pdk_spec.get("cell_library")
    if not isinstance(cell_library, str) or not cell_library:
        raise SynthesizeError("request.pdk.cell_library is required")
    requested_corner = pdk_spec.get("corner")
    if requested_corner is not None and not (
        isinstance(requested_corner, str) and requested_corner
    ):
        raise SynthesizeError(
            "request.pdk.corner must be a non-empty string when given"
        )

    constraints = request.get("constraints")
    if constraints is not None and not isinstance(constraints, dict):
        raise SynthesizeError("request.constraints must be a JSON object")

    liberty_path, corner, pdk_info = _resolve_liberty(cell_library, requested_corner)

    output_dir = os.path.join(request_dir, ".klt", "synthesize")
    try:
        os.makedirs(output_dir, exist_ok=True)
    except OSError as exc:
        raise SynthesizeError(
            f"could not create output directory '{output_dir}': {exc}"
        ) from exc

    script_path = os.path.join(output_dir, f"synth_{hdl_toplevel}.ys")
    netlist_path = os.path.join(output_dir, f"{hdl_toplevel}_synth.v")
    stats_path = os.path.join(output_dir, f"{hdl_toplevel}_stats.json")

    _write_script(
        script_path=script_path,
        sources=resolved_sources,
        hdl_toplevel=hdl_toplevel,
        liberty_path=liberty_path,
        stats_path=stats_path,
        netlist_path=netlist_path,
    )

    _run_yosys(script_path)

    if not os.path.isfile(netlist_path):
        raise SynthesizeError(
            f"yosys exited successfully but did not produce '{netlist_path}'"
        )

    module_stats = _read_stats(stats_path, hdl_toplevel)
    engine_version = _yosys_version()

    deck_name = f"{cell_library}__{corner}"
    provenance = build_provenance(
        deck_name=deck_name,
        deck_path=liberty_path,
        pdk=pdk_info,
        input_path=resolved_sources[0] if len(resolved_sources) == 1 else None,
    )
    if len(resolved_sources) > 1:
        provenance["input"] = {"content_hash": _combined_content_hash(resolved_sources)}

    return {
        "schema_version": SCHEMA_VERSION,
        "engine": engine,
        "engine_version": engine_version,
        "hdl_toplevel": hdl_toplevel,
        "status": "ok",
        "instance_count": module_stats["num_cells"],
        "area_um2": module_stats["area"],
        # ``sequential_area`` is only emitted by newer Yosys builds (0.67+);
        # distro-packaged Yosys (e.g. Ubuntu 24.04's 0.33) omits it from
        # `stat -json` entirely. Degrade to `None` rather than a raw
        # `KeyError` -- see #560.
        "sequential_area_um2": module_stats.get("sequential_area"),
        "instance_counts_by_type": dict(
            sorted((module_stats.get("num_cells_by_type") or {}).items())
        ),
        "timing": None,
        "netlist_path": netlist_path,
        "script_path": script_path,
        "provenance": provenance,
    }


def _resolve_sources(sources: Any, request_dir: str) -> list[str]:
    """Validate ``request.sources`` and resolve each entry to an absolute,
    readable path (relative to ``request_dir``, the request file's own
    directory -- the same convention every other request-taking verb uses).

    Raises :class:`SynthesizeError` naming the offending entry for a missing
    or unreadable RTL source, matching this module's docstring's "unreadable
    RTL source" error scope.
    """
    if not isinstance(sources, list) or not sources:
        raise SynthesizeError("request.sources must be a non-empty array of paths")
    if not all(isinstance(entry, str) and entry for entry in sources):
        raise SynthesizeError("request.sources entries must be non-empty strings")

    resolved: list[str] = []
    for entry in sources:
        path = entry if os.path.isabs(entry) else os.path.join(request_dir, entry)
        if not os.path.isfile(path):
            raise SynthesizeError(f"RTL source not found: {entry}")
        try:
            with open(path, "rb"):
                pass
        except OSError as exc:
            raise SynthesizeError(
                f"could not read RTL source '{entry}': {exc}"
            ) from exc
        resolved.append(os.path.abspath(path))
    return resolved


def _resolve_liberty(
    cell_library: str, requested_corner: str | None
) -> tuple[str, str, dict[str, Any]]:
    """Resolve ``(liberty_path, corner, pdk_info)`` for ``cell_library``.

    ``pdk_info`` is :func:`klayout_tools.pdk.find_pdk`'s own resolution dict
    -- passed straight through to :func:`build_provenance`'s ``pdk``
    argument. Raises :class:`SynthesizeError` (never
    :class:`~klayout_tools.pdk.PdkNotFoundError`) for every failure mode:
    no PDK install resolves at all, the resolved install ships no
    ``libs_ref``/``cell_library`` asset, or ``cell_library`` has no
    ``corner`` (explicit or nominal-default) liberty view -- the "liberty
    not found for deck" posture this module's docstring describes.
    """
    try:
        info = find_pdk()
    except PdkNotFoundError as exc:
        raise SynthesizeError(str(exc)) from exc

    libs_ref = info["assets"]["libs_ref"]
    if libs_ref is None:
        raise SynthesizeError(
            f"liberty not found for deck: resolved PDK install "
            f"'{info['variant']}' at '{info['root']}' ships no libs_ref asset"
        )

    lib_dir = os.path.join(libs_ref, cell_library)
    if not os.path.isdir(lib_dir):
        raise SynthesizeError(
            f"liberty not found for deck: standard-cell library "
            f"'{cell_library}' not found under resolved PDK install "
            f"'{info['variant']}' at '{info['root']}'"
        )

    corner = requested_corner
    if corner is None:
        libraries = list_cell_libraries()
        entry = next(
            (lib for lib in libraries["libraries"] if lib["name"] == cell_library),
            None,
        )
        corner = entry["nominal_corner"] if entry else None
        if corner is None:
            raise SynthesizeError(
                f"liberty not found for deck: could not determine a nominal "
                f"corner for '{cell_library}' -- pass request.pdk.corner "
                "explicitly"
            )

    liberty_path = os.path.join(lib_dir, "lib", f"{cell_library}__{corner}.lib")
    if not os.path.isfile(liberty_path):
        raise SynthesizeError(
            f"liberty not found for deck: no '{corner}' corner for "
            f"'{cell_library}' under resolved PDK install '{info['variant']}' "
            f"(expected '{liberty_path}')"
        )
    return liberty_path, corner, info


def _write_script(
    *,
    script_path: str,
    sources: list[str],
    hdl_toplevel: str,
    liberty_path: str,
    stats_path: str,
    netlist_path: str,
) -> None:
    """Generate the ``.ys`` synthesis script (Yosys survey section 1's exact
    pass sequence: ``read_verilog`` -> ``hierarchy`` -> ``synth`` ->
    ``dfflibmap`` -> ``abc -liberty`` -> ``clean`` -> ``stat``/
    ``write_verilog``) into ``script_path``.

    Every path embedded in the script is absolute, so the script runs
    correctly regardless of the invoking process's own working directory --
    :func:`_run_yosys` never sets ``cwd=``.
    """
    lines = [f"read_verilog {path}" for path in sources]
    lines += [
        f"hierarchy -check -top {hdl_toplevel}",
        f"synth -top {hdl_toplevel}",
        f"dfflibmap -liberty {liberty_path}",
        f"abc -liberty {liberty_path}",
        "clean",
        f"tee -q -o {stats_path} "
        f"stat -liberty {liberty_path} -json -top {hdl_toplevel}",
        f"write_verilog -noattr {netlist_path}",
    ]
    try:
        with open(script_path, "w", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")
    except OSError as exc:
        raise SynthesizeError(
            f"could not write synthesis script '{script_path}': {exc}"
        ) from exc


def _run_yosys(script_path: str) -> None:
    """Invoke ``yosys -s <script_path>`` and raise :class:`SynthesizeError`
    on any failure to run (missing binary, timeout-free run exits nonzero).

    Never raises on a *successful* (exit 0) run -- the caller is responsible
    for validating the declared output files actually appeared.
    """
    try:
        completed = subprocess.run(
            ["yosys", "-s", script_path],
            capture_output=True,
            text=True,
        )
    except OSError as exc:
        raise SynthesizeError(f"could not launch yosys: {exc}") from exc

    if completed.returncode != 0:
        raise SynthesizeError(_synthesis_error_message(completed))


def _synthesis_error_message(completed: subprocess.CompletedProcess) -> str:
    """Build an actionable error message from a failed ``yosys -s`` run.

    Prefers the last ``ERROR:`` line Yosys itself printed (its own error
    lines go to stderr -- verified live, Yosys survey worked example), on
    either stream; falls back to a generic exit-code message with a short
    tail of captured output when no ``ERROR:`` line is found.
    """
    for stream in (completed.stderr or "", completed.stdout or ""):
        error_lines = [line.strip() for line in stream.splitlines() if "ERROR:" in line]
        if error_lines:
            return f"yosys synthesis failed: {error_lines[-1]}"

    tail_source = (completed.stderr or completed.stdout or "").strip().splitlines()
    snippet = " ".join(tail_source[-3:]) if tail_source else "no output captured"
    return f"yosys exited with code {completed.returncode}: {snippet}"


def _yosys_version() -> str | None:
    """The resolved Yosys build string (``yosys -V``'s own version token),
    or ``None`` if unresolvable -- never raises."""
    try:
        completed = subprocess.run(["yosys", "-V"], capture_output=True, text=True)
    except OSError:
        return None
    if completed.returncode != 0:
        return None
    match = _YOSYS_VERSION_RE.search(completed.stdout)
    return match.group(1) if match else None


def _read_stats(stats_path: str, hdl_toplevel: str) -> dict[str, Any]:
    """Parse ``stat -liberty ... -json``'s captured output (Yosys survey
    section 2) and return the top module's own stats block.

    Prefers ``modules["\\<hdl_toplevel>"]`` (Yosys's own escaped-identifier
    naming for a public module name); falls back to the ``design`` rollup
    key when that lookup misses (a defensive fallback, not the primary
    path -- for a single top-level design the two are identical, per the
    Yosys survey's own worked example). Raises :class:`SynthesizeError` if
    the file is missing, unparseable, or names no recognisable module.
    """
    if not os.path.isfile(stats_path):
        raise SynthesizeError(
            f"yosys did not produce the expected stats output '{stats_path}'"
        )
    try:
        with open(stats_path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, UnicodeDecodeError) as exc:
        raise SynthesizeError(
            f"could not read stats output '{stats_path}': {exc}"
        ) from exc
    except json.JSONDecodeError as exc:
        raise SynthesizeError(
            f"stats output '{stats_path}' is not valid JSON: {exc}"
        ) from exc

    modules = data.get("modules") if isinstance(data, dict) else None
    module_stats = (modules or {}).get(f"\\{hdl_toplevel}")
    if module_stats is None:
        module_stats = data.get("design") if isinstance(data, dict) else None
    if not isinstance(module_stats, dict) or "num_cells" not in module_stats:
        raise SynthesizeError(
            f"could not find synthesis statistics for top module "
            f"'{hdl_toplevel}' in '{stats_path}'"
        )
    return module_stats


def _combined_content_hash(paths: list[str]) -> str | None:
    """A single ``sha256:``-prefixed digest covering every file in ``paths``
    (order-independent -- sorted before hashing), for the multi-``sources``
    case :func:`klayout_tools._provenance.build_provenance`'s single-path
    ``input_path`` argument cannot express on its own. ``None`` if any file
    cannot be hashed.
    """
    digests = []
    for path in sorted(paths):
        digest = sha256_file(path)
        if digest is None:
            return None
        digests.append(digest)
    combined = hashlib.sha256("".join(digests).encode("utf-8")).hexdigest()
    return f"sha256:{combined}"
