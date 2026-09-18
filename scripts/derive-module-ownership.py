#!/usr/bin/env python3
"""Derive module ownership map for multi-file klt verbs.

Mechanically generates a mapping of which source files own which pieces of
functionality for verbs implemented across multiple Python modules.

Uses:
1. cli/parser.py's set_defaults(func=...) wiring to find registered verbs
2. AST import graph analysis to find related modules
3. Module docstrings to document ownership boundaries

Output: A list of multi-file verbs with their constituent modules and
role descriptions, formatted for inclusion in docs/module-ownership.md.
"""

import ast
import re
import sys
from pathlib import Path
from collections import defaultdict


def find_repo_root() -> Path:
    """Find the repository root by looking for .git."""
    current = Path.cwd()
    while current != current.parent:
        if (current / ".git").exists():
            return current
        current = current.parent
    raise RuntimeError("Could not find repository root (no .git found)")


def extract_verbs_from_parser(parser_file: Path) -> dict[str, str]:
    """Extract verb name -> cmd_module mapping from parser.py.

    Looks for patterns like: verb_parser.set_defaults(func=verb_cmd.run)
    Returns: {verb_name: cmd_module_name, ...}
    """
    content = parser_file.read_text(encoding="utf-8")
    verbs = {}

    for match in re.finditer(
        r"(\w+)_parser\.set_defaults\(func=(\w+_cmd)\.run\)", content
    ):
        verb_name = match.group(1)
        cmd_module = match.group(2)
        verbs[verb_name] = cmd_module

    return verbs


def find_python_modules(
    src_base: Path,
) -> dict[str, list[Path]]:
    """Find all Python modules in src_base.

    Returns: {module_path: [file_paths], ...}
    where module_path is relative dotted notation.
    """
    modules = defaultdict(list)

    for py_file in src_base.rglob("*.py"):
        if "__pycache__" in py_file.parts:
            continue

        # Calculate module path (dotted notation)
        rel_path = py_file.relative_to(src_base)
        module_name = str(rel_path)[:-3].replace("/", ".").replace("\\", ".")

        modules[module_name].append(py_file)

    return modules


def get_module_docstring(file_path: Path) -> str:
    """Extract first docstring from a Python module."""
    try:
        content = file_path.read_text(encoding="utf-8")
        tree = ast.parse(content)
        docstring = ast.get_docstring(tree)
        if docstring:
            # Return first line (or first 80 chars + "...")
            first_line = docstring.split("\n")[0]
            if len(first_line) > 80:
                return first_line[:77] + "..."
            return first_line
    except Exception:
        pass

    return "(no docstring)"


def get_imports_from_file(file_path: Path) -> list[str]:
    """Extract all import statements from a Python file."""
    try:
        content = file_path.read_text(encoding="utf-8")
        tree = ast.parse(content)
    except Exception:
        return []

    imports = []

    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            # Collect names being imported
            for alias in node.names:
                imports.append((module, alias.name))

    return imports


def find_related_modules(
    verb_name: str,
    cmd_module: str,
    all_modules: dict[str, list[Path]],
    src_base: Path,
) -> list[tuple[str, str]]:
    """Find all modules related to a verb.

    Returns: [(module_name, description), ...]
    """
    related = {}

    # 1. Add the cmd module
    related[cmd_module] = get_module_docstring(src_base / f"cli/{cmd_module}.py")

    # 2. Look for modules matching the verb name pattern
    for module_name in sorted(all_modules.keys()):
        # Check if module belongs to this verb (by name)
        module_basename = module_name.split(".")[-1]

        # Match: module starts with verb name or is a subsystem (extract_abstract, etc.)
        if module_basename.startswith(verb_name) and module_basename != cmd_module:
            for file_path in all_modules[module_name]:
                if file_path.exists():
                    doc = get_module_docstring(file_path)
                    related[module_name] = doc
                    break

    # 3. Look at cmd module imports to find related base modules
    cmd_file = src_base / f"cli/{cmd_module}.py"
    if cmd_file.exists():
        imports = get_imports_from_file(cmd_file)
        for module_name, imported_name in imports:
            # Look for relative imports from parent (.. package)
            if module_name and (module_name.startswith("..") or module_name == ""):
                # Check if the imported name matches the verb
                if imported_name.startswith(verb_name) and imported_name != cmd_module:
                    full_name = imported_name
                    if full_name in all_modules:
                        for file_path in all_modules[full_name]:
                            if file_path.exists():
                                doc = get_module_docstring(file_path)
                                related[full_name] = doc
                                break

    # 4. For primary module (e.g., extract.py), check its imports
    primary_module = f"{verb_name}"
    if primary_module in all_modules:
        for file_path in all_modules[primary_module]:
            imports = get_imports_from_file(file_path)
            for module_name, imported_name in imports:
                # Look for related modules (same verb prefix)
                if imported_name.startswith(verb_name) and imported_name != cmd_module:
                    if imported_name not in related:
                        full_name = imported_name
                        if full_name in all_modules:
                            for fp in all_modules[full_name]:
                                if fp.exists():
                                    doc = get_module_docstring(fp)
                                    related[full_name] = doc
                                    break

    # Filter to multi-file only: need more than just the cmd module
    # Count non-cmd modules
    non_cmd_modules = [name for name in related.keys() if not name.endswith("_cmd")]
    if len(non_cmd_modules) <= 1:
        return []

    return sorted(related.items())


def main():
    """Main entry point."""
    try:
        repo_root = find_repo_root()
    except RuntimeError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    # Paths
    src_base = repo_root / "src/klayout_tools"
    parser_file = src_base / "cli/parser.py"

    if not parser_file.exists():
        print(f"Error: parser.py not found at {parser_file}", file=sys.stderr)
        sys.exit(1)

    # Extract verbs and find modules
    verbs = extract_verbs_from_parser(parser_file)
    all_modules = find_python_modules(src_base)

    # Build the map
    multi_file_verbs = {}

    for verb_name, cmd_module in sorted(verbs.items()):
        related = find_related_modules(verb_name, cmd_module, all_modules, src_base)
        if related:
            multi_file_verbs[verb_name] = related

    # Output as markdown sections
    print("# Multi-File klt Verbs (mechanically derived)\n")
    print("(This output can be integrated into docs/module-ownership.md)\n")

    for verb_name in sorted(multi_file_verbs.keys()):
        modules = multi_file_verbs[verb_name]
        print(f"## {verb_name}\n")

        for module_name, doc in modules:
            print(f"- **{module_name}**: {doc}")

        print()

    return 0


if __name__ == "__main__":
    sys.exit(main())
