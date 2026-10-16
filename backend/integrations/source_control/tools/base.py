# backend/integrations/source_control/tools/base.py
"""
Base classes and registry for Source Control agent tools.

This module defines the contract that every source-control tool
follows. The contract has two important properties:

    1. Tools NEVER raise. Every failure mode is expressed as a
       `ToolResult` with `success=False`. This lets the agent react
       to failures without a try/except around every call, and it
       keeps exception traces out of the model's context.

    2. Tools are registered in a `ToolRegistry` that the agent
       runtime queries by name. Registration is explicit; this
       module does not import any concrete tool.

A tool implementation provides:

    - `name`             : a stable, namespaced identifier.
    - `description`      : a short, English sentence for the model.
    - `input_schema`     : a JSON-schema-like mapping describing the
                           accepted arguments.
    - `execute()`        : the actual work; may raise.

The base class wraps `execute()` with:

    - Input validation.
    - Exception normalization to `SourceControlError`.
    - Result redaction.
    - Result size bounding.
    - Optional audit logging.

Design rules:
    - `SourceControlTool.run` is the ONLY method the caller invokes.
      `execute` is reserved for subclasses.
    - Every tool name MUST start with `source_control.` so that the
      agent's namespace stays organized.
    - Tool results MUST NOT contain secrets. The base class redacts
      mappings and strings defensively, but subclasses SHOULD also
      avoid returning anything that was never explicitly permitted.
    - The registry is thread-safe.
"""

from __future__ import annotations

import logging
import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import (
    Any,
    Dict,
    Mapping,
    Optional,
    Sequence,
)

from ..errors import (
    SourceControlError,
    ValidationError,
    error_code_of,
    to_source_control_error,
)
from ..security.audit import (
    AuditEventType,
    AuditLogger,
    AuditSeverity,
    get_default_audit_logger,
)
from ..security.redaction import redact_mapping, redact_text

__all__ = [
    "ToolResult",
    "ToolContext",
    "SourceControlTool",
    "ToolRegistry",
    "get_default_tool_registry",
    "set_default_tool_registry",
    "reset_default_tool_registry",
    "TOOL_NAME_PREFIX",
    "MAX_RESULT_BYTES",
]


logger = logging.getLogger(__name__)


#: Every tool name MUST start with this prefix. The prefix keeps the
#: agent's tool namespace organized and prevents collisions with
#: tools from other subsystems.
TOOL_NAME_PREFIX: str = "source_control."

#: Maximum serialized size (in bytes) of a tool result's `data`
#: payload. Larger payloads are truncated with an explicit marker.
MAX_RESULT_BYTES: int = 100_000

#: Marker appended to truncated payloads.
_TRUNCATION_MARKER = "... [result truncated]"


# ---------------------------------------------------------------------------
# Result and context
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ToolResult:
    """
    Outcome of a single tool invocation.

    Fields:
        success:       True when the tool completed without error.
        data:          Payload returned to the agent. May be `None`
                       for tools that only perform side effects.
        error_code:    A `SourceControlErrorCode` value when
                       `success` is False.
        error_message: A redacted, human-readable error message.
        metadata:      Optional context that does not fit in `data`.
                       MUST NOT contain secrets.
    """

    success: bool
    data: Optional[Mapping[str, Any]] = None
    error_code: Optional[str] = None
    error_message: Optional[str] = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def ok(
        cls,
        data: Optional[Mapping[str, Any]] = None,
        *,
        metadata: Optional[Mapping[str, Any]] = None,
    ) -> "ToolResult":
        """Build a successful result."""
        return cls(
            success=True,
            data=dict(data) if data else None,
            metadata=dict(metadata or {}),
        )

    @classmethod
    def fail(
        cls,
        error_code: str,
        error_message: str,
        *,
        data: Optional[Mapping[str, Any]] = None,
        metadata: Optional[Mapping[str, Any]] = None,
    ) -> "ToolResult":
        """Build a failed result."""
        return cls(
            success=False,
            data=dict(data) if data else None,
            error_code=error_code,
            error_message=redact_text(error_message),
            metadata=dict(metadata or {}),
        )

    def to_dict(self) -> Dict[str, Any]:
        """
        Serialize for the agent runtime and for logs.

        The returned mapping is safe: `data` is not re-redacted here
        (the caller is expected to have produced safe data), but
        `error_message` was redacted at construction time.
        """
        return {
            "success": self.success,
            "data": dict(self.data) if self.data else None,
            "error_code": self.error_code,
            "error_message": self.error_message,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True)
class ToolContext:
    """
    Execution context passed to every tool.

    Fields:
        operation_id: The operation this tool is working on, when
                      applicable. Tools that require a prior
                      operation use this to look it up in the
                      journal.
        run_id:       The agent run identifier, when applicable.
        actor:        Who is invoking the tool. `"agent"` (default)
                      or `"user"`.
        metadata:     Optional extension bag. MUST NOT contain
                      secrets.
    """

    operation_id: Optional[str] = None
    run_id: Optional[str] = None
    actor: str = "agent"
    metadata: Mapping[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Base tool
# ---------------------------------------------------------------------------


class SourceControlTool(ABC):
    """
    Abstract base class for every source-control tool.

    Subclasses MUST override:

        - `name` (property)
        - `description` (property)
        - `input_schema` (property)
        - `execute` (async method)

    Subclasses MAY override `validate_input` to add stronger input
    validation. The default implementation performs a shallow type
    check against `input_schema`.
    """

    def __init__(
        self,
        *,
        audit_logger: Optional[AuditLogger] = None,
    ) -> None:
        self._audit = audit_logger or get_default_audit_logger()

    # --- Metadata ------------------------------------------------------

    @property
    @abstractmethod
    def name(self) -> str:
        """
        Return the stable, namespaced tool name.

        The name MUST start with `source_control.`. Examples:
        `source_control.list_repositories`,
        `source_control.push_branch`.
        """
        raise NotImplementedError

    @property
    @abstractmethod
    def description(self) -> str:
        """
        Return a short, English description of the tool.

        The description is presented to the model. It MUST fit in
        one sentence and MUST NOT contain line breaks.
        """
        raise NotImplementedError

    @property
    @abstractmethod
    def input_schema(self) -> Mapping[str, Any]:
        """
        Return a JSON-schema-like mapping describing the tool's
        accepted arguments.

        The schema is informational: the agent runtime may use it to
        validate calls, but the tool is responsible for its own
        validation in `execute` or `validate_input`.
        """
        raise NotImplementedError

    # --- Execution -----------------------------------------------------

    async def run(
        self,
        context: ToolContext,
        **kwargs: Any,
    ) -> ToolResult:
        """
        Execute the tool and return a `ToolResult`.

        This method NEVER raises. Every failure — validation errors,
        policy denials, network failures, programming errors — is
        converted into a failed `ToolResult`.

        Args:
            context: Execution context. Required; MUST be a
                `ToolContext`.
            **kwargs: Tool-specific arguments.

        Returns:
            A `ToolResult`. On success, `data` carries the tool's
            output. On failure, `error_code` and `error_message`
            describe the reason.
        """
        if not isinstance(context, ToolContext):
            return ToolResult.fail(
                error_code="VALIDATION_ERROR",
                error_message=(
                    "context must be a ToolContext instance."
                ),
            )

        # Validate the tool name so that a subclass cannot smuggle
        # an unbranded name into the registry.
        if not self.name.startswith(TOOL_NAME_PREFIX):
            return ToolResult.fail(
                error_code="VALIDATION_ERROR",
                error_message=(
                    f"Tool name '{self.name}' does not start with "
                    f"'{TOOL_NAME_PREFIX}'."
                ),
            )

        # Shallow input validation.
        try:
            self.validate_input(kwargs)
        except SourceControlError as exc:
            return self._result_from_error(exc)
        except Exception as exc:  # noqa: BLE001
            return self._result_from_error(to_source_control_error(exc))

        # Execute the tool.
        try:
            result = await self.execute(context, **kwargs)
        except SourceControlError as exc:
            result = self._result_from_error(exc)
        except Exception as exc:  # noqa: BLE001
            result = self._result_from_error(to_source_control_error(exc))

        # Ensure the result respects the size bound and the
        # redaction contract.
        result = self._finalize_result(result)

        # Audit the invocation, but never let an audit failure
        # affect the result.
        self._audit_invocation(context, result)

        return result

    @abstractmethod
    async def execute(
        self, context: ToolContext, **kwargs: Any
    ) -> ToolResult:
        """
        Perform the tool's work.

        Subclasses MUST override this method. Implementations MAY
        raise; `run` converts exceptions to failed `ToolResult`s.
        """
        raise NotImplementedError

    # --- Validation ----------------------------------------------------

    def validate_input(self, kwargs: Mapping[str, Any]) -> None:
        """
        Validate the tool's input arguments.

        The default implementation checks that every `required`
        field declared in `input_schema` is present. Subclasses MAY
        override to add stronger checks.

        Raises:
            ValidationError: When validation fails.
        """
        if not isinstance(kwargs, Mapping):
            raise ValidationError(
                "Tool inputs must be provided as a mapping."
            )
        schema = self.input_schema or {}
        required = schema.get("required") or ()
        missing = [field_name for field_name in required if field_name not in kwargs]
        if missing:
            raise ValidationError(
                f"Missing required input field(s): {', '.join(missing)}.",
                details={"missing": missing},
            )

    # --- Helpers for subclasses ----------------------------------------

    @staticmethod
    def success_result(
        data: Optional[Mapping[str, Any]] = None,
        *,
        metadata: Optional[Mapping[str, Any]] = None,
    ) -> ToolResult:
        """Convenience wrapper around `ToolResult.ok`."""
        return ToolResult.ok(data, metadata=metadata)

    @staticmethod
    def failure_result(
        error_code: str,
        error_message: str,
        *,
        data: Optional[Mapping[str, Any]] = None,
        metadata: Optional[Mapping[str, Any]] = None,
    ) -> ToolResult:
        """Convenience wrapper around `ToolResult.fail`."""
        return ToolResult.fail(
            error_code, error_message, data=data, metadata=metadata
        )

    # --- Internal: result construction ---------------------------------

    def _result_from_error(self, exc: SourceControlError) -> ToolResult:
        """
        Convert a normalized exception to a failed `ToolResult`.
        """
        return ToolResult.fail(
            error_code=exc.code.value,
            error_message=exc.message,
            metadata={"retryable": exc.is_retryable},
        )

    def _finalize_result(self, result: ToolResult) -> ToolResult:
        """
        Apply the size bound and the redaction contract to a result.

        The `data` mapping is redacted and truncated; `metadata` is
        redacted but not truncated (it is expected to be small).
        """
        safe_data = _redact_data(result.data) if result.data else None
        safe_metadata = _redact_data(result.metadata) if result.metadata else {}

        if safe_data is not None:
            safe_data = _truncate_mapping(safe_data)

        return ToolResult(
            success=result.success,
            data=safe_data,
            error_code=result.error_code,
            error_message=result.error_message,
            metadata=safe_metadata,
        )

    def _audit_invocation(
        self, context: ToolContext, result: ToolResult
    ) -> None:
        """
        Record the tool invocation in the audit log.

        The payload contains the tool name, the operation ID when
        available, and the success flag. It NEVER contains the
        tool's arguments or result data, because those may include
        repository content or identifiers that the user did not
        consent to persist.
        """
        try:
            self._audit.record(
                AuditEventType.OTHER,
                severity=(
                    AuditSeverity.INFO
                    if result.success
                    else AuditSeverity.WARNING
                ),
                action="tool_call",
                message=(
                    f"Tool '{self.name}' "
                    f"{'succeeded' if result.success else 'failed'}."
                ),
                payload={
                    "tool_name": self.name,
                    "success": result.success,
                    "error_code": result.error_code,
                },
                operation_id=context.operation_id,
                run_id=context.run_id,
                actor=context.actor,
            )
        except Exception:  # noqa: BLE001
            # Audit failure MUST NOT affect the tool result.
            logger.debug(
                "Failed to audit tool invocation for '%s'.",
                self.name,
                exc_info=True,
            )

    # --- Presentation --------------------------------------------------

    def __repr__(self) -> str:  # pragma: no cover - trivial formatting
        return f"<{type(self).__name__} name={self.name!r}>"


# ---------------------------------------------------------------------------
# Data sanitization helpers
# ---------------------------------------------------------------------------


def _redact_data(value: Any) -> Any:
    """
    Recursively redact a JSON-like value.

    Mappings are passed through `redact_mapping`; strings are passed
    through `redact_text`; sequences are recursed; scalars are
    returned unchanged.
    """
    if isinstance(value, Mapping):
        return redact_mapping(value)
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, (list, tuple)):
        return [_redact_data(item) for item in value]
    return value


def _truncate_mapping(value: Mapping[str, Any]) -> Mapping[str, Any]:
    """
    Truncate a mapping when its JSON serialization exceeds
    `MAX_RESULT_BYTES`.

    Truncation is applied at the top level: the mapping is reduced to
    the first few keys until the serialized size fits. The truncated
    mapping carries a `_truncated` flag with a short message.
    """
    import json

    try:
        encoded = json.dumps(value, default=str)
    except (TypeError, ValueError):
        return {
            "_error": "Result data is not JSON serializable.",
            "_truncated": True,
        }

    if len(encoded.encode("utf-8")) <= MAX_RESULT_BYTES:
        return value

    # Reduce the mapping until it fits.
    trimmed: Dict[str, Any] = {}
    running_size = 2  # "{}"
    for key, item in value.items():
        try:
            item_json = json.dumps(item, default=str)
        except (TypeError, ValueError):
            continue
        added = len(item_json.encode("utf-8")) + len(str(key).encode("utf-8")) + 6
        if running_size + added > MAX_RESULT_BYTES:
            break
        trimmed[str(key)] = item
        running_size += added

    trimmed["_truncated"] = _TRUNCATION_MARKER
    return trimmed


# ---------------------------------------------------------------------------
# Tool registry
# ---------------------------------------------------------------------------


class ToolRegistry:
    """
    Thread-safe registry of source-control tools.

    The registry maps `tool.name` to a `SourceControlTool` instance.
    Duplicate names are rejected unless `overwrite=True` is passed.
    """

    def __init__(self) -> None:
        self._tools: Dict[str, SourceControlTool] = {}
        self._lock = threading.RLock()

    # --- Registration ---------------------------------------------------

    def register(
        self,
        tool: SourceControlTool,
        *,
        overwrite: bool = False,
    ) -> None:
        """
        Register a tool.

        Raises:
            TypeError: If `tool` is not a `SourceControlTool`.
            ValidationError: If the name is malformed, or if the
                name is already registered and `overwrite` is False.
        """
        if not isinstance(tool, SourceControlTool):
            raise TypeError(
                "ToolRegistry.register expects a SourceControlTool."
            )
        name = tool.name
        if not name or not name.startswith(TOOL_NAME_PREFIX):
            raise ValidationError(
                f"Tool name must start with '{TOOL_NAME_PREFIX}'.",
                details={"tool_name": name},
            )

        with self._lock:
            if name in self._tools and not overwrite:
                raise ValidationError(
                    f"Tool '{name}' is already registered.",
                    details={"tool_name": name},
                )
            self._tools[name] = tool

    def unregister(self, name: str) -> bool:
        """
        Remove a tool. Returns True if a tool was present.
        """
        if not name:
            return False
        with self._lock:
            return self._tools.pop(name, None) is not None

    def clear(self) -> None:
        """Remove every tool. Intended for tests."""
        with self._lock:
            self._tools.clear()

    # --- Lookup ---------------------------------------------------------

    def get(self, name: str) -> Optional[SourceControlTool]:
        """Return the tool with the given name, or `None`."""
        if not name:
            return None
        with self._lock:
            return self._tools.get(name)

    def require(self, name: str) -> SourceControlTool:
        """
        Return the tool with the given name, or raise
        `ValidationError`.
        """
        tool = self.get(name)
        if tool is None:
            raise ValidationError(
                f"Tool '{name}' is not registered.",
                details={"tool_name": name},
            )
        return tool

    def list_names(self) -> Sequence[str]:
        """Return the sorted list of registered tool names."""
        with self._lock:
            return tuple(sorted(self._tools.keys()))

    def list_tools(self) -> Sequence[SourceControlTool]:
        """Return the registered tools, sorted by name."""
        with self._lock:
            items = sorted(self._tools.items(), key=lambda kv: kv[0])
        return tuple(tool for _name, tool in items)

    # --- Container protocol --------------------------------------------

    def __len__(self) -> int:
        with self._lock:
            return len(self._tools)

    def __contains__(self, name: object) -> bool:
        if not isinstance(name, str):
            return False
        with self._lock:
            return name in self._tools

    def __repr__(self) -> str:  # pragma: no cover - trivial formatting
        return f"<ToolRegistry tools={len(self)}>"


# ---------------------------------------------------------------------------
# Default registry singleton
# ---------------------------------------------------------------------------


_DEFAULT_REGISTRY: Optional[ToolRegistry] = None
_DEFAULT_LOCK = threading.RLock()


def get_default_tool_registry() -> ToolRegistry:
    """
    Return the process-wide default tool registry, creating it on
    first use.
    """
    global _DEFAULT_REGISTRY
    with _DEFAULT_LOCK:
        if _DEFAULT_REGISTRY is None:
            _DEFAULT_REGISTRY = ToolRegistry()
        return _DEFAULT_REGISTRY


def set_default_tool_registry(registry: ToolRegistry) -> None:
    """
    Replace the process-wide default tool registry.

    Intended for application startup and for tests.
    """
    if not isinstance(registry, ToolRegistry):
        raise TypeError(
            "set_default_tool_registry expects a ToolRegistry."
        )
    global _DEFAULT_REGISTRY
    with _DEFAULT_LOCK:
        _DEFAULT_REGISTRY = registry


def reset_default_tool_registry() -> ToolRegistry:
    """
    Replace the default registry with a fresh empty one.

    Used by tests to guarantee isolation between test cases.
    """
    global _DEFAULT_REGISTRY
    with _DEFAULT_LOCK:
        _DEFAULT_REGISTRY = ToolRegistry()
        return _DEFAULT_REGISTRY
