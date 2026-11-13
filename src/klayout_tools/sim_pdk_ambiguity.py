"""Backend-aware PDK-ambiguity reporting for ``klt sim`` (issue #5449).

:func:`klayout_tools.pdk.ambiguity_warning`'s default remedy -- pin the root
with ``--pdk-root`` or a request's ``models.pdk_root`` -- is a **dead end on
``backend: batch``**. Both spellings land in the same field (``--pdk-root`` is
folded into ``models.pdk_root`` as an absolute path by
``sim_request_overrides._with_pdk_root_override``), and a host-local
``models.pdk_root`` is refused there before any S3 write with
``models_pdk_root_host_path`` (issue #4097): it is forwarded to the runner
verbatim, where a submitter's path does not exist. A submitter who took the
advice could not submit.

``$PDK_ROOT`` is the remedy that *does* work on that backend. It is the first
search candidate when ``models.pdk_root`` is unset (``pdk._candidate_roots``)
and is never forwarded to the runner (``sim_staging.host_resolved_roots``), so
it chooses which install this *client* resolves -- for ``models.lib``, for the
``environment.models_lib_sha256`` hash, and for ``provenance.pdk`` -- while the
runner keeps reading its own install, selected by ``models.pdk``.

Only the remedy half of each message changes, and only on ``batch``. The
hazard half (which roots were read and skipped) and
``provenance.pdk.ambiguous_sources`` are identical on every backend.
"""

from __future__ import annotations

import sys
from typing import Any

from .pdk import ambiguity_warning as pdk_ambiguity_warning

#: Replaces :data:`klayout_tools.pdk.AMBIGUITY_REMEDY` in the stderr warning
#: on ``backend: batch``.
BATCH_REMEDY = (
    "Resolution is first-match-wins, so a pinned open_pdks build may not be "
    "the one this client read -- set $PDK_ROOT to choose which install this "
    "client resolves (the only client-side pin backend 'batch' accepts); the "
    "runner reads its own install, selected by models.pdk."
)

#: The ``pdk: WARNING`` tail ``klt sim --format text`` prints off ``batch`` --
#: unchanged since issue #2564.
DEFAULT_TEXT_REMEDY = "pin --pdk-root (or models.pdk_root) to choose deliberately"

#: The ``pdk: WARNING`` tail on ``batch``: the short form of
#: :data:`BATCH_REMEDY`, matching the one-line style of the text renderer's
#: other PDK lines.
BATCH_TEXT_REMEDY = (
    "set $PDK_ROOT to choose which install this client resolves; "
    "the runner reads its own, selected by models.pdk"
)


def remedy_for(backend: str | None) -> str | None:
    """The ``remedy=`` argument for :func:`klayout_tools.pdk.ambiguity_warning`
    on ``backend``: ``None`` (the module default) off ``batch``."""
    return BATCH_REMEDY if backend == "batch" else None


def text_remedy_for(backend: str | None) -> str:
    """The ``pdk: WARNING`` tail for ``backend`` in ``--format text``."""
    return BATCH_TEXT_REMEDY if backend == "batch" else DEFAULT_TEXT_REMEDY


def warn_pdk_ambiguity(
    resolution: dict[str, Any] | None, *, backend: str | None = None
) -> None:
    """Print :func:`klayout_tools.pdk.ambiguity_warning` to stderr when the
    run's PDK root was resolved by *search* and more than one install on this
    host holds the requested variant (issue #2564).

    The run is still well-defined -- resolution is first-match-wins -- but a
    record can name a pinned ``open_pdks`` commit that a *different* build
    actually produced, and nothing else says so up front: the JSON's
    ``provenance.pdk.source`` is correct but purely after-the-fact, and a
    caller only finds it by inspecting provenance it had no reason to
    suspect. No-op for an unresolved PDK or an unambiguous one.

    ``backend`` selects the remedy sentence (see this module's docstring);
    everything but ``"batch"`` gets the default text unchanged. Pass the
    **resolved** backend -- the value the report records as
    ``environment.backend.resolved``, read after the last
    ``sim._yield_host_default_backend`` call, not the host default a
    single-unit run under ``$KLT_SIM_BACKEND=batch`` steps back from. A
    pre-yield value advises ``$PDK_ROOT`` on a run that executes locally,
    where it is neither the only pin nor the one that silences the warning,
    and contradicts the ``--format text`` line (which reads the report).
    """
    if not resolution:
        return
    warning = pdk_ambiguity_warning(resolution, remedy=remedy_for(backend))
    if warning is not None:
        print(warning, file=sys.stderr)
