"""
PELDRUN Universal Engines Architecture Package.

Provides clean engine abstraction, registry management, and native adapters.
Initializes contracts and registry upfront to guarantee zero circular import cycles.
"""

from .base import EngineRunContext, ExecutionEngine
from .registry import EngineNotFoundError, EngineRegistry

# Global authoritative engine registry instance initialized before concrete engine imports
engine_registry = EngineRegistry()

from .openmanus_engine import OpenManusEngine
from .peldrun_engine import PeldrunEngine

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