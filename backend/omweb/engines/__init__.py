"""
PELDRUN Universal Engines Architecture Package.

Provides clean engine abstraction, registry management, and native adapters.
"""

from .base import EngineRunContext, ExecutionEngine
from .openmanus_engine import OpenManusEngine
from .peldrun_engine import PeldrunEngine
from .registry import EngineNotFoundError, EngineRegistry

# Global authoritative engine registry instance pre-loaded with supported engines
engine_registry = EngineRegistry()
engine_registry.register(PeldrunEngine(), aliases=["peldrun-core", "core"])
engine_registry.register(OpenManusEngine(), aliases=["legacy", "manus"])

__all__ = [
    "ExecutionEngine",
    "EngineRunContext",
    "EngineRegistry",
    "EngineNotFoundError",
    "PeldrunEngine",
    "OpenManusEngine",
    "engine_registry",
]