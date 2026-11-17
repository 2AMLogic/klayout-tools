"""`klt env-provenance`: the pseudonymous host id.

Split out of `tests/test_env_provenance.py` when that file reached its
per-file size ceiling (scripts/check_file_size_baseline.py, issue #2766);
the id's shape, stability, and never-fabricate rules live here.
"""

from __future__ import annotations

import re

from klayout_tools import env_provenance as ep

# --------------------------------------------------------------------------- #
# pseudonymous host id
# --------------------------------------------------------------------------- #

_HOST_ID_RE = re.compile(r"^host-[0-9a-f]{8}$")


def test_host_id_has_the_fleet_host_8hex_shape():
    assert _HOST_ID_RE.match(ep.opaque_host_id("robb-pro"))


def test_host_id_is_stable_for_the_same_hostname():
    assert ep.opaque_host_id("robb-pro") == ep.opaque_host_id("robb-pro")


def test_host_id_differs_between_hosts():
    assert ep.opaque_host_id("robb-pro") != ep.opaque_host_id("other-host")


def test_host_id_does_not_contain_the_hostname():
    assert "robb" not in ep.opaque_host_id("robb-pro")


def test_host_id_normalises_case_and_the_mdns_local_suffix():
    """macOS reports `gethostname()` inconsistently as `robb-pro` or
    `Robb-Pro.local` depending on the network; both are the same host and
    must pseudonymise to the same id, or the id is not stable."""
    canonical = ep.opaque_host_id("robb-pro")
    assert ep.opaque_host_id("Robb-Pro.local") == canonical
    assert ep.opaque_host_id("robb-pro.") == canonical


def test_host_id_salt_changes_the_id():
    assert ep.opaque_host_id("robb-pro", salt="a") != ep.opaque_host_id(
        "robb-pro", salt="b"
    )


def test_host_id_salt_env_var_overrides_the_default(monkeypatch):
    default = ep.opaque_host_id("robb-pro")
    monkeypatch.setenv(ep.HOST_ID_SALT_ENV_VAR, "project-specific-salt")
    assert ep.opaque_host_id("robb-pro") != default


def test_host_id_defaults_to_this_hosts_own_name(monkeypatch):
    monkeypatch.setattr(ep.socket, "gethostname", lambda: "robb-pro")
    assert ep.opaque_host_id() == ep.opaque_host_id("robb-pro")


def test_unresolvable_hostname_is_reported_as_unknown_not_fabricated(monkeypatch):
    def _boom():
        raise OSError("no hostname")

    monkeypatch.setattr(ep.socket, "gethostname", _boom)
    assert ep.opaque_host_id() == ep.UNKNOWN_HOST_ID


def test_hostname_that_normalises_to_empty_is_unknown_not_fabricated():
    """A hostname that is only dots (`"."`, `".local"`, or whitespace)
    normalises to the empty string; there is no host name to hash, so the id
    is UNKNOWN_HOST_ID rather than a hash of the salt alone."""
    assert ep.opaque_host_id(".") == ep.UNKNOWN_HOST_ID
    assert ep.opaque_host_id(".local") == ep.UNKNOWN_HOST_ID
    assert ep.opaque_host_id("   ") == ep.UNKNOWN_HOST_ID
