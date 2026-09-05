import ast
from pathlib import Path


def _specifier_segments(specifier: str) -> set[str]:
    return {segment for segment in specifier.split(".") if segment}


def _import_violations(node: ast.Import, forbidden_segments: set[str]) -> set[str]:
    return {
        alias.name
        for alias in node.names
        if _specifier_segments(alias.name) & forbidden_segments
    }


def _import_from_violations(
    node: ast.ImportFrom, forbidden_segments: set[str]
) -> set[str]:
    module = node.module or ""
    specifier = f"{'.' * node.level}{module}"
    imported_names = {alias.name for alias in node.names}
    violations: set[str] = set()
    if _specifier_segments(module) & forbidden_segments:
        violations.add(specifier)
    violations |= {
        f"from {specifier} import {name}"
        for name in imported_names & forbidden_segments
    }
    return violations


def assert_no_forbidden_imports(path: Path, forbidden_segments: set[str]) -> None:
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    violations: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            violations |= _import_violations(node, forbidden_segments)
        elif isinstance(node, ast.ImportFrom):
            violations |= _import_from_violations(node, forbidden_segments)
    assert not violations, (
        f"{path.name} must not import any of {forbidden_segments} (found: {violations})"
    )
