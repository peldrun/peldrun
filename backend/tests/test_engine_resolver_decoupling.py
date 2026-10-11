"""
backend/tests/test_engine_resolver_decoupling.py

Unit test suite verifying Single Engine Resolver Decoupling:
1. Strict resolution for 'peldrun' and core aliases ('peldrun-core', 'core').
2. Explicit rejection of legacy OpenManus identifiers ('openmanus', 'legacy', 'manus').
3. Fail-fast error propagation for unknown engine IDs (UnknownEngineError).
4. Validation error for empty engine IDs (EngineConfigurationError).
5. Elimination of engine_config.json disk coupling.
6. Diagnostic health reporting reflecting zero legacy availability.
"""

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


def test_validate_engine_id_legacy_and_aliases_rejected():
    """Verify primary and alias identifiers for legacy engine are explicitly rejected."""
    for legacy_id in ["openmanus", "legacy", "manus", "OPENMANUS"]:
        with pytest.raises(ValueError, match="OpenManus is no longer supported"):
            validate_engine_id(legacy_id)


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
    assert "Supported engine" in str(exc_info.value)


def test_get_active_engine_type_deterministic():
    """Verify active engine resolves deterministically to CORE without disk reads."""
    assert get_active_engine_type() == EngineType.CORE
    assert get_active_engine_name() == "peldrun"


def test_set_active_engine_type_validation():
    """Verify setting engine type accepts valid core and rejects legacy IDs."""
    set_active_engine_type("peldrun")
    set_active_engine_type("peldrun-core")
    set_active_engine_type(EngineType.CORE)

    with pytest.raises(ValueError, match="OpenManus is no longer supported"):
        set_active_engine_type("openmanus")


def test_resolve_active_engine_path_core_isolation():
    """Verify core engine resolves deterministically to backend peldrun directory."""
    resolved = resolve_active_engine_path()
    assert resolved.exists()
    assert (resolved == engine_resolver.BACKEND_DIR / "peldrun") or (resolved == engine_resolver.BACKEND_DIR)


def test_legacy_engine_strictly_unavailable():
    """Verify legacy engine availability checks strictly return False."""
    assert engine_resolver.is_legacy_engine_available() is False
    assert engine_resolver.is_valid_legacy_dir(Path("/any/path")) is False


def test_get_engine_status_diagnostics():
    """Verify get_engine_status returns structured diagnostic metadata reflecting core authority."""
    status = get_engine_status()
    assert status["active_engine"] == "peldrun"
    assert status["core_embedded"] is True
    assert status["legacy_available"] is False
    assert "engine_path" in status
