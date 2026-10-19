# backend/tests/manual/test_phase6_facade_awaits.py
"""
Static check: every call site of `self._require_token(provider_id)`
inside `SourceControlFacade` MUST be preceded by `await`.

This test parses the source file with `ast` so that it is not fooled
by comments, strings, or decorators. It complements the behavioural
test_phase6_facade.py, which only checks the method's signature and
cannot detect missing `await` at call sites.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path


TARGET_METHOD = "_require_token"
TARGET_ARG = "provider_id"


def _is_target_call(node: ast.AST) -> bool:
    """Return True if `node` is `self._require_token(provider_id)`."""
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    if not isinstance(func, ast.Attribute):
        return False
    if func.attr != TARGET_METHOD:
        return False
    if not isinstance(func.value, ast.Name) or func.value.id != "self":
        return False
    if len(node.args) != 1:
        return False
    arg = node.args[0]
    return isinstance(arg, ast.Name) and arg.id == TARGET_ARG


def main() -> int:
    src_path = Path("backend/omweb/services/source_control_facade.py")
    tree = ast.parse(src_path.read_text(encoding="utf-8"))

    # Collect all Call nodes that look like self._require_token(provider_id)
    # and record whether they are awaited.
    class Visitor(ast.NodeVisitor):
        def __init__(self) -> None:
            self.calls: list[tuple[int, bool]] = []  # (line, awaited)

        def visit_Await(self, node: ast.Await) -> None:
            if _is_target_call(node.value):
                self.calls.append((node.lineno, True))
                # Do not recurse: the awaited call is accounted for.
                return
            self.generic_visit(node)

        def visit_Call(self, node: ast.Call) -> None:
            if _is_target_call(node):
                self.calls.append((node.lineno, False))
            self.generic_visit(node)

    visitor = Visitor()
    visitor.visit(tree)

    awaited = [ln for ln, aw in visitor.calls if aw]
    not_awaited = [ln for ln, aw in visitor.calls if not aw]

    print(f"[INFO] call sites found: {len(visitor.calls)}")
    print(f"[INFO] awaited:          {len(awaited)}")
    print(f"[INFO] not awaited:      {len(not_awaited)}")

    if not_awaited:
        for ln in not_awaited:
            print(f"[FAIL] missing await at line {ln}")
        print()
        print("Add 'await' before every self._require_token(provider_id) call.")
        return 1

    if len(awaited) != 9:
        print(f"[FAIL] expected 9 awaited calls, got {len(awaited)}")
        return 1

    print()
    print("AWAIT COVERAGE PASSED (9/9)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
