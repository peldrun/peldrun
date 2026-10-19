# backend/tests/manual/test_phase7_resolve_repo.py
"""
Acceptance test for the `_resolve_repo` wiring.

Verifies:
  1. `_resolve_repo` exists and is async.
  2. All 7 previously-broken methods now call `self._resolve_repo`.
  3. No method outside `_resolve_repo` calls `adapter.get_repository`
     with a positional string.
"""

from __future__ import annotations

import ast
import inspect
import sys
from pathlib import Path


FACADE_PATH = Path("backend/omweb/services/source_control_facade.py")


def main() -> int:
    failures: list[str] = []

    def check(label: str, cond: bool, detail: str = "") -> None:
        if cond:
            print(f"[ OK ] {label}")
        else:
            failures.append(label)
            print(f"[FAIL] {label}" + (f" — {detail}" if detail else ""))

    # --- 1. Method exists and is async -----------------------------
    from omweb.services.source_control_facade import SourceControlFacade

    check(
        "_resolve_repo exists",
        hasattr(SourceControlFacade, "_resolve_repo"),
    )
    check(
        "_resolve_repo is async",
        inspect.iscoroutinefunction(SourceControlFacade._resolve_repo),
    )

    # --- 2. Parse AST ----------------------------------------------
    src = FACADE_PATH.read_text(encoding="utf-8")
    tree = ast.parse(src)

    facade_class: ast.ClassDef | None = None
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == "SourceControlFacade":
            facade_class = node
            break

    if facade_class is None:
        check("SourceControlFacade class found", False)
        return 1

    # Get the line range of _resolve_repo
    resolve_repo_lineno: int | None = None
    resolve_repo_endline: int | None = None
    for item in facade_class.body:
        if (
            isinstance(item, (ast.AsyncFunctionDef, ast.FunctionDef))
            and item.name == "_resolve_repo"
        ):
            resolve_repo_lineno = item.lineno
            resolve_repo_endline = item.end_lineno
            break

    check(
        "_resolve_repo line range found",
        resolve_repo_lineno is not None,
    )
    if resolve_repo_lineno is None:
        return 1

    print(
        f"[INFO] _resolve_repo spans lines "
        f"{resolve_repo_lineno}-{resolve_repo_endline}"
    )

    # --- 3. Every previously-broken method uses self._resolve_repo -
    target_methods = [
        "get_repository",
        "list_branches",
        "create_branch",
        "read_file",
        "create_workspace",
        "run_checks",
        "create_change_request",
    ]

    for method_name in target_methods:
        method: ast.AsyncFunctionDef | ast.FunctionDef | None = None
        for item in facade_class.body:
            if (
                isinstance(item, (ast.AsyncFunctionDef, ast.FunctionDef))
                and item.name == method_name
            ):
                method = item
                break

        if method is None:
            check(f"{method_name} exists", False)
            continue

        method_source = ast.get_source_segment(src, method) or ""
        uses = "self._resolve_repo" in method_source
        check(
            f"{method_name} uses self._resolve_repo",
            uses,
        )

    # --- 4. No direct adapter.get_repository(str) outside _resolve_repo
    direct_calls_outside: list[int] = []

    class Visitor(ast.NodeVisitor):
        def visit_Call(self, node: ast.Call) -> None:
            func = node.func
            if (
                isinstance(func, ast.Attribute)
                and func.attr == "get_repository"
                and isinstance(func.value, ast.Name)
                and func.value.id == "adapter"
            ):
                # Determine whether the first arg is positional (bad)
                # or ``repo=`` keyword (ok).
                has_positional_first = bool(node.args)
                has_repo_kw = any(kw.arg == "repo" for kw in node.keywords)

                # Inside _resolve_repo, both shapes are allowed (the
                # helper is the only place that legitimately talks to
                # the adapter this way).
                inside_helper = (
                    resolve_repo_lineno
                    <= node.lineno
                    <= (resolve_repo_endline or resolve_repo_lineno)
                )

                if not inside_helper and has_positional_first and not has_repo_kw:
                    direct_calls_outside.append(node.lineno)

            self.generic_visit(node)

    Visitor().visit(tree)

    check(
        "no direct adapter.get_repository(str) calls outside _resolve_repo",
        not direct_calls_outside,
        f"lines: {direct_calls_outside}",
    )

    # --- 5. Count self._resolve_repo call sites --------------------
    resolve_repo_calls = 0

    class Counter(ast.NodeVisitor):
        def visit_Call(self, node: ast.Call) -> None:
            nonlocal resolve_repo_calls
            func = node.func
            if (
                isinstance(func, ast.Attribute)
                and func.attr == "_resolve_repo"
                and isinstance(func.value, ast.Name)
                and func.value.id == "self"
            ):
                resolve_repo_calls += 1
            self.generic_visit(node)

    Counter().visit(tree)
    check(
        "7 self._resolve_repo call sites",
        resolve_repo_calls == 7,
        f"got {resolve_repo_calls}",
    )

    print()
    if failures:
        print(f"{len(failures)} FAILURE(S)")
        return 1
    print("PHASE 7 (RESOLVE_REPO) ACCEPTANCE PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
