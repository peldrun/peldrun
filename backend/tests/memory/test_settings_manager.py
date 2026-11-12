# backend/tests/memory/test_settings_manager.py
"""
Unit tests for ``MemorySettingsManager`` (P1.7 task 1.7.1).

Verifies the file format, atomic write behavior, and the
"never-crash-on-corrupt-file" contract documented in the module
docstring.
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
import json
import tempfile
from pathlib import Path

from peldrun.memory.domain import MemoryMode, MemorySettings, default_settings
from omweb.memory.manager.memory_settings_manager import (
    MemorySettingsManager,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run(coro):
    return asyncio.run(coro)


def _make_temp_manager() -> tuple[MemorySettingsManager, Path]:
    """Return a manager bound to a fresh temp file (not yet created)."""
    td = tempfile.mkdtemp(prefix="peldrun_mem_settings_")
    path = Path(td) / "memory_settings.json"
    return MemorySettingsManager(path=path), path


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_load_missing_file_returns_defaults() -> None:
    """A non-existent settings file yields plan-default values."""
    mgr, path = _make_temp_manager()
    assert not path.exists()
    settings = _run(mgr.load())
    assert isinstance(settings, MemorySettings)
    assert settings.mode == MemoryMode.READ_WRITE
    assert settings.provider_id == "sqlite_fts"
    # File was NOT created by a read.
    assert not path.exists()


def test_save_and_load_roundtrip() -> None:
    """save() then load() returns an identical configuration."""
    mgr, path = _make_temp_manager()
    original = default_settings().model_copy(
        update={"mode": MemoryMode.READ_ONLY}
    )
    _run(mgr.save(original))
    assert path.exists()

    loaded = _run(mgr.load())
    assert loaded.mode == MemoryMode.READ_ONLY
    assert loaded.provider_id == original.provider_id
    # Full equality of the model dump proves round-trip safety.
    assert (
        loaded.model_dump(mode="json")
        == original.model_dump(mode="json")
    )


def test_load_corrupt_json_returns_defaults() -> None:
    """Malformed JSON does not crash; falls back to defaults."""
    mgr, path = _make_temp_manager()
    path.write_text("{ not valid json", encoding="utf-8")
    settings = _run(mgr.load())
    assert settings.mode == MemoryMode.READ_WRITE  # default
    # The corrupt file was NOT overwritten by the read.
    assert "not valid json" in path.read_text(encoding="utf-8")


def test_load_invalid_fields_returns_defaults() -> None:
    """A structurally valid JSON with a bad field value falls back to defaults."""
    mgr, path = _make_temp_manager()
    path.write_text(
        json.dumps({"mode": "not_a_real_mode"}),
        encoding="utf-8",
    )
    settings = _run(mgr.load())
    assert settings.mode == MemoryMode.READ_WRITE


def test_update_merges_and_validates() -> None:
    """update() applies overrides and re-validates via Pydantic."""
    mgr, path = _make_temp_manager()
    new_settings = _run(mgr.update(mode="read_only"))
    assert new_settings.mode == MemoryMode.READ_ONLY

    # Persisted to disk.
    loaded = _run(mgr.load())
    assert loaded.mode == MemoryMode.READ_ONLY

    # The original file is a fully-formed JSON document.
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["mode"] == "read_only"


def test_update_rejects_invalid_value_before_writing() -> None:
    """An invalid override raises BEFORE the file is touched."""
    mgr, path = _make_temp_manager()
    _run(mgr.save(default_settings()))
    original_bytes = path.read_bytes()

    try:
        _run(mgr.update(mode="bogus_mode"))
    except Exception:
        pass  # expected: pydantic.ValidationError
    else:
        raise AssertionError(
            "expected update() to reject invalid mode value"
        )

    # File content unchanged.
    assert path.read_bytes() == original_bytes


def test_atomic_write_leaves_no_tmp_files() -> None:
    """A successful save leaves only the target file in the directory."""
    mgr, path = _make_temp_manager()
    _run(mgr.save(default_settings()))
    siblings = sorted(p.name for p in path.parent.iterdir())
    assert siblings == ["memory_settings.json"], siblings


# ---------------------------------------------------------------------------
# Standalone runner
# ---------------------------------------------------------------------------


def _run_all() -> None:
    import traceback

    test_functions = [
        test_load_missing_file_returns_defaults,
        test_save_and_load_roundtrip,
        test_load_corrupt_json_returns_defaults,
        test_load_invalid_fields_returns_defaults,
        test_update_merges_and_validates,
        test_update_rejects_invalid_value_before_writing,
        test_atomic_write_leaves_no_tmp_files,
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
