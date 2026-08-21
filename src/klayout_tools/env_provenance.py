"""Committable environment provenance for evidence records (issue #1254).

An evidence record (``docs/design/sim-evidence-discipline-spike.md``) is
published, permanent, and append-only: a record that names the machine that
produced it names it forever, in public, and cannot be scrubbed without
breaking the record-ID/commit-SHA chain that makes the evidence checkable in
the first place. A 2026-08 disclosure audit of the public canary repos found
~3,937 committed records carrying three identifier classes written *by
design* by each canary's own harness:

    - PDK: volare `gf180mcuD`, open_pdks `c6d73a3`
      (/Users/<author>/.volare/gf180mcuD, found via search_root:~/.volare)
    - Host: macOS-26.6.1-arm64-arm-64bit-Mach-O (<hostname>)

-- an absolute home-directory path, the dispatch host's name, and (elsewhere)
the author's login. This module is the reference emitter those harnesses call
instead of collecting that themselves, so the hygiene rule stated in
``docs/design-evidence-tiers.md`` ("Provenance hygiene in evidence records")
has one implementation to converge on rather than N drifting copies:

- **Repo-relative paths only.** :func:`repo_relative_path` reports a path
  inside the repo relative to its root, and reports a path *outside* it as
  ``{"path": null, "scope": "external"}`` -- never as an absolute path. A
  PDK's identity is its name/version/content hash (the shared
  :mod:`._provenance` block already records those), not where it happens to
  be installed on one machine.
- **A stable pseudonymous host id.** :func:`opaque_host_id` reports
  ``host-<8hex>``, a salted hash of the normalised hostname -- the same
  *shape* the Loom fleet's own lease records use for host identity. Two runs
  on one machine correlate; the machine is not named.
- **No login/author field.** Nothing here reads (or emits) the user's login,
  full name, or home directory. Git already records authorship, once, in a
  place a reader expects to find it.

The guarantee is mechanical, not advisory: :func:`environment_provenance`
runs its own finished payload through :func:`find_leaks` before returning it
and raises :class:`EnvironmentProvenanceError` rather than hand a caller a
record that leaks. :func:`find_leaks` is exported for the same reason -- a
repo can run it over newly-added record files in CI (``klt env-provenance
scan``) and catch a regression at PR time.

Adopting this from a harness is two lines::

    from klayout_tools.env_provenance import environment_provenance, render_text_lines

    env = environment_provenance(paths={"pdk": pdk_root, "netlist": netlist})
    body.extend(render_text_lines(env))

Deliberately *not* in scope: rewriting records that already leak. Record IDs
embed commit SHAs, and a rewrite breaks the verifiability that is the reason
the evidence is published at all. The fix belongs at the writer.
"""

from __future__ import annotations

import hashlib
import os
import platform
import re
import socket
from collections.abc import Iterable, Mapping
from typing import Any

__all__ = [
    "DEFAULT_HOST_ID_SALT",
    "HOST_ID_PREFIX",
    "HOST_ID_SALT_ENV_VAR",
    "LEAKS_FOUND_EXIT_CODE",
    "SCHEMA_VERSION",
    "UNKNOWN_HOST_ID",
    "EnvironmentProvenanceError",
    "environment_provenance",
    "find_leaks",
    "find_repo_root",
    "opaque_host_id",
    "render_path_field",
    "render_text_lines",
    "repo_relative_path",
    "scan_files",
]

#: ``klt env-provenance``'s own payload version (``docs/json-contract.md``).
SCHEMA_VERSION = 1

#: Exit code for ``klt env-provenance scan`` when the scan ran fine and found
#: leaks -- a successful run with findings, mirroring ``klt drc``'s exit ``3``
#: for "the deck ran and found violations" (``docs/json-contract.md``).
LEAKS_FOUND_EXIT_CODE = 3

#: Prefix of the pseudonymous host id, matching the ``host-<8hex>`` shape the
#: Loom fleet's lease records already use for opaque host identity.
HOST_ID_PREFIX = "host-"

#: Hex digits of hash kept in a host id. Eight is the fleet's existing width:
#: enough to separate the machines in a fleet, short enough to read.
_HOST_ID_HEX_DIGITS = 8

#: Salt mixed into the host hash by default. A fixed salt makes the id
#: *pseudonymous*, not anonymous -- the hostname space is small enough to
#: enumerate, so treat the id as "does not name the host in the record", not
#: as "cannot be recovered by a determined reader". Set
#: :data:`HOST_ID_SALT_ENV_VAR` to a project-held value for an id that is
#: also unlinkable across projects.
DEFAULT_HOST_ID_SALT = "klayout-tools/env-provenance/1"

#: Environment variable overriding :data:`DEFAULT_HOST_ID_SALT`.
HOST_ID_SALT_ENV_VAR = "KLT_HOST_ID_SALT"

#: Host id reported when the hostname cannot be resolved at all. Distinct
#: from a real id and never fabricated -- the envelope convention for an
#: unresolvable value (``docs/json-contract.md``).
UNKNOWN_HOST_ID = f"{HOST_ID_PREFIX}unknown"

#: Shortest identifier the leak scan will match. A one- or two-character
#: login (``ci``, ``rw``) matches everywhere and would make the scan useless.
_MIN_IDENTIFIER_LENGTH = 3

#: POSIX home-directory-shaped absolute paths: ``/Users/<name>`` (macOS) and
#: ``/home/<name>`` (Linux, including CI runners' ``/home/runner``). The
#: user-name segment must be a plausible name -- a documentation placeholder
#: like ``/Users/<author>`` deliberately does not match.
_POSIX_HOME_RE = re.compile(r"(?<![\w./~-])/(?:Users|home)/[A-Za-z0-9._-]+(?:/\S*)?")

#: The Windows equivalent, in either separator style.
_WINDOWS_HOME_RE = re.compile(
    r"(?<![\w.-])[A-Za-z]:[\\/]Users[\\/][A-Za-z0-9._-]+(?:[\\/]\S*)?"
)


class EnvironmentProvenanceError(Exception):
    """Raised when a record cannot be emitted (or scanned) safely."""


# --------------------------------------------------------------------------- #
# pseudonymous host id
# --------------------------------------------------------------------------- #


def _normalise_hostname(raw: str) -> str:
    """Canonicalise a hostname so one machine always hashes to one id.

    macOS reports ``gethostname()`` as ``robb-pro`` or ``Robb-Pro.local``
    depending on whether mDNS has claimed the name; both are the same host,
    and an id that flips between them is not stable. Lower-cases, drops a
    trailing FQDN dot, and drops a trailing ``.local`` label. Other domain
    labels are kept -- two hosts with the same short name in different
    domains are different machines.
    """
    name = raw.strip().lower().rstrip(".")
    if name.endswith(".local"):
        name = name[: -len(".local")]
    return name


def opaque_host_id(hostname: str | None = None, *, salt: str | None = None) -> str:
    """The pseudonymous ``host-<8hex>`` id for ``hostname``.

    ``hostname`` defaults to this machine's own (``socket.gethostname()``);
    an unresolvable one is reported as :data:`UNKNOWN_HOST_ID`, never
    fabricated. ``salt`` defaults to ``$KLT_HOST_ID_SALT`` if set, else
    :data:`DEFAULT_HOST_ID_SALT` -- see that constant on what the id does and
    does not promise.
    """
    if hostname is None:
        try:
            hostname = socket.gethostname()
        except OSError:
            return UNKNOWN_HOST_ID
    normalised = _normalise_hostname(hostname or "")
    if not normalised:
        return UNKNOWN_HOST_ID
    if salt is None:
        salt = os.environ.get(HOST_ID_SALT_ENV_VAR) or DEFAULT_HOST_ID_SALT
    digest = hashlib.sha256(f"{salt}\0{normalised}".encode()).hexdigest()
    return f"{HOST_ID_PREFIX}{digest[:_HOST_ID_HEX_DIGITS]}"


# --------------------------------------------------------------------------- #
# repo-relative paths
# --------------------------------------------------------------------------- #


def find_repo_root(start: str | os.PathLike[str] | None = None) -> str | None:
    """The nearest ancestor of ``start`` (default: the process's cwd) holding
    a ``.git`` entry, or ``None`` if there is none.

    A ``.git`` *file* counts as well as a directory, so this resolves
    correctly from inside a linked git worktree. No subprocess is spawned --
    a record writer runs this per record, and shelling out to git would be
    both slower and dependent on git being installed.
    """
    current = os.path.realpath(str(start) if start is not None else os.getcwd())
    while True:
        if os.path.exists(os.path.join(current, ".git")):
            return current
        parent = os.path.dirname(current)
        if parent == current:
            return None
        current = parent


def repo_relative_path(
    path: str | os.PathLike[str] | None,
    *,
    repo_root: str | os.PathLike[str] | None = None,
) -> dict[str, Any]:
    """Normalise one path into the committable ``{path, scope}`` shape.

    - ``{"path": "<repo-relative POSIX path>", "scope": "repo"}`` when ``path``
      is inside ``repo_root`` (the repo root itself is ``"."``).
    - ``{"path": null, "scope": "external"}`` when it is outside -- the
      absolute path is *never* emitted. This is the case that used to leak:
      a PDK under the author's home directory. Pin such an input by identity
      (name/version/content hash), not by location.
    - ``{"path": null, "scope": "absent"}`` when ``path`` is ``None``, so
      "not resolved" stays distinguishable from "outside the repo".

    ``repo_root`` defaults to :func:`find_repo_root`'s answer for ``path``'s
    own directory. With no repo root at all, every path is ``external``: the
    failure mode is losing detail, never leaking it.
    """
    if path is None:
        return {"path": None, "scope": "absent"}

    resolved = os.path.realpath(str(path))
    root = (
        repo_root
        if repo_root is not None
        else find_repo_root(os.path.dirname(resolved))
    )
    if root is None:
        return {"path": None, "scope": "external"}

    root_resolved = os.path.realpath(str(root))
    if resolved == root_resolved:
        return {"path": ".", "scope": "repo"}
    # `startswith` on the root *plus a separator* -- `/x/canary-secrets` must
    # not be read as living inside `/x/canary`.
    if not resolved.startswith(root_resolved.rstrip(os.sep) + os.sep):
        return {"path": None, "scope": "external"}

    relative = os.path.relpath(resolved, root_resolved)
    return {"path": relative.replace(os.sep, "/"), "scope": "repo"}


# --------------------------------------------------------------------------- #
# the leak scan
# --------------------------------------------------------------------------- #


def find_leaks(
    text: str, *, extra_identifiers: Iterable[str] = ()
) -> list[dict[str, Any]]:
    """Every identifier-shaped run of text in ``text``, as
    ``{kind, match, line}`` dicts (empty list when clean).

    Two kinds are reported:

    - ``"home-path"`` -- a home-directory-shaped absolute path
      (``/Users/<name>/...``, ``/home/<name>/...``, ``C:\\Users\\<name>\\...``).
      Pattern-based, so it works on a CI runner that shares nothing with the
      machine that wrote the record. A ``~/``-rooted path is *not* flagged:
      it names no user.
    - ``"identifier"`` -- one of ``extra_identifiers`` (a hostname, a login, a
      home directory) appearing verbatim. Matched case-insensitively, on word
      boundaries for bare names, as a substring for anything path-shaped.
      Identifiers shorter than three characters are ignored.

    The returned ``match`` contains the leaking text itself -- that is what
    makes a finding actionable, and is also why scan *output* should not be
    committed to the repo it scanned.
    """
    leaks: list[dict[str, Any]] = []
    patterns: list[tuple[str, re.Pattern[str]]] = [
        ("home-path", _POSIX_HOME_RE),
        ("home-path", _WINDOWS_HOME_RE),
    ]
    for identifier in extra_identifiers:
        if not identifier or len(identifier) < _MIN_IDENTIFIER_LENGTH:
            continue
        quoted = re.escape(identifier)
        if os.sep in identifier or "/" in identifier:
            pattern = re.compile(quoted, re.IGNORECASE)
        else:
            # Asymmetric boundaries on purpose: a hyphen *before* the
            # identifier does not make it a different word (`26.6.1-robb-pro`
            # is a genuine leak of `robb-pro`), while a hyphen *after* it may
            # (`robb-prometheus` is a different name).
            pattern = re.compile(rf"(?<!\w){quoted}(?![\w-])", re.IGNORECASE)
        patterns.append(("identifier", pattern))

    for line_number, line in enumerate(text.splitlines(), start=1):
        for kind, pattern in patterns:
            for match in pattern.finditer(line):
                leaks.append(
                    {"kind": kind, "match": match.group(0), "line": line_number}
                )
    return leaks


def scan_files(paths: Iterable[str]) -> dict[str, Any]:
    """Run :func:`find_leaks` over each file in ``paths``.

    Returns ``{schema_version, status, leak_count, files[]}`` where ``status``
    is ``"clean"`` or ``"leaked"`` and each ``files[]`` entry is
    ``{file, leaks[]}``. Raises :class:`EnvironmentProvenanceError` for a file
    that cannot be read -- an unreadable file is an error, never a silent
    "clean" verdict.
    """
    entries: list[dict[str, Any]] = []
    total = 0
    for path in paths:
        try:
            with open(path, encoding="utf-8", errors="replace") as handle:
                text = handle.read()
        except OSError as exc:
            raise EnvironmentProvenanceError(f"could not read {path}: {exc}") from exc
        leaks = find_leaks(text)
        total += len(leaks)
        entries.append({"file": str(path), "leaks": leaks})

    return {
        "schema_version": SCHEMA_VERSION,
        "status": "leaked" if total else "clean",
        "leak_count": total,
        "files": entries,
    }


# --------------------------------------------------------------------------- #
# the emitted record
# --------------------------------------------------------------------------- #


def _local_identifiers() -> list[str]:
    """The identifiers this machine must never write into a record: its
    hostname (raw and normalised), the login, and the home directory. Read
    only to be checked *against*, never emitted."""
    identifiers: list[str] = []
    try:
        hostname = socket.gethostname()
    except OSError:
        hostname = ""
    if hostname:
        identifiers.extend([hostname, _normalise_hostname(hostname)])
    for var in ("USER", "LOGNAME", "USERNAME"):
        value = os.environ.get(var)
        if value:
            identifiers.append(value)
    home = os.path.expanduser("~")
    if home and home not in ("/", os.sep):
        identifiers.append(home)
    return [value for value in dict.fromkeys(identifiers) if value]


def environment_provenance(
    *,
    paths: Mapping[str, str | os.PathLike[str] | None] | None = None,
    repo_root: str | os.PathLike[str] | None = None,
    hostname: str | None = None,
    salt: str | None = None,
) -> dict[str, Any]:
    """Build the committable environment-provenance block for one record.

    ``paths`` maps a caller-chosen label (``"pdk"``, ``"netlist"``, ...) to a
    path to normalise via :func:`repo_relative_path`; ``repo_root`` defaults
    to :func:`find_repo_root` from the cwd. ``hostname``/``salt`` are for
    tests and for a caller that pins its own salt.

    Shape (every field either resolved or ``null``, never fabricated)::

        {
          "schema_version": 1,
          "host_id": "host-1f4c8a21",
          "os": {"system": "Darwin", "release": "25.6.0", "machine": "arm64"},
          "python_version": "3.12.4",
          "klt_version": "0.4.2",
          "klayout_version": "0.29.8",
          "paths": {"pdk": {"path": null, "scope": "external"}}
        }

    ``os`` keeps the kernel/arch identity a reproduction attempt actually
    needs; it is deliberately **not** ``platform.platform()``, whose string
    the audited harnesses concatenated the hostname onto.

    Raises :class:`EnvironmentProvenanceError` if the finished payload
    contains any local identifier or home-shaped path -- the mechanism that
    makes the hygiene rule enforceable rather than merely documented.
    """
    if repo_root is None:
        repo_root = find_repo_root()

    normalised_paths = {
        label: repo_relative_path(value, repo_root=repo_root)
        for label, value in (paths or {}).items()
    }

    report: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "host_id": opaque_host_id(hostname, salt=salt),
        "os": {
            "system": platform.system() or None,
            "release": platform.release() or None,
            "machine": platform.machine() or None,
        },
        "python_version": platform.python_version(),
        "klt_version": _klt_version(),
        "klayout_version": _klayout_version(),
        "paths": normalised_paths,
    }

    _assert_no_leaks(report)
    return report


def _klt_version() -> str | None:
    from ._provenance import _klt_version as resolve

    return resolve()


def _klayout_version() -> str | None:
    from ._provenance import _klayout_version as resolve

    return resolve()


def _string_values(value: Any) -> list[str]:
    """Every string leaf in a nested payload -- what the self-check scans."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, Mapping):
        found: list[str] = []
        for key, item in value.items():
            found.extend(_string_values(key))
            found.extend(_string_values(item))
        return found
    if isinstance(value, (list, tuple)):
        found = []
        for item in value:
            found.extend(_string_values(item))
        return found
    return []


def _assert_no_leaks(report: Mapping[str, Any]) -> None:
    identifiers = _local_identifiers()
    for value in _string_values(report):
        leaks = find_leaks(value, extra_identifiers=identifiers)
        if leaks:
            kinds = ", ".join(sorted({leak["kind"] for leak in leaks}))
            raise EnvironmentProvenanceError(
                "refusing to emit environment provenance: a collected value "
                f"carries a local identifier ({kinds}). This is a bug in the "
                "collection, not in the record -- fix the source of the value "
                "rather than committing it. See docs/cli/env-provenance.md."
            )


def render_path_field(entry: Mapping[str, Any] | None) -> str:
    """Human-readable rendering of one :func:`repo_relative_path` entry.

    Shared by :func:`render_text_lines` and any other ``--format text``
    renderer that reports a normalised ``{path, scope}`` input field (e.g.
    ``klt pex``/``klt sim``'s own ``layout``/``netlist``/``reference_netlist``/
    ``request``/``schematic_netlist``/``checkpoint_path`` fields, issue
    #1261) -- ``"repo"`` renders the repo-relative path itself, ``"external"``
    renders ``<outside repo>`` (the absolute path is never rendered, even in
    text mode), and anything else (``"absent"``, or a missing/malformed
    entry) renders ``<unresolved>``.
    """
    if not entry:
        return "<unresolved>"
    scope = entry.get("scope")
    if scope == "repo":
        return str(entry.get("path"))
    if scope == "external":
        return "<outside repo>"
    return "<unresolved>"


def render_text_lines(report: Mapping[str, Any]) -> list[str]:
    """A human-readable rendering of :func:`environment_provenance`'s payload,
    one line per fact, for a harness that writes Markdown/plain-text records.

    Courtesy rendering, not the contract (``docs/json-contract.md``) -- but
    shared, so every harness's record reads the same and no harness re-derives
    (and re-leaks) the same facts on its own. External paths render as
    ``<outside repo>``; absent ones as ``<unresolved>``.
    """
    os_block = report.get("os") or {}
    os_parts = [
        str(os_block.get(key))
        for key in ("system", "machine")
        if os_block.get(key) is not None
    ]
    release = os_block.get("release")
    os_text = " ".join(os_parts) or "unknown"
    if release is not None:
        os_text = f"{os_text}, release {release}"

    lines = [
        f"host: {report.get('host_id')} ({os_text})",
        f"python: {report.get('python_version')}",
        f"klt: {report.get('klt_version')} (klayout {report.get('klayout_version')})",
    ]
    for label, entry in (report.get("paths") or {}).items():
        lines.append(f"path {label}: {render_path_field(entry)}")
    return lines
