#!/usr/bin/env python3
"""Static verb-module import-boundary gate (issue #2767).

`docs/ARCHITECTURE.md` says verb modules stay self-contained, except for PDK
resolution. This checker enforces that mechanically, on the AST only: it never
imports `klayout_tools` (or anything it analyses), so it needs neither KLayout
nor any EDA tool, and a module with import-time side effects is merely parsed.

Why not `import-linter`: it models *module-to-module* edges only. Its
`ignore_imports` cannot distinguish `from shared import _a` from
`from shared import _b`, nor name an exact private symbol per consumer, which
is the contract here (evaluated 2.15; see the PR for the fixture).

Policy lives in `import-boundaries.json` (repo root):

* `groups` -- every governed module belongs to exactly one group. Kinds:
  `verb` (an implementation group: core modules + CLI adapter), `shared`
  (public symbols importable by anyone, e.g. PDK resolution) and
  `infrastructure` (everything else; cross-group imports must be declared).
  A group may set `public_import_any` (CLI wiring such as the parser) to
  import the public API of every group.
* `allowed_group_edges` -- public (non-underscore) imports between groups.
* `shared_private_permissions` -- exact `(consumer, exporter, symbol)`
  permissions to import a private symbol across groups.
* `legacy_exceptions` -- grandfathered debt, kept apart from intended policy:
  `private_symbols` (exact triples) and `module_edges` (exact pairs).

Rules enforced (diagnostics name the rule in brackets):

* `unclassified-module` -- a module under the source root is in no group.
* `private-symbol` -- a symbol whose *original* name starts with a single
  underscore is imported (or accessed as `module._name`) across groups
  without a permission or exception. Aliases do not matter:
  `public as _local` is public, `_private as public` stays private.
* `module-edge` -- a cross-group import with no declared group edge or
  module-edge exception.
* `star-import` -- `from <internal> import *` (cannot be analysed).
* `parse-error` / `policy-error` -- unparseable source or invalid policy.

Static-analysis limits (this is a lint, not a sandbox or a runtime
dependency proof): `importlib`/`__import__`/`getattr` are not followed;
values copied through variables (`m = alias; m._x`) are not tracked;
re-export chains are not chased (a symbol imported from a module that itself
re-exports it is attributed to that module); a name bound more than once in
one scope (import plus assignment) is treated as ambiguous and not resolved
for `alias._x` access; string annotations are ignored.

Usage::

    python scripts/check_import_boundaries.py            # check (read-only)
    python scripts/check_import_boundaries.py --strict-stale
    python scripts/check_import_boundaries.py --inventory  # JSON of internal imports

Exit codes: 0 clean, 1 boundary violations, 2 invalid policy / parse errors.
The policy is never rewritten by this script: exceptions are edited by hand
and reviewed. When debt disappears, delete the entry (stale entries are
reported on stderr; `--strict-stale` makes them fail).
"""

from __future__ import annotations

import argparse
import ast
import json
import sys
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_POLICY = REPO_ROOT / "import-boundaries.json"
POLICY_SCHEMA_VERSION = 1
GROUP_KINDS = ("verb", "shared", "infrastructure")

EXIT_OK = 0
EXIT_VIOLATIONS = 1
EXIT_POLICY = 2


def is_private(name: str) -> bool:
    """Single-underscore names are private; dunders are not."""
    return name.startswith("_") and not (name.startswith("__") and name.endswith("__"))


@dataclass(frozen=True)
class ImportRecord:
    consumer: str
    exporter: str
    symbol: str | None  # None => module edge
    path: str
    line: int


@dataclass(frozen=True)
class Diagnostic:
    path: str
    line: int
    rule: str
    message: str

    def render(self) -> str:
        return f"{self.path}:{self.line}: [{self.rule}] {self.message}"

    def key(self) -> tuple[str, int, str, str]:
        return (self.path, self.line, self.rule, self.message)


# --------------------------------------------------------------------------
# Source discovery
# --------------------------------------------------------------------------


def discover_modules(src_root: Path, package: str) -> dict[str, tuple[Path, bool]]:
    """Map dotted module name -> (file path, is_package) for every .py file."""
    base = src_root / package
    modules: dict[str, tuple[Path, bool]] = {}
    for path in sorted(base.rglob("*.py")):
        rel = path.relative_to(base).with_suffix("")
        parts = [package, *rel.parts]
        if parts[-1] == "__init__":
            parts.pop()
            modules[".".join(parts)] = (path, True)
        else:
            modules[".".join(parts)] = (path, False)
    return modules


# --------------------------------------------------------------------------
# Per-file AST analysis
# --------------------------------------------------------------------------


class _Scope:
    def __init__(self, kind: str, parent: _Scope | None) -> None:
        self.kind = kind  # module | function | class | comp
        self.parent = parent
        # name -> list of bound module names (None = non-module binding)
        self.bindings: dict[str, list[str | None]] = {}

    def bind(self, name: str, module: str | None) -> None:
        self.bindings.setdefault(name, []).append(module)


class _FileAnalyzer(ast.NodeVisitor):
    def __init__(
        self,
        consumer: str,
        is_package: bool,
        package: str,
        modules: dict[str, tuple[Path, bool]],
        rel_path: str,
    ) -> None:
        self.consumer = consumer
        self.is_package = is_package
        self.package = package
        self.modules = modules
        self.rel_path = rel_path
        self.records: list[ImportRecord] = []
        self.diagnostics: list[Diagnostic] = []
        self.scope = _Scope("module", None)
        self._attr_uses: list[tuple[_Scope, ast.Attribute]] = []
        self._inner_attrs: set[int] = set()

    # -- helpers ---------------------------------------------------------
    def _internal(self, name: str) -> bool:
        return name == self.package or name.startswith(self.package + ".")

    def _record(self, exporter: str, symbol: str | None, line: int) -> None:
        if exporter == self.consumer or exporter not in self.modules:
            return
        self.records.append(
            ImportRecord(self.consumer, exporter, symbol, self.rel_path, line)
        )

    def _base_package(self) -> str:
        return self.consumer if self.is_package else self.consumer.rpartition(".")[0]

    # -- imports ---------------------------------------------------------
    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            if alias.asname:
                self.scope.bind(alias.asname, alias.name)
            else:
                top = alias.name.split(".")[0]
                self.scope.bind(top, top)
            if self._internal(alias.name):
                self._record(alias.name, None, node.lineno)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if node.level:
            parts = self._base_package().split(".") if self._base_package() else []
            up = node.level - 1
            if up >= len(parts):
                self.diagnostics.append(
                    Diagnostic(
                        self.rel_path,
                        node.lineno,
                        "parse-error",
                        "relative import escapes the top-level package",
                    )
                )
                return
            parts = parts[: len(parts) - up]
            if node.module:
                parts += node.module.split(".")
            source = ".".join(parts)
        else:
            source = node.module or ""
        internal = self._internal(source)
        for alias in node.names:
            local = alias.asname or alias.name
            if alias.name == "*":
                if internal:
                    self.diagnostics.append(
                        Diagnostic(
                            self.rel_path,
                            node.lineno,
                            "star-import",
                            f"{self.consumer}: `from {source} import *` is "
                            "ambiguous for static analysis; import explicit "
                            "names instead",
                        )
                    )
                continue
            if not internal:
                self.scope.bind(local, None)
                continue
            submodule = f"{source}.{alias.name}"
            if submodule in self.modules:
                self.scope.bind(local, submodule)
                self._record(submodule, None, node.lineno)
            else:
                self.scope.bind(local, None)
                self._record(source, None, node.lineno)
                if is_private(alias.name):
                    self._record(source, alias.name, node.lineno)

    # -- scopes / bindings ----------------------------------------------
    def _push(self, kind: str) -> _Scope:
        self.scope = _Scope(kind, self.scope)
        return self.scope

    def _pop(self) -> None:
        assert self.scope.parent is not None
        self.scope = self.scope.parent

    def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        self.scope.bind(node.name, None)
        for dec in node.decorator_list:
            self.visit(dec)
        self._visit_args_outer(node.args)
        if node.returns:
            self.visit(node.returns)
        self._push("function")
        self._bind_args(node.args)
        for stmt in node.body:
            self.visit(stmt)
        self._pop()

    visit_FunctionDef = _visit_function
    visit_AsyncFunctionDef = _visit_function

    def _visit_args_outer(self, args: ast.arguments) -> None:
        for default in [*args.defaults, *args.kw_defaults]:
            if default is not None:
                self.visit(default)

    def _bind_args(self, args: ast.arguments) -> None:
        for a in [*args.posonlyargs, *args.args, *args.kwonlyargs]:
            self.scope.bind(a.arg, None)
        if args.vararg:
            self.scope.bind(args.vararg.arg, None)
        if args.kwarg:
            self.scope.bind(args.kwarg.arg, None)

    def visit_Lambda(self, node: ast.Lambda) -> None:
        self._visit_args_outer(node.args)
        self._push("function")
        self._bind_args(node.args)
        self.visit(node.body)
        self._pop()

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.scope.bind(node.name, None)
        for dec in node.decorator_list:
            self.visit(dec)
        for base in node.bases:
            self.visit(base)
        for kw in node.keywords:
            self.visit(kw.value)
        self._push("class")
        for stmt in node.body:
            self.visit(stmt)
        self._pop()

    def _visit_comp(self, node: ast.expr) -> None:
        self._push("comp")
        for gen in node.generators:  # type: ignore[attr-defined]
            self.visit(gen.target)
            self.visit(gen.iter)
            for cond in gen.ifs:
                self.visit(cond)
        if isinstance(node, ast.DictComp):
            self.visit(node.key)
            self.visit(node.value)
        else:
            self.visit(node.elt)  # type: ignore[attr-defined]
        self._pop()

    visit_ListComp = _visit_comp
    visit_SetComp = _visit_comp
    visit_GeneratorExp = _visit_comp
    visit_DictComp = _visit_comp

    def visit_Name(self, node: ast.Name) -> None:
        if isinstance(node.ctx, ast.Store | ast.Del):
            self.scope.bind(node.id, None)

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
        if node.name:
            self.scope.bind(node.name, None)
        self.generic_visit(node)

    def visit_MatchAs(self, node: ast.MatchAs) -> None:
        if node.name:
            self.scope.bind(node.name, None)
        self.generic_visit(node)

    def visit_MatchStar(self, node: ast.MatchStar) -> None:
        if node.name:
            self.scope.bind(node.name, None)

    def visit_MatchMapping(self, node: ast.MatchMapping) -> None:
        if node.rest:
            self.scope.bind(node.rest, None)
        self.generic_visit(node)

    # -- attribute access ------------------------------------------------
    def visit_Attribute(self, node: ast.Attribute) -> None:
        if id(node) not in self._inner_attrs:
            inner = node.value
            while isinstance(inner, ast.Attribute):
                self._inner_attrs.add(id(inner))
                inner = inner.value
            if isinstance(inner, ast.Name):
                self._attr_uses.append((self.scope, node))
        self.generic_visit(node)

    def _lookup(self, scope: _Scope, name: str) -> str | None:
        cur: _Scope | None = scope
        first = True
        while cur is not None:
            if (first or cur.kind != "class") and name in cur.bindings:
                bound = cur.bindings[name]
                if len(set(bound)) == 1:
                    return bound[0]
                return None
            first = False
            cur = cur.parent
        return None

    def resolve_attribute_uses(self) -> None:
        for scope, node in self._attr_uses:
            attrs: list[str] = []
            cur: ast.expr = node
            while isinstance(cur, ast.Attribute):
                attrs.append(cur.attr)
                cur = cur.value
            assert isinstance(cur, ast.Name)
            attrs.reverse()
            module = self._lookup(scope, cur.id)
            if module is None:
                continue
            for attr in attrs:
                candidate = f"{module}.{attr}"
                if candidate in self.modules:
                    module = candidate
                    self._record(module, None, node.lineno)
                    continue
                if module in self.modules and is_private(attr):
                    self._record(module, attr, node.lineno)
                break


def analyze_file(
    consumer: str,
    path: Path,
    is_package: bool,
    package: str,
    modules: dict[str, tuple[Path, bool]],
    rel_path: str,
) -> tuple[list[ImportRecord], list[Diagnostic]]:
    try:
        tree = ast.parse(path.read_bytes(), filename=str(path))
    except (SyntaxError, ValueError) as exc:
        line = getattr(exc, "lineno", None) or 1
        return [], [Diagnostic(rel_path, line, "parse-error", f"{exc}")]
    analyzer = _FileAnalyzer(consumer, is_package, package, modules, rel_path)
    analyzer.visit(tree)
    analyzer.resolve_attribute_uses()
    return analyzer.records, analyzer.diagnostics


def analyze_tree(
    src_root: Path, package: str, repo_root: Path
) -> tuple[dict[str, tuple[Path, bool]], list[ImportRecord], list[Diagnostic]]:
    modules = discover_modules(src_root, package)
    records: list[ImportRecord] = []
    diags: list[Diagnostic] = []
    for name, (path, is_pkg) in sorted(modules.items()):
        try:
            rel = path.relative_to(repo_root).as_posix()
        except ValueError:
            rel = path.as_posix()
        recs, ds = analyze_file(name, path, is_pkg, package, modules, rel)
        records.extend(recs)
        diags.extend(ds)
    return modules, records, diags


# --------------------------------------------------------------------------
# Policy
# --------------------------------------------------------------------------


class PolicyError(Exception):
    pass


@dataclass
class Policy:
    source_root: str
    package: str
    group_of: dict[str, str]
    kinds: dict[str, str]
    import_any: set[str]
    group_edges: set[tuple[str, str]]
    permissions: set[tuple[str, str, str]]
    private_exceptions: set[tuple[str, str, str]]
    edge_exceptions: set[tuple[str, str]]


def _unique(items: list, label: str, errors: list[str]) -> set:
    seen: set = set()
    for item in items:
        if item in seen:
            errors.append(f"duplicate {label}: {item}")
        seen.add(item)
    return seen


def load_policy(path: Path) -> Policy:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise PolicyError([f"cannot read policy {path}: {exc}"]) from exc  # type: ignore[arg-type]
    errors: list[str] = []
    if not isinstance(raw, dict):
        raise PolicyError(["policy must be a JSON object"])  # type: ignore[arg-type]
    if raw.get("schema_version") != POLICY_SCHEMA_VERSION:
        errors.append(f"schema_version must be {POLICY_SCHEMA_VERSION}")
    for key in ("source_root", "package"):
        if not isinstance(raw.get(key), str) or not raw.get(key):
            errors.append(f"missing string field `{key}`")

    group_of: dict[str, str] = {}
    kinds: dict[str, str] = {}
    import_any: set[str] = set()
    groups = raw.get("groups")
    if not isinstance(groups, dict) or not groups:
        errors.append("`groups` must be a non-empty object")
        groups = {}
    for gname, g in sorted(groups.items()):
        if not isinstance(g, dict) or g.get("kind") not in GROUP_KINDS:
            errors.append(f"group `{gname}`: `kind` must be one of {GROUP_KINDS}")
            continue
        kinds[gname] = g["kind"]
        if g.get("public_import_any") is True:
            import_any.add(gname)
        mods = g.get("modules")
        if not isinstance(mods, list) or not mods:
            errors.append(f"group `{gname}`: `modules` must be a non-empty list")
            continue
        _unique(mods, f"module in group `{gname}`", errors)
        for m in mods:
            if m in group_of and group_of[m] != gname:
                errors.append(f"module `{m}` is in both `{group_of[m]}` and `{gname}`")
            group_of[m] = gname

    def need_group(g: object, where: str) -> None:
        if g not in kinds:
            errors.append(f"{where}: unknown group `{g}`")

    def need_module(m: object, where: str) -> None:
        if m not in group_of:
            errors.append(f"{where}: module `{m}` is not classified in any group")

    def entries(section: object, fields: tuple[str, ...], label: str) -> list[tuple]:
        out: list[tuple] = []
        if not isinstance(section, list):
            errors.append(f"`{label}` must be a list")
            return out
        for i, e in enumerate(section):
            if not isinstance(e, dict) or any(
                not isinstance(e.get(f), str) or not e.get(f) for f in fields
            ):
                errors.append(f"{label}[{i}]: needs string fields {fields}")
                continue
            if not isinstance(e.get("reason"), str) or not e["reason"].strip():
                errors.append(f"{label}[{i}]: a non-empty `reason` is required")
            out.append(tuple(e[f] for f in fields))
        return out

    edge_list = entries(
        raw.get("allowed_group_edges", []),
        ("consumer", "exporter"),
        "allowed_group_edges",
    )
    perm_list = entries(
        raw.get("shared_private_permissions", []),
        ("consumer", "exporter", "symbol"),
        "shared_private_permissions",
    )
    legacy = raw.get("legacy_exceptions", {})
    if not isinstance(legacy, dict):
        errors.append("`legacy_exceptions` must be an object")
        legacy = {}
    priv_list = entries(
        legacy.get("private_symbols", []),
        ("consumer", "exporter", "symbol"),
        "legacy_exceptions.private_symbols",
    )
    medge_list = entries(
        legacy.get("module_edges", []),
        ("consumer", "exporter"),
        "legacy_exceptions.module_edges",
    )

    group_edges = _unique(edge_list, "allowed_group_edges entry", errors)
    permissions = _unique(perm_list, "shared_private_permissions entry", errors)
    priv_exc = _unique(priv_list, "legacy private-symbol exception", errors)
    edge_exc = _unique(medge_list, "legacy module-edge exception", errors)

    for c, e in sorted(group_edges):
        need_group(c, "allowed_group_edges")
        need_group(e, "allowed_group_edges")
    for label, triples in (
        ("shared_private_permissions", permissions),
        ("legacy_exceptions.private_symbols", priv_exc),
    ):
        for c, e, s in sorted(triples):
            need_module(c, label)
            need_module(e, label)
            if not is_private(s):
                errors.append(f"{label}: symbol `{s}` is not private")
            if "*" in (c, e, s):
                errors.append(f"{label}: wildcards are not allowed")
    for c, e in sorted(edge_exc):
        need_module(c, "legacy_exceptions.module_edges")
        need_module(e, "legacy_exceptions.module_edges")
    for t in sorted(permissions & priv_exc):
        errors.append(f"{t} is both a permission and a legacy exception")

    if errors:
        raise PolicyError(errors)  # type: ignore[arg-type]
    return Policy(
        raw["source_root"],
        raw["package"],
        group_of,
        kinds,
        import_any,
        group_edges,
        permissions,
        priv_exc,
        edge_exc,
    )


# --------------------------------------------------------------------------
# Rule evaluation
# --------------------------------------------------------------------------


def evaluate(
    policy: Policy,
    modules: dict[str, tuple[Path, bool]],
    records: list[ImportRecord],
    repo_root: Path,
) -> tuple[list[Diagnostic], list[str]]:
    diags: list[Diagnostic] = []
    used_edges: set[tuple[str, str]] = set()
    used_perm: set[tuple[str, str, str]] = set()
    used_priv: set[tuple[str, str, str]] = set()
    used_medge: set[tuple[str, str]] = set()

    def rel(name: str) -> str:
        path = modules[name][0]
        try:
            return path.relative_to(repo_root).as_posix()
        except ValueError:
            return path.as_posix()

    for name in sorted(modules):
        if name not in policy.group_of:
            diags.append(
                Diagnostic(
                    rel(name),
                    1,
                    "unclassified-module",
                    f"{name} is not classified in import-boundaries.json; add it "
                    "to a group (a `verb` group for its verb, `shared` for "
                    "cross-verb helpers, or `infrastructure`)",
                )
            )
    for name in sorted(policy.group_of):
        if name not in modules:
            diags.append(
                Diagnostic(
                    "import-boundaries.json",
                    1,
                    "policy-error",
                    f"classified module `{name}` does not exist under the "
                    "source root; remove it from the policy",
                )
            )

    for r in records:
        gc = policy.group_of.get(r.consumer)
        ge = policy.group_of.get(r.exporter)
        if gc is None or ge is None or gc == ge:
            continue
        if r.symbol is not None:
            triple = (r.consumer, r.exporter, r.symbol)
            if triple in policy.permissions:
                used_perm.add(triple)
                continue
            if triple in policy.private_exceptions:
                used_priv.add(triple)
                continue
            diags.append(
                Diagnostic(
                    r.path,
                    r.line,
                    "private-symbol",
                    f"{r.consumer} imports private `{r.symbol}` from "
                    f"{r.exporter} (group `{gc}` -> `{ge}`); make it public, "
                    "move it to a shared module, or (reviewed) add an exact "
                    "entry to shared_private_permissions",
                )
            )
            continue
        pair = (r.consumer, r.exporter)
        if pair in policy.edge_exceptions:
            used_medge.add(pair)
            continue
        if policy.kinds[ge] == "shared" or gc in policy.import_any:
            continue
        if (gc, ge) in policy.group_edges:
            used_edges.add((gc, ge))
            continue
        diags.append(
            Diagnostic(
                r.path,
                r.line,
                "module-edge",
                f"{r.consumer} imports {r.exporter} (group `{gc}` -> `{ge}`) "
                "with no declared edge; add it to allowed_group_edges if "
                "intended",
            )
        )

    stale: list[str] = []
    for c, e in sorted(policy.group_edges - used_edges):
        stale.append(f"stale allowed_group_edges: {c} -> {e}")
    for t in sorted(policy.permissions - used_perm):
        stale.append(f"stale shared_private_permissions: {t}")
    for t in sorted(policy.private_exceptions - used_priv):
        stale.append(f"stale legacy private-symbol exception: {t}")
    for t in sorted(policy.edge_exceptions - used_medge):
        stale.append(f"stale legacy module-edge exception: {t}")
    return diags, stale


def run(policy_path: Path, repo_root: Path, strict_stale: bool, inventory: bool) -> int:
    try:
        policy = load_policy(policy_path)
    except PolicyError as exc:
        print("invalid import-boundary policy:", file=sys.stderr)
        for e in exc.args[0]:
            print(f"  - {e}", file=sys.stderr)
        return EXIT_POLICY
    src_root = repo_root / policy.source_root
    if not (src_root / policy.package).is_dir():
        print(f"source root missing: {src_root / policy.package}", file=sys.stderr)
        return EXIT_POLICY
    modules, records, parse_diags = analyze_tree(src_root, policy.package, repo_root)
    if inventory:
        out = sorted({(r.consumer, r.exporter, r.symbol or "") for r in records})
        json.dump(
            [{"consumer": c, "exporter": e, "symbol": s or None} for c, e, s in out],
            sys.stdout,
            indent=2,
        )
        print()
        return EXIT_OK
    hard = [d for d in parse_diags if d.rule == "parse-error"]
    diags, stale = evaluate(policy, modules, records, repo_root)
    star = [d for d in parse_diags if d.rule != "parse-error"]
    all_diags = sorted({*hard, *star, *diags}, key=Diagnostic.key)
    for d in all_diags:
        print(d.render(), file=sys.stderr)
    for s in stale:
        print(f"note: {s}", file=sys.stderr)
    if hard or any(d.rule == "policy-error" for d in all_diags):
        return EXIT_POLICY
    if all_diags or (strict_stale and stale):
        print(
            f"import-boundary check FAILED: {len(all_diags)} violation(s)"
            + (f", {len(stale)} stale entr(ies)" if strict_stale and stale else ""),
            file=sys.stderr,
        )
        return EXIT_VIOLATIONS
    print(
        f"import-boundary check passed ({len(modules)} modules, "
        f"{len(records)} internal imports)"
    )
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--policy", type=Path, default=DEFAULT_POLICY)
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=None,
        help="root that source_root is relative to (default: policy's directory)",
    )
    parser.add_argument("--strict-stale", action="store_true")
    parser.add_argument(
        "--inventory",
        action="store_true",
        help="print every cross-module internal import as JSON (for seeding)",
    )
    args = parser.parse_args(argv)
    policy_path = args.policy.resolve()
    repo_root = (args.repo_root or policy_path.parent).resolve()
    return run(policy_path, repo_root, args.strict_stale, args.inventory)


if __name__ == "__main__":
    sys.exit(main())
