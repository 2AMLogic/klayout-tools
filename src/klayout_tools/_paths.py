"""Shared path-resolution and request-loading helpers used across the ``klt``
verb modules.

Several request-parsing paths (``klt lvs``'s ``layout.file``, ``klt sim``'s
``models`` resolution, ``klt gen-compose``'s report paths) need to turn a
user-supplied path -- possibly relative, possibly containing ``~`` or an
environment variable -- into an absolute path anchored at the request's own
directory. ``_resolve_relative`` was previously defined identically in
``lvs.py``, ``sim.py``, and ``gen_compose.py``; it now lives here as the
single source of truth.

Every request-taking verb's own ``load_request(request_path)`` also opens
the same "does this path exist, is it a file, does it parse as JSON"
question the same way -- only the exception class raised on failure
differs per module. ``_load_request_json`` factors that literal prefix out;
it was previously duplicated verbatim across ``synthesize.py``, ``lvs.py``,
``sim.py``, ``place_and_route.py``, and ``functional_verification.py``.

``lvs.py`` and ``functional_verification.py`` also each defined an
identically-shaped ``_validate_request_shape(data, source)`` (the "is this
a dict, are the required top-level fields present" check) and
``load_request_arg(value)`` (the stdin / existing-file / inline-JSON
dispatch) -- differing only in the exception class and the tuple of
required fields. ``validate_request_shape`` and ``load_request_arg`` below
factor those out; each caller still owns its own exception class and
required-field tuple.

``erc.py``, ``power.py``, and ``mom.py`` each defined their own near-
identical ``_load_spec(spec_path)`` (read a JSON spec file, same missing/
directory/unreadable/not-an-object checks as ``_load_request_json`` above,
just with ``"spec"`` wording) and ``_parse_layer_datatype(raw, spec_path,
field)`` (parse a ``"<layer>/<datatype>"`` string) -- again differing only
in which ``*Error`` class each module raises. ``_load_spec_json`` and
``_parse_layer_datatype`` below factor those out the same way.

``erc.py`` and ``power.py`` also each defined an identically-shaped
``_validate_vias(spec, spec_path, stackup_names)`` (validate the optional
``"vias"`` array against a ``stackup_names`` list: default/type-check the
raw array, check each entry is an object with the required keys, parse
``"layer"`` via ``_parse_layer_datatype``, check ``"between"`` names two
distinct ``stackup_names`` entries, and reject duplicate via/stackup
names) -- differing only in the exception class, ``power.py``'s extra
required ``"resistance_ohm"`` key, and ``power.py``'s additional
``resistance_ohm``/``current_limit_a``/``current_limit_source`` fields
layered on top of the shared result. ``_validate_via_entries`` below
factors out the shared shape/parse/duplicate-tracking logic; each caller
still builds its own final entry dict from the returned pieces.

``place_and_route.py`` and ``post_route_sta.py`` also each defined an
identical ``_tcl_net_list(names)`` (render ``names`` as a brace-quoted Tcl
list body -- the standard Tcl idiom for embedding arbitrary literal
strings in a generated script without word-splitting or ``$``/``[...]``
substitution inside each token) and an identically-shaped
``_count_spef_nets_annotated(stdout)`` (parse the four
``nets_annotated``/``nets_total``/``design_nets_annotated``/
``design_nets_total`` ints out of a marker-delimited ``puts`` block in a
completed OpenSTA run's stdout, or ``None`` when the markers aren't
found) -- differing only in which module-local marker constants
(``_SPEF_NET_CHECK_BEGIN``/``_END``/``_RE``) each module's own generated
Tcl embeds. ``_tcl_net_list`` below is unchanged; ``_count_spef_nets_annotated``
now takes the three marker values (``begin``, ``end``, ``pattern``) as
parameters instead of reading module-local constants, since that was the
only per-module variation.

``lvs_netgen.py``'s ``_resolve_netgen_binary`` (issue #2373) established the
"``options.<tool>_binary`` -> ``$KLT_<TOOL>_BINARY`` -> bare name(s) on
``PATH``" resolution order for an externally-wrapped engine binary, so an
agent whose ``yosys``/``ngspice``/``iverilog``/``vvp`` on ``PATH`` is wrong
(a WASI-sandboxed ``yowasp-yosys`` build that cannot read a script file off
disk, a second ngspice build, ...) has a documented escape hatch instead of
having to reshape ``$PATH`` itself. :func:`resolve_tool_binary` below (issue
#2423) generalises that one netgen-specific function into a shared helper
``equiv.py`` (``yosys``, ``iverilog``, ``vvp``) and ``sim.py`` (``ngspice``)
both call -- ``lvs_netgen.py`` keeps its own already-shipped, already-tested
``_resolve_netgen_binary`` rather than being retrofitted onto this shared
version, since netgen alone has a second built-in fallback name
(``netgen-lvs``) this helper's callers do not need.

``pex.py``, ``op_sanity.py``, and ``netlist_normalize.py`` each also
independently implemented the SPICE ``+`` continuation-line fold -- glue a
line starting with ``+`` onto the end of the previously-collected line,
dropping the ``+`` -- as part of their own ``_logical_lines``/
``_merge_continuations`` helpers. The fold step itself was byte-for-byte
identical in intent across all three; only what happens *before* the fold
(comment stripping, blank/``*``-comment-line dropping) genuinely varies per
caller. ``_fold_spice_continuations`` below factors out just the fold: each
caller still runs its own pre-pass over the raw lines (and, for
``op_sanity.py``, an additional post-pass -- see its docstring) before/after
delegating to this helper.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
from collections.abc import Callable, Iterable, Mapping
from typing import Any


def _resolve_relative(path: str, base_dir: str) -> str:
    """Expand env vars/``~`` in ``path``; join relative paths against ``base_dir``."""
    expanded = os.path.expanduser(os.path.expandvars(path))
    if os.path.isabs(expanded):
        return expanded
    return os.path.join(base_dir, expanded)


def resolve_tool_binary(
    tool_label: str,
    values: Mapping[str, Any],
    *,
    option_key: str,
    option_label: str,
    env_var: str,
    request_dir: str,
    fallback_names: tuple[str, ...],
    error_cls: type[Exception],
    install_hint: str,
    required: bool = True,
) -> str | None:
    """Resolve an external engine's binary, returning an absolute path (or
    ``None`` when unresolved and ``required`` is ``False``) -- the shared
    generalisation (issue #2423) of ``lvs_netgen._resolve_netgen_binary``
    (issue #2373), reused by ``equiv.py`` (``yosys``, ``iverilog``, ``vvp``)
    and ``sim.py`` (``ngspice``).

    Resolution order, first runnable candidate wins:

    1. ``values[option_key]`` -- an explicit binary name or path from the
       request document, for a from-source build in an unusual location.
    2. ``$env_var`` -- the same override without editing the request
       document.
    3. Each of ``fallback_names`` on ``PATH``, in order.

    An explicitly-named binary (from either of the first two sources) that
    is not runnable is an ``error_cls`` naming *which* source named it
    (``option_label`` or ``$env_var``) -- never a silent fallback to the
    next source: an override the caller believes is in force but is not
    would be worse than not supporting one at all. A value containing a
    path separator is resolved against ``request_dir`` first (via
    :func:`_resolve_relative`); a bare name is looked up on ``PATH``.

    When nothing resolves (no override given, and no ``fallback_names``
    entry is on ``PATH``): raises ``error_cls`` naming every fallback tried
    plus ``install_hint`` when ``required`` is ``True`` (the default,
    matching ``_resolve_netgen_binary``'s always-required behaviour); returns
    ``None`` when ``required`` is ``False`` -- for a caller (``equiv.py``'s
    ``iverilog``/``vvp`` counterexample replay) whose pre-existing contract
    already degrades gracefully to a diagnostic when the tool is simply
    absent, and must keep doing so for that case. Either way, an
    *explicitly-named-but-broken* override always raises regardless of
    ``required`` -- that distinction is never silently dropped.
    """
    for value, source in (
        (values.get(option_key), option_label),
        (os.environ.get(env_var), f"${env_var}"),
    ):
        if value is None or value == "":
            continue
        candidate = value
        if os.sep in value or (os.altsep and os.altsep in value):
            candidate = _resolve_relative(value, request_dir)
        resolved = shutil.which(candidate)
        if resolved is None:
            raise error_cls(
                f"{source} does not name a runnable {tool_label} binary: "
                f"'{candidate}' is not an executable file and was not found "
                "on PATH"
            )
        return resolved

    for name in fallback_names:
        resolved = shutil.which(name)
        if resolved is not None:
            return resolved

    if not required:
        return None

    tried = ", ".join(f"'{name}'" for name in fallback_names)
    raise error_cls(
        f"could not launch {tool_label}: no {tool_label} binary found on "
        f"PATH (tried {tried}). {install_hint} Name the binary explicitly "
        f"with {option_label} or ${env_var}."
    )


def _load_request_json(request_path: str, error_cls: type[Exception]) -> Any:
    """Read ``request_path`` and decode it as JSON, raising ``error_cls`` for
    every failure mode a ``load_request`` needs to report: missing file, a
    directory instead of a file, an unreadable/undecodable file, or invalid
    JSON.

    Returns whatever :func:`json.load` produced, unvalidated -- callers
    still own the "is this a JSON object" and "are the required fields
    present" checks, since those (and their exact wording) are genuinely
    module-specific and some callers reuse them across non-file request
    sources (stdin, inline JSON) that never reach this function.
    """
    if not os.path.exists(request_path):
        raise error_cls(f"file not found: {request_path}")
    if os.path.isdir(request_path):
        raise error_cls(f"not a file: {request_path}")

    try:
        with open(request_path, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, UnicodeDecodeError) as exc:
        raise error_cls(f"could not read request file: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise error_cls(f"request file is not valid JSON: {exc}") from exc


def _load_spec_json(spec_path: str, error_cls: type[Exception]) -> dict[str, Any]:
    """Read ``spec_path`` and decode it as a JSON object, raising
    ``error_cls`` for every failure mode ``erc.py``/``power.py``/``mom.py``'s
    ``run_*`` entry points need to report: missing file, a directory instead
    of a file, an unreadable/undecodable file, or a JSON value that isn't an
    object.

    Unlike :func:`_load_request_json`, this also enforces "is this a JSON
    object" itself -- all three ``_load_spec`` callers needed that exact
    check, so it is folded in here rather than left to each caller.
    """
    if not os.path.exists(spec_path):
        raise error_cls(f"spec file not found: {spec_path}")
    if os.path.isdir(spec_path):
        raise error_cls(f"not a file: {spec_path}")
    try:
        with open(spec_path) as f:
            spec = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        raise error_cls(f"could not read spec '{spec_path}': {exc}") from exc
    if not isinstance(spec, dict):
        raise error_cls(f"spec '{spec_path}' must be a JSON object")
    return spec


def _parse_layer_datatype(
    raw: str, spec_path: str, field: str, error_cls: type[Exception]
) -> tuple[int, int]:
    """Parse a ``"<layer>/<datatype>"`` string into an ``(layer, datatype)``
    int pair, raising ``error_cls`` with ``field`` named in the message for
    anything malformed (wrong shape, non-integer parts).
    """
    parts = raw.split("/")
    malformed = error_cls(
        f"spec '{spec_path}': {field} must be '<layer>/<datatype>' with "
        f"integer layer/datatype (got {raw!r})"
    )
    if len(parts) != 2:
        raise malformed
    try:
        return int(parts[0]), int(parts[1])
    except ValueError as exc:
        raise malformed from exc


ViaEntry = tuple[int, dict[str, Any], str, tuple[int, int], tuple[str, str]]


def _validate_via_entries(
    spec: dict[str, Any],
    spec_path: str,
    stackup_names: list[str],
    error_cls: type[Exception],
    *,
    required_keys: tuple[str, ...] = ("layer", "between"),
) -> list[ViaEntry]:
    """Validate the ``"vias"`` array shared shape used by ``erc.py``'s and
    ``power.py``'s ``_validate_vias``: default/type-check ``spec["vias"]``,
    require each entry be a JSON object containing ``required_keys``, parse
    ``"layer"`` via :func:`_parse_layer_datatype`, check that ``"between"``
    names two distinct entries from ``stackup_names``, and reject duplicate
    via/stackup names (tracked cumulatively against ``stackup_names``).

    Returns one ``(index, raw_entry, name, layer, between)`` tuple per via,
    in input order, where ``layer`` is the parsed ``(layer, datatype)`` pair
    and ``between`` is the ``(str, str)`` pair of stackup names. Callers
    still own building their own final entry dict -- ``power.py`` layers
    ``resistance_ohm``/``current_limit_a``/``current_limit_source`` handling
    on top of ``raw_entry`` using ``required_keys=("layer", "between",
    "resistance_ohm")``; ``erc.py`` uses the pieces as-is.
    """
    raw = spec.get("vias", [])
    if raw is None:
        raw = []
    if not isinstance(raw, list):
        raise error_cls(f"spec '{spec_path}': 'vias' must be an array")

    results: list[ViaEntry] = []
    names: list[str] = list(stackup_names)
    for i, entry in enumerate(raw):
        if not isinstance(entry, dict):
            raise error_cls(f"spec '{spec_path}': vias[{i}] must be a JSON object")
        for key in required_keys:
            if key not in entry:
                raise error_cls(f"spec '{spec_path}': vias[{i}] missing {key!r}")

        layer = _parse_layer_datatype(
            str(entry["layer"]), spec_path, f"vias[{i}].layer", error_cls
        )

        between = entry["between"]
        if (
            not isinstance(between, list)
            or len(between) != 2
            or str(between[0]) == str(between[1])
            or any(str(n) not in stackup_names for n in between)
        ):
            raise error_cls(
                f"spec '{spec_path}': vias[{i}].between must name two distinct "
                f"'stackup' entries (got {between!r})"
            )

        name = str(entry.get("name", f"via{i}"))
        if name in names:
            raise error_cls(f"spec '{spec_path}': duplicate via/stackup name {name!r}")
        names.append(name)

        results.append((i, entry, name, layer, (str(between[0]), str(between[1]))))

    return results


def _tcl_net_list(names: Iterable[str]) -> str:
    """Render ``names`` as a brace-quoted Tcl list body (each name wrapped
    ``{...}``, whitespace-joined) -- the standard Tcl idiom for embedding
    arbitrary literal strings in a generated script without word-splitting
    or ``$``/``[...]`` substitution inside each token. Shared by
    ``place_and_route.py`` and ``post_route_sta.py``, whose net names come
    from ``klt extract``'s own SPICE-safe naming
    (:func:`klayout_tools.extract.spice_safe_net_name`) or a caller-supplied
    ``spef``'s own (unescaped) net names -- neither introduces a literal
    ``}``, so an unbalanced brace in a caller-supplied name is a known,
    unguarded edge case."""
    return " ".join("{" + name + "}" for name in names)


def _count_spef_nets_annotated(
    stdout: str, *, begin: str, end: str, pattern: re.Pattern[str]
) -> tuple[int, int, int, int] | None:
    """``(nets_annotated, nets_total, design_nets_annotated,
    design_nets_total)`` parsed from a ``begin``/``end``-delimited stdout
    block -- ``place_and_route.py``'s ``_spef_sta_script_lines`` and
    ``post_route_sta.py``'s ``_spef_net_check_lines`` each ``puts`` this
    block using their own module-local marker constants (differing only in
    the literal marker string), which callers pass in as ``begin``/``end``/
    ``pattern`` rather than this function hardcoding either module's own.

    Returns ``None`` when the markers aren't found, or the block's contents
    don't match ``pattern`` (defensive; should not happen for a successful
    run)."""
    try:
        start_idx = stdout.index(begin) + len(begin)
        stop_idx = stdout.index(end, start_idx)
    except ValueError:
        return None
    match = pattern.search(stdout[start_idx:stop_idx])
    if match is None:
        return None
    return (
        int(match.group(1)),
        int(match.group(2)),
        int(match.group(3)),
        int(match.group(4)),
    )


def _fold_spice_continuations(lines: list[str]) -> list[str]:
    """Fold SPICE ``+`` continuation lines onto the entry they continue.

    ``lines`` is whatever a caller's own pre-pass has already produced (raw
    lines, comment-stripped lines, or comment-stripped-and-filtered lines --
    this function does not care). For each entry, if it starts with ``+``
    (after left-stripping) *and* there is already a previous entry to fold
    onto, that previous entry is replaced with itself plus a single space
    plus the continuation's own content (the ``+`` and any leading/trailing
    whitespace around it dropped); otherwise the entry is appended as-is.

    A ``+``-prefixed entry with nothing preceding it to fold onto (the first
    entry in ``lines`` starts with ``+``, or every entry before it was
    itself folded away) is appended verbatim, ``+`` prefix intact, rather
    than raising or being silently dropped -- callers that want an orphan
    continuation line dropped (as ``op_sanity.py`` does) filter such
    unfolded ``+``-prefixed entries out of this function's return value
    themselves; callers that want it kept as a literal line (``pex.py``,
    ``netlist_normalize.py``) use the return value as-is.
    """
    folded: list[str] = []
    for line in lines:
        stripped = line.lstrip()
        if stripped.startswith("+") and folded:
            folded[-1] = f"{folded[-1]} {stripped[1:].strip()}"
        else:
            folded.append(line)
    return folded


def validate_request_shape(
    data: Any,
    source: str,
    *,
    error_cls: type[Exception],
    required_fields: tuple[str, ...],
) -> dict[str, Any]:
    """Shared ``required_fields`` shape check for a JSON-decoded request,
    however it was sourced (file, inline JSON, stdin). ``source`` is folded
    into the "must be a JSON object" error for context.
    """
    if not isinstance(data, dict):
        raise error_cls(f"{source} must contain a JSON object")

    for field in required_fields:
        if field not in data:
            raise error_cls(f"request is missing required field: {field}")

    return data


#: How many leading bytes :func:`looks_like_request_document` reads off a
#: candidate file to decide whether it is a JSON document. A request document
#: may legitimately start with whitespace/newlines before its opening ``{``,
#: so this is generous relative to the single byte actually needed.
_REQUEST_SNIFF_BYTES = 512


def looks_like_request_document(value: str) -> bool:
    """Is this positional CLI value a request *document* rather than an
    input *file* path?

    ``klt drc``/``klt extract`` (issue #1867) accept either shape in the same
    positional slot -- unlike ``klt lvs``, whose positional has only ever been
    a request document, they predate the convention and already take a layout
    path there. argparse cannot disambiguate two optional positionals (the
    first one always wins), so the two forms share one slot and are told apart
    by *value shape*, never by argument position:

    - ``"-"`` -- always a request document read from stdin. A layout stream is
      never read from stdin (KLayout's reader needs a seekable file).
    - an *existing* file whose first non-whitespace byte is ``{`` -- a request
      document file. A GDSII stream starts with the binary record header
      ``00 06 00 02`` and an OASIS stream with ``%SEMI-OASIS``, so no layout
      this tool can read can be mistaken for one. This check is gated on
      ``os.path.isfile`` running *first*, so a real layout whose bare
      filename happens to start with ``{`` (e.g. ``{weird}.gds``, issue
      #1922) is still routed here for content-sniffing rather than being
      misread as inline JSON from its name alone.
    - otherwise, a value whose first non-whitespace character is ``{`` -- an
      inline JSON object string (or a mistyped/nonexistent path that merely
      looks like one).

    Everything else (including a path that does not exist and does not start
    with ``{``) is a layout path, so an unreadable/mistyped layout path still
    produces the same "file not found" error it always has, rather than a
    JSON parse error.
    """
    if value == "-":
        return True
    if os.path.isfile(value):
        # An existing file always wins over the bare-string shape check --
        # e.g. a layout named "{weird}.gds" must be content-sniffed, not
        # misclassified as inline JSON just because its name starts with
        # "{".
        try:
            with open(value, "rb") as handle:
                head = handle.read(_REQUEST_SNIFF_BYTES)
        except OSError:
            # Unreadable: fall through to the layout-path reader, which
            # reports the read failure in its own vocabulary.
            return False
        return head.decode("utf-8", errors="replace").lstrip()[:1] == "{"
    return value.lstrip()[:1] == "{"


def load_request_arg(
    value: str,
    *,
    error_cls: type[Exception],
    required_fields: tuple[str, ...],
    load_request_fn: Callable[[str], dict[str, Any]],
) -> tuple[dict[str, Any], str]:
    """Resolve a CLI ``request`` argument into a request dict plus the
    directory relative paths inside it should resolve against.

    ``value`` is one of three forms (the convention shared by ``klt lvs``
    and ``klt functional-verification``):

    - ``"-"`` -- read the request JSON document from stdin. Relative paths
      inside it resolve against the current working directory, since there
      is no request *file* to anchor them to.
    - a path to an existing, readable file -- read and parse that file via
      the caller's own ``load_request_fn`` (unchanged). Relative paths
      resolve against the file's own directory, exactly as before.
    - anything else -- parsed as an inline JSON object string. This mirrors
      ``klt gen --params``'s ``load_params_arg`` (``gen.py``): an existing
      file always wins first, so this only applies once ``os.path.isfile``
      has already said no. Relative paths resolve against the current
      working directory, same as the stdin form.

    Raises ``error_cls`` for any read/parse/shape failure -- the same
    exception type ``load_request_fn`` raises, so callers do not need to
    distinguish the three forms.
    """
    if value == "-":
        try:
            data = json.load(sys.stdin)
        except json.JSONDecodeError as exc:
            raise error_cls(f"stdin request is not valid JSON: {exc}") from exc
        return (
            validate_request_shape(
                data,
                "stdin request",
                error_cls=error_cls,
                required_fields=required_fields,
            ),
            os.getcwd(),
        )

    if os.path.isfile(value):
        return load_request_fn(value), os.path.dirname(os.path.abspath(value))

    if os.path.isdir(value):
        raise error_cls(f"not a file: {value}")

    try:
        data = json.loads(value)
    except json.JSONDecodeError as exc:
        raise error_cls(
            f"request '{value}' is neither an existing file (file not "
            f"found) nor valid inline JSON: {exc}"
        ) from exc
    return (
        validate_request_shape(
            data,
            "inline request",
            error_cls=error_cls,
            required_fields=required_fields,
        ),
        os.getcwd(),
    )
