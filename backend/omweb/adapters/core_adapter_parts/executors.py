# backend/omweb/adapters/core_adapter_parts/executors.py
"""
Real Tool Execution Factory — Dynamic Adapter Facade.

This module is a thin facade over the ``tools_adapters`` package. It
performs one-time dynamic discovery of ``*_adapter.py`` modules at
import time and exposes every registered ``create_<tool>_executor``
factory as a staticmethod-like attribute of
``RealToolExecutionFactory``.

Design invariants:
  * Adding a new tool adapter requires creating exactly one file in
    ``tools_adapters/`` — this module does NOT need to be modified.
  * Discovery is eager (performed once at import) because the total
    adapter count is small and import-time cost is negligible.
  * Attribute access is resolved dynamically via ``__getattr__`` so that
    ``RealToolExecutionFactory.create_<tool>_executor`` continues to
    work for any adapter that exists at import time.
  * Helper utilities (``_resolve_workspace_path``, ``_ensure_tool_result``,
    ``_invoke_file_ops``, ``WorkspacePathType``) are re-exported from
    ``_helpers.py`` for backward compatibility with any external code
    that imported them from this module.
"""

from __future__ import annotations

import importlib
import inspect
import logging
import pkgutil
from typing import Any, Callable, Dict, Iterable, List, Optional

from .tools_adapters._contract import AdapterRegistry, AdapterSpec
from .tools_adapters._helpers import (
    WorkspacePathType,
    _ensure_tool_result,
    _invoke_file_ops,
    _resolve_workspace_path,
)

logger = logging.getLogger("omweb.adapters.executors")


# ---------------------------------------------------------------------------
# Dynamic adapter discovery
# ---------------------------------------------------------------------------

def _discover_adapters() -> None:
    """
    Import every ``*_adapter.py`` module in ``tools_adapters/`` and
    register its exported spec(s) in the ``AdapterRegistry``.

    A module may export either:

      * ``ADAPTER_SPEC: AdapterSpec``       — single-spec module.
      * ``ADAPTER_SPECS: List[AdapterSpec]`` — multi-spec module (used
                                              when one adapter module
                                              serves multiple tools that
                                              share underlying logic).

    Both forms may coexist in the same module; they are merged before
    registration. Idempotent: repeated calls are safe because the
    registry rejects duplicate registrations by raising
    ``ValueError`` — so we short-circuit if discovery has already run.
    """
    if AdapterRegistry.all_specs():
        return

    package_name = f"{__package__}.tools_adapters"
    try:
        package = importlib.import_module(package_name)
    except Exception as exc:
        logger.exception("Failed to import tools_adapters package: %s", exc)
        return

    for module_info in pkgutil.iter_modules(package.__path__):
        name = module_info.name
        if not name.endswith("_adapter"):
            continue
        if name.startswith("_"):
            continue

        full_name = f"{package_name}.{name}"
        try:
            module = importlib.import_module(full_name)
        except Exception as exc:
            logger.exception(
                "Failed to import adapter module %s: %s", full_name, exc
            )
            continue

        # Collect specs from both supported export forms.
        specs: List[AdapterSpec] = []

        single = getattr(module, "ADAPTER_SPEC", None)
        if single is not None:
            if isinstance(single, AdapterSpec):
                specs.append(single)
            else:
                logger.warning(
                    "Module %s exported ADAPTER_SPEC of wrong type %s; "
                    "ignoring.",
                    full_name,
                    type(single).__name__,
                )

        multi = getattr(module, "ADAPTER_SPECS", None)
        if multi is not None:
            if (
                isinstance(multi, (list, tuple))
                and all(isinstance(s, AdapterSpec) for s in multi)
            ):
                specs.extend(multi)
            else:
                logger.warning(
                    "Module %s exported ADAPTER_SPECS of wrong type or "
                    "with non-AdapterSpec entries; ignoring.",
                    full_name,
                )

        if not specs:
            logger.warning(
                "Adapter module %s exported neither ADAPTER_SPEC nor "
                "ADAPTER_SPECS; skipping.",
                full_name,
            )
            continue

        for spec in specs:
            try:
                AdapterRegistry.register(spec)
            except ValueError as exc:
                logger.error(
                    "Adapter registration collision in %s: %s",
                    full_name,
                    exc,
                )

# Perform discovery once at import time.
_discover_adapters()


# ---------------------------------------------------------------------------
# RealToolExecutionFactory — dynamic facade
# ---------------------------------------------------------------------------

class RealToolExecutionFactory:
    """
    Facade exposing every registered adapter factory as
    ``create_<tool>_executor`` attributes.

    Attribute access is resolved dynamically via ``__getattr__`` against
    the process-wide ``AdapterRegistry``. This preserves backward
    compatibility with existing call sites
    (``RealToolExecutionFactory.create_bash_executor(...)``) while
    eliminating the need to hand-write one staticmethod per adapter.
    """

    def __getattr__(self, name: str) -> Any:  # pragma: no cover — class-level
        raise AttributeError(name)


def _factory_lookup(name: str) -> Callable[..., Any]:
    """
    Resolve a ``create_<tool>_executor`` attribute name to the underlying
    factory callable via the AdapterRegistry.

    Raises:
        AttributeError: if no adapter declares the requested tool.
    """
    if not name.startswith("create_") or not name.endswith("_executor"):
        raise AttributeError(name)

    tool_name = name[len("create_"): -len("_executor")]
    spec = AdapterRegistry.lookup(tool_name)
    if spec is None:
        raise AttributeError(
            f"No adapter registered for tool {tool_name!r} "
            f"(requested attribute: {name!r})."
        )
    return spec.factory


# Install a class-level ``__getattr__`` so that any unbound attribute
# access on the *class object* (e.g. ``RealToolExecutionFactory.create_...``)
# resolves through the registry.
def _class_getattr(cls: type, name: str) -> Any:
    return _factory_lookup(name)


RealToolExecutionFactory.__getattr__ = classmethod(_class_getattr)  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Public re-exports (backward compatibility)
# ---------------------------------------------------------------------------

__all__ = [
    "RealToolExecutionFactory",
    "AdapterRegistry",
    "AdapterSpec",
    "WorkspacePathType",
    "_resolve_workspace_path",
    "_ensure_tool_result",
    "_invoke_file_ops",
]
