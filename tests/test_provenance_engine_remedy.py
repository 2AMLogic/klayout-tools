"""Unit tests for the engine-mismatch warning's install remedy
(issue #5601), split out of `test_provenance.py` for its size-ratchet
ceiling: the warning renders the running build's real `reinstall_spec()`
value instead of the hardcoded `uv tool install "klayout-tools @
git+...@<sha>"` whose `...@<sha>` placeholder was copy-pasteable by no
one. The warning itself lives in `klayout_tools._engine_remedy`, split
from `_provenance.py` for the same ceiling.
"""

from __future__ import annotations

from klayout_tools import _provenance


def _capture_mismatch_warning(monkeypatch, capsys, reinstall_spec):
    """Drive `build_provenance` into the mismatch-warning path with a forced
    reinstall spec, returning the captured stderr. Patches build identity
    resolution (`build_version`/`_klt_build_handle`) so no git probe of the
    dev checkout runs -- mirroring
    `test_build_provenance_mismatch_check_does_not_shell_out`'s isolation --
    which keeps the no-subprocess guard below about the warning itself."""
    from klayout_tools import build_identity

    monkeypatch.setattr(_provenance, "_klayout_version", lambda: "0.30.12")
    monkeypatch.setattr(
        build_identity, "_recorded_klayout_version_expected", lambda: "0.30.10"
    )
    monkeypatch.setattr(build_identity, "build_version", lambda: "0.9.2")
    monkeypatch.setattr(
        _provenance, "_klt_build_handle", lambda: (None, reinstall_spec)
    )
    prov = _provenance.build_provenance(include_klayout_version_mismatch=True)
    assert prov["klayout_version_mismatch"] is True
    return capsys.readouterr()


def test_mismatch_warning_released_pin_form(monkeypatch, capsys):
    """A released install (`is_release is True`) renders the version-pin
    install form the issue asks for -- runnable as printed."""
    _provenance._warn_klayout_version_mismatch(
        "0.30.12", "0.30.10", "klayout-tools==0.9.2"
    )
    err = capsys.readouterr().err
    assert 'uv tool install "klayout-tools==0.9.2" --with klayout==0.30.10' in err
    assert "git+" not in err


def test_mismatch_warning_dev_build_renders_real_sha(monkeypatch, capsys):
    """A clean dev build renders a real `git+https://...@<commit>` -- the
    literal `...@<sha>` placeholder must never come back."""
    spec = (
        "klayout-tools @ git+https://github.com/2AMLogic/"
        "klayout-tools@4f1c8a9b2d3e5f60718293a4b5c6d7e8f9a0b1c2"
    )
    _provenance._warn_klayout_version_mismatch("0.30.12", "0.30.10", spec)
    err = capsys.readouterr().err
    assert f'uv tool install "{spec}" --with klayout==0.30.10' in err
    assert "...@<sha>" not in err


def test_mismatch_warning_remedy_has_no_placeholder(monkeypatch, capsys):
    """Guard: the remedy segment of the warning contains no `...` placeholder,
    so the hardcoded `git+...@<sha>` form cannot silently return. (The counts
    list earlier in the message legitimately contains `...`; only the
    install-command segment is checked.)"""
    for spec in (
        "klayout-tools==0.9.2",
        "klayout-tools @ git+https://github.com/2AMLogic/klayout-tools@abc123",
    ):
        _provenance._warn_klayout_version_mismatch("0.30.12", "0.30.10", spec)
        err = capsys.readouterr().err
        remedy = err.split("To reproduce the exact engine: ", 1)[1]
        assert "..." not in remedy, spec


def test_mismatch_warning_none_spec_no_fabricated_command(monkeypatch, capsys):
    """A `None` spec (dirty tree / no recorded commit) must never render as
    a fabricated (or literally `"None"`) command -- the warning says the
    build is not reinstallable and points at `klt version --format json`."""
    _provenance._warn_klayout_version_mismatch("0.30.12", "0.30.10", None)
    err = capsys.readouterr().err
    assert "uv tool install" not in err
    assert "None" not in err
    assert "not reinstallable" in err
    assert "klt version --format json" in err
    assert "--with klayout==0.30.10" in err


def test_build_provenance_warning_wiring_passes_reinstall_spec(monkeypatch, capsys):
    """End to end: the warning rendered from `build_provenance` uses the spec
    already computed for `provenance.klt_reinstall_spec` -- here the released
    form -- adding no re-derivation of its own."""
    captured = _capture_mismatch_warning(monkeypatch, capsys, "klayout-tools==0.9.2")
    err = captured.err
    assert 'uv tool install "klayout-tools==0.9.2" --with klayout==0.30.10' in err


def test_build_provenance_warning_emission_shells_out_nothing(monkeypatch, capsys):
    """Regression guard mirroring
    `test_build_provenance_mismatch_check_does_not_shell_out`, now covering
    the warning *emission* itself (issue #5601): rendering the remedy must
    not call `subprocess.run` -- the decision record
    (`docs/design/klayout-engine-version-pin.md`) forbids a git probe on this
    path, which is satisfied by reusing the spec `build_provenance` already
    computed rather than consulting `build_identity` again."""

    def _fail(*_args, **_kwargs):  # pragma: no cover - must not be reached
        raise AssertionError("emitting the mismatch warning shelled out")

    monkeypatch.setattr(_provenance.subprocess, "run", _fail)
    captured = _capture_mismatch_warning(monkeypatch, capsys, "klayout-tools==0.9.2")
    assert 'uv tool install "klayout-tools==0.9.2"' in captured.err
