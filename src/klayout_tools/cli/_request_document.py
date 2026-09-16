"""Shared helpers for the optional request-document input form ``klt drc``
and ``klt extract`` accept (issue #1867).

``klt synthesize``/``klt place-and-route``/``klt sta``/``klt lvs`` all take
their inputs as a committed, diffable, content-hashable JSON *request
document*. ``klt drc`` and ``klt extract`` were the two physical-flow stages
that did not, so a caller who wanted the whole flow expressed as data had to
invent a repo-local JSON shape plus an argv translator for exactly those two
stages. They now accept a request document too.

The three-form dispatch itself (file path / ``-`` for stdin / inline JSON
object string) is *not* reimplemented here -- ``drc.py``'s and
``extract.py``'s own ``load_request_arg`` delegate to the shared
:mod:`klayout_tools._paths` helpers, exactly as ``lvs.py``'s does. What lives
here is the layer above: turning a loaded request dict into the same values
the command's own argv flags would have produced, so both input forms run
through one code path and cannot drift.

**Precedence rule: mutually exclusive, not "argv wins".** A request document
and the command's own input flags may not be combined -- passing both is a
clean application error (exit 1). The point of the document is to *be* the
stage's inputs: one committed file whose content hash pins exactly what a
``status: clean`` verdict covered. A flag that silently overrode a field
would make the document no longer authoritative, and no longer safe to cite
from an evidence record by hash. Output-shaping flags (``--format``) and the
separate ``--check``/``--rerun`` re-verification mode are unaffected --
neither is an input to the run.

Private to the :mod:`klayout_tools.cli` package -- not part of the public
``klayout_tools`` API.
"""

from __future__ import annotations

from typing import Any

from .._paths import _resolve_relative

ErrorFactory = type[Exception]


def _type_error(
    verb: str, key: str, expected: str, value: Any, error_cls: ErrorFactory
) -> Exception:
    return error_cls(
        f"`klt {verb}` request field {key!r} must be {expected} (got "
        f"{type(value).__name__}: {value!r})"
    )


def check_schema(
    request: dict[str, Any],
    *,
    expected: str,
    verb: str,
    error_cls: ErrorFactory,
) -> None:
    """Validate the optional ``schema`` field.

    The field is optional (matching ``klt lvs``/``klt sim``'s convention for
    user-authored input), but a document that *declares* a contract
    identifier and names a different one is rejected rather than silently
    interpreted under this build's schema.
    """
    schema = request.get("schema")
    if schema is None:
        return
    if not isinstance(schema, str):
        raise _type_error(verb, "schema", "a string", schema, error_cls)
    if schema != expected:
        raise error_cls(
            f"`klt {verb}` request declares schema {schema!r}, but this build "
            f"reads {expected!r} -- the field is optional; omit it or set it "
            "to the supported value"
        )


def check_known_fields(
    request: dict[str, Any],
    known: tuple[str, ...],
    *,
    verb: str,
    error_cls: ErrorFactory,
) -> None:
    """Reject unknown top-level fields.

    A request document mirrors the command's flag surface one-for-one, so an
    unrecognised key is almost always a typo or a field from a newer build --
    either way, silently ignoring it would drop an input the caller believed
    was applied, which is precisely what the hashable-request contract exists
    to prevent.
    """
    unknown = sorted(key for key in request if key != "schema" and key not in known)
    if unknown:
        raise error_cls(
            f"unknown field(s) in `klt {verb}` request document: "
            f"{', '.join(unknown)} -- known fields: schema, "
            f"{', '.join(sorted(known))}"
        )


def reject_argv_flags(
    args: Any,
    *,
    verb: str,
    error_cls: ErrorFactory,
) -> None:
    """Enforce this module's stated precedence rule: a request document is
    mutually exclusive with the command's own input flags.

    The flag table is recorded on the namespace by ``cli/parser.py``
    (``request_argv_flags``: ``{dest: (option_string, default)}``, derived
    from the subparser's own actions so a newly-added flag is covered without
    anyone remembering to update a list here). A flag left at its parser
    default is indistinguishable from an omitted one, so re-passing a flag at
    its own default value alongside a request document is accepted and has no
    effect; any flag carrying a non-default value is an error.
    """
    flags: dict[str, tuple[str, Any]] = getattr(args, "request_argv_flags", None) or {}
    given = sorted(
        option
        for dest, (option, default) in flags.items()
        if getattr(args, dest, default) != default
    )
    if given:
        raise error_cls(
            f"`klt {verb}` request document is mutually exclusive with this "
            f"command's own input flags, but {', '.join(given)} "
            f"{'were' if len(given) > 1 else 'was'} also given -- move the "
            "value(s) into the request document instead (see "
            f"docs/cli/{verb}.md, 'Request document')"
        )


def get_str(
    request: dict[str, Any], key: str, *, verb: str, error_cls: ErrorFactory
) -> str | None:
    """``request[key]`` as a string, or ``None`` when absent/``null``."""
    value = request.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise _type_error(verb, key, "a string", value, error_cls)
    return value


def get_path(
    request: dict[str, Any],
    key: str,
    *,
    base_dir: str,
    verb: str,
    error_cls: ErrorFactory,
) -> str | None:
    """``request[key]`` as a path resolved against ``base_dir``.

    Matches ``klt lvs``'s convention exactly: ``~``/``$VAR`` are expanded and
    a relative path is anchored at the request document's own directory (or
    the current working directory for the stdin/inline forms).
    """
    value = get_str(request, key, verb=verb, error_cls=error_cls)
    if value is None:
        return None
    return _resolve_relative(value, base_dir)


def get_bool(
    request: dict[str, Any], key: str, *, verb: str, error_cls: ErrorFactory
) -> bool:
    """``request[key]`` as a bool -- ``False`` when absent/``null``, matching
    the ``store_true`` flag it mirrors."""
    value = request.get(key)
    if value is None:
        return False
    if not isinstance(value, bool):
        raise _type_error(verb, key, "a boolean", value, error_cls)
    return value


def get_number(
    request: dict[str, Any], key: str, *, verb: str, error_cls: ErrorFactory
) -> float | None:
    """``request[key]`` as a float, or ``None`` when absent/``null``."""
    value = request.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _type_error(verb, key, "a number", value, error_cls)
    return float(value)


def get_str_list(
    request: dict[str, Any],
    key: str,
    *,
    verb: str,
    error_cls: ErrorFactory,
    base_dir: str | None = None,
) -> list[str] | None:
    """``request[key]`` as a list of strings, or ``None`` when absent/``null``.

    A bare string is accepted as a one-element list, since the flags these
    fields mirror are repeatable and a single value is the common case (the
    issue's own sketch writes ``"abstract_cells": "sky130_fd_sc_hd__*"``).
    When ``base_dir`` is given each entry is resolved as a path.
    """
    value = request.get(key)
    if value is None:
        return None
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list) or not all(
        isinstance(entry, str) for entry in value
    ):
        raise _type_error(
            verb, key, "a string or an array of strings", value, error_cls
        )
    if base_dir is None:
        return list(value)
    return [_resolve_relative(entry, base_dir) for entry in value]


def get_comma_joined(
    request: dict[str, Any],
    key: str,
    *,
    verb: str,
    error_cls: ErrorFactory,
) -> str | None:
    """``request[key]`` as the comma-separated string its flag takes.

    Accepts either an array of names (the natural JSON shape) or the literal
    comma-separated string the flag itself takes. An array entry containing a
    comma is rejected rather than silently split into two names -- the flag's
    own encoding cannot represent it, so accepting it would mean the document
    and the flag form disagree about the same input.
    """
    value = request.get(key)
    if value is None:
        return None
    if isinstance(value, str):
        return value
    names = get_str_list(request, key, verb=verb, error_cls=error_cls)
    assert names is not None  # get_str_list only returns None for a None value
    for name in names:
        if "," in name:
            raise error_cls(
                f"`klt {verb}` request field {key!r} entry {name!r} contains a "
                "comma -- entries are joined into this command's own "
                "comma-separated flag value, which cannot represent one"
            )
    return ",".join(names)


def get_str_map_as_pairs(
    request: dict[str, Any],
    key: str,
    *,
    verb: str,
    flag: str,
    error_cls: ErrorFactory,
) -> list[str] | None:
    """``request[key]`` as the ``["NAME=VALUE", ...]`` list its repeatable
    flag accumulates, or ``None`` when absent/``null``.

    Values are stringified the way a shell would have written them (JSON
    ``true`` becomes ``"true"``, ``3`` becomes ``"3"``), so a deck variable
    can be written as a natural JSON boolean/number rather than a quoted
    string. A key containing ``=`` is rejected: the flag's own
    ``NAME=VALUE`` encoding cannot represent it.
    """
    value = request.get(key)
    if value is None:
        return None
    if not isinstance(value, dict):
        raise _type_error(verb, key, "a JSON object", value, error_cls)
    pairs: list[str] = []
    for name, raw in value.items():
        if "=" in name:
            raise error_cls(
                f"`klt {verb}` request field {key!r} key {name!r} contains a "
                f"'=' -- entries are passed through this command's own "
                f"{flag} NAME=VALUE encoding, which cannot represent one"
            )
        pairs.append(f"{name}={_scalar_to_flag_value(verb, key, raw, error_cls)}")
    return pairs


def _scalar_to_flag_value(
    verb: str, key: str, raw: Any, error_cls: ErrorFactory
) -> str:
    if isinstance(raw, bool):
        return "true" if raw else "false"
    if isinstance(raw, (int, float)):
        return str(raw)
    if isinstance(raw, str):
        return raw
    raise _type_error(
        verb, key, "a JSON object of string/number/boolean values", raw, error_cls
    )


def get_group_map_as_pairs(
    request: dict[str, Any],
    key: str,
    *,
    verb: str,
    flag: str,
    error_cls: ErrorFactory,
) -> list[str] | None:
    """``request[key]`` as the ``["NAME=A,B", ...]`` list its repeatable flag
    accumulates, or ``None`` when absent/``null``.

    Rejects a group name containing ``=`` and a member name containing ``,``
    for the same reason :func:`get_str_map_as_pairs` rejects a ``=`` in a
    key: the flag encoding cannot represent them, so accepting them would let
    the two input forms disagree.
    """
    value = request.get(key)
    if value is None:
        return None
    if not isinstance(value, dict):
        raise _type_error(
            verb, key, "a JSON object of name -> array-of-names", value, error_cls
        )
    pairs: list[str] = []
    for name, members in value.items():
        if "=" in name:
            raise error_cls(
                f"`klt {verb}` request field {key!r} group name {name!r} "
                f"contains a '=' -- entries are passed through this command's "
                f"own {flag} NAME=A,B encoding, which cannot represent one"
            )
        if isinstance(members, str):
            members = [members]
        if not isinstance(members, list) or not all(
            isinstance(member, str) for member in members
        ):
            raise _type_error(
                verb,
                f"{key}.{name}",
                "an array of strings",
                members,
                error_cls,
            )
        for member in members:
            if "," in member:
                raise error_cls(
                    f"`klt {verb}` request field {key!r} group {name!r} member "
                    f"{member!r} contains a comma -- entries are passed through "
                    f"this command's own {flag} NAME=A,B encoding, which cannot "
                    "represent one"
                )
        pairs.append(f"{name}={','.join(members)}")
    return pairs
