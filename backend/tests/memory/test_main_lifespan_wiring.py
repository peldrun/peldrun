# backend/tests/memory/test_main_lifespan_wiring.py
"""
AST-level structural test for the memory subsystem wiring in
``omweb/main.py`` (P1.7 task 1.7.5).

Verifies that the FastAPI lifespan:
  * imports and calls ``initialize_memory_service()`` on startup,
  * imports and calls ``shutdown_memory_service()`` on shutdown.

A structural test is the right scope here: running the full
FastAPI lifespan requires a live event loop, a temp storage root,
and orchestration of the resource monitor, temp cleaner, MCP pool,
etc. -- all unrelated to memory. The contract that matters for
P1.7 is "the two calls exist inside the ``lifespan`` function
body", which ``ast`` inspects without executing anything.

The end-to-end run (initialize on boot, retrieve during a
request, shutdown on teardown) is covered by the integration test
that will be added once P1.8 lands its API surface.

Runnable via pytest OR standalone:
    python tests/memory/test_main_lifespan_wiring.py
"""

from __future__ import annotations

import sys as _sys_bootstrap
from pathlib import Path as _Path_bootstrap

if __name__ == "__main__":
    _BACKEND_ROOT = _Path_bootstrap(__file__).resolve().parents[2]
    if str(_BACKEND_ROOT) not in _sys_bootstrap.path:
        _sys_bootstrap.path.insert(0, str(_BACKEND_ROOT))

# --- Normal imports follow ----------------------------------------------

import ast
import inspect
from pathlib import Path


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _main_py_path() -> Path:
    """Return the path to omweb/main.py relative to the backend root."""
    # tests/memory/test_*.py -> parents[0]=memory, [1]=tests, [2]=backend
    backend_root = Path(__file__).resolve().parents[2]
    return backend_root / "omweb" / "main.py"


def _read_main_py_source() -> str:
    """Read ``omweb/main.py``, stripping any UTF-8 BOM.

    The file currently starts with a BOM (U+FEFF) -- harmless to
    Python's normal module loader, which handles ``utf-8-sig``
    transparently, but fatal to ``ast.parse()`` on a raw string.
    Reading with ``utf-8-sig`` handles both BOM-present and
    BOM-absent files identically.
    """
    return _main_py_path().read_text(encoding="utf-8-sig")


def _find_lifespan_async_func(source: str) -> ast.AsyncFunctionDef:
    """Return the AST node for the ``lifespan`` async function.

    The function may be decorated with ``@asynccontextmanager``.
    We identify it by its name and its ``async def`` declaration;
    there is exactly one such function in ``omweb/main.py``.
    """
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.AsyncFunctionDef)
            and node.name == "lifespan"
        ):
            return node
    raise AssertionError(
        "no 'async def lifespan' found in omweb/main.py"
    )


def _collect_function_calls(func_node: ast.AST) -> set[str]:
    """Collect the bare names of every function CALL inside ``func_node``.

    Returns the last attribute name for ``foo.bar()`` calls, and the
    plain name for ``foo()`` calls. Nested function definitions
    are traversed as well (the memory init is inside a ``try``
    block, not a nested function, but this makes the check
    tolerant of future refactors).
    """
    names: set[str] = set()
    for node in ast.walk(func_node):
        if not isinstance(node, ast.Call):
            continue
        target = node.func
        if isinstance(target, ast.Name):
            names.add(target.id)
        elif isinstance(target, ast.Attribute):
            names.add(target.attr)
    return names


def _collect_function_imports(func_node: ast.AST) -> set[str]:
    """Collect names imported INSIDE ``func_node`` (function-local imports).

    Returns the "as" name when present, else the last module
    segment. E.g. ``from omweb.memory import initialize_memory_service``
    contributes ``{"initialize_memory_service"}``.
    """
    names: set[str] = set()
    for node in ast.walk(func_node):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                names.add(alias.asname or alias.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                # ``import foo`` -> add "foo"; ``import foo as bar`` -> "bar"
                if alias.asname:
                    names.add(alias.asname)
                else:
                    names.add(alias.name.split(".")[0])
    return names


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_lifespan_function_exists() -> None:
    """Sanity: the AST helper locates the ``lifespan`` function."""
    path = _main_py_path()
    assert path.exists(), f"omweb/main.py not found at {path}"
    fn = _find_lifespan_async_func(_read_main_py_source())
    assert fn.name == "lifespan"


def test_lifespan_imports_initialize_memory_service() -> None:
    """``initialize_memory_service`` must be imported inside lifespan."""
    path = _main_py_path()
    fn = _find_lifespan_async_func(_read_main_py_source())
    imported = _collect_function_imports(fn)
    assert "initialize_memory_service" in imported, (
        "lifespan must import initialize_memory_service from "
        "omweb.memory; found imports: " + repr(sorted(imported))
    )


def test_lifespan_imports_shutdown_memory_service() -> None:
    """``shutdown_memory_service`` must be imported inside lifespan."""
    path = _main_py_path()
    fn = _find_lifespan_async_func(_read_main_py_source())
    imported = _collect_function_imports(fn)
    assert "shutdown_memory_service" in imported, (
        "lifespan must import shutdown_memory_service from "
        "omweb.memory; found imports: " + repr(sorted(imported))
    )


def test_lifespan_calls_initialize_memory_service() -> None:
    """``initialize_memory_service()`` must be CALLED in lifespan."""
    path = _main_py_path()
    fn = _find_lifespan_async_func(_read_main_py_source())
    calls = _collect_function_calls(fn)
    assert "initialize_memory_service" in calls, (
        "lifespan must call initialize_memory_service(); "
        "found calls: " + repr(sorted(calls))
    )


def test_lifespan_calls_shutdown_memory_service() -> None:
    """``shutdown_memory_service()`` must be CALLED in lifespan."""
    path = _main_py_path()
    fn = _find_lifespan_async_func(_read_main_py_source())
    calls = _collect_function_calls(fn)
    assert "shutdown_memory_service" in calls, (
        "lifespan must call shutdown_memory_service(); "
        "found calls: " + repr(sorted(calls))
    )


def test_omweb_memory_exports_are_importable() -> None:
    """The public API required by lifespan wiring must exist."""
    from omweb.memory import (
        initialize_memory_service,
        shutdown_memory_service,
        get_memory_service,
    )
    assert callable(initialize_memory_service)
    assert callable(shutdown_memory_service)
    assert callable(get_memory_service)


# ---------------------------------------------------------------------------
# Standalone runner
# ---------------------------------------------------------------------------


def _run_all() -> None:
    import traceback

    test_functions = [
        test_lifespan_function_exists,
        test_lifespan_imports_initialize_memory_service,
        test_lifespan_imports_shutdown_memory_service,
        test_lifespan_calls_initialize_memory_service,
        test_lifespan_calls_shutdown_memory_service,
        test_omweb_memory_exports_are_importable,
    ]
    failures = 0
    for fn in test_functions:
        name = fn.__name__
        try:
            fn()
            print(f"PASS  {name}")
        except AssertionError as exc:
            failures += 1
            print(f"FAIL  {name}: {exc}")
            traceback.print_exc()
        except Exception as exc:  # noqa: BLE001
            failures += 1
            print(f"ERROR {name}: {exc}")
            traceback.print_exc()

    print()
    if failures:
        print(f"=== {failures} test(s) failed ===")
        raise SystemExit(1)
    print(f"=== all {len(test_functions)} tests passed ===")


if __name__ == "__main__":
    _run_all()
