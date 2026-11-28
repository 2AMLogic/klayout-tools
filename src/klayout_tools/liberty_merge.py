"""Merge several Liberty group files into one ``.lib`` (issue #4087).

Some upstream PDK trees split a standard-cell library's cells over several
complete ``library(){}`` files per corner (for example ``AO``/``OA``/
``INVBUF``/``SEQ``/``SIMPLE`` groups) that share one header. ``klt-pdk.json``
lets a ``liberty`` corner list those files; :func:`merge_liberty` produces the
single file every ``liberty_path`` consumer (Yosys, OpenROAD, OpenSTA) expects.

Merge rule: the header (text before the first ``cell (``) comes from the first
file and must agree with the rest field by field -- whitespace, lexical
comments, the ``library (<name>)`` name and the free-text ``date``,
``revision`` and ``comment`` attributes are ignored; any other difference
(units, operating conditions, ``default_*``, ...) reports every
conflicting field and both values (issue #7011). One exception (issue #6940): when
each file declares a single ``operating_conditions`` group that its own
``default_operating_conditions`` selects and the group bodies agree, the
group's *name* alone may differ -- the merged header keeps the first
file's spelling, and a renamed file whose retained text still references
its local name refuses instead of dangling. Top-level ``lu_table_template``
and ``power_lut_template`` groups are a second exception: they are unioned
by name. Differing direct index values are safe only when every referencing
table supplies all its own axes. The first definition is kept, and the ones the
first file lacks are appended after its header. A third exception (issue
#7809): an optional library-level ``input_voltage``/``output_voltage`` group
that some files declare and others lack entirely is unioned by name, not
compared (see :mod:`liberty_merge_voltage`). Each file's cell groups are
appended in listed order; a duplicate cell name is an error. The
result is written content-addressed under a klt cache directory -- never into
the PDK tree -- so repeated runs reuse it and its path (and hash) is stable.
"""

from __future__ import annotations

import hashlib
import os
import re
import tempfile

from .liberty_header_diff import header_mismatch
from .liberty_merge_syntax import (
    _TEMPLATE_KINDS,
    _colon_attr,
    _depth1_groups,
    _find_attr_reference,
    _find_matching,
    _next_significant,
    _normalize_header,
    _scan_cells,
    _template_shape,
    _tokens,
    _unquote,
    inherited_table_axis,
)
from .liberty_merge_syntax import (
    _find_terminator as _find_terminator,  # kept for tests/test_liberty_merge.py
)
from .liberty_merge_voltage import VoltageUnion, _without

#: Environment override for the cache root (otherwise ``$XDG_CACHE_HOME`` or
#: ``~/.cache``, plus ``klt/liberty-merged``).
CACHE_ENV = "KLT_CACHE_DIR"


#: Header attributes ignored when comparing files for a merge: free text with
#: no electrical content (a per-file timestamp, a version string, a vendor
#: note). This is a closed set -- every other header field must agree.
_IGNORED_HEADER_ATTRS = frozenset({"date", "revision", "comment"})

#: Key carrying header text that is neither an attribute nor a group, so
#: unrecognised residue still participates in the comparison (fail closed).
_RESIDUE_KEY = "<other header text>"


class LibertyMergeError(Exception):
    """Liberty files cannot be merged (header mismatch, duplicate cell, ...)."""


def cache_root() -> str:
    """Root directory the Liberty merge cache lives under.

    ``$KLT_CACHE_DIR`` itself when set, else ``${XDG_CACHE_HOME:-~/.cache}``
    plus ``klt`` -- the path a container ``openroad`` wrapper must mount for
    :func:`cache_dir`'s contents to be reachable inside the container
    (issue #5626).
    """
    base = os.environ.get(CACHE_ENV)
    if base:
        return os.path.expanduser(base)
    xdg = os.environ.get("XDG_CACHE_HOME") or os.path.join(
        os.path.expanduser("~"), ".cache"
    )
    return os.path.join(xdg, "klt")


def cache_dir() -> str:
    """Directory merged Liberty files are written to."""
    return os.path.join(cache_root(), "liberty-merged")


def merge_liberty(paths: list[str]) -> str:
    """Merge ``paths`` into one cached ``.lib`` and return its path.

    A single path is returned unchanged (the list form of one element equals
    the string form). Raises :class:`LibertyMergeError` on a header field
    conflict, a duplicate cell name, or a file that is not a ``library``
    group.
    """
    if not paths:
        raise LibertyMergeError("no Liberty files to merge")
    if len(paths) == 1:
        return paths[0]

    texts = [_read(path) for path in paths]
    # Earlier cache entries may have missed differences between repeated groups.
    digest = hashlib.sha256(b"liberty-merge-v2\0")
    for text in texts:
        digest.update(hashlib.sha256(text.encode("utf-8")).digest())
    out_path = os.path.join(cache_dir(), f"merged-{digest.hexdigest()[:24]}.lib")
    if os.path.isfile(out_path):
        # Content-addressed: still validate nothing, inputs are unchanged.
        return out_path

    header, extra_templates, bodies = _collect(paths, texts)
    head = header.rstrip()
    if extra_templates:
        head += "\n" + "\n".join(extra_templates)
    merged = head + "\n\n" + "\n".join(b.strip() for b in bodies) + "\n}\n"
    _write_atomic(out_path, merged)
    return out_path


def _collect(paths: list[str], texts: list[str]) -> tuple[str, list[str], list[str]]:
    """Validate headers/cell names across files; ``(header, extra header
    groups, bodies)``. The extras are the templates and voltage groups the
    first header does not define."""
    header = ""
    header_fields: dict[str, str] = {}
    first_path = paths[0]
    first_condition_count = 0
    seen: dict[str, str] = {}
    templates: dict[str, tuple[str, str, str]] = {}  # name -> (norm, raw, path)
    ambiguous_axes: dict[str, set[str]] = {}
    extra: list[str] = []
    bodies: list[str] = []
    voltage = VoltageUnion()
    for path, text in zip(paths, texts, strict=True):
        file_header, body, cells, condition_count = _split(path, text)
        stripped, file_templates = _extract_templates(file_header)
        fields = _header_fields(path, stripped)
        if path is first_path:
            header, header_fields = file_header, fields
            first_condition_count = condition_count
            voltage.first(path, file_header, fields, text)
        else:
            retained = "".join(raw for _, raw in file_templates) + body
            first_fields, other_fields, context = _reconcile_conditions(
                first_path,
                header_fields,
                path,
                fields,
                retained,
                single_condition=first_condition_count == condition_count == 1,
            )
            first_fields, other_fields, clash = voltage.reconcile(
                first_path, first_fields, path, other_fields, file_header, text
            )
            mismatch = clash or header_mismatch(
                first_path, first_fields, texts[0], path, other_fields, text
            )
            if mismatch:
                raise LibertyMergeError(mismatch + context)
        _union_templates(
            file_templates, path, first_path, templates, extra, ambiguous_axes
        )
        for cell in cells:
            if cell in seen:
                raise LibertyMergeError(
                    f"duplicate cell '{cell}' in '{path}' (already defined in "
                    f"'{seen[cell]}')"
                )
            seen[cell] = path
        bodies.append(body)
    if dangling := voltage.dangling():
        raise LibertyMergeError(dangling)
    extra.extend(voltage.extra)
    if ambiguous_axes:
        for path, text in zip(paths, texts, strict=True):
            if inherited := inherited_table_axis(text, ambiguous_axes):
                name, axis = inherited
                raise LibertyMergeError(
                    f"Liberty template '{name}' differs across members and a table "
                    f"in '{path}' inherits {axis} (cannot merge cell groups)"
                )
    return header, extra, bodies


def _union_templates(
    file_templates: list[tuple[str, str]],
    path: str,
    first_path: str,
    templates: dict[str, tuple[str, str, str]],
    extra: list[str],
    ambiguous_axes: dict[str, set[str]],
) -> None:
    """Keep first definitions and record axes requiring local values in every member."""
    for name, raw in file_templates:
        tnorm = _normalize_header(raw)
        known = templates.get(name)
        if known is None:
            templates[name] = (tnorm, raw, path)
            if path is not first_path:
                extra.append(raw)
        elif known[0] != tnorm:
            shape, axes = _template_shape(raw)
            if not axes or (shape, axes) != _template_shape(known[1]):
                raise LibertyMergeError(
                    f"Liberty template '{name}' differs between '{known[2]}' "
                    f"and '{path}' (cannot merge cell groups)"
                )
            ambiguous_axes[name] = axes


def _header_fields(path: str, header: str) -> dict[str, str]:
    """Depth-1 fields of a template-stripped header, keyed by name.

    ``attr : value ;`` statements map to their normalised value (quoted
    strings kept verbatim), ``name (args) { ... }`` groups to their
    normalised body under a ``name(args)`` key, ``name (args) ;`` complex
    attributes to their normalised args. Attributes in
    :data:`_IGNORED_HEADER_ATTRS` are dropped; anything unrecognised is
    carried under :data:`_RESIDUE_KEY` so it still takes part in the
    comparison.

    Raises :class:`LibertyMergeError` when the header has statements yet
    yields no field at all (only ignored attributes, or text the parser
    swallowed): an empty map would compare equal to every other empty map, so
    the comparator has understood nothing and must refuse instead.
    """
    fields: dict[str, str] = {}
    groups: dict[str, list[str]] = {}
    statements = 0
    cursor = depth = 0
    for kind, pos, end in _tokens(header):
        if pos < cursor:
            continue  # inside a statement already consumed below
        if kind == "{":
            depth += 1
            if depth == 1:
                cursor = end  # the 'library (<name>) {' opener
            continue
        if kind == "}":
            depth -= 1
            continue
        if depth != 1:
            continue
        if cursor < pos:
            _add_residue(header[cursor:pos], fields)
        taken = _take_field(header, pos, end, cursor, fields, groups)
        statements += taken != cursor
        cursor = taken
    _add_residue(header[cursor:], fields)
    fields.update(_group_fields(groups))
    if statements and not fields:
        ignored = ", ".join(sorted(_IGNORED_HEADER_ATTRS))
        raise LibertyMergeError(
            f"cannot merge: no comparable header field found in '{path}' "
            f"(only ignored {ignored} statements, or a header that could not "
            "be parsed)"
        )
    return fields


def _group_fields(groups: dict[str, list[str]]) -> dict[str, str]:
    """Compare all normalized bodies for a group without imposing declaration order."""
    fields = {}
    for key, bodies in groups.items():
        first, *additional = sorted(bodies)
        fields[key] = first
        for occurrence, body in enumerate(additional, start=2):
            fields[f"{key} [occurrence {occurrence}]"] = body
    return fields


def _take_field(
    header: str,
    pos: int,
    end: int,
    cursor: int,
    fields: dict[str, str],
    groups: dict[str, list[str]],
) -> int:
    """Consume the depth-1 statement starting at the identifier ``pos`` and
    return the new cursor (the old one when unrecognised, so the text rides
    in the next residue flush instead of being silently skipped)."""
    name = header[pos:end]
    n = len(header)
    if attr := _colon_attr(header, end):
        # A terminator scan, not a bracket match: a second unquoted ':' in the
        # value (``date : $Date: N $;``) must not look like a nested opener.
        if name not in _IGNORED_HEADER_ATTRS:
            fields[name] = _normalize_header(header[attr[0] + 1 : attr[1]])
        return min(attr[1] + 1, n)
    nxt, ch = _next_significant(header, end)
    if ch == "(":
        close = _find_matching(header, nxt, "(", ")")
        args = _normalize_header(header[nxt + 1 : close])
        name = f"{name}({args})"
        nxt, ch = _next_significant(header, close + 1)
        if ch != "{":
            fields[name] = args  # complex attribute 'name (v1, v2) ;'
            return min(nxt + 1, n)
    if ch == "{":
        bend = _find_matching(header, nxt, "{", "}")
        body = _normalize_header(header[nxt + 1 : min(bend, n)])
        fields[name] = body  # retain the field's source position in diagnostics
        groups.setdefault(name, []).append(body)
        return min(bend + 1, n)
    return cursor


def _add_residue(text: str, fields: dict[str, str]) -> None:
    """Carry unrecognised header text into the field map (fail closed)."""
    norm = _normalize_header(text)
    if not norm:
        return
    prior = fields.get(_RESIDUE_KEY)
    fields[_RESIDUE_KEY] = f"{prior} {norm}" if prior else norm


def _local_condition(fields: dict[str, str]) -> tuple[str, str, str] | None:
    """``(group key, name, body)`` when the header declares exactly one
    ``operating_conditions`` group whose name the
    ``default_operating_conditions`` attribute selects; ``None`` otherwise
    (no group, several groups, or a default pointing elsewhere -- none of
    those is an unambiguous local rename, so the normal field comparison
    decides)."""
    groups = {k: v for k, v in fields.items() if k.startswith("operating_conditions(")}
    if len(groups) != 1:
        return None
    key, body = next(iter(groups.items()))
    name = _unquote(key[len("operating_conditions(") : -1])
    if not name or name != _unquote(fields.get("default_operating_conditions", "")):
        return None
    return key, name, body


def _references_condition(text: str, name: str) -> bool:
    """Look for the discarded condition in retained Liberty references."""
    value_attrs = {
        "related_condition",
        "default_operating_conditions",
        "operating_conditions",
    }
    if _find_attr_reference(text, value_attrs, {name}) is not None:
        return True
    if "scaled_cell" not in text:
        return False
    for kind, start, end in _tokens(text):
        if kind != "ident" or text[start:end] != "scaled_cell":
            continue
        pos, char = _next_significant(text, end)
        if char != "(":
            continue
        stop = _find_matching(text, pos, "(", ")")
        args = _normalize_header(text[pos + 1 : stop])
        # scaled_cell(existing_cell, operating_conditions_group)
        match = re.fullmatch(r'(?:"(?:[^"\\]|\\.)*"|[^",]+)\s*,\s*(.+)', args)
        if match and _unquote(match[1]) == name:
            return True
    return False


def _reconcile_conditions(
    first_path: str,
    first: dict[str, str],
    path: str,
    other: dict[str, str],
    retained: str,
    *,
    single_condition: bool,
) -> tuple[dict[str, str], dict[str, str], str]:
    """The header field maps with the operating-condition pair removed when
    the two headers differ only by its local name (issue #6940).

    Each header must declare exactly one ``operating_conditions`` group its
    own default selects and the group bodies must agree; the merged header
    then keeps the first file's spelling, so the second file must not
    reference its local name in ``retained`` (its body and template text --
    a reference would dangle in the output). Anything else about the pair
    (content, count, a foreign or missing default) is left in place for the
    normal field-by-field comparison to refuse. The last return value adds
    context when the retained text refers to a discarded name."""
    if not single_condition:
        return first, other, ""
    a = _local_condition(first)
    b = _local_condition(other)
    if a is None or b is None or a[1] == b[1] or a[2] != b[2]:
        return first, other, ""
    if _references_condition(retained, b[1]):
        return (
            first,
            other,
            f"\n'{path}' references operating condition '{b[1]}' "
            f"outside its header, but the merged header keeps only "
            f"'{first_path}' spelling '{a[1]}'",
        )
    drop = {a[0], b[0], "default_operating_conditions"}
    return _without(first, drop), _without(other, drop), ""


def _extract_templates(header: str) -> tuple[str, list[tuple[str, str]]]:
    """``(header without template groups, [(name, group text), ...])``."""
    stripped, last = [], 0
    found = []
    for _, name, start, stop in _depth1_groups(header, _TEMPLATE_KINDS):
        found.append((name, header[start:stop]))
        stripped.append(header[last:start])
        last = stop
    stripped.append(header[last:])
    return " ".join(stripped), found


def _write_atomic(out_path: str, merged: str) -> None:
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(out_path), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(merged)
        os.replace(tmp, out_path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def _read(path: str) -> str:
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            return handle.read()
    except OSError as exc:
        raise LibertyMergeError(f"cannot read Liberty file '{path}': {exc}") from exc


def _split(path: str, text: str) -> tuple[str, str, list[str], int]:
    """Split a library and report its cell names and condition count.

    ``body`` runs from the first top-level ``cell (`` to just before the
    library's closing brace.
    """
    first_ident = next((text[p:e] for k, p, e in _tokens(text) if k == "ident"), None)
    first_cell, cells, close, conditions = _scan_cells(text)
    if close is None or first_ident != "library":
        raise LibertyMergeError(f"'{path}' is not a complete Liberty library group")
    if first_cell is None:
        raise LibertyMergeError(f"'{path}' defines no cells")
    return text[:first_cell], text[first_cell:close], cells, conditions
