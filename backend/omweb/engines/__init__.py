"""
backend/omweb/engines/__init__.py

PELDRUN Universal Engines Architecture Package.

Provides clean engine abstraction, registry management, and native adapters.
Initializes contracts and registry upfront to guarantee zero circular import cycles.
Hardened under PR 2: OpenManus engine removed completely.
PELDRUN Core is the sole registered runtime engine.
"""

from .base import EngineRunContext, ExecutionEngine
from .registry import EngineNotFoundError, EngineRegistry

# Global authoritative engine registry instance
engine_registry = EngineRegistry()

from .peldrun_engine import PeldrunEngine

# Authoritative registration: PELDRUN Core is the sole registered runtime engine.
engine_registry.register(PeldrunEngine(), aliases=["peldrun-core", "core"])

__all__ = [
    "ExecutionEngine",
    "EngineRunContext",
    "EngineRegistry",
    "EngineNotFoundError",
    "PeldrunEngine",
    "engine_registry",
]
