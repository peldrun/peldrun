# backend/omweb/adapters/core_adapter_parts/web_tool_adapter.py
"""
Web-to-Core Tool Adapter for PELDRUN.

Bridges Web-layer tool definitions and async executors into canonical
peldrun-core BaseTool instances compatible with ToolRegistry, ToolCollection,
and ToolCallAgent.

Key engineering guarantees (Phase 4 — SR-401):
- Preserves structured Core ToolResult instances verbatim, including `is_error`,
  `exit_code`, `metadata`, and `artifacts`, so failed tool invocations are never
  relabeled as successful plain-text observations.
- Does not inject synthetic default argument values when optional parameters
  (such as `max_results` on `web_search`) are omitted by the caller.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from typing import Any, Awaitable, Callable, Dict, List, Optional

from peldrun.tools.base import BaseTool
from peldrun.tools.contract import ToolResult

logger = logging.getLogger("omweb.adapters.web_tool_adapter")


from pydantic import create_model as _create_model


# JSON Schema keywords whose presence means the schema is too complex
# for pydantic's ``create_model`` to preserve faithfully. Every one of
# these would be silently dropped, corrupting the OpenAI function
# schema that reaches the LLM.
_COMPLEX_SCHEMA_KEYS = frozenset({
    "$ref", "$defs", "definitions",
    "oneOf", "anyOf", "allOf", "not",
    "if", "then", "else",
})


def _should_disable_args_schema(parameters: Dict[str, Any]) -> bool:
    """
    Return True when building a pydantic ``args_schema`` would silently
    corrupt the JSON schema for OpenAI function calling.

    pydantic's ``create_model`` treats ``array`` as bare ``list`` and
    ``object`` as bare ``dict`` — dropping nested ``items`` and
    ``properties``. It also drops every compositional keyword
    (``$ref``, ``oneOf``, ``anyOf``, ``allOf``, ``not``, conditionals).

    For schemas that carry those constructs the faithful behaviour is
    to let Core use the ``parameters`` dict verbatim, which is exactly
    what ``to_openai_tool()`` / ``to_param()`` / ``to_openai_schema()``
    already return. Setting ``args_schema = None`` triggers that
    fallback path.

    The check is deliberately conservative: a false negative (we build
    an args_schema that loses something) is worse than a false
    positive (we skip args_schema and use the raw schema, which is
    always correct).
    """
    if not isinstance(parameters, dict):
        return False

    props = parameters.get("properties")
    if not isinstance(props, dict):
        return False

    # Top-level compositional keywords → whole schema is complex.
    if any(k in parameters for k in _COMPLEX_SCHEMA_KEYS):
        return True

    for spec in props.values():
        if not isinstance(spec, dict):
            continue

        # Compositional keyword on a property → complex.
        if any(k in spec for k in _COMPLEX_SCHEMA_KEYS):
            return True

        ptype = spec.get("type")

        # Array with nested item structure → pydantic would lose it.
        if ptype == "array":
            items = spec.get("items")
            if isinstance(items, dict):
                if any(k in items for k in _COMPLEX_SCHEMA_KEYS):
                    return True
                if items.get("type") in ("array", "object"):
                    return True
                if isinstance(items.get("properties"), dict):
                    return True

        # Object with declared properties → pydantic would lose them.
        if ptype == "object":
            if isinstance(spec.get("properties"), dict):
                return True
            if any(k in spec for k in _COMPLEX_SCHEMA_KEYS):
                return True

    return False


def _schema_to_pydantic_model(name: str, parameters: Optional[Dict[str, Any]]) -> Optional[type]:
    """
    Build a dynamic pydantic model from a JSON schema dict so that
    ToolRegistry.get_openai_schemas() (which reads args_schema) preserves
    parameter definitions when constructing OpenAI function-calling schemas.
    """
    if not isinstance(parameters, dict):
        return None
    props = parameters.get("properties")
    if not isinstance(props, dict) or not props:
        return None

    required = set(parameters.get("required") or [])
    fields: Dict[str, Any] = {}

    for prop_name, spec in props.items():
        py_type: Any = str
        if isinstance(spec, dict):
            t = spec.get("type")
            if t == "integer":
                py_type = int
            elif t == "number":
                py_type = float
            elif t == "boolean":
                py_type = bool
            elif t == "array":
                # Preserve the item type for the common scalar cases so
                # the LLM receives ``array<string>`` / ``array<integer>``
                # instead of a bare ``array``. Complex item types are
                # caught upstream by _should_disable_args_schema and
                # never reach this branch — so any remaining array here
                # is safe to annotate with List[T].
                items = spec.get("items")
                items_type = items.get("type") if isinstance(items, dict) else None
                if items_type == "integer":
                    py_type = List[int]
                elif items_type == "number":
                    py_type = List[float]
                elif items_type == "boolean":
                    py_type = List[bool]
                elif items_type == "string":
                    py_type = List[str]
                else:
                    py_type = List[Any]
            elif t == "object":
                # Only reachable for object without declared properties
                # (free-form dict). The complex case is filtered upstream.
                py_type = Dict[str, Any]
            # else: string

        default = ... if prop_name in required else None
        fields[prop_name] = (py_type, default)

    try:
        model_name = f"{name}_Args"
        return _create_model(model_name, **fields)
    except Exception:
        return None

class WebToolAdapter(BaseTool):
    """
    Adapter wrapping a Web runtime tool executor as a canonical Core BaseTool.
    """

    name: str = "web_tool"
    description: str = "Adapted PELDRUN Web tool."

    def __init__(
        self,
        name: str,
        description: str = "",
        parameters: Optional[Dict[str, Any]] = None,
        execute_fn: Optional[Callable[..., Any]] = None,
        *,
        executor: Optional[Callable[..., Any]] = None,
        workspace_root: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ) -> None:
        resolved_fn = execute_fn or executor or kwargs.pop("func", None)
        if resolved_fn is None:
            raise ValueError(f"WebToolAdapter for tool {name!r} requires a valid execute_fn callable.")

        try:
            super().__init__(workspace_root=workspace_root, **kwargs)
        except TypeError:
            try:
                super().__init__()
            except Exception:
                pass

        self.name = str(name).strip()
        self.description = str(description or "").strip()
        self._parameters_schema: Dict[str, Any] = self._normalize_parameters_schema(parameters)
        self.parameters: Dict[str, Any] = self._parameters_schema

        # Expose args_schema so Core's ToolRegistry can build proper
        # OpenAI tool schemas for SIMPLE schemas. For schemas that carry
        # compositional constructs ($ref, oneOf, anyOf, allOf, not) or
        # nested arrays/objects, pydantic's create_model would silently
        # flatten the structure (array→list, object→dict), producing a
        # schema the LLM rejects or misinterprets.
        #
        # In those cases we set args_schema = None so that Core's
        # schema-builder falls back to reading `self.parameters`
        # directly (see to_openai_schema / to_openai_tool / to_param),
        # which preserves the original JSON schema verbatim.
        if _should_disable_args_schema(self._parameters_schema):
            self.args_schema = None
        else:
            self.args_schema = _schema_to_pydantic_model(
                str(name).strip() or "web_tool",
                self._parameters_schema,
            )
        self.execute_fn: Callable[..., Any] = resolved_fn
        self.workspace_root: Optional[str] = workspace_root
        self.adapter_metadata: Dict[str, Any] = dict(metadata or {})

    @staticmethod
    def _normalize_parameters_schema(schema: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        """Ensure the tool parameter schema is a valid JSON Schema object definition."""
        if not isinstance(schema, dict) or not schema:
            return {
                "type": "object",
                "properties": {},
                "required": [],
            }

        normalized = dict(schema)
        if "type" not in normalized:
            normalized["type"] = "object"
        if "properties" not in normalized or not isinstance(normalized.get("properties"), dict):
            normalized["properties"] = {}
        if "required" in normalized and not isinstance(normalized.get("required"), list):
            normalized["required"] = list(normalized["required"])
        return normalized

    @staticmethod
    def _standardize_result(raw_result: Any) -> ToolResult:
        """
        Convert raw executor output into a canonical Core ToolResult.

        CRITICAL (SR-401): If `raw_result` is already a `ToolResult` (or a duck-typed
        equivalent carrying `is_error` and `output`), preserve its exact error state,
        exit code, artifacts, and metadata instead of flattening it into a successful string.
        """
        if isinstance(raw_result, ToolResult):
            return raw_result

        # Duck-typed ToolResult compatibility across module reload boundaries
        if (
            raw_result is not None
            and hasattr(raw_result, "output")
            and hasattr(raw_result, "is_error")
        ):
            is_err = bool(getattr(raw_result, "is_error", False))
            raw_exit = getattr(raw_result, "exit_code", None)
            exit_code = int(raw_exit) if isinstance(raw_exit, int) else (1 if is_err else 0)
            raw_meta = getattr(raw_result, "metadata", None)
            meta_dict = dict(raw_meta) if isinstance(raw_meta, dict) else {}
            raw_artifacts = getattr(raw_result, "artifacts", None)
            artifacts_list: List[Any] = list(raw_artifacts) if isinstance(raw_artifacts, (list, tuple)) else []
            out_val = getattr(raw_result, "output", "")
            return ToolResult(
                output="" if out_val is None else str(out_val),
                exit_code=exit_code,
                is_error=is_err,
                metadata=meta_dict,
                artifacts=artifacts_list,
            )

        if isinstance(raw_result, dict):
            is_err = bool(raw_result.get("is_error") or raw_result.get("error"))
            raw_exit = raw_result.get("exit_code")
            exit_code = int(raw_exit) if isinstance(raw_exit, int) else (1 if is_err else 0)
            output_val = raw_result.get("output")
            if output_val is None:
                output_val = raw_result.get("error") or ""
            raw_meta = raw_result.get("metadata")
            meta_dict = dict(raw_meta) if isinstance(raw_meta, dict) else {}
            raw_artifacts = raw_result.get("artifacts")
            artifacts_list = list(raw_artifacts) if isinstance(raw_artifacts, (list, tuple)) else []
            return ToolResult(
                output=str(output_val),
                exit_code=exit_code,
                is_error=is_err,
                metadata=meta_dict,
                artifacts=artifacts_list,
            )

        if raw_result is None:
            return ToolResult(
                output="",
                exit_code=0,
                is_error=False,
            )

        return ToolResult(
            output=str(raw_result),
            exit_code=0,
            is_error=False,
        )

    async def _invoke_executor(self, **kwargs: Any) -> ToolResult:
        """
        Invoke the underlying sync or async `execute_fn` with caller-supplied arguments
        and standardize the return value into a Core `ToolResult`.
        """
        try:
            call_kwargs = dict(kwargs)
            try:
                sig = inspect.signature(self.execute_fn)
                params = sig.parameters
                accepts_var_kw = any(
                    p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()
                )
                if not accepts_var_kw:
                    call_kwargs = {
                        k: v for k, v in call_kwargs.items() if k in params
                    }
            except (TypeError, ValueError):
                pass

            outcome = self.execute_fn(**call_kwargs)
            if inspect.isawaitable(outcome):
                raw_result = await outcome
            else:
                raw_result = outcome

            return self._standardize_result(raw_result)

        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "WebToolAdapter execution failed for tool %r: %s",
                self.name,
                exc,
                exc_info=True,
            )
            return ToolResult(
                output=f"Tool Execution Error ({self.name}): {exc}",
                exit_code=1,
                is_error=True,
                metadata={
                    "tool": self.name,
                    "error_code": "WEB_TOOL_ADAPTER_EXCEPTION",
                },
            )

    async def _arun(self, **kwargs: Any) -> ToolResult:
        """Core BaseTool async implementation hook."""
        return await self._invoke_executor(**kwargs)

    async def aexecute(self, **kwargs: Any) -> ToolResult:
        """Canonical async execution entrypoint invoked by ToolCallAgent and adapters."""
        return await self._invoke_executor(**kwargs)

    async def execute(self, **kwargs: Any) -> ToolResult:
        """Async alias for callers invoking tool.execute(**kwargs)."""
        return await self._invoke_executor(**kwargs)

    def _run(self, **kwargs: Any) -> ToolResult:
        """Synchronous fallback bridge."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        if loop and loop.is_running():
            import concurrent.futures

            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(asyncio.run, self._invoke_executor(**kwargs))
                return future.result()

        return asyncio.run(self._invoke_executor(**kwargs))

    def to_openai_tool(self) -> Dict[str, Any]:
        """Return the OpenAI function-calling tool specification."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self._parameters_schema,
            },
        }

    def to_param(self) -> Dict[str, Any]:
        """Alias for OpenAI tool parameter serialization."""
        return self.to_openai_tool()

    def to_openai_schema(self) -> Dict[str, Any]:
        """
        Core-compatible alias matching ``DynamicMCPTool.to_openai_schema``.

        Core's ToolCollection accepts either naming convention
        depending on the code path — providing both guarantees this
        adapter is discovered and serialised correctly regardless of
        which method the caller probes for.
        """
        return self.to_openai_tool()

    def get_schema(self) -> Dict[str, Any]:
        """Return the tool parameter JSON schema."""
        return dict(self._parameters_schema)


__all__ = ["WebToolAdapter"]
