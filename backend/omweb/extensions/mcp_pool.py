# backend/omweb/extensions/mcp_pool.py
"""
Centralized Connection Pool and Runtime Session Manager for MCP Servers.

Responsibilities:
  * Maintain persistent MCPClient instances over stdio and sse/http transports.
  * Integrate runtime configuration and environment variables from McpSettingsManager.
  * Execute real JSON-RPC handshakes and dynamic tool discovery.
  * Provide robust process cleanup and prevent leaked background subprocesses.
  * Cache tool discovery results with explicit invalidation and a TTL fallback.
  * Serialize session lifecycle operations to prevent concurrent-close races
    (a close_session racing with an in-flight get_or_create_client would
    otherwise leave an orphaned subprocess behind).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import shlex
import shutil
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from peldrun.tools.mcp_client import BaseMCPTransport, MCPClient
from peldrun.tools.transports.stdio import StdioTransport
from omweb.extensions.settings_manager import mcp_settings_manager

try:
    from peldrun.tools.transports.sse import SSETransport
except ImportError:
    SSETransport = None

logger = logging.getLogger("omweb.extensions.mcp_pool")


# How long a discovered tool list is considered fresh. After this window
# the next get_tools() call re-probes the server and refreshes the cache.
# Explicit invalidation (invalidate_cache) bypasses this window.
DEFAULT_CACHE_TTL_SECONDS: float = 300.0


# ---------------------------------------------------------------------------
# Per-chat credential override contract
# ---------------------------------------------------------------------------
#
# When ``env_override`` is passed to get_or_create_client / build_transport,
# it FULLY REPLACES the manifest's global env. This is the per-chat
# isolation contract: a chat that chose "custom" credentials must not
# silently inherit any global variable.
#
# Two keys inside ``env_override`` are treated as META-keys rather than
# environment variables:
#
#   _oauth_access_token : Bearer token for HTTP/SSE transports.
#                         Never forwarded as an env var to stdio
#                         subprocesses (there is no HTTP there).
#   _oauth_token_type   : Authorization scheme, default "Bearer".
#
# Both participate in the session-key fingerprint so a token refresh
# produces a fresh client instead of silently reusing a stale session.
_OAUTH_ACCESS_TOKEN_KEY = "_oauth_access_token"
_OAUTH_TOKEN_TYPE_KEY = "_oauth_token_type"
_OAUTH_PREFIX = "_oauth_"


def _split_env_override(
    env_override: Optional[Dict[str, str]],
) -> Tuple[Optional[Dict[str, str]], Optional[str], str]:
    """
    Split an ``env_override`` into three components.

    Returns ``(real_env, access_token, token_type)``:

      * ``real_env``     — environment variables, or None if empty.
      * ``access_token`` — Bearer token for HTTP/SSE Authorization
                           header, or None if not supplied.
      * ``token_type``   — scheme, defaults to "Bearer".

    ``_oauth_*`` keys are stripped from ``real_env`` so a stdio
    subprocess never sees them as environment variables — a token that
    only makes sense for HTTP must not leak into a process env dump.
    """
    if not env_override:
        return None, None, "Bearer"

    real_env: Dict[str, str] = {}
    access_token: Optional[str] = None
    token_type = "Bearer"

    for key, value in env_override.items():
        sk = str(key)
        if sk == _OAUTH_ACCESS_TOKEN_KEY:
            access_token = str(value) if value is not None else None
            continue
        if sk == _OAUTH_TOKEN_TYPE_KEY:
            if value:
                token_type = str(value)
            continue
        if sk.startswith(_OAUTH_PREFIX):
            # Unknown meta-key — consumed here, never forwarded.
            continue
        real_env[str(sk)] = str(value) if value is not None else ""

    return (real_env or None), access_token, token_type


def _compute_creds_hash(
    env_override: Optional[Dict[str, str]],
) -> Optional[str]:
    """
    Fingerprint the effective credentials for session-key purposes.

    The hash covers ALL keys — including ``_oauth_access_token`` — so
    a token refresh yields a new session-key and a fresh client. The
    hash is truncated to 12 hex chars; collisions at this scale are
    astronomically unlikely within a single server pool.
    """
    if not env_override:
        return None
    try:
        payload = json.dumps(
            env_override, sort_keys=True, ensure_ascii=False
        )
    except (TypeError, ValueError):
        # Defensive fallback for pathological inputs (non-serialisable
        # values that should never occur given the type contract).
        payload = repr(
            sorted(
                ((str(k), str(v)) for k, v in env_override.items()),
                key=lambda kv: kv[0],
            )
        )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]


# ---------------------------------------------------------------------------
# JSON Schema sanitizer for LLM providers
# ---------------------------------------------------------------------------
#
# Many MCP servers (Notion, Linear, Atlassian) publish tool schemas that
# use JSON Schema draft 2019-09 features: ``$defs`` at the root, ``$ref``
# pointers into ``$defs`` from deep inside ``anyOf``/``oneOf``/nested
# objects, and combinatorial composition keywords.
#
# Generic OpenAI-compatible providers — LM Studio (llama.cpp engine),
# llama.cpp servers, some vLLM configurations — do NOT implement $ref
# resolution. When they receive such a schema they reject the ENTIRE
# request with "cannot resolve $ref #/$defs/...". One broken tool
# schema disables ALL tools from the server.
#
# ``_sanitize_json_schema_for_llm`` converts any schema into a fully
# inlined, $ref-free form every provider can process. If a reference
# cannot be resolved (missing $defs entry, cyclic ref, unsupported
# pointer), the field is replaced with a permissive ``{type: object}``
# so the tool remains callable — the LLM can still pass any structure
# the underlying server accepts.

_ALLOWED_SCHEMA_KEYS = frozenset({
    "type", "description", "title", "format", "enum",
    "default", "minimum", "maximum", "exclusiveMinimum",
    "exclusiveMaximum", "minLength", "maxLength", "minItems",
    "maxItems", "pattern", "required", "properties", "items",
    "additionalProperties", "nullable",
})


def _sanitize_json_schema_for_llm(
    node: Any,
    root: Optional[Dict[str, Any]] = None,
    depth: int = 0,
    seen_refs: Optional[Set[str]] = None,
) -> Any:
    """Convert an MCP tool JSON schema to a $ref-free form."""
    if depth > 10 or not isinstance(node, dict):
        return {} if depth > 10 else node

    if seen_refs is None:
        seen_refs = set()

    # ── Resolve $ref ────────────────────────────────────────────
    ref = node.get("$ref")
    if isinstance(ref, str):
        if ref in seen_refs:
            return {"type": "object", "additionalProperties": True}
        if not ref.startswith("#/"):
            return {"type": "object", "additionalProperties": True}

        parts = ref.split("/")
        target: Optional[Dict[str, Any]] = None
        if (
            len(parts) >= 3
            and parts[1] in ("$defs", "definitions")
            and isinstance(root, dict)
        ):
            defs = root.get(parts[1])
            if isinstance(defs, dict):
                cand = defs.get(parts[2])
                if isinstance(cand, dict):
                    target = cand

        if target is None:
            return {"type": "object", "additionalProperties": True}

        seen_refs.add(ref)
        resolved = _sanitize_json_schema_for_llm(
            target, root, depth + 1, seen_refs
        )
        seen_refs.discard(ref)

        extra = {k: v for k, v in node.items() if k != "$ref"}
        if isinstance(resolved, dict) and extra:
            merged = dict(resolved)
            for k, v in extra.items():
                if k in _ALLOWED_SCHEMA_KEYS:
                    merged[k] = _sanitize_json_schema_for_llm(
                        v, root, depth + 1, seen_refs
                    )
            return merged
        return resolved

    # ── Handle composition ──────────────────────────────────────
    for comb in ("anyOf", "oneOf"):
        variants = node.get(comb)
        if isinstance(variants, list) and variants:
            first = variants[0]
            sanitized = _sanitize_json_schema_for_llm(
                first, root, depth + 1, seen_refs
            )
            if isinstance(sanitized, dict) and sanitized.get("type"):
                return sanitized
            return {"type": "object"}

    all_of = node.get("allOf")
    if isinstance(all_of, list) and all_of:
        merged_props: Dict[str, Any] = {}
        merged_required: list = []
        for opt in all_of:
            sanitized = _sanitize_json_schema_for_llm(
                opt, root, depth + 1, seen_refs
            )
            if isinstance(sanitized, dict):
                props = sanitized.get("properties")
                if isinstance(props, dict):
                    merged_props.update(props)
                req = sanitized.get("required")
                if isinstance(req, list):
                    merged_required.extend(req)
        return {
            "type": "object",
            "properties": merged_props,
            "required": list(dict.fromkeys(merged_required)),
        }

    # ── Recurse into children ───────────────────────────────────
    result: Dict[str, Any] = {}
    for key, value in node.items():
        if key not in _ALLOWED_SCHEMA_KEYS:
            continue
        if key == "properties" and isinstance(value, dict):
            result["properties"] = {
                k: _sanitize_json_schema_for_llm(
                    v, root, depth + 1, seen_refs
                )
                for k, v in value.items()
            }
        elif key == "items":
            result["items"] = _sanitize_json_schema_for_llm(
                value, root, depth + 1, seen_refs
            )
        elif key == "additionalProperties" and isinstance(value, dict):
            result["additionalProperties"] = _sanitize_json_schema_for_llm(
                value, root, depth + 1, seen_refs
            )
        elif key == "required" and isinstance(value, list):
            result["required"] = list(value)
        else:
            result[key] = value

    return result


def _sanitize_tool_input_schema(input_schema: Any) -> Dict[str, Any]:
    """
    Wrap ``_sanitize_json_schema_for_llm`` for the top-level tool input
    schema. Ensures the result is always a valid ``{type: object, ...}``
    schema so OpenAI/llama.cpp validators accept it.
    """
    root = input_schema if isinstance(input_schema, dict) else {}
    sanitized = _sanitize_json_schema_for_llm(root, root)
    if not isinstance(sanitized, dict):
        return {"type": "object", "properties": {}, "required": []}
    if sanitized.get("type") != "object":
        sanitized["type"] = "object"
    if not isinstance(sanitized.get("properties"), dict):
        sanitized["properties"] = {}
    if not isinstance(sanitized.get("required"), list):
        sanitized["required"] = []
    return sanitized


class McpSessionPool:
    """Manages persistent active connections and dynamic sessions for MCP servers."""

    def __init__(self, cache_ttl_seconds: float = DEFAULT_CACHE_TTL_SECONDS) -> None:
        self._clients: Dict[str, MCPClient] = {}
        self._locks: Dict[str, asyncio.Lock] = {}
        self._global_lock = asyncio.Lock()

        # Session keys currently being torn down. get_or_create_client must
        # refuse to hand out a client whose key is in this set — otherwise
        # it would race with close_session / close_server_sessions and
        # leak a subprocess.
        self._closing: Set[str] = set()

        # Per-server cache of the discovered tools list.
        # Structure: {server_id: {"tools": [...], "fetched_at": float}}
        self._tool_cache: Dict[str, Dict[str, Any]] = {}
        self._cache_ttl = float(cache_ttl_seconds)

    # ─────────────────────────────────────────────────────────────
    # Key construction
    # ─────────────────────────────────────────────────────────────

    def _get_session_key(
        self,
        server_id: str,
        workspace_root: Optional[str] = None,
        creds_hash: Optional[str] = None,
    ) -> str:
        """
        Construct a unique cache key per server, workspace, and
        credential set. The creds_hash segment is omitted when no
        env_override is in play, preserving the historical key shape
        for the common global-settings case.
        """
        normalized_root = str(Path(workspace_root).resolve()) if workspace_root else "default"
        base = f"{server_id}:{normalized_root}"
        return f"{base}:{creds_hash}" if creds_hash else base

    def _server_prefix(self, server_id: str) -> str:
        return f"{server_id}:"

    # ─────────────────────────────────────────────────────────────
    # Probe workspace resolution (filesystem-specific)
    # ─────────────────────────────────────────────────────────────

    def _resolve_workspace_for_transport(
        self,
        manifest: Dict[str, Any],
        workspace_root: Optional[str],
    ) -> Optional[str]:
        """
        Ensure filesystem servers always receive at least one directory.

        Priority (highest → lowest):
          1. Explicit ``workspace_root`` passed by the caller
             (chat context during a run or explicit Chat Tools call).
          2. The most recently active chat's files directory — used
             when testing / refreshing from the Store UI, which does
             not know the chat context. Matches user expectation: the
             last chat you were in is the one you're testing for.
          3. ``~/.peldrun/probe_workspace`` — final fallback when no
             chats exist yet.

        Non-filesystem servers pass through unchanged.
        """
        if workspace_root is not None:
            return workspace_root

        command = str(manifest.get("command") or "")
        is_filesystem = (
            "server-filesystem" in command
            or "mcp-server-filesystem" in command
        )
        if not is_filesystem:
            return None

        # ── 2. Most recently active chat's workspace ──────────────
        try:
            from omweb.project_manager import project_manager

            chats = project_manager.list_chats()
            if chats:
                latest = chats[0]
                chat_id = latest.get("id")
                project_id = latest.get("project_id") or "default_project"
                if chat_id:
                    fdir = project_manager.get_chat_files_dir(
                        chat_id, project_id
                    )
                    if fdir.exists():
                        logger.debug(
                            "Probe workspace resolved from latest chat "
                            "'%s': %s",
                            chat_id,
                            fdir,
                        )
                        return str(fdir)
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "Latest-chat workspace resolution failed: %s", exc
            )

        # ── 3. Fallback: probe workspace ──────────────────────────
        probe_dir = Path.home() / ".peldrun" / "probe_workspace"
        try:
            probe_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            logger.warning(
                "Could not create probe workspace at %s: %s", probe_dir, exc
            )
            return None
        logger.debug("Using probe_workspace fallback: %s", probe_dir)
        return str(probe_dir)

    # ─────────────────────────────────────────────────────────────
    # Command preparation
    # ─────────────────────────────────────────────────────────────

    def _build_command_args(
        self,
        command_str: str,
        settings: Dict[str, Any],
        workspace_root: Optional[str] = None,
    ) -> Tuple[str, List[str]]:
        """
        Parse raw command string and append configured arguments and directory targets.

        Security / robustness hardening:
          * On Windows, shlex is invoked with posix=False; surrounding quotes
            are then stripped manually so that paths with spaces (e.g.
            "C:\\Program Files\\...") resolve correctly.
          * The resolved executable must exist in PATH or as a concrete file;
            otherwise we raise immediately with a clear error rather than
            spawning a doomed subprocess whose failure surface is opaque.
        """
        if os.name == "nt":
            cmd_parts = [
                p.strip('"').strip("'")
                for p in shlex.split(command_str, posix=False)
            ]
        else:
            cmd_parts = shlex.split(command_str)

        if not cmd_parts:
            raise ValueError("Command string cannot be empty.")

        executable = cmd_parts[0]
        resolved_exe = shutil.which(executable)
        if not resolved_exe:
            candidate = Path(executable)
            if candidate.is_file():
                resolved_exe = str(candidate.resolve())
            else:
                raise ValueError(
                    f"MCP server executable '{executable}' was not found in PATH "
                    f"and is not a valid file path."
                )

        args = cmd_parts[1:]

        is_filesystem = (
            "@modelcontextprotocol/server-filesystem" in command_str
            or "mcp-server-filesystem" in command_str
        )

        # ── Priority order for filesystem servers ─────────────────
        # 1. workspace_root (chat's actual files directory) — highest
        #    priority, always appended when available.
        # 2. allowed_directories (from Settings) — appended after, as
        #    additional accessible paths, deduped against #1.
        #
        # For non-filesystem servers, allowed_directories is passed
        # through as-is (most servers ignore it, but some plugins use
        # it for scoping).
        if is_filesystem and workspace_root:
            ws_str = str(workspace_root)
            if ws_str not in args:
                args.append(ws_str)

        allowed_dirs = settings.get("allowed_directories", [])
        if isinstance(allowed_dirs, list):
            for directory in allowed_dirs:
                d_str = str(directory) if directory else ""
                if d_str and d_str not in args:
                    args.append(d_str)

        return resolved_exe, args

    # ─────────────────────────────────────────────────────────────
    # Transport building
    # ─────────────────────────────────────────────────────────────

    def build_transport(
        self,
        manifest: Dict[str, Any],
        workspace_root: Optional[str] = None,
        env_override: Optional[Dict[str, str]] = None,
    ) -> Tuple[Optional[BaseMCPTransport], Optional[str], Optional[str]]:
        """
        Construct the designated MCP transport based on manifest specification.

        Returns ``(transport_instance, fallback_http_url, auth_token)``.

        ``env_override`` semantics (per-chat credential isolation):

          * When provided, it FULLY REPLACES the manifest's global env.
            No merge — a chat that chose "custom" credentials sees only
            those, never a mix.
          * Meta-keys (``_oauth_access_token``, ``_oauth_token_type``)
            are extracted here, not forwarded to the subprocess. The
            access token becomes an ``Authorization`` header for
            HTTP/SSE transports — this is the correct OAuth 2.1 wire
            format and is what the server expects.
          * For stdio transports, the access token is simply dropped:
            a stdio MCP server has no HTTP stack to receive it, and
            forwarding it as an env var would leak the secret into a
            process env dump.

        The third tuple slot (``auth_token``) is only non-None for the
        fallback HTTP JSON-RPC path — the SSE and stdio paths carry
        auth inside the transport instance.
        """
        server_id = manifest.get("id", "unknown")
        transport_type = manifest.get("transport", "stdio").lower()
        command = manifest.get("command")
        target_url = manifest.get("url")

        runtime_config = mcp_settings_manager.get_effective_runtime_config(server_id, manifest)
        settings = runtime_config.get("settings", {})

        if env_override is not None:
            # Full replacement: per-chat isolation.
            env_vars, oauth_token, token_type = _split_env_override(env_override)
            env_vars = env_vars or {}
        else:
            # Global fallback.
            env_vars = {str(k): str(v) for k, v in runtime_config.get("env", {}).items()}
            oauth_token = None
            token_type = "Bearer"

        if transport_type == "stdio":
            if not command:
                raise ValueError(
                    f"MCP server '{server_id}' specifies 'stdio' transport but lacks 'command'."
                )

            # Filesystem servers require at least one directory argument.
            # When workspace_root is None (e.g. from the Store UI's
            # probe / refresh paths), this resolves a safe default so
            # the server can start. Non-filesystem servers pass through
            # unchanged — see _resolve_workspace_for_transport.
            effective_ws = self._resolve_workspace_for_transport(
                manifest, workspace_root
            )

            executable, args = self._build_command_args(
                command, settings, effective_ws
            )
            from omweb.extensions.mcp_subsystem_settings import (
                get_startup_timeout_seconds as _startup_timeout,
            )
            transport = StdioTransport(
                command=executable,
                args=args,
                env=env_vars if env_vars else None,
                cwd=effective_ws,
                timeout=float(
                    manifest.get("timeout") or _startup_timeout()
                ),
            )
            # Note: oauth_token is intentionally NOT forwarded to a
            # stdio subprocess — it is an HTTP-layer credential.
            return transport, None, None

        elif transport_type in ("sse", "http"):
            # ── Placeholder substitution from env ──────────────────
            # Allows manifests to declare a dynamic URL such as
            #   "{WORDPRESS_URL}/wp-json/mcp/mcp-adapter-default-server"
            # where WORDPRESS_URL is supplied per-chat (or globally)
            # via env_schema. Substitution is limited to {KEY} tokens
            # found in env_vars; unreplaced tokens are left as-is so a
            # missing variable produces a clear error at request time
            # rather than silently sending a malformed URL.
            if isinstance(target_url, str) and env_vars:
                for k, v in env_vars.items():
                    if v is None:
                        continue
                    target_url = target_url.replace("{" + str(k) + "}", str(v))

            if not target_url:
                raise ValueError(
                    f"MCP server '{server_id}' specifies '{transport_type}' transport but lacks 'url'."
                )

            # ── Global OAuth tokens as fallback ────────────────────
            # OAuth-only servers (Notion, Linear, etc.) have no
            # env_schema. Their credentials live in the server-level
            # config as `oauth_enc`. When NO per-chat override exists
            # (env_override is None), we pull the global token bundle
            # and use it as a Bearer credential.
            #
            # Precedence:
            #   1. env_override with _oauth_access_token (per-chat)
            #   2. global mcp_settings_manager OAuth bundle
            #   3. HTTP Basic Auth (WordPress)
            #   4. Nothing → unauthenticated (Store Test will 401)
            if env_override is None:
                try:
                    from omweb.extensions.settings_manager import (
                        mcp_settings_manager as _settings,
                    )

                    global_bundle = _settings.get_oauth(server_id)
                    if (
                        isinstance(global_bundle, dict)
                        and global_bundle.get("access_token")
                    ):
                        oauth_token = str(global_bundle["access_token"])
                        token_type = str(
                            global_bundle.get("token_type") or "Bearer"
                        )
                        logger.debug(
                            "Using global OAuth token for '%s' (expires_at=%s).",
                            server_id,
                            global_bundle.get("expires_at"),
                        )
                except Exception as g_err:  # noqa: BLE001
                    logger.debug(
                        "Failed to load global OAuth tokens for '%s': %s",
                        server_id,
                        g_err,
                    )

            # Compose the outbound headers: static manifest headers
            # first, then an Authorization header from either OAuth
            # (Bearer) or HTTP Basic credentials declared via the
            # optional `http_basic_auth` manifest block.
            headers: Dict[str, str] = {"Content-Type": "application/json"}
            manifest_headers = manifest.get("headers")
            if isinstance(manifest_headers, dict):
                for hk, hv in manifest_headers.items():
                    headers[str(hk)] = str(hv)

            # ── HTTP Basic Auth (WordPress, GitLab, etc.) ──────────
            # Only applied when no Bearer token is available. If both
            # are somehow present, Bearer wins (an OAuth flow is a
            # more specific user decision than static env creds).
            basic_cfg = manifest.get("http_basic_auth")
            if (
                not oauth_token
                and isinstance(basic_cfg, dict)
                and env_vars
            ):
                u_key = str(basic_cfg.get("user_env") or "")
                p_key = str(basic_cfg.get("pass_env") or "")
                u_val = env_vars.get(u_key) if u_key else None
                p_val = env_vars.get(p_key) if p_key else None
                if u_val and p_val:
                    import base64 as _b64
                    token = _b64.b64encode(
                        f"{u_val}:{p_val}".encode("utf-8")
                    ).decode("ascii")
                    headers["Authorization"] = f"Basic {token}"
                    logger.debug(
                        "Applied HTTP Basic Auth for '%s' (user=%r).",
                        server_id,
                        u_val,
                    )

            # OAuth Bearer takes precedence over Basic if both are
            # somehow present.
            if oauth_token:
                headers["Authorization"] = f"{token_type} {oauth_token}"

            if transport_type == "sse" and SSETransport is not None:
                from omweb.extensions.mcp_subsystem_settings import (
                    get_startup_timeout_seconds as _startup_timeout,
                )
                # Defensive construction: SSETransport's signature has
                # varied across PELDRUN core versions and MCP SDK
                # releases. Two auth mechanisms exist:
                #
                #   * ``headers`` kwarg — newer builds that accept a
                #     full header dict (rare; not the case today).
                #   * ``auth_token`` kwarg — the current PELDRUN core
                #     signature (endpoint_url, auth_token, timeout,
                #     transport). The transport prepends ``Bearer ``
                #     internally, so we must pass the RAW token here.
                #
                # We detect the actual parameters at runtime so this
                # code works regardless of the installed version.
                import inspect as _inspect

                _timeout = float(
                    manifest.get("timeout") or _startup_timeout()
                )
                try:
                    _params = _inspect.signature(SSETransport).parameters
                except (TypeError, ValueError):
                    _params = {}

                _url_key = next(
                    (
                        k
                        for k in (
                            "endpoint_url",
                            "url",
                            "base_url",
                            "server_url",
                            "endpoint",
                            "server_endpoint",
                        )
                        if k in _params
                    ),
                    None,
                )

                _kwargs: Dict[str, Any] = {}
                if "timeout" in _params:
                    _kwargs["timeout"] = _timeout

                # ── Auth wiring ────────────────────────────────────
                # Prefer the newer ``headers`` kwarg when available;
                # otherwise fall back to the current core's
                # ``auth_token`` parameter. For Bearer tokens we pass
                # the raw token (the transport prepends "Bearer ").
                #
                # HTTP Basic auth over SSE is NOT expressible via
                # ``auth_token`` (the transport hard-codes "Bearer ").
                # WordPress is the only server we ship that uses Basic,
                # and it is HTTP-only, so this limitation does not
                # affect any first-party integration today.
                if "headers" in _params:
                    _kwargs["headers"] = headers
                    logger.debug(
                        "SSETransport auth via headers kwarg for '%s'.",
                        server_id,
                    )
                elif "auth_token" in _params and oauth_token:
                    _kwargs["auth_token"] = oauth_token
                    logger.debug(
                        "SSETransport auth via auth_token kwarg for "
                        "'%s' (token_type=%s).",
                        server_id,
                        token_type,
                    )

                try:
                    if _url_key:
                        transport = SSETransport(
                            **{_url_key: target_url, **_kwargs}
                        )
                    else:
                        # Positional URL — assume it's the first arg.
                        transport = SSETransport(target_url, **_kwargs)
                    logger.debug(
                        "SSETransport built for '%s' (url_param=%r, "
                        "auth_kw=%r).",
                        server_id,
                        _url_key or "<positional>",
                        "headers" if "headers" in _kwargs else (
                            "auth_token" if "auth_token" in _kwargs else "none"
                        ),
                    )
                except TypeError as sse_err:
                    # Last-ditch: pass the URL positionally and drop
                    # everything else. This drops auth too — but a
                    # transport with no auth at least surfaces a
                    # clean 401 instead of a TypeError.
                    logger.warning(
                        "SSETransport failed with detected signature "
                        "(%s); retrying with positional URL only.",
                        sse_err,
                    )
                    transport = SSETransport(target_url)

                return transport, None, None

            # Fallback to direct HTTP JSON-RPC client. The auth token
            # is surfaced so get_or_create_client can pass it to
            # MCPClient, which builds its own Authorization header.
            #
            # Strip a trailing slash from the URL: Notion (and other
            # Streamable HTTP servers) reject "/mcp/" with 404 while
            # accepting "/mcp". The MCPClient appends a slash
            # internally in some versions, so passing the clean URL
            # is the only reliable way to hit the right endpoint.
            _clean_url = target_url.rstrip("/") if isinstance(target_url, str) else target_url
            return None, _clean_url, oauth_token

        raise ValueError(
            f"Unsupported transport type '{transport_type}' for MCP server '{server_id}'."
        )

    # ─────────────────────────────────────────────────────────────
    # Client lifecycle
    # ─────────────────────────────────────────────────────────────

    async def get_or_create_client(
        self,
        server_id: str,
        manifest: Dict[str, Any],
        workspace_root: Optional[str] = None,
        env_override: Optional[Dict[str, str]] = None,
    ) -> MCPClient:
        """
        Retrieve an active MCPClient instance or initialize a new one thread-safely.

        ``env_override`` (per-chat credential isolation):

          * When provided, it fully replaces the manifest's global env
            (see build_transport for the exact contract).
          * The full override — including meta-keys — participates in
            the session-key hash, so a token refresh (which mutates
            ``_oauth_access_token``) produces a fresh client rather
            than reusing a session bound to the expired token.

        Concurrency model:
          1. Acquire the per-session lock (serialises lifecycle work for a key).
          2. Under the global lock, refuse if the key is being closed —
             this is the guard that prevents a close_session racing with
             a get_or_create_client from leaking a subprocess.
          3. Reuse healthy cached clients; re-create dead stdio processes.
          4. Before storing the new client, re-check the closing set under
             the global lock.
        """
        creds_hash = _compute_creds_hash(env_override)
        session_key = self._get_session_key(server_id, workspace_root, creds_hash)

        async with self._global_lock:
            if session_key not in self._locks:
                self._locks[session_key] = asyncio.Lock()
            session_lock = self._locks[session_key]

        async with session_lock:
            # Guard against concurrent close_session / close_server_sessions.
            async with self._global_lock:
                if session_key in self._closing:
                    raise RuntimeError(
                        f"MCP session '{session_key}' was closed concurrently; "
                        f"retry the operation."
                    )

            existing_client = self._clients.get(session_key)
            if existing_client is not None:
                # Validate if stdio subprocess is still healthy.
                if isinstance(existing_client.transport, StdioTransport):
                    proc = existing_client.transport._process
                    if proc is not None and proc.returncode is None:
                        return existing_client
                    logger.warning(
                        "MCP subprocess for '%s' terminated. Re-initializing...",
                        server_id,
                    )
                    await self._safe_close_client(existing_client)
                else:
                    return existing_client

            transport, fallback_url, effective_auth_token = self.build_transport(
                manifest, workspace_root, env_override
            )
            # A per-chat OAuth token supersedes the manifest's static
            # auth_token. Falling back to manifest.auth_token preserves
            # the historical behaviour for HTTP servers that were
            # pre-configured with a baked-in token.
            auth_token = effective_auth_token or manifest.get("auth_token")

            # ── mTLS config resolution ─────────────────────────────
            # Only relevant for HTTP/SSE transports (fallback_url is
            # non-empty). stdio servers have no TLS layer, so any
            # ``mtls`` block is silently ignored for them.
            #
            # Credentials come from env vars: the manifest declares
            # which env var holds each path, and the env var itself
            # carries the filesystem path. This lets an operator
            # rotate a cert by updating an env var without editing
            # the manifest.
            #
            # When ``env_override`` is provided, it FULLY replaces
            # the global env (per-chat isolation contract), so we do
            # NOT merge — a chat that chose "custom" must not
            # silently inherit the global cert path.
            mtls_cert: Optional[Any] = None
            mtls_ca: Optional[str] = None
            mtls_block = manifest.get("mtls")
            if (
                isinstance(mtls_block, dict)
                and isinstance(fallback_url, str)
                and fallback_url
            ):
                try:
                    if env_override is not None:
                        mtls_env = {
                            str(k): str(v) for k, v in env_override.items()
                        }
                    else:
                        mtls_env = {
                            str(k): str(v)
                            for k, v in (
                                mcp_settings_manager.get_effective_runtime_config(
                                    server_id, manifest
                                ).get("env", {})
                            ).items()
                        }

                    cert_path = mtls_env.get(
                        str(mtls_block.get("cert_env") or ""), ""
                    )
                    key_path = mtls_env.get(
                        str(mtls_block.get("key_env") or ""), ""
                    )
                    ca_path = mtls_env.get(
                        str(mtls_block.get("ca_env") or ""), ""
                    )

                    if cert_path:
                        mtls_cert = (
                            (cert_path, key_path) if key_path else cert_path
                        )
                    if ca_path:
                        mtls_ca = ca_path
                except Exception as _mtls_err:  # noqa: BLE001
                    logger.warning(
                        "mTLS config resolution failed for '%s': %s",
                        server_id,
                        _mtls_err,
                    )

            client = MCPClient(
                server_url=fallback_url,
                auth_token=auth_token,
                timeout=float(manifest.get("timeout", 30.0)),
                transport=transport,
                client_cert=mtls_cert,
                ca_bundle=mtls_ca,
            )

            # Eagerly connect transport if applicable.
            if transport is not None:
                await transport.aconnect()

            # Store under global lock and re-verify the closing guard.
            async with self._global_lock:
                if session_key in self._closing:
                    # Someone called close_* while we were connecting.
                    # Tear down what we just created and refuse to hand it out.
                    await self._safe_close_client(client)
                    raise RuntimeError(
                        f"MCP session '{session_key}' was closed concurrently; "
                        f"retry the operation."
                    )
                self._clients[session_key] = client

            logger.info(
                "Initialized active MCPClient session for '%s' (key: %s)",
                server_id,
                session_key,
            )
            return client

    # ─────────────────────────────────────────────────────────────
    # Tool discovery (cached)
    # ─────────────────────────────────────────────────────────────

    async def get_tools(
        self,
        server_id: str,
        manifest: Dict[str, Any],
        workspace_root: Optional[str] = None,
        *,
        force_refresh: bool = False,
        env_override: Optional[Dict[str, str]] = None,
    ) -> List[Dict[str, Any]]:
        """
        Return the discovered tool list for a server.

        Layered cache strategy:

          1. Subsystem kill-switch check — returns [] immediately when
             `enabled=False` in MCP subsystem settings.
          2. Persistent cache (survives backend restarts) — fast path,
             no disk I/O beyond reading one small JSON file.
          3. In-memory per-credential cache — fast path within a single
             process lifecycle (short TTL, ~5 minutes).
          4. Live discovery — spawns the subprocess, calls tools/list,
             merges result into the persistent cache (preserving
             per-tool `user_enabled` decisions), and mirrors the merged
             list into the in-memory cache.

        The returned list includes a `user_enabled` boolean per tool.
        The caller (peldrun_engine) uses it to decide whether to
        advertise the tool to the LLM. Disabled tools are still
        returned so the "Manage Tools" UI can display the full set.

        Set ``force_refresh=True`` to bypass both caches and re-probe
        the server (used by the "Refresh Tools" button in the UI).

        A failed ``tools/list`` raises. Failed probes are never cached;
        only successful discoveries update the persistent cache.
        """
        # Lazy imports keep mcp_pool importable without dragging the
        # whole settings/cache stack at module load time.
        from omweb.extensions.mcp_subsystem_settings import (
            is_enabled as _subsystem_enabled,
            get_tool_cache_ttl_seconds,
            get_max_tools_per_server,
            is_send_all_by_default,
        )
        from omweb.extensions.mcp_tool_cache import (
            compute_config_hash,
            load_cache as _load_persistent_cache,
            is_cache_fresh,
            store_discovered_tools,
        )

        # ── 1. Subsystem kill switch ─────────────────────────────
        if not _subsystem_enabled():
            logger.debug(
                "MCP subsystem is disabled; returning [] for '%s'.",
                server_id,
            )
            return []

        config_hash = compute_config_hash(manifest)
        ttl_seconds = get_tool_cache_ttl_seconds()

        # ── 2. Persistent cache fast path ────────────────────────
        if not force_refresh:
            persistent = _load_persistent_cache(server_id)
            if is_cache_fresh(persistent, config_hash, ttl_seconds):
                age = time.time() - float(persistent.get("fetched_at", 0))
                logger.debug(
                    "Persistent cache HIT for '%s' (age: %.1fs, tools: %d).",
                    server_id,
                    age,
                    len(persistent.get("tools", [])),
                )
                return list(persistent["tools"])

        # ── 3. In-memory cache fast path ─────────────────────────
        creds_hash = _compute_creds_hash(env_override)
        cache_key = f"{server_id}:{creds_hash}" if creds_hash else server_id

        if not force_refresh:
            cached = self._tool_cache.get(cache_key)
            if cached and (time.monotonic() - cached["fetched_at"]) < self._cache_ttl:
                return list(cached["tools"])

        # ── 4. Live discovery ────────────────────────────────────
        probe_key = f"tool_probe:{server_id}"
        async with self._global_lock:
            if probe_key not in self._locks:
                self._locks[probe_key] = asyncio.Lock()
            probe_lock = self._locks[probe_key]

        async with probe_lock:
            # Double-check both caches under the lock so concurrent
            # callers collapse into a single discovery.
            if not force_refresh:
                persistent = _load_persistent_cache(server_id)
                if is_cache_fresh(persistent, config_hash, ttl_seconds):
                    return list(persistent["tools"])

                cached = self._tool_cache.get(cache_key)
                if cached and (time.monotonic() - cached["fetched_at"]) < self._cache_ttl:
                    return list(cached["tools"])

            client = await self.get_or_create_client(
                server_id=server_id,
                manifest=manifest,
                workspace_root=workspace_root,
                env_override=env_override,
            )

            # Direct JSON-RPC call so failures surface to the caller.
            result = await client._send_jsonrpc_request("tools/list")
            raw_tools = result.get("tools", []) if isinstance(result, dict) else []

            # Cap at max_tools_per_server to protect against a
            # malformed server that returns thousands of entries.
            max_tools = get_max_tools_per_server()
            discovered: List[Dict[str, Any]] = []
            for td in raw_tools:
                if not isinstance(td, dict):
                    continue
                name = td.get("name")
                if not name:
                    continue
                raw_input_schema = td.get("inputSchema") or {
                    "type": "object",
                    "properties": {},
                    "required": [],
                }

                # MCP 2025-11-25 §7.2: tools may declare an
                # outputSchema describing the shape of their
                # structuredContent result. Capture it verbatim
                # (sanitized) so the UI and the agent can reason
                # about structured outputs. `_sanitize_tool_input_schema`
                # is safe to run on output schemas as well — it only
                # strips $ref/combinatorics that break LLM providers,
                # which applies equally to any schema we display.
                raw_output_schema = td.get("outputSchema")
                output_schema: Optional[Dict[str, Any]] = None
                if isinstance(raw_output_schema, dict):
                    try:
                        output_schema = _sanitize_tool_input_schema(
                            raw_output_schema
                        )
                    except Exception as _os_err:  # noqa: BLE001
                        logger.debug(
                            "outputSchema sanitization failed for "
                            "'%s::%s': %s",
                            server_id,
                            name,
                            _os_err,
                        )
                        output_schema = None

                entry: Dict[str, Any] = {
                    "name": str(name),
                    "description": str(td.get("description") or ""),
                    # Sanitize the JSON schema so generic LLM providers
                    # (LM Studio, llama.cpp) can process it. Without
                    # this, one $ref-using tool (e.g. Notion's
                    # query_database) rejects the ENTIRE tool list at
                    # the provider level.
                    "parameters": _sanitize_tool_input_schema(
                        raw_input_schema
                    ),
                }
                if output_schema is not None:
                    entry["output_schema"] = output_schema
                discovered.append(entry)
                if len(discovered) >= max_tools:
                    logger.warning(
                        "Server '%s' returned more tools than max_tools_per_server=%d; capping.",
                        server_id,
                        max_tools,
                    )
                    break

            # Merge into persistent cache (preserves per-tool
            # user_enabled decisions; new tools default per
            # send_all_tools_by_default).
            default_enabled = is_send_all_by_default()
            merged = store_discovered_tools(
                server_id,
                config_hash,
                discovered,
                default_enabled,
            )

            # Mirror into in-memory cache so subsequent calls in the
            # same process avoid touching the disk.
            self._tool_cache[cache_key] = {
                "tools": merged,
                "fetched_at": time.monotonic(),
            }

            logger.info(
                "Discovered %d tool(s) for '%s' (enabled: %d, disabled: %d).",
                len(merged),
                server_id,
                sum(1 for t in merged if t.get("user_enabled")),
                sum(1 for t in merged if not t.get("user_enabled")),
            )

            # Lazy-start behavior: when enabled, close the discovery
            # subprocess so it does not sit idle. The next tool call
            # will spawn fresh. Best-effort — a failure to close does
            # not affect the returned tool list.
            try:
                from omweb.extensions.mcp_subsystem_settings import (
                    is_lazy_start_enabled as _is_lazy,
                )

                if _is_lazy():
                    await self.close_session(server_id, workspace_root)
                    logger.debug(
                        "Lazy start: closed discovery session for '%s'.",
                        server_id,
                    )
            except Exception as lazy_err:  # noqa: BLE001
                logger.debug(
                    "Lazy-start cleanup notice for '%s': %s",
                    server_id,
                    lazy_err,
                )

            return list(merged)

    # ─────────────────────────────────────────────────────────────
    # Resources & Prompts (MCP 2025-11-25 §5, §6)
    # ─────────────────────────────────────────────────────────────

    async def get_resources(
        self,
        server_id: str,
        manifest: Dict[str, Any],
        workspace_root: Optional[str] = None,
        *,
        force_refresh: bool = False,
        env_override: Optional[Dict[str, str]] = None,
    ) -> List[Dict[str, Any]]:
        """
        Return the resources exposed by a server.

        Resources (MCP 2025-11-25 §5) are addressable read-only
        data the agent can fetch by URI. Unlike tools they are not
        invoked — you read a specific URI.

        Caching mirrors ``get_tools``: persistent cache file first,
        then live discovery. Failed discovery is NEVER cached.

        Returns an empty list when the server does not support
        resources (the common case for tool-only servers) so the
        caller does not need to special-case the "no resources"
        path.
        """
        from omweb.extensions.mcp_subsystem_settings import (
            is_enabled as _subsystem_enabled,
            get_tool_cache_ttl_seconds,
        )
        from omweb.extensions.mcp_tool_cache import (
            compute_config_hash,
            load_cache as _load_persistent_cache,
            is_cache_fresh,
            get_resources as _get_cached_resources,
            store_discovered_resources,
        )

        if not _subsystem_enabled():
            logger.debug(
                "MCP subsystem is disabled; returning [] for resources "
                "of '%s'.",
                server_id,
            )
            return []

        config_hash = compute_config_hash(manifest)

        # Persistent cache fast path.
        if not force_refresh:
            persistent = _load_persistent_cache(server_id)
            if is_cache_fresh(
                persistent,
                config_hash,
                get_tool_cache_ttl_seconds(),
            ):
                logger.debug(
                    "Persistent cache HIT for resources of '%s'.",
                    server_id,
                )
                return _get_cached_resources(server_id)

        # Live discovery.
        client = await self.get_or_create_client(
            server_id=server_id,
            manifest=manifest,
            workspace_root=workspace_root,
            env_override=env_override,
        )
        resources = await client.list_resources()
        store_discovered_resources(server_id, config_hash, resources)
        logger.info(
            "Discovered %d resource(s) for '%s'.",
            len(resources),
            server_id,
        )
        return resources

    async def get_prompts(
        self,
        server_id: str,
        manifest: Dict[str, Any],
        workspace_root: Optional[str] = None,
        *,
        force_refresh: bool = False,
        env_override: Optional[Dict[str, str]] = None,
    ) -> List[Dict[str, Any]]:
        """
        Return the prompts exposed by a server.

        Prompts (MCP 2025-11-25 §6) are parameterized templates
        that produce a sequence of messages. They are not invoked
        by the LLM directly — they appear in the UI as
        starting-point choices for the user.

        Caching, fail-soft semantics, and the empty-list-on-
        unsupported contract all mirror ``get_resources``
        deliberately: the two primitives are always read together
        by the Store UI and must not diverge in behaviour.
        """
        from omweb.extensions.mcp_subsystem_settings import (
            is_enabled as _subsystem_enabled,
            get_tool_cache_ttl_seconds,
        )
        from omweb.extensions.mcp_tool_cache import (
            compute_config_hash,
            load_cache as _load_persistent_cache,
            is_cache_fresh,
            get_prompts as _get_cached_prompts,
            store_discovered_prompts,
        )

        if not _subsystem_enabled():
            logger.debug(
                "MCP subsystem is disabled; returning [] for prompts "
                "of '%s'.",
                server_id,
            )
            return []

        config_hash = compute_config_hash(manifest)

        if not force_refresh:
            persistent = _load_persistent_cache(server_id)
            if is_cache_fresh(
                persistent,
                config_hash,
                get_tool_cache_ttl_seconds(),
            ):
                logger.debug(
                    "Persistent cache HIT for prompts of '%s'.",
                    server_id,
                )
                return _get_cached_prompts(server_id)

        client = await self.get_or_create_client(
            server_id=server_id,
            manifest=manifest,
            workspace_root=workspace_root,
            env_override=env_override,
        )
        prompts = await client.list_prompts()
        store_discovered_prompts(server_id, config_hash, prompts)
        logger.info(
            "Discovered %d prompt(s) for '%s'.",
            len(prompts),
            server_id,
        )
        return prompts

    async def invalidate_cache(self, server_id: str) -> None:
        """
        Drop every cached tool list for the given server, in BOTH the
        in-memory cache and the persistent on-disk cache.

        Used when the server's configuration changes in a way that
        could invalidate every cred-scoped entry at once (bundle
        reinstall, settings update, status toggle).
        """
        # 1. Drop the persistent cache file (best-effort).
        try:
            from omweb.extensions.mcp_tool_cache import (
                invalidate as _invalidate_persistent,
            )

            _invalidate_persistent(server_id)
        except Exception as persist_err:  # noqa: BLE001
            logger.debug(
                "Failed to invalidate persistent cache for '%s': %s",
                server_id,
                persist_err,
            )

        # 2. Drop every in-memory cred-scoped variant.
        prefix = f"{server_id}:"
        async with self._global_lock:
            keys_to_drop = [
                k for k in list(self._tool_cache.keys())
                if k == server_id or k.startswith(prefix)
            ]
            for k in keys_to_drop:
                self._tool_cache.pop(k, None)

    def invalidate_cache_sync(self, server_id: str) -> None:
        """
        Synchronous cache invalidation for callers outside the async loop.

        Used by ``McpBundleManager.install_bundle`` (synchronous by
        contract) to evict every cred-scoped variant of a stale tool
        list when a bundle is being re-uploaded under the same id.

        Invalidates BOTH the persistent on-disk cache and the
        in-memory cache. The underlying dict mutations are atomic under
        the GIL, so no lock is required here — and acquiring the async
        lock from a sync context is impossible without an event loop.
        """
        # 1. Persistent cache (best-effort).
        try:
            from omweb.extensions.mcp_tool_cache import (
                invalidate as _invalidate_persistent,
            )

            _invalidate_persistent(server_id)
        except Exception as persist_err:  # noqa: BLE001
            logger.debug(
                "Failed to invalidate persistent cache for '%s': %s",
                server_id,
                persist_err,
            )

        # 2. In-memory cred-scoped variants.
        prefix = f"{server_id}:"
        keys_to_drop = [
            k for k in list(self._tool_cache.keys())
            if k == server_id or k.startswith(prefix)
        ]
        for k in keys_to_drop:
            self._tool_cache.pop(k, None)

    def get_cached_tools(
        self, server_id: str
    ) -> Optional[List[Dict[str, Any]]]:
        """
        Return the cached tool list for a server without spawning a
        subprocess.

        Layered lookup:
          1. In-memory entry for the bare server id.
          2. Any fresh cred-scoped in-memory variant.
          3. Persistent on-disk cache (used by the delegation resolver,
             which is synchronous and cannot `await` a live probe).

        Returns ``None`` when no cache (memory or disk) is available,
        so the caller can fall back gracefully.
        """
        # 1. In-memory (default key).
        cached = self._tool_cache.get(server_id)
        if cached and (time.monotonic() - cached["fetched_at"]) < self._cache_ttl:
            return list(cached["tools"])

        # 2. Any fresh cred-scoped in-memory variant.
        prefix = f"{server_id}:"
        for key, entry in self._tool_cache.items():
            if key.startswith(prefix) and (
                time.monotonic() - entry["fetched_at"]
            ) < self._cache_ttl:
                return list(entry["tools"])

        # 3. Persistent on-disk fallback (survives restarts).
        try:
            from omweb.extensions.mcp_tool_cache import (
                load_cache as _load_persistent_cache,
            )

            persistent = _load_persistent_cache(server_id)
            if persistent and isinstance(persistent.get("tools"), list):
                return list(persistent["tools"])
        except Exception as persist_err:  # noqa: BLE001
            logger.debug(
                "Persistent cache read failed for '%s': %s",
                server_id,
                persist_err,
            )

        return None

    # ─────────────────────────────────────────────────────────────
    # Probe (strict one-shot diagnostic)
    # ─────────────────────────────────────────────────────────────

    async def probe_connection(
        self,
        manifest: Dict[str, Any],
        workspace_root: Optional[str] = None,
        env_override: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        """
        Perform an isolated one-shot connectivity check and discover exposed tools.

        This is a STRICT diagnostic: any failure (spawn error, RPC error,
        timeout) surfaces as {"status": "error"}. Previous behaviour would
        mask a failed tools/list as {"status": "ok", "tools": []} because
        MCPClient.discover_tools swallows exceptions internally.

        Guarantees prompt cleanup of any spawned subprocess.
        """
        server_id = manifest.get("id", "probe_test")
        client: Optional[MCPClient] = None
        try:
            transport, fallback_url, effective_auth_token = self.build_transport(
                manifest, workspace_root, env_override
            )

            # ── mTLS config resolution (mirrors get_or_create_client) ──
            # Differs from get_or_create_client in one respect: for
            # probe_connection, ``env_override`` is a "test override"
            # (typically from the Test Connection button with custom
            # credentials), so we MERGE it on top of the global env
            # rather than replacing. This matches the mental model
            # "try with these credentials; fall back to global for
            # anything I didn't supply" — which is exactly what a
            # user testing a freshly-pasted cert expects.
            _mtls_cert: Optional[Any] = None
            _mtls_ca: Optional[str] = None
            _mtls_block = manifest.get("mtls")
            if (
                isinstance(_mtls_block, dict)
                and isinstance(fallback_url, str)
                and fallback_url
            ):
                try:
                    _mtls_env = {
                        str(k): str(v)
                        for k, v in (
                            mcp_settings_manager.get_effective_runtime_config(
                                server_id, manifest
                            ).get("env", {})
                        ).items()
                    }
                    if env_override:
                        _mtls_env.update(
                            {str(k): str(v) for k, v in env_override.items()}
                        )

                    _cp = _mtls_env.get(
                        str(_mtls_block.get("cert_env") or ""), ""
                    )
                    _kp = _mtls_env.get(
                        str(_mtls_block.get("key_env") or ""), ""
                    )
                    _cap = _mtls_env.get(
                        str(_mtls_block.get("ca_env") or ""), ""
                    )
                    if _cp:
                        _mtls_cert = (_cp, _kp) if _kp else _cp
                    if _cap:
                        _mtls_ca = _cap
                except Exception as _mtls_err:  # noqa: BLE001
                    logger.warning(
                        "mTLS config resolution failed for probe '%s': %s",
                        server_id,
                        _mtls_err,
                    )

            client = MCPClient(
                server_url=fallback_url,
                auth_token=effective_auth_token or manifest.get("auth_token"),
                timeout=float(manifest.get("timeout", 15.0)),
                transport=transport,
                client_cert=_mtls_cert,
                ca_bundle=_mtls_ca,
            )

            if transport is not None:
                await transport.aconnect()

            result = await client._send_jsonrpc_request("tools/list")
            raw_tools = result.get("tools", []) if isinstance(result, dict) else []

            tool_summaries: List[Dict[str, Any]] = []
            for td in raw_tools:
                if not isinstance(td, dict):
                    continue
                name = td.get("name")
                if not name:
                    continue
                raw_input_schema = td.get("inputSchema") or {
                    "type": "object",
                    "properties": {},
                    "required": [],
                }

                # Capture outputSchema (MCP 2025-11-25 §7.2) so the
                # probe result carries the same shape as the cached
                # tool list. Without this, "Test Connection" would
                # show the input schema but hide the output schema,
                # and the user would have no way to know a tool
                # returns structured data — a mismatch with the
                # Manage Tools view that reads from the cache.
                raw_output_schema = td.get("outputSchema")
                output_schema: Optional[Dict[str, Any]] = None
                if isinstance(raw_output_schema, dict):
                    try:
                        output_schema = _sanitize_tool_input_schema(
                            raw_output_schema
                        )
                    except Exception:  # noqa: BLE001
                        # Sanitization is best-effort; a broken output
                        # schema must not abort the probe. We simply
                        # omit the field so the tool remains callable.
                        output_schema = None

                summary: Dict[str, Any] = {
                    "name": str(name),
                    "description": str(td.get("description") or ""),
                    "parameters": _sanitize_tool_input_schema(
                        raw_input_schema
                    ),
                }
                if output_schema is not None:
                    summary["output_schema"] = output_schema
                tool_summaries.append(summary)

            return {
                "status": "ok",
                "message": (
                    f"Successfully connected to MCP server. "
                    f"Discovered {len(tool_summaries)} tool(s)."
                ),
                "tools": tool_summaries,
            }
        except Exception as exc:
            logger.warning("MCP connection probe failed for '%s': %s", server_id, exc)
            return {
                "status": "error",
                "message": f"Connection failed: {str(exc)}",
                "tools": [],
            }
        finally:
            if client is not None:
                await self._safe_close_client(client)

    # ─────────────────────────────────────────────────────────────
    # Session teardown
    # ─────────────────────────────────────────────────────────────

    async def _safe_close_client(self, client: MCPClient) -> None:
        """Safely close client transport and ignore benign teardown exceptions."""
        try:
            await client.close()
        except Exception as exc:
            logger.debug("Error while closing MCPClient session: %s", exc)

    async def _close_session_by_key(self, session_key: str) -> None:
        """
        Internal teardown routine shared by close_session /
        close_server_sessions / close_all.

        Marks the key as closing BEFORE popping the client so that any
        concurrent get_or_create_client bails out instead of creating a
        second client for the same key.

        Hardened with a timeout: a stuck transport (e.g. a subprocess
        that ignores SIGTERM, or an HTTP connection that hangs during
        close) must not hold `session_key` in `_closing` forever —
        that would make every subsequent get_or_create_client for the
        same key raise "closed concurrently", effectively bricking the
        server until restart.
        """
        async with self._global_lock:
            self._closing.add(session_key)
            client = self._clients.pop(session_key, None)

        try:
            if client is not None:
                # Cap the close duration. 5s is more than enough for
                # any well-behaved transport to finish; beyond that,
                # assume the transport is wedged and move on. The
                # underlying socket/file descriptor will be closed by
                # the OS when the process exits — but in the meantime
                # the session pool stays usable for new requests.
                try:
                    await asyncio.wait_for(
                        self._safe_close_client(client),
                        timeout=5.0,
                    )
                    logger.info(
                        "Closed active MCP session for key '%s'",
                        session_key,
                    )
                except asyncio.TimeoutError:
                    logger.warning(
                        "MCP session close timed out after 5s for "
                        "key '%s'. Forcing detachment.",
                        session_key,
                    )
        finally:
            async with self._global_lock:
                self._closing.discard(session_key)
                self._locks.pop(session_key, None)

    async def close_session(
        self,
        server_id: str,
        workspace_root: Optional[str] = None,
    ) -> None:
        """Terminate and remove a specific active MCP session."""
        session_key = self._get_session_key(server_id, workspace_root)
        await self._close_session_by_key(session_key)

    async def close_server_sessions(self, server_id: str) -> None:
        """
        Terminate every active session belonging to a server, across every
        bound workspace_root. Used by delete_bundle / delete_extension /
        toggle / settings-update paths so that stale per-workspace
        subprocesses never survive a configuration change.
        """
        prefix = self._server_prefix(server_id)

        async with self._global_lock:
            session_keys = [
                k for k in list(self._clients.keys()) if k.startswith(prefix)
            ]

        # Also invalidate any cached tool list so the next call re-probes.
        await self.invalidate_cache(server_id)

        for key in session_keys:
            await self._close_session_by_key(key)

        # Drop any leftover probe lock for the server.
        async with self._global_lock:
            self._locks.pop(f"tool_probe:{server_id}", None)

        if session_keys:
            logger.info(
                "Closed %d session(s) for server '%s' across all workspaces.",
                len(session_keys),
                server_id,
            )

    async def close_all(self) -> None:
        """
        Terminate all active MCP sessions across the platform.

        Called from the FastAPI lifespan shutdown hook. Marking every key
        as closing before popping ensures no concurrent caller can sneak
        in a fresh session while we tear things down.
        """
        async with self._global_lock:
            session_keys = list(self._clients.keys())
            clients_to_close = list(self._clients.values())
            self._closing.update(session_keys)
            self._clients.clear()
            self._locks.clear()
            self._tool_cache.clear()

        if clients_to_close:
            async def _bounded_close(c: MCPClient) -> None:
                try:
                    await asyncio.wait_for(
                        self._safe_close_client(c),
                        timeout=5.0,
                    )
                except asyncio.TimeoutError:
                    logger.warning(
                        "MCP session close timed out after 5s during "
                        "close_all(); forcing detachment."
                    )

            # Fire all closes in parallel. Even if every session hangs
            # at the 5s cap, the total wall time is still 5s (not
            # N × 5s), because they are concurrent.
            await asyncio.gather(
                *(_bounded_close(c) for c in clients_to_close),
                return_exceptions=True,
            )

        async with self._global_lock:
            self._closing.clear()

        logger.info(
            "Closed %d active MCP pool session(s) during shutdown.",
            len(clients_to_close),
        )


mcp_session_pool = McpSessionPool()

__all__ = [
    "McpSessionPool",
    "mcp_session_pool",
    "DEFAULT_CACHE_TTL_SECONDS",
]
