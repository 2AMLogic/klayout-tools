r"""Live-``ngspice`` pins for the shared inline-comment rule (issue #5417).

``_spice_text._SPICE_INLINE_COMMENT`` documents what ngspice itself does
with the disputed ``$`` shapes and one deliberate divergence from it. A
claim about a simulator is only worth its evidence, so this file runs
``ngspice -b`` on the disputed cards and pins the observed behaviour (the
review of PR #5427 asked for exactly that, including the ``ngbehavior``
modes and ``.include $VAR/...``):

1. in the default mode, a ``$`` that begins a token on an element card
   starts a comment *whatever follows it* -- ``R1 a $1 1k`` is read as
   ``r1 a``;
2. that holds under every ``ngbehavior`` family *except* the PSpice ones:
   ``ps``/``psa`` keep ``$1`` as a node name, so on this shape the shared
   rule matches ngspice's PSpice-compat reading and is stricter than its
   default one;
3. a ``$`` *inside* a token is part of its name -- ``R$3 a 0 2k`` parses
   -- in every mode;
4. ``.include $VAR/...`` keeps the ``$VAR`` path and expands the
   environment variable (mode-independently), which is why the shared
   rule must keep a glued ``$`` token rather than strip it.

Each test also asserts the shared rule's own take on the same card, so
the documented divergence is pinned against the simulator rather than
asserted in prose.

``ngbehavior`` must be set from a ``.spiceinit`` read at startup: a
``set`` inside the deck's own ``.control`` block runs *after* the deck is
read and never influences card reading (the vacuous-test trap this file
was nearly caught by). The mode tests therefore assert the mode actually
applied -- ngspice prints ``Compatibility modes selected`` only when it
did.

Skipped when ngspice is absent or ``KLT_SKIP_NGSPICE_TESTS=1`` (the
local-only opt-out, never set by CI); CI installs ngspice, so the pins
run there.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from klayout_tools._spice_text import strip_spice_inline_comment

HAVE_NGSPICE = (
    shutil.which("ngspice") is not None
    and os.environ.get("KLT_SKIP_NGSPICE_TESTS") != "1"
)

pytestmark = pytest.mark.skipif(not HAVE_NGSPICE, reason="ngspice not installed")

#: Every ``ngbehavior=`` family ngspice-47 recognises that drops the
#: disputed card (other documented names -- ``spice3``, ``spy``, ``solo``,
#: ``xt`` -- do not register and fall back to the default). The PSpice
#: families (``ps``/``psa``) are the exception and get their own test.
_DROP_MODES = ("hs", "ltc", "ltcy", "lta", "ltx", "compat", "all")
_PSPICE_MODES = ("ps", "psa")

_INVALID_INSTANCE = "is not a valid resistor instance"


def _run_ngspice(
    tmp_path: Path,
    name: str,
    deck: str,
    env: dict[str, str],
    spiceinit: str | None = None,
) -> str:
    """``ngspice -b`` on ``deck`` (written to ``tmp_path/name.cir``).

    ``spiceinit``, when given, is written to ``tmp_path/.spiceinit`` and
    ngspice runs with that as its working directory, so the file is picked
    up at startup -- the only point at which ``set ngbehavior=`` can
    affect card reading."""
    deck_path = tmp_path / f"{name}.cir"
    deck_path.write_text(deck)
    if spiceinit is not None:
        (tmp_path / ".spiceinit").write_text(spiceinit)
    result = subprocess.run(
        ["ngspice", "-b", str(deck_path)],
        capture_output=True,
        text=True,
        timeout=120,
        env=env,
        cwd=str(tmp_path),
        check=False,
    )
    return result.stdout + result.stderr


def test_ngspice_reads_a_glued_token_initial_dollar_as_comment(tmp_path):
    """``R1 a $1 1k`` is ``r1 a`` to a default-mode ngspice; the shared
    rule keeps ``$1``. This is the one deliberate divergence of
    ``_spice_text._SPICE_INLINE_COMMENT`` from ngspice's default
    element-card reading, pinned here so it cannot silently widen in
    either direction."""
    out = _run_ngspice(
        tmp_path,
        "glued-dollar",
        "* title\nR1 a $1 1k\nV1 a 0 1\n.op\n.print v(a)\n.end\n",
        env=dict(os.environ),
    )
    assert _INVALID_INSTANCE in out
    assert strip_spice_inline_comment("R1 a $1 1k") == "R1 a $1 1k"


@pytest.mark.parametrize("mode", _DROP_MODES)
def test_ngspice_drops_a_glued_dollar_in_every_non_pspice_mode(tmp_path, mode):
    """No non-PSpice ``ngbehavior`` family rescues a token-initial glued
    ``$``. The ``Compatibility modes selected`` assert guards against the
    mode silently not applying (which would make this test vacuous)."""
    out = _run_ngspice(
        tmp_path,
        f"ngbehavior-{mode}",
        "* title\nR1 a $1 1k\nV1 a 0 1\n.op\n.print v(a)\n.end\n",
        env=dict(os.environ),
        spiceinit=f"set ngbehavior={mode}\n",
    )
    assert "Compatibility modes selected" in out
    assert _INVALID_INSTANCE in out


@pytest.mark.parametrize("mode", _PSPICE_MODES)
def test_ngspice_pspice_modes_keep_a_glued_dollar_as_a_node(tmp_path, mode):
    """``ngbehavior=ps``/``psa`` (PSpice compatibility) read ``$1`` as a
    node name, not a comment -- on the disputed shape the shared rule
    *matches* ngspice's PSpice-compat reading. This is why
    ``_SPICE_INLINE_COMMENT`` documents the divergence against the
    *default* reading, not against ngspice as a whole."""
    out = _run_ngspice(
        tmp_path,
        f"ngbehavior-{mode}",
        "* title\nR1 a $1 1k\nV1 a 0 1\n.op\n.print v(a)\n.end\n",
        env=dict(os.environ),
        spiceinit=f"set ngbehavior={mode}\n",
    )
    assert "Compatibility modes selected" in out
    assert _INVALID_INSTANCE not in out
    # `$1` shows up as a solved node in the voltage table
    assert "$1" in out
    assert strip_spice_inline_comment("R1 a $1 1k") == "R1 a $1 1k"


def test_ngspice_keeps_a_dollar_inside_a_token(tmp_path):
    """``R$3`` is a name, not a truncated ``R`` card -- to ngspice and to
    the shared rule alike (KLayout's ``X$1``/``M$1``/``C$1`` cards,
    issue #2972, depend on this half)."""
    out = _run_ngspice(
        tmp_path,
        "mid-token-dollar",
        "* title\nR$3 a 0 2k\nV1 a 0 1\n.op\n.print v(a)\n.end\n",
        env=dict(os.environ),
    )
    assert _INVALID_INSTANCE not in out
    assert "1.000000e+00" in out
    assert strip_spice_inline_comment("R$3 a 0 2k") == "R$3 a 0 2k"


def test_ngspice_expands_an_include_dollar_var(tmp_path):
    """``.include $VAR/...`` survives verbatim and is environment-expanded
    by ngspice itself (mode-independently): the divider only resolves to
    0.5 V if the included ``Rinc`` actually loaded. The shared rule keeping
    such lines intact is therefore *matching* the simulator, not diverging
    from it."""
    (tmp_path / "inc.cir").write_text("Rinc b 0 5k\n")
    out = _run_ngspice(
        tmp_path,
        "include-var",
        "* title\n.include $KLT5427_INC/inc.cir\nRser a b 5k\nV1 a 0 1\n"
        ".op\n.print v(a) v(b)\n.end\n",
        env={**os.environ, "KLT5427_INC": str(tmp_path)},
    )
    assert _INVALID_INSTANCE not in out
    assert "Could not find include file" not in out
    assert "5.000000e-01" in out
    assert (
        strip_spice_inline_comment(".include $KLT5427_INC/inc.cir")
        == ".include $KLT5427_INC/inc.cir"
    )


def test_ngspice_reports_an_unset_include_dollar_var(tmp_path):
    """An unset ``$VAR`` in an include path is an ngspice env-var error,
    not a silent truncation of the card to ``.include`` -- the failure the
    pre-#5417 whitespace-``$`` strippers used to produce in klt's own
    readers."""
    env = {k: v for k, v in os.environ.items() if k != "KLT5427_INC"}
    out = _run_ngspice(
        tmp_path,
        "include-var-unset",
        "* title\n.include $KLT5427_INC/inc.cir\nV1 a 0 1\n.op\n.print v(a)\n.end\n",
        env=env,
    )
    assert "Cannot read environmental variable KLT5427_INC" in out
