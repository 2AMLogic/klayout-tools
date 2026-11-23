"""Keep native-yield tests in the CI job that builds their extension."""

import ast
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
NATIVE_MODULE = "klt_yield_native"


def _has_yield_gate(path):
    source = path.read_text(encoding="utf-8")
    if NATIVE_MODULE not in source:
        return False
    for call in ast.walk(ast.parse(source, filename=str(path))):
        if not isinstance(call, ast.Call) or not call.args:
            continue
        target = call.args[0]
        if isinstance(target, ast.Constant) and target.value == NATIVE_MODULE:
            name = ast.unparse(call.func).rsplit(".", 1)[-1]
            if name in {"find_spec", "importorskip"}:
                return True
    return False


def test_native_yield_job_covers_gated_test_files():
    workflow = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
    matrix = workflow["jobs"]["native"]["strategy"]["matrix"]["include"]
    (job,) = [entry for entry in matrix if entry["crate"] == "yield"]
    covered = {node.split("::", 1)[0] for node in job["test_paths"].split()}
    gated = {
        path.relative_to(ROOT).as_posix()
        for path in (ROOT / "tests").rglob("*.py")
        if _has_yield_gate(path)
    }
    missing = gated - covered
    assert not missing, f"Add to the native yield job's test_paths: {sorted(missing)}"
