# backend/omweb/adapters/core_adapter_parts/tools_adapters/_contract.py
"""
Adapter Contract for PELDRUN Web-to-Core Tool Adapters.

Defines the immutable ``AdapterSpec`` record that every tool adapter
module must expose, and the process-wide ``AdapterRegistry`` that
aggregates them for dynamic lookup by tool name.

Design invariants:
  * ``AdapterSpec`` is frozen (immutable) so registry entries cannot be
    mutated after registration.
  * ``tool_names`` is a tuple so hashability and deterministic ordering
    are guaranteed.
  * ``requires_context`` declares which keyword arguments (e.g.
    ``search_config``, ``run_id``) the factory accepts, so callers can
    filter context kwargs safely without introspecting signatures.
  * ``AdapterRegistry`` is a process-wide singleton keyed by tool name.
    Collisions raise ``ValueError`` at registration time to fail fast.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, FrozenSet, Iterable, List, Optional, Tuple

from peldrun.tools.contract import ToolResult

logger = logging.getLogger("omweb.adapters.tools_adapters.contract")


# ---------------------------------------------------------------------------
# AdapterSpec — the immutable declaration every adapter module exports
# ---------------------------------------------------------------------------

# Factory signature: (workspace_dir, **context) -> Callable[..., Awaitable[ToolResult]]
AdapterFactory = Callable[..., Callable[..., Awaitable[ToolResult]]]


@dataclass(frozen=True)
class AdapterSpec:
    """
    Immutable declaration of a single tool adapter.

    Attributes:
        tool_names:
            Tuple of one or more tool identifiers served by this adapter.
            Multiple names allow a single adapter module to expose
            alias-equivalent tools (e.g. ``bash`` and ``shell_exec``).
        factory:
            The ``create_<tool>_executor`` callable. It must accept
            ``workspace_dir`` as its first positional argument, plus any
            keyword arguments declared in ``requires_context``.
        requires_context:
            Frozenset of context keyword names (e.g. ``"run_id"``,
            ``"search_config"``) that the factory is willing to accept.
            Callers use this to filter a context dict before invocation
            without resorting to ``inspect.signature``.
    """

    tool_names: Tuple[str, ...]
    factory: AdapterFactory
    requires_context: FrozenSet[str] = field(default_factory=frozenset)

    def __post_init__(self) -> None:
        if not self.tool_names:
            raise ValueError("AdapterSpec.tool_names must not be empty.")
        for name in self.tool_names:
            if not isinstance(name, str) or not name.strip():
                raise ValueError(
                    f"AdapterSpec.tool_names contains an invalid entry: {name!r}."
                )
        if not callable(self.factory):
            raise ValueError("AdapterSpec.factory must be callable.")

    def matches_context(self, context: Dict[str, Any]) -> Dict[str, Any]:
        """
        Return the subset of ``context`` keys accepted by this adapter's
        factory (based on ``requires_context``). Values that are ``None``
        are dropped so default factory behavior is preserved.
        """
        if not self.requires_context:
            return {}
        return {
            k: v
            for k, v in context.items()
            if k in self.requires_context and v is not None
        }


# ---------------------------------------------------------------------------
# AdapterRegistry — process-wide, name-keyed registry of AdapterSpec
# ---------------------------------------------------------------------------

class AdapterRegistry:
    """
    Process-wide registry mapping tool name → AdapterSpec.

    Registration is performed by the dynamic discovery routine in
    ``executors.py`` at import time. Collisions are rejected to prevent
    silent shadowing between adapter modules.
    """

    _by_name: Dict[str, AdapterSpec] = {}
    _all: List[AdapterSpec] = []

    @classmethod
    def register(cls, spec: AdapterSpec) -> None:
        """
        Register a single AdapterSpec under all of its declared tool names.

        Raises:
            ValueError: if any declared tool name is already registered
                (collision safety).
        """
        if not isinstance(spec, AdapterSpec):
            raise TypeError(
                f"AdapterRegistry.register expects AdapterSpec, got "
                f"{type(spec).__name__}."
            )

        # Collision check first (atomic rejection before mutation)
        for name in spec.tool_names:
            if name in cls._by_name:
                existing = cls._by_name[name]
                raise ValueError(
                    f"Adapter collision: tool name {name!r} is already "
                    f"registered by {existing.factory.__module__}."
                )

        for name in spec.tool_names:
            cls._by_name[name] = spec
        cls._all.append(spec)

        logger.debug(
            "Registered adapter for tools %s from %s.",
            list(spec.tool_names),
            spec.factory.__module__,
        )

    @classmethod
    def lookup(cls, tool_name: str) -> Optional[AdapterSpec]:
        """Return the AdapterSpec serving the given tool name, or None."""
        return cls._by_name.get(tool_name)

    @classmethod
    def all_specs(cls) -> List[AdapterSpec]:
        """Return every registered AdapterSpec (registration order)."""
        return list(cls._all)

    @classmethod
    def all_tool_names(cls) -> List[str]:
        """Return every registered tool name (deterministic order)."""
        return list(cls._by_name.keys())

    @classmethod
    def clear(cls) -> None:
        """
        Clear the registry. Intended for test suites that need a clean
        slate between cases; production code should never call this.
        """
        cls._by_name.clear()
        cls._all.clear()

    @classmethod
    def __contains__(cls, tool_name: str) -> bool:  # noqa: N805
        return tool_name in cls._by_name


__all__ = [
    "AdapterSpec",
    "AdapterFactory",
    "AdapterRegistry",
]
