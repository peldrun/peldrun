# backend/omweb/extensions/mcp_auth_state.py
"""
Shared runtime state for MCP authentication signaling.

Used by two subsystems that cannot import each other without creating
a circular dependency:

  * ``omweb.adapters.core_adapter_parts.resolve_tool_runtime`` records
    that a tool call required authentication before it could proceed.
    This happens *lazily* — only when the agent actually tries to call
    an MCP tool — not on every run.
  * ``omweb.engines.peldrun_engine`` consumes that record after the
    run finishes to fail the job with the correct error code and to
    skip the normal completion path (which would otherwise overwrite
    the failure with a successful result).

The state lives in memory only. A process restart clears it, which is
correct: any in-flight job is already dead once its owning process
exits.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

logger = logging.getLogger("omweb.extensions.mcp_auth_state")


_job_auth_required: Dict[str, Dict[str, Any]] = {}


def manifest_needs_auth(manifest: Dict[str, Any]) -> bool:
    """
    Decide whether an MCP server requires user-provided credentials.

    Two independent signals are OR'd:
      * An explicit ``auth_strategy`` declaring a recognized auth kind.
      * A required secret field inside ``env_schema``.

    A server with neither signal (filesystem, fetch, ...) is treated
    as auth-free and never prompts the user.
    """
    if not isinstance(manifest, dict):
        return False

    strategy = str(manifest.get("auth_strategy") or "").lower().strip()
    if strategy in ("oauth", "env", "apikey", "api_key", "bearer", "token"):
        return True

    env_schema = manifest.get("env_schema")
    if not isinstance(env_schema, dict):
        return False
    required = env_schema.get("required") or []
    properties = env_schema.get("properties") or {}
    if not isinstance(required, list) or not isinstance(properties, dict):
        return False
    for field in required:
        prop = properties.get(field)
        if isinstance(prop, dict) and prop.get("secret"):
            return True
    return False


def manifest_is_oauth_capable(manifest: Dict[str, Any]) -> bool:
    """An MCP server can do OAuth only if it has an HTTP URL."""
    transport = str(manifest.get("transport") or "").lower().strip()
    url = manifest.get("url")
    if not isinstance(url, str) or not url:
        return False
    return transport in ("sse", "http", "streamable-http", "")


def manifest_auth_method(manifest: Dict[str, Any]) -> str:
    """Classify the auth mechanism for the modal payload."""
    strategy = str(manifest.get("auth_strategy") or "").lower().strip()
    if strategy in ("oauth", "env", "apikey", "api_key", "bearer", "token"):
        return strategy
    if manifest_is_oauth_capable(manifest):
        return "oauth"
    return "env"


def mark_auth_required(
    job_id: str,
    *,
    chat_id: str,
    server_id: str,
    manifest: Dict[str, Any],
) -> None:
    """Record that the given job hit a lazy MCP auth requirement."""
    _job_auth_required[job_id] = {
        "chat_id": chat_id,
        "server_id": server_id,
        "server_name": manifest.get("name") or server_id,
        "auth_method": manifest_auth_method(manifest),
        "env_schema": manifest.get("env_schema"),
        "oauth_supported": manifest_is_oauth_capable(manifest),
    }


def pop_auth_required(job_id: str) -> Optional[Dict[str, Any]]:
    """Return and remove the auth-required marker for a job (one-shot)."""
    return _job_auth_required.pop(job_id, None)


def clear(job_id: str) -> None:
    """Remove any stored auth-required marker for a job."""
    _job_auth_required.pop(job_id, None)


__all__ = [
    "manifest_needs_auth",
    "manifest_is_oauth_capable",
    "manifest_auth_method",
    "mark_auth_required",
    "pop_auth_required",
    "clear",
]
