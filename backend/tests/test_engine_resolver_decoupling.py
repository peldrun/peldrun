"""
backend/tests/test_engine_resolver_decoupling.py

Unit test suite verifying P1-01 Engine Resolver Decoupling:
1. Strict resolution for 'peldrun' and 'openmanus'.
2. Canonical normalization of aliases ('peldrun-core', 'core', 'legacy', 'manus').
3. Fail-fast error propagation for unknown engine IDs (UnknownEngineError).
4. Explicit error handling for missing configuration (EngineConfigurationError).
5. Elimination of runtime heuristic parent/home directory probing.
6. Diagnostic health reporting in get_engine_status().
"""

import json
from pathlib import Path
import pytest

from omweb import engine_resolver
from omweb.engine_resolver import (
    EngineConfigurationError,
    EngineType,
    UnknownEngineError,
    get_active_engine_name,
    get_active_engine_type,
    get_engine_status,
    resolve_active_engine_path,
    set_active_engine_type,
    validate_engine_id,
)


def test_validate_engine_id_core_and_aliases():
    """Verify primary and alias identifiers for peldrun core resolve correctly."""
    assert validate_engine_id("peldrun") == "peldrun"
    assert validate_engine_id("peldrun-core") == "peldrun"
    assert validate_engine_id("core") == "peldrun"
    assert validate_engine_id("PELDRUN") == "peldrun"


def test_validate_engine_id_legacy_and_aliases():
    """Verify primary and alias identifiers for legacy engine resolve correctly."""
    assert validate_engine_id("openmanus") == "openmanus"
    assert validate_engine_id("legacy") == "openmanus"
    assert validate_engine_id("manus") == "openmanus"
    assert validate_engine_id("OPENMANUS") == "openmanus"


def test_validate_engine_id_empty_raises_configuration_error():
    """Verify empty or whitespace-only engine IDs raise EngineConfigurationError."""
    with pytest.raises(EngineConfigurationError):
        validate_engine_id("")

    with pytest.raises(EngineConfigurationError):
        validate_engine_id("   ")


def test_validate_engine_id_unknown_raises_fail_fast():
    """Verify unknown engine IDs raise UnknownEngineError with informative message."""
    with pytest.raises(UnknownEngineError) as exc_info:
        validate_engine_id("unregistered_engine_xyz")

    assert "unregistered_engine_xyz" in str(exc_info.value)
    assert "Supported engines" in str(exc_info.value) or "Registered engines" in str(exc_info.value)


def test_get_active_engine_type_explicit(monkeypatch, tmp_path):
    """Verify explicit configuration resolves cleanly without heuristic search."""
    fake_config = tmp_path / "engine_config.json"
    fake_config.write_text(json.dumps({"engine_type": "peldrun"}), encoding="utf-8")
    monkeypatch.setattr(engine_resolver, "CONFIG_FILE", fake_config)

    assert get_active_engine_type() == EngineType.CORE
    assert get_active_engine_name() == "peldrun"


def test_get_active_engine_type_missing_raises_error(monkeypatch, tmp_path):
    """Verify missing engine configuration raises EngineConfigurationError."""
    fake_config = tmp_path / "engine_config.json"
    fake_config.write_text(json.dumps({}), encoding="utf-8")
    monkeypatch.setattr(engine_resolver, "CONFIG_FILE", fake_config)

    with pytest.raises(EngineConfigurationError):
        get_active_engine_type(allow_default=False)

    # When allow_default is True, it safely falls back to CORE
    assert get_active_engine_type(allow_default=True) == EngineType.CORE


def test_get_active_engine_type_unknown_raises_error(monkeypatch, tmp_path):
    """Verify persisted unknown engine ID raises UnknownEngineError."""
    fake_config = tmp_path / "engine_config.json"
    fake_config.write_text(json.dumps({"engine_type": "unsupported_v2"}), encoding="utf-8")
    monkeypatch.setattr(engine_resolver, "CONFIG_FILE", fake_config)

    with pytest.raises(UnknownEngineError):
        get_active_engine_type()


def test_set_active_engine_type_persists_canonical_id(monkeypatch, tmp_path):
    """Verify setting engine type persists normalized identifier and cleans legacy keys."""
    fake_config = tmp_path / "engine_config.json"
    fake_config.write_text(json.dumps({"peldrun_path": "/obsolete/path"}), encoding="utf-8")
    monkeypatch.setattr(engine_resolver, "CONFIG_FILE", fake_config)

    set_active_engine_type("peldrun-core")

    saved_data = json.loads(fake_config.read_text(encoding="utf-8"))
    assert saved_data["engine_type"] == "peldrun"
    assert "peldrun_path" not in saved_data


def test_resolve_active_engine_path_core_isolation(monkeypatch, tmp_path):
    """Verify core engine resolves deterministically to backend peldrun directory."""
    fake_config = tmp_path / "engine_config.json"
    fake_config.write_text(json.dumps({"engine_type": "peldrun"}), encoding="utf-8")
    monkeypatch.setattr(engine_resolver, "CONFIG_FILE", fake_config)

    resolved = resolve_active_engine_path()
    assert resolved.exists()
    assert (resolved == engine_resolver.BACKEND_DIR / "peldrun") or (resolved == engine_resolver.BACKEND_DIR)


def test_resolve_active_engine_path_legacy_unconfigured_fails_fast(monkeypatch, tmp_path):
    """Verify selecting legacy engine without valid installation fails fast with FileNotFoundError."""
    fake_config = tmp_path / "engine_config.json"
    fake_config.write_text(json.dumps({"engine_type": "openmanus", "openmanus_path": str(tmp_path / "non_existent")}), encoding="utf-8")
    monkeypatch.setattr(engine_resolver, "CONFIG_FILE", fake_config)

    with pytest.raises(FileNotFoundError) as exc_info:
        resolve_active_engine_path()

    assert "does not exist or is not a valid OpenManus directory" in str(exc_info.value)


def test_get_engine_status_diagnostics(monkeypatch, tmp_path):
    """Verify get_engine_status returns structured diagnostic metadata."""
    fake_config = tmp_path / "engine_config.json"
    fake_config.write_text(json.dumps({"engine_type": "peldrun"}), encoding="utf-8")
    monkeypatch.setattr(engine_resolver, "CONFIG_FILE", fake_config)

    status = get_engine_status()
    assert status["active_engine"] == "peldrun"
    assert status["core_embedded"] is True
    assert "engine_path" in status
    assert "legacy_available" in status