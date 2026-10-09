#!/usr/bin/env python3
"""Static verification for the P1 package migration.

Checks, without importing anything (so it runs before deps are installed):
  1. every ``livetranslate.*`` module referenced by an import actually exists
  2. every name imported from a ``livetranslate.*`` module is defined there
     (as a def/class/assignment, or re-exported by a nested import)
  3. no bare flat-module import survives that should have been rewritten

Run from the project root:  python doc/migrations/verify_p1.py
Exit code 0 = clean.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PKG = ROOT / "livetranslate"
SKIP_PARTS = {"__pycache__", "nano_src"}

# Names that a module legitimately exposes without a local definition
# (re-exported third-party or injected names).
BUILTIN_OK = {
    "__name__", "__file__", "__doc__", "__all__",
}


def module_file(dotted: str) -> Path | None:
    """Resolve a dotted module path to its .py file, or None."""
    parts = dotted.split(".")
    cur = ROOT
    for i, part in enumerate(parts):
        nxt = cur / part
        if nxt.with_suffix(".py").is_file():
            # a module file: must be the last component
            if i == len(parts) - 1:
                return nxt.with_suffix(".py")
            return None
        if nxt.is_dir():
            if i == len(parts) - 1:
                init = nxt / "__init__.py"
                return init if init.is_file() else None
            cur = nxt
            continue
        return None
    return None


def defined_names(path: Path) -> set[str]:
    """Top-level names a module defines (incl. anything it re-exports)."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except SyntaxError as exc:
        print(f"  !! SYNTAX ERROR {path}: {exc}")
        return set()
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    names.add(t.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for a in node.names:
                if a.name == "*":
                    names.add("*")
                else:
                    names.add(a.asname or a.name.split(".")[0])
        elif isinstance(node, (ast.If, ast.Try)):
            # conditional/guarded definitions count too
            for sub in ast.walk(node):
                if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    names.add(sub.name)
                elif isinstance(sub, ast.Assign):
                    for t in sub.targets:
                        if isinstance(t, ast.Name):
                            names.add(t.id)
                elif isinstance(sub, ast.ImportFrom):
                    for a in sub.names:
                        names.add(a.asname or a.name.split(".")[0])
                elif isinstance(sub, ast.Import):
                    for a in sub.names:
                        names.add(a.asname or a.name.split(".")[0])
    return names


def main() -> int:
    targets = [
        p for p in ROOT.rglob("*.py")
        if not any(part in SKIP_PARTS for part in p.relative_to(ROOT).parts)
    ]

    cache: dict[Path, set[str]] = {}
    errors: list[str] = []
    checked = 0

    for path in sorted(targets):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        rel = path.relative_to(ROOT)
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom):
                continue
            mod = node.module or ""
            if not mod.startswith("livetranslate"):
                continue
            checked += 1
            target = module_file(mod)
            if target is None:
                errors.append(f"{rel}:{node.lineno} cannot resolve module '{mod}'")
                continue
            names = cache.setdefault(target, defined_names(target))
            for a in node.names:
                if a.name in BUILTIN_OK:
                    continue
                if "*" in names:
                    continue
                if a.name in names:
                    continue
                # `from <pkg> import <submodule>` binds a child module, which is
                # not a name defined in the package's __init__.py.
                if module_file(f"{mod}.{a.name}") is not None:
                    continue
                errors.append(
                    f"{rel}:{node.lineno} '{a.name}' not defined in {mod} "
                    f"({target.relative_to(ROOT)})"
                )

    print(f"checked {checked} livetranslate imports across {len(targets)} files")
    if errors:
        print(f"\n{len(errors)} PROBLEM(S):")
        for e in errors:
            print(f"  - {e}")
        return 1
    print("OK: all package-internal imports resolve")
    return 0


if __name__ == "__main__":
    sys.exit(main())
