"""The #7857 pinned-client remedy for a runner/client klt version skew.

Both mismatch messages -- the pre-launch assessment in
:mod:`klayout_tools.sim_batch` and the runner-side refusal diagnostic in
:mod:`klayout_tools.sim_batch_features` -- refused a client whose
major.minor differs from the image's without saying how to submit from a
matching one, which reads as a dead end to an agent pinned to an older klt
minor once the fleet image moves on. The sentence here names the image's
major.minor and the ``uvx --from`` wildcard pin (PEP 440) that runs the
same request from a client of it -- the form the worker host rules already
steer pinned-version runs to.
"""

from __future__ import annotations

from collections.abc import Iterable


def older_client_remedy(
    client_release: str, image_releases: Iterable[str | None]
) -> str:
    """The sentence appended to a mismatch message, or ``""`` when the
    client is not older than every published image.

    Non-empty only when the client's major.minor predates every entry of
    ``image_releases`` (a newer client, an unknown release or a
    mixed-direction fleet keeps the existing wording): it names the oldest
    published minor and its pinned-client command, because a client of that
    minor turns the any-image-match gate into a match. Inputs are
    :func:`klayout_tools.sim_batch_contract_spec._runner_gate_release`
    output, so every non-``None`` entry has at least a major and minor.
    """
    keys = sorted(
        tuple(int(part) for part in release.split("."))
        for release in image_releases
        if release is not None
    )
    if not keys or keys[0] <= tuple(int(part) for part in client_release.split(".")):
        return ""
    release = ".".join(str(part) for part in keys[0])
    return (
        "; the client is older than the image, so run the same request from "
        f"a klt {release} client via uvx --from "
        f'"klayout-tools=={release}.*" klt sim ...'
    )
