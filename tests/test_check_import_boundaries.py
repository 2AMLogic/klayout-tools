"""Tests for the static verb-module import-boundary gate (issue #2767).

`scripts/check_import_boundaries.py` is evaluated on synthetic package
fixtures (never importing them) plus the real tree and CI wiring.

`import-linter` (2.15) was evaluated first: it only models module->module
edges, so `from shared import _a` and `from shared import _b` are
indistinguishable and `ignore_imports` cannot name a symbol per consumer.
That gap is why this is a small stdlib AST checker.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent
SCRIPT = REPO_ROOT / "scripts" / "check_import_boundaries.py"

_spec = importlib.util.spec_from_file_location("check_import_boundaries", SCRIPT)
assert _spec is not None and _spec.loader is not None
cib = importlib.util.module_from_spec(_spec)
sys.modules["check_import_boundaries"] = cib
_spec.loader.exec_module(cib)

PKG = "pkg"


def make_repo(tmp_path: Path, files: dict[str, str], groups: dict, **policy_extra):
    """Write `src/pkg/<files>` plus an import-boundaries.json; return policy path."""
    for rel, text in files.items():
        f = tmp_path / "src" / PKG / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text, encoding="utf-8")
    pol = {
        "schema_version": 1,
        "source_root": "src",
        "package": PKG,
        "groups": {
            g: {"kind": k, "modules": [f"{PKG}.{m}" if m else PKG for m in mods]}
            for g, (k, mods) in groups.items()
        },
    }
    pol.update(policy_extra)
    path = tmp_path / "import-boundaries.json"
    path.write_text(json.dumps(pol), encoding="utf-8")
    return path


def check(path: Path, capsys, *extra: str):
    rc = cib.main(["--policy", str(path), *extra])
    err = capsys.readouterr().err
    return rc, err


def priv_exc(consumer, exporter, symbol):
    return {
        "consumer": f"{PKG}.{consumer}",
        "exporter": f"{PKG}.{exporter}",
        "symbol": symbol,
        "reason": "test",
    }


BASE_GROUPS = {
    "root": ("shared", [""]),
    "a": ("verb", ["a"]),
    "b": ("verb", ["b", "b_helper"]),
    "shared": ("shared", ["shared"]),
}
SHARED = "def _x(): pass\ndef _y(): pass\ndef pub(): pass\n"


def files(**kw: str) -> dict[str, str]:
    base = {
        "__init__.py": "",
        "a.py": "",
        "b.py": "",
        "b_helper.py": "def _h(): pass\n",
        "shared.py": SHARED,
    }
    base.update(
        {
            k.replace("__", "/") + ".py" if not k.endswith("init") else k: v
            for k, v in kw.items()
        }
    )
    return base


def test_clean_tree_passes(tmp_path, capsys):
    p = make_repo(tmp_path, files(), BASE_GROUPS)
    assert check(p, capsys)[0] == 0


@pytest.mark.parametrize(
    "src",
    [
        "from .shared import _x",
        "from pkg.shared import _x",
        "from pkg.shared import _x as pub_name",
        "from . import shared\nshared._x()",
        "from . import shared as s\ns._x()",
        "import pkg.shared\npkg.shared._x()",
        "import pkg.shared as s\ns._x()",
        "def f():\n    from .shared import _x\n",
        "try:\n    from .shared import _x\nexcept ImportError:\n    pass",
        (
            "from typing import TYPE_CHECKING\n"
            "if TYPE_CHECKING:\n    from .shared import _x"
        ),
        (
            "class C:\n    def m(self):\n"
            "        from . import shared\n        return shared._x"
        ),
    ],
)
def test_private_forms_are_caught(tmp_path, capsys, src):
    p = make_repo(tmp_path, files(a=src), BASE_GROUPS)
    rc, err = check(p, capsys)
    assert rc == 1
    assert "[private-symbol]" in err and "_x" in err and "pkg.a" in err


def test_public_symbol_with_underscore_alias_passes(tmp_path, capsys):
    p = make_repo(tmp_path, files(a="from .shared import pub as _local"), BASE_GROUPS)
    assert check(p, capsys)[0] == 0


def test_private_name_rebound_public_still_fails(tmp_path, capsys):
    p = make_repo(
        tmp_path, files(a="from .shared import _x as public_name"), BASE_GROUPS
    )
    assert check(p, capsys)[0] == 1


def test_package_init_relative_import(tmp_path, capsys):
    fs = files()
    fs["sub/__init__.py"] = "from ..shared import _x\n"
    fs["sub/mod.py"] = "from .. import shared\nshared._y()\n"
    groups = dict(BASE_GROUPS, sub=("verb", ["sub", "sub.mod"]))
    p = make_repo(tmp_path, fs, groups)
    rc, err = check(p, capsys)
    assert rc == 1
    assert "pkg.sub imports private `_x`" in err
    assert "pkg.sub.mod imports private `_y`" in err


def test_alias_shadowing_not_mistaken_for_module(tmp_path, capsys):
    src = (
        "from . import shared\n"
        "def f(shared):\n    return shared._x\n"  # parameter shadows
        "def g():\n    shared = object()\n    return shared._x\n"  # local rebind
        "def h():\n    return [shared for shared in range(3) if shared._y]\n"
        "class K:\n    shared = 1\n    v = shared._x\n"  # class scope own binding
    )
    p = make_repo(tmp_path, files(a=src), BASE_GROUPS)
    assert check(p, capsys)[0] == 0


def test_shadow_in_one_function_does_not_hide_other(tmp_path, capsys):
    src = (
        "from . import shared\n"
        "def f(shared):\n    return shared._x\n"
        "def g():\n    return shared._y\n"
    )
    p = make_repo(tmp_path, files(a=src), BASE_GROUPS)
    rc, err = check(p, capsys)
    assert rc == 1 and "_y" in err and "_x" not in err.replace("_y", "")


def test_same_group_private_import_passes(tmp_path, capsys):
    p = make_repo(tmp_path, files(b="from .b_helper import _h"), BASE_GROUPS)
    assert check(p, capsys)[0] == 0


def test_cross_verb_private_import_fails_and_public_edge_needs_declaration(
    tmp_path, capsys
):
    fs = files(a="from .b import pub_b")
    fs["b.py"] = "def pub_b(): pass\n"
    p = make_repo(tmp_path, fs, BASE_GROUPS)
    rc, err = check(p, capsys)
    assert rc == 1 and "[module-edge]" in err
    p = make_repo(
        tmp_path,
        fs,
        BASE_GROUPS,
        allowed_group_edges=[{"consumer": "a", "exporter": "b", "reason": "r"}],
    )
    assert check(p, capsys)[0] == 0


def test_shared_public_import_passes(tmp_path, capsys):
    p = make_repo(tmp_path, files(a="from .shared import pub"), BASE_GROUPS)
    assert check(p, capsys)[0] == 0


def test_exact_exception_semantics(tmp_path, capsys):
    exc = {"private_symbols": [priv_exc("a", "shared", "_x")]}
    one = make_repo(
        tmp_path, files(a="from .shared import _x"), BASE_GROUPS, legacy_exceptions=exc
    )
    assert check(one, capsys, "--strict-stale")[0] == 0
    # lines move: still passes
    two = make_repo(
        tmp_path,
        files(a="\n\n# c\nfrom .shared import _x"),
        BASE_GROUPS,
        legacy_exceptions=exc,
    )
    assert check(two, capsys)[0] == 0
    # a second symbol on the same edge fails
    three = make_repo(
        tmp_path,
        files(a="from .shared import _x, _y"),
        BASE_GROUPS,
        legacy_exceptions=exc,
    )
    rc, err = check(three, capsys)
    assert rc == 1 and "`_y`" in err and "`_x`" not in err
    # a second consumer of the same shared symbol fails
    four = make_repo(
        tmp_path,
        files(a="from .shared import _x", b="from .shared import _x"),
        BASE_GROUPS,
        legacy_exceptions=exc,
    )
    rc, err = check(four, capsys)
    assert rc == 1 and "pkg.b imports" in err and "pkg.a imports" not in err
    # qualified access cannot hide a different symbol on a permitted edge
    five = make_repo(
        tmp_path,
        files(a="from . import shared\nshared._y()"),
        BASE_GROUPS,
        legacy_exceptions=exc,
    )
    assert check(five, capsys)[0] == 1


def test_permission_is_exact_and_separate_from_exceptions(tmp_path, capsys):
    perm = [priv_exc("a", "shared", "_x")]
    p = make_repo(
        tmp_path,
        files(a="from .shared import _x, _y"),
        BASE_GROUPS,
        shared_private_permissions=perm,
    )
    rc, err = check(p, capsys)
    assert rc == 1 and "`_y`" in err
    both = make_repo(
        tmp_path,
        files(),
        BASE_GROUPS,
        shared_private_permissions=perm,
        legacy_exceptions={"private_symbols": perm},
    )
    assert check(both, capsys)[0] == 2


def test_module_edge_exception_is_exact(tmp_path, capsys):
    fs = files(a="from .b import pub_b")
    fs["b.py"] = "def pub_b(): pass\n"
    mod_exc = {
        "module_edges": [{"consumer": "pkg.a", "exporter": "pkg.b", "reason": "r"}]
    }
    p = make_repo(tmp_path, fs, BASE_GROUPS, legacy_exceptions=mod_exc)
    assert check(p, capsys)[0] == 0
    fs["b_helper.py"] = "from .b import pub_b\n"
    p = make_repo(tmp_path, fs, BASE_GROUPS, legacy_exceptions=mod_exc)
    assert check(p, capsys)[0] == 0  # b_helper is in b's own group
    fs["a.py"] = "from .b import pub_b\nfrom .b_helper import _h"
    p = make_repo(tmp_path, fs, BASE_GROUPS, legacy_exceptions=mod_exc)
    assert check(p, capsys)[0] == 1


def test_similar_names_stay_distinct_groups(tmp_path, capsys):
    groups = {
        "root": ("shared", [""]),
        "sta": ("verb", ["sta"]),
        "stats": ("verb", ["stats"]),
        "gen": ("verb", ["gen"]),
        "gen_compose": ("verb", ["gen_compose", "gen_compose_routing"]),
    }
    fs = {
        "__init__.py": "",
        "sta.py": "def _s(): pass\n",
        "stats.py": "from .sta import _s\n",
        "gen.py": "",
        "gen_compose.py": "from .gen_compose_routing import _r\n",
        "gen_compose_routing.py": "def _r(): pass\n",
    }
    p = make_repo(tmp_path, fs, groups)
    rc, err = check(p, capsys)
    assert rc == 1
    assert "pkg.stats imports private `_s`" in err
    assert "gen_compose imports private" not in err


def test_external_imports_ignored(tmp_path, capsys):
    src = (
        "import os\nimport klayout.db as kdb\n"
        "from os.path import _joinrealpath\nos._exit"
    )
    p = make_repo(tmp_path, files(a=src), BASE_GROUPS)
    assert check(p, capsys)[0] == 0


def test_unclassified_module_fails(tmp_path, capsys):
    fs = files()
    fs["newmod.py"] = ""
    p = make_repo(tmp_path, fs, BASE_GROUPS)
    rc, err = check(p, capsys)
    assert rc == 1 and "[unclassified-module]" in err and "pkg.newmod" in err


def test_classified_but_missing_module_is_policy_error(tmp_path, capsys):
    groups = dict(BASE_GROUPS, ghost=("verb", ["ghost"]))
    p = make_repo(tmp_path, files(), groups)
    assert check(p, capsys)[0] == 2


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d.update(schema_version=9),
        lambda d: d["groups"]["a"]["modules"].append("pkg.b"),
        lambda d: d["groups"]["a"]["modules"].append("pkg.a"),
        lambda d: d.update(
            allowed_group_edges=[{"consumer": "a", "exporter": "nope", "reason": "r"}]
        ),
        lambda d: d.update(allowed_group_edges=[{"consumer": "a", "exporter": "b"}]),
        lambda d: d.update(
            legacy_exceptions={"private_symbols": [priv_exc("a", "shared", "*")]}
        ),
        lambda d: d.update(
            legacy_exceptions={"private_symbols": [priv_exc("a", "shared", "pub")]}
        ),
        lambda d: d.update(
            legacy_exceptions={"private_symbols": [priv_exc("a", "shared", "_x")] * 2}
        ),
        lambda d: d["groups"]["a"].update(kind="bogus"),
    ],
)
def test_invalid_policy_rejected(tmp_path, capsys, mutate):
    p = make_repo(tmp_path, files(), BASE_GROUPS)
    d = json.loads(p.read_text())
    mutate(d)
    p.write_text(json.dumps(d))
    assert check(p, capsys)[0] == 2


def test_malformed_json_and_missing_source_root(tmp_path, capsys):
    bad = tmp_path / "bad.json"
    bad.write_text("{nope")
    assert check(bad, capsys)[0] == 2
    p = make_repo(tmp_path, files(), BASE_GROUPS)
    d = json.loads(p.read_text())
    d["source_root"] = "does-not-exist"
    p.write_text(json.dumps(d))
    assert check(p, capsys)[0] == 2


def test_syntax_error_fails(tmp_path, capsys):
    p = make_repo(tmp_path, files(a="def (:\n"), BASE_GROUPS)
    rc, err = check(p, capsys)
    assert rc == 2 and "[parse-error]" in err


def test_internal_star_import_rejected_external_ok(tmp_path, capsys):
    p = make_repo(tmp_path, files(a="from .shared import *"), BASE_GROUPS)
    rc, err = check(p, capsys)
    assert rc == 1 and "[star-import]" in err
    p = make_repo(tmp_path, files(a="from os.path import *"), BASE_GROUPS)
    assert check(p, capsys)[0] == 0


def test_stale_entries_note_and_strict(tmp_path, capsys):
    exc = {"private_symbols": [priv_exc("a", "shared", "_x")]}
    p = make_repo(tmp_path, files(), BASE_GROUPS, legacy_exceptions=exc)
    rc, err = check(p, capsys)
    assert rc == 0 and "stale legacy private-symbol exception" in err
    assert check(p, capsys, "--strict-stale")[0] == 1


def test_deterministic_and_read_only(tmp_path, capsys):
    p = make_repo(
        tmp_path,
        files(a="from .shared import _y, _x", b="from .shared import _x"),
        BASE_GROUPS,
    )
    before = {f: f.read_bytes() for f in [p, *(tmp_path / "src").rglob("*.py")]}
    first = check(p, capsys)
    second = check(p, capsys)
    assert first == second and first[0] == 1
    assert before == {f: f.read_bytes() for f in before}


def test_analyzed_source_is_never_executed(tmp_path, capsys):
    marker = tmp_path / "executed"
    src = (
        f"open({str(marker)!r}, 'w').write('x')\nraise RuntimeError('boom')\n"
        "import klayout.db\nimport not_installed_eda_pkg\n"
    )
    p = make_repo(tmp_path, files(a=src), BASE_GROUPS)
    assert check(p, capsys)[0] == 0
    assert not marker.exists()


def test_real_tree_passes_with_committed_policy():
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "--strict-stale"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr


def test_real_policy_covers_known_seeds():
    pol = cib.load_policy(REPO_ROOT / "import-boundaries.json")
    k = "klayout_tools."
    assert (k + "erc", k + "extract", "_resistor_body_region") in pol.private_exceptions
    g = pol.group_of
    assert g[k + "cli.sta_cmd"] == g[k + "post_route_sta"] != g[k + "stats"]
    assert g[k + "cli.yield_cmd"] == g[k + "yield_analysis"]
    assert g[k + "gen"] != g[k + "gen_compose"]
    assert g[k + "gen_compose"] == g[k + "gen_compose_routing"]
    assert pol.kinds[g[k + "pdk_families"]] == "shared"


def test_ci_runs_gate_without_continue_on_error():
    text = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text()
    i = text.index("Verb-module import boundaries")
    block = text[i : text.index("\n      - name:", i)]
    assert "scripts/check_import_boundaries.py" in block
    assert "continue-on-error: true" not in block
