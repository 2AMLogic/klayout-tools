"""Flat staged-name allocation for an off-host deck job (issue #5539).

:func:`klayout_tools.sim_decks.stage_deck_job` turns a deck-list request's
planned units into the flat file list ``remote_transport``/``sim_batch``
transfer. This module owns the naming and dedup half of that job: which
flat name each file ships under, and the fact that a closure shared by
every unit of a grid is shipped **once**.

The staged name is internal -- the deck runner copies each file to its
declared in-unit destination before the engine runs -- so only transport
safety matters here, never what the engine sees.
"""

from __future__ import annotations

import os
from collections.abc import Iterable

from . import _provenance, sim_staging

#: Prefix of a staged name shared by more than one unit.
#: :func:`_alloc_staged_name` always starts a name with ``u<index>``, so
#: this prefix cannot collide with a per-unit name.
_SHARED_STAGED_PREFIX = "sha-"


def _alloc_staged_name(used: set[str], tag: str, kind: str, base: str) -> str:
    """A collision-free flat staged name: sanitized basename, prefixed with
    the unit's index and role so two units can never collide (only unit
    ``u<i>``'s own files share its prefix, and ``kind``+counter separates
    those)."""
    base = sim_staging._UNSAFE_NAME_CHARS.sub("_", base) or "file"
    candidate = f"{tag}-{kind}-{base}"
    counter = 1
    while candidate in used:
        candidate = f"{tag}-{kind}-{counter}-{base}"
        counter += 1
    used.add(candidate)
    return candidate


def _alloc_shared_staged_name(used: set[str], digest: str, base: str) -> str:
    """A collision-free flat staged name for bytes **shared** by more than
    one unit: :func:`_alloc_staged_name`'s shape with the content digest in
    place of the ``u<i>`` unit tag, so the name belongs to the content
    rather than to whichever unit happened to need it first.

    The digest prefix is already unique per content; the counter is kept
    only so two distinct digests sharing a 16-hex prefix could never be
    handed the same name (which would ship one file's bytes under the
    other's name).
    """
    base = sim_staging._UNSAFE_NAME_CHARS.sub("_", base) or "file"
    candidate = f"{_SHARED_STAGED_PREFIX}{digest[:16]}-{base}"
    counter = 1
    while candidate in used:
        candidate = f"{_SHARED_STAGED_PREFIX}{digest[:16]}-{counter}-{base}"
        counter += 1
    used.add(candidate)
    return candidate


class DeckStager:
    """The flat staged-file set one off-host deck job ships, deduplicated
    by content (issue #5539).

    A grid sweeps corners against one model set, so the *same* include /
    model closure is staged by every unit: N units x M identical includes
    used to ship ``N * M`` objects, one ``aws s3 cp`` or ``scp`` each.
    Files whose bytes are identical (sha256) get **one**
    :class:`sim_staging.StagedFile` and one flat name, which every unit
    that needs those bytes references -- so the same request ships
    ``N decks + M`` files.

    This is invisible below: each unit's own ``name`` (its in-unit
    destination, the only thing the engine ever sees) is untouched,
    because the deck runner *copies* -- never moves -- each staged file
    into the unit directory, and a unit's resume key hashes by
    unit-relative ``name``. Two files that share a basename but differ in
    bytes still get distinct staged names.

    One instance per job. ``reserved`` names are treated as already taken
    (the worker request document's own filename).
    """

    def __init__(self, *, reserved: Iterable[str] = ()) -> None:
        #: The staged files to transfer, in first-use order.
        self.files: list[sim_staging.StagedFile] = []
        self._used: set[str] = set(reserved)
        self._shared: dict[str, str] = {}
        self._digest_of_path: dict[str, str] = {}

    def stage(self, local_path: str, *, tag: str, kind: str, label: str) -> str:
        """The staged name ``local_path``'s bytes ship under, appending a
        :class:`sim_staging.StagedFile` the first time those bytes are
        seen and reusing the shared name afterwards.

        A shared file keeps the ``label`` of the first unit that needed it
        (a label only names a file in a transfer failure's message).
        ``tag``/``kind`` name the per-unit fallback form, used only when
        the source cannot be hashed -- which cannot happen after
        ``plan_deck_units``' existence check, but the digest helper is
        defensive, so the fallback is the pre-#5539 behaviour.
        """
        digest = self._digest(local_path)
        if digest is not None and digest in self._shared:
            return self._shared[digest]
        base = os.path.basename(local_path)
        if digest is None:
            staged = _alloc_staged_name(self._used, tag, kind, base)
        else:
            staged = _alloc_shared_staged_name(self._used, digest, base)
            self._shared[digest] = staged
        self.files.append(
            sim_staging.StagedFile(
                staged_name=staged, label=label, local_path=local_path
            )
        )
        return staged

    def _digest(self, local_path: str) -> str | None:
        """``local_path``'s content digest, read once per file however many
        units stage it (a grid stages one model set N times)."""
        resolved = os.path.realpath(local_path)
        digest = self._digest_of_path.get(resolved)
        if digest is None:
            digest = _provenance.sha256_file(local_path)
            if digest is not None:
                self._digest_of_path[resolved] = digest
        return digest
