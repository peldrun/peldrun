# backend/integrations/source_control/tools/list_providers.py
"""
The `source_control.list_providers` tool.

This tool reports the source-control providers that are currently
registered with the process-wide `ProviderRegistry`, together with
their declared capabilities.

The tool is read-only. It never touches the network and never reads
credentials. It is safe to invoke from any context, including at the
start of an agent run, when the agent needs to know which providers
are available before planning any further action.

Design rules:
    - The tool MUST NOT require credentials. It only inspects the
      registry.
    - The tool MUST NOT return connection state (tokens, expiry,
      scopes). Connection state belongs to a separate, dedicated
      tool.
    - The tool MUST be deterministic: the order of providers is
      stable across invocations.
    - The tool MUST handle an empty registry gracefully.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Mapping

from ..capabilities import CapabilityChecker
from ..errors import to_source_control_error
from ..registry import ProviderRegistry, get_default_registry
from .base import SourceControlTool, ToolContext, ToolResult

__all__ = [
    "ListProvidersTool",
    "DEFAULT_INCLUDE_CAPABILITIES",
]


logger = logging.getLogger(__name__)


#: When True (default), the tool includes each provider's
#: capabilities and the derived operation lists. Callers that only
#: need the provider IDs can pass `include_capabilities=False` to
#: reduce the payload size.
DEFAULT_INCLUDE_CAPABILITIES: bool = True


# ---------------------------------------------------------------------------
# Tool
# ---------------------------------------------------------------------------


class ListProvidersTool(SourceControlTool):
    """
    Report the registered source-control providers.

    The tool exposes two optional inputs:

        - `include_capabilities` (bool, default True): when False,
          only the provider IDs are returned.

    The tool's output shape is::

        {
            "count": <int>,
            "providers": [
                {
                    "id": "<provider_id>",
                    "capabilities": {...},              # optional
                    "supported_operations": [...],      # optional
                    "unsupported_operations": [...],    # optional
                },
                ...
            ]
        }
    """

    def __init__(
        self,
        *,
        registry: ProviderRegistry | None = None,
        audit_logger: Any = None,
    ) -> None:
        super().__init__(audit_logger=audit_logger)
        # The registry is resolved lazily so that tests can replace
        # the default registry after the tool has been constructed.
        self._explicit_registry = registry

    # --- Metadata -------------------------------------------------------

    @property
    def name(self) -> str:
        return "source_control.list_providers"

    @property
    def description(self) -> str:
        return (
            "List the source-control providers that are configured "
            "in this workspace, with their declared capabilities."
        )

    @property
    def input_schema(self) -> Mapping[str, Any]:
        return {
            "type": "object",
            "properties": {
                "include_capabilities": {
                    "type": "boolean",
                    "description": (
                        "When true (default), include each provider's "
                        "capabilities and the derived operation lists."
                    ),
                    "default": DEFAULT_INCLUDE_CAPABILITIES,
                },
            },
            "required": [],
            "additionalProperties": False,
        }

    # --- Execution ------------------------------------------------------

    async def execute(
        self, context: ToolContext, **kwargs: Any
    ) -> ToolResult:
        """
        Return the list of registered providers.

        The tool NEVER raises. Every failure mode is expressed as a
        failed `ToolResult`.
        """
        # Coerce the input. The base class performs shallow
        # validation; here we apply the default and enforce the
        # type.
        raw_include = kwargs.get(
            "include_capabilities", DEFAULT_INCLUDE_CAPABILITIES
        )
        try:
            include_capabilities = bool(raw_include)
        except Exception as exc:  # noqa: BLE001
            normalized = to_source_control_error(exc)
            return self.failure_result(
                error_code=normalized.code.value,
                error_message=(
                    "`include_capabilities` must be a boolean."
                ),
            )

        # Resolve the registry. Prefer the explicit one when the
        # caller supplied it; otherwise use the process-wide default.
        registry = self._explicit_registry or get_default_registry()

        try:
            providers = self._serialize_registry(
                registry, include_capabilities=include_capabilities
            )
        except Exception as exc:  # noqa: BLE001
            normalized = to_source_control_error(exc)
            return self.failure_result(
                error_code=normalized.code.value,
                error_message=(
                    "Failed to inspect the provider registry."
                ),
                data={"reason": normalized.message},
            )

        return self.success_result(
            {
                "count": len(providers),
                "providers": providers,
            },
            metadata={
                "include_capabilities": include_capabilities,
            },
        )

    # --- Internal -------------------------------------------------------

    def _serialize_registry(
        self,
        registry: ProviderRegistry,
        *,
        include_capabilities: bool,
    ) -> List[Dict[str, Any]]:
        """
        Serialize every registered provider.

        The result is sorted by `provider_id` so that the output is
        deterministic. A provider whose capabilities cannot be read
        is included with an empty capability block rather than being
        skipped; the caller sees the full provider list and the
        problem is visible in the payload.
        """
        providers = registry.list_providers()

        serialized: List[Dict[str, Any]] = []
        for provider in providers:
            provider_id = str(getattr(provider, "provider_id", "") or "")
            if not provider_id:
                continue

            entry: Dict[str, Any] = {"id": provider_id}

            if include_capabilities:
                try:
                    capabilities = provider.capabilities
                except Exception as exc:  # noqa: BLE001
                    logger.debug(
                        "Provider '%s' failed to expose capabilities: %s",
                        provider_id,
                        type(exc).__name__,
                    )
                    entry["capabilities"] = {}
                    entry["supported_operations"] = []
                    entry["unsupported_operations"] = []
                else:
                    checker = CapabilityChecker(capabilities)
                    entry["capabilities"] = checker.to_dict()
                    entry["supported_operations"] = list(
                        checker.supported_operations()
                    )
                    entry["unsupported_operations"] = list(
                        checker.unsupported_operations()
                    )

            serialized.append(entry)

        serialized.sort(key=lambda item: item["id"])
        return serialized
