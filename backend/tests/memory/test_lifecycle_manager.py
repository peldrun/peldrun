# backend/tests/memory/test_lifecycle_manager.py
"""
Unit tests for ``MemoryLifecycleManager`` (P1.7 task 1.7.2).

Verifies the lifecycle contract, provider rebuild rules under
``reload_settings``, and the disabled-mode "no DB I/O" guarantee.
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
from unittest.mock import AsyncMock, MagicMock, patch

from peldrun.memory.domain import (
    MemoryMode,
    MemorySettings,
    NullMemoryProvider,
    default_settings,
)
from peldrun.memory.providers import SqliteFtsProvider

from omweb.memory.manager.memory_lifecycle import MemoryLifecycleManager
from omweb.memory.manager.memory_settings_manager import (
    MemorySettingsManager,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run(coro):
    return asyncio.run(coro)


def _make_manager_with_settings(
    settings: MemorySettings,
    *,
    db_path: Path,
) -> tuple[MemoryLifecycleManager, MemorySettingsManager]:
    """Build a manager with settings persisted to a temp file."""
    td = tempfile.mkdtemp(prefix="peldrun_mem_lifecycle_")
    settings_path = Path(td) / "memory_settings.json"
    smgr = MemorySettingsManager(path=settings_path)
    _run(smgr.save(settings))
    lc = MemoryLifecycleManager(settings_manager=smgr, db_path=db_path)
    return lc, smgr


def _temp_db_path() -> Path:
    td = tempfile.mkdtemp(prefix="peldrun_mem_db_")
    return Path(td) / "memory.db"


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_initialize_opens_real_provider() -> None:
    """READ_WRITE mode: initialize opens a real SqliteFtsProvider."""
    db_path = _temp_db_path()
    lc, _smgr = _make_manager_with_settings(
        default_settings(), db_path=db_path
    )
    assert lc.is_ready is False

    _run(lc.ainitialize())

    assert lc.is_ready is True
    assert isinstance(lc.provider, SqliteFtsProvider)

    # ``is_open`` is an ASYNC method (per BaseMemoryProvider's
    # contract -- see providers/base.py). A synchronous
    # ``is_open()`` call would produce a coroutine that is
    # immediately garbage-collected, emitting a
    # ``RuntimeWarning: coroutine was never awaited`` and
    # silently passing the assertion regardless of the actual
    # provider state. Await it properly so the test verifies
    # the real open state.
    is_open_state = _run(lc.provider.is_open())
    assert is_open_state is True, (
        f"expected provider.is_open() == True after ainitialize(), "
        f"got {is_open_state}"
    )

    # The DB file was created by the SQLite migrations on open.
    assert db_path.exists(), f"expected DB at {db_path} to exist after init"


def test_initialize_is_idempotent() -> None:
    """A second ainitialize() call is a no-op (same provider object)."""
    db_path = _temp_db_path()
    lc, _smgr = _make_manager_with_settings(
        default_settings(), db_path=db_path
    )

    _run(lc.ainitialize())
    first_provider = lc.provider

    _run(lc.ainitialize())
    second_provider = lc.provider

    assert first_provider is second_provider


def test_initialize_disabled_mode_uses_null_provider_and_no_db_io() -> None:
    """DISABLED mode: provider is NullMemoryProvider; DB file not touched."""
    db_path = _temp_db_path()
    disabled = default_settings().model_copy(
        update={"mode": MemoryMode.DISABLED}
    )
    lc, _smgr = _make_manager_with_settings(disabled, db_path=db_path)

    _run(lc.ainitialize())

    assert lc.is_ready is True
    assert isinstance(lc.provider, NullMemoryProvider)
    # THE KEY ASSERTION: no DB file was created.
    assert not db_path.exists(), (
        "DISABLED mode must NOT touch the SQLite file "
        "(acceptance test #11)"
    )


def test_close_before_initialize_is_safe() -> None:
    """aclose() on a never-initialized manager is a no-op."""
    lc, _smgr = _make_manager_with_settings(
        default_settings(), db_path=_temp_db_path()
    )
    _run(lc.aclose())  # must not raise
    assert lc.is_ready is False


def test_close_is_idempotent() -> None:
    """Two aclose() calls in a row must not raise or change state."""
    lc, _smgr = _make_manager_with_settings(
        default_settings(), db_path=_temp_db_path()
    )
    _run(lc.ainitialize())
    _run(lc.aclose())
    assert lc.is_ready is False
    _run(lc.aclose())  # second call: must not raise
    assert lc.is_ready is False


def test_provider_property_raises_before_initialize() -> None:
    """Accessing .provider before ainitialize() raises RuntimeError."""
    lc, _smgr = _make_manager_with_settings(
        default_settings(), db_path=_temp_db_path()
    )
    try:
        _ = lc.provider
    except RuntimeError as exc:
        assert "not initialized" in str(exc).lower()
        return
    raise AssertionError("expected RuntimeError from .provider")


def test_settings_property_raises_before_initialize() -> None:
    """Accessing .settings before ainitialize() raises RuntimeError."""
    lc, _smgr = _make_manager_with_settings(
        default_settings(), db_path=_temp_db_path()
    )
    try:
        _ = lc.settings
    except RuntimeError as exc:
        assert "not initialized" in str(exc).lower()
        return
    raise AssertionError("expected RuntimeError from .settings")


def test_reload_settings_same_provider_keeps_instance() -> None:
    """Reload without provider/mode change keeps the SAME provider object."""
    db_path = _temp_db_path()
    lc, smgr = _make_manager_with_settings(
        default_settings(), db_path=db_path
    )
    _run(lc.ainitialize())
    first_provider = lc.provider

    # Persist a settings change that affects only retrieval tuning.
    _run(
        smgr.update(
            min_relevance_score=0.5,
            default_limit=10,
        )
    )
    new_settings = _run(lc.reload_settings())

    # Same provider object -- no rebuild.
    assert lc.provider is first_provider
    # New settings are visible on the manager.
    assert new_settings.min_relevance_score == 0.5
    assert new_settings.default_limit == 10


def test_reload_settings_flip_to_disabled_closes_provider() -> None:
    """Reload that flips mode to DISABLED closes the real provider."""
    db_path = _temp_db_path()
    lc, smgr = _make_manager_with_settings(
        default_settings(), db_path=db_path
    )
    _run(lc.ainitialize())
    assert isinstance(lc.provider, SqliteFtsProvider)

    _run(smgr.update(mode="disabled"))
    _run(lc.reload_settings())

    assert isinstance(lc.provider, NullMemoryProvider)


def test_reload_settings_flip_from_disabled_opens_provider() -> None:
    """Reload that flips mode OUT of DISABLED opens the real provider."""
    db_path = _temp_db_path()
    disabled = default_settings().model_copy(
        update={"mode": MemoryMode.DISABLED}
    )
    lc, smgr = _make_manager_with_settings(disabled, db_path=db_path)
    _run(lc.ainitialize())
    assert isinstance(lc.provider, NullMemoryProvider)
    assert not db_path.exists()

    _run(smgr.update(mode="read_write"))
    _run(lc.reload_settings())

    assert isinstance(lc.provider, SqliteFtsProvider)
    # Opening the provider ran migrations, so the DB file now exists.
    assert db_path.exists()


def test_reload_settings_provider_id_change_rebuilds() -> None:
    """A provider_id change triggers a rebuild even in the same mode."""
    db_path = _temp_db_path()
    lc, smgr = _make_manager_with_settings(
        default_settings(), db_path=db_path
    )
    _run(lc.ainitialize())
    first_provider = lc.provider

    _run(smgr.update(provider_id="does_not_exist"))
    _run(lc.reload_settings())

    # Unknown provider_id -> factory returns NullMemoryProvider.
    # Old provider was closed; new "provider" is Null.
    assert lc.provider is not first_provider
    assert isinstance(lc.provider, NullMemoryProvider)


def test_reload_settings_before_initialize_raises() -> None:
    """reload_settings() before ainitialize() raises RuntimeError."""
    lc, _smgr = _make_manager_with_settings(
        default_settings(), db_path=_temp_db_path()
    )
    try:
        _run(lc.reload_settings())
    except RuntimeError as exc:
        assert "requires a ready manager" in str(exc)
        return
    raise AssertionError("expected RuntimeError from reload_settings")


# ---------------------------------------------------------------------------
# Standalone runner
# ---------------------------------------------------------------------------


def _run_all() -> None:
    import traceback

    test_functions = [
        test_initialize_opens_real_provider,
        test_initialize_is_idempotent,
        test_initialize_disabled_mode_uses_null_provider_and_no_db_io,
        test_close_before_initialize_is_safe,
        test_close_is_idempotent,
        test_provider_property_raises_before_initialize,
        test_settings_property_raises_before_initialize,
        test_reload_settings_same_provider_keeps_instance,
        test_reload_settings_flip_to_disabled_closes_provider,
        test_reload_settings_flip_from_disabled_opens_provider,
        test_reload_settings_provider_id_change_rebuilds,
        test_reload_settings_before_initialize_raises,
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
