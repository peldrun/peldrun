# backend/tests/memory/test_memory_service.py
"""
Integration tests for ``MemoryService`` (P1.7 task 1.7.3) and the
package-level singletons (P1.7 task 1.7.4).

Verifies:
  * lifecycle (ainitialize / aclose / is_available),
  * ``retrieve`` returns a MemoryContext in every mode,
  * the DISABLED mode fast path,
  * ``reload_settings`` reaches through to the lifecycle manager,
  * the module-level singleton (initialize / get / shutdown).
"""

from __future__ import annotations

import sys as _sys_bootstrap
from pathlib import Path as _Path_bootstrap

if __name__ == "__main__":
    _BACKEND_ROOT = _Path_bootstrap(__file__).resolve().parents[2]
    if str(_BACKEND_ROOT) not in _sys_bootstrap.path:
        _sys_bootstrap.path.insert(0, str(_BACKEND_ROOT))

# --- Normal imports follow ----------------------------------------------

import asyncio
import tempfile
from pathlib import Path

from peldrun.memory.domain import (
    MemoryContext,
    MemoryMode,
    default_settings,
)

from omweb.memory import (
    get_memory_service,
    initialize_memory_service,
    set_memory_service_for_tests,
    shutdown_memory_service,
)
from omweb.memory.manager.memory_service import MemoryService
from omweb.memory.manager.memory_settings_manager import (
    MemorySettingsManager,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run(coro):
    return asyncio.run(coro)


def _build_isolated_service(mode: MemoryMode = MemoryMode.READ_WRITE):
    """Build a MemoryService with its own temp DB + settings."""
    td = Path(tempfile.mkdtemp(prefix="peldrun_mem_svc_"))
    db_path = td / "memory.db"
    settings_path = td / "memory_settings.json"

    smgr = MemorySettingsManager(path=settings_path)
    settings = default_settings().model_copy(update={"mode": mode})
    _run(smgr.save(settings))

    svc = MemoryService(db_path=db_path, settings_path=settings_path)
    return svc, td


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


def test_ainitialize_opens_provider() -> None:
    svc, _td = _build_isolated_service()
    assert svc.is_available is False
    _run(svc.ainitialize())
    assert svc.is_available is True
    _run(svc.aclose())
    assert svc.is_available is False


def test_ainitialize_is_idempotent() -> None:
    svc, _td = _build_isolated_service()
    _run(svc.ainitialize())
    provider_1 = svc.provider
    _run(svc.ainitialize())
    provider_2 = svc.provider
    assert provider_1 is provider_2
    _run(svc.aclose())


def test_properties_raise_before_initialize() -> None:
    svc, _td = _build_isolated_service()
    for prop in ("mode", "settings", "provider"):
        try:
            getattr(svc, prop)
        except RuntimeError:
            continue
        raise AssertionError(f".{prop} must raise before ainitialize()")


# ---------------------------------------------------------------------------
# Retrieval
# ---------------------------------------------------------------------------


def test_retrieve_returns_empty_context_for_empty_query() -> None:
    svc, _td = _build_isolated_service()
    _run(svc.ainitialize())
    try:
        ctx = _run(
            svc.retrieve(
                query="",
                chat_id="chat_x",
                project_id="proj_y",
                run_id="run_z",
            )
        )
        assert isinstance(ctx, MemoryContext)
        assert ctx.is_empty is True
    finally:
        _run(svc.aclose())


def test_retrieve_in_disabled_mode_returns_empty() -> None:
    svc, _td = _build_isolated_service(mode=MemoryMode.DISABLED)
    _run(svc.ainitialize())
    try:
        ctx = _run(
            svc.retrieve(
                query="anything",
                chat_id="c",
                project_id="p",
            )
        )
        assert isinstance(ctx, MemoryContext)
        assert ctx.is_empty is True
        assert ctx.router_decision == "no_need"
    finally:
        _run(svc.aclose())


def test_retrieve_raises_before_initialize() -> None:
    svc, _td = _build_isolated_service()
    try:
        _run(
            svc.retrieve(
                query="hi",
                chat_id="c",
                project_id="p",
            )
        )
    except RuntimeError as exc:
        assert "not initialized" in str(exc).lower()
        return
    raise AssertionError("retrieve must raise before ainitialize()")


# ---------------------------------------------------------------------------
# Settings reload
# ---------------------------------------------------------------------------


def test_reload_settings_swaps_mode() -> None:
    svc, _td = _build_isolated_service(mode=MemoryMode.READ_WRITE)
    _run(svc.ainitialize())
    try:
        assert svc.mode == MemoryMode.READ_WRITE

        # Flip to read_only via the manager (simulating the API endpoint).
        _run(
            svc._settings_manager.update(mode="read_only")
        )
        new_settings = _run(svc.reload_settings())
        assert new_settings.mode == MemoryMode.READ_ONLY
        assert svc.mode == MemoryMode.READ_ONLY
    finally:
        _run(svc.aclose())


# ---------------------------------------------------------------------------
# Module-level singletons
# ---------------------------------------------------------------------------


def test_singleton_lifecycle() -> None:
    td = Path(tempfile.mkdtemp(prefix="peldrun_mem_singleton_"))
    db_path = td / "memory.db"
    settings_path = td / "memory_settings.json"

    # Clean slate
    _run(shutdown_memory_service())

    # Before init
    try:
        get_memory_service()
    except RuntimeError:
        pass
    else:
        raise AssertionError("get_memory_service must raise before init")

    # Init
    _run(
        initialize_memory_service(
            db_path=db_path,
            settings_path=settings_path,
        )
    )
    svc = get_memory_service()
    assert svc.is_available is True

    # Second init is idempotent
    _run(
        initialize_memory_service(
            db_path=db_path,
            settings_path=settings_path,
        )
    )
    assert get_memory_service() is svc

    # Shutdown
    _run(shutdown_memory_service())
    try:
        get_memory_service()
    except RuntimeError:
        pass
    else:
        raise AssertionError("get_memory_service must raise after shutdown")


def test_set_memory_service_for_tests_injects_and_clears() -> None:
    # Build an isolated service
    svc, _td = _build_isolated_service()
    _run(svc.ainitialize())

    try:
        set_memory_service_for_tests(svc)
        assert get_memory_service() is svc
    finally:
        set_memory_service_for_tests(None)
        try:
            get_memory_service()
        except RuntimeError:
            pass
        else:
            raise AssertionError(
                "get_memory_service must raise after "
                "set_memory_service_for_tests(None)"
            )
        _run(svc.aclose())


# ---------------------------------------------------------------------------
# Standalone runner
# ---------------------------------------------------------------------------


def _run_all() -> None:
    import traceback

    test_functions = [
        test_ainitialize_opens_provider,
        test_ainitialize_is_idempotent,
        test_properties_raise_before_initialize,
        test_retrieve_returns_empty_context_for_empty_query,
        test_retrieve_in_disabled_mode_returns_empty,
        test_retrieve_raises_before_initialize,
        test_reload_settings_swaps_mode,
        test_singleton_lifecycle,
        test_set_memory_service_for_tests_injects_and_clears,
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
