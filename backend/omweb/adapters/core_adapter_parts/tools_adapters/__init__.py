# backend/omweb/adapters/core_adapter_parts/tools_adapters/__init__.py
"""
PELDRUN Web-to-Core Tool Adapters Package.

This package hosts self-contained tool adapter modules. Each module
exposes a single ``ADAPTER_SPEC`` constant conforming to ``AdapterSpec``
(see ``_contract.py``), which the dynamic facade in ``executors.py``
discovers at import time.

Adding a new tool adapter requires creating exactly one file in this
directory named ``<tool>_adapter.py``. No other file needs to be
modified.

Discovery contract for adapter modules:
  * Filename pattern  : ``*_adapter.py`` (private ``_``-prefixed files
                        such as ``_contract.py`` and ``_helpers.py``
                        are excluded).
  * Exported symbol   : a module-level ``ADAPTER_SPEC`` instance of
                        ``AdapterSpec``.
  * Factory signature : ``create_<tool>_executor(workspace_dir=None,
                        **context) -> Callable[..., Awaitable[ToolResult]]``.
"""

from __future__ import annotations

__all__: list[str] = []
