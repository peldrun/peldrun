# backend/omweb/routers/mcp_auth.py
"""
MCP OAuth 2.1 and Per-Chat Credential Router.

Provides the HTTP surface for the per-chat MCP authentication flow:

  OAuth 2.1 flow:
    POST /api/chats/{chat_id}/mcp-auth/{server_id}/start
    GET  /api/mcp/oauth/callback
    GET  /api/mcp/oauth/client-metadata.json   (CIMD document)

  Credential mode management:
    POST   /api/chats/{chat_id}/mcp-auth/{server_id}/token
    POST   /api/chats/{chat_id}/mcp-auth/{server_id}/env          (alias)
    POST   /api/chats/{chat_id}/mcp-auth/{server_id}/use-global
    POST   /api/chats/{chat_id}/mcp-auth/{server_id}/ephemeral
    DELETE /api/chats/{chat_id}/mcp-auth/{server_id}
    GET    /api/chats/{chat_id}/mcp-auth

Design notes:

  * Paths are ABSOLUTE (they start with "/api/..."). This router must
    be mounted WITHOUT a prefix in main.py, in the same style as the
    source_control and resource_monitor routers. The endpoints span
    two different path roots (/api/chats and /api/mcp), so a single
    explicit router is cleaner than splitting into two.

  * The OAuth callback completes in a popup window. It sends a
    postMessage to the opener (the chat UI) and then closes itself.
    targetOrigin is window.location.origin — both windows are served
    by the same PELDRUN instance, so the same-origin check is
    sufficient and no CORS postMessage dance is required.

  * The CIMD document (GET /api/mcp/oauth/client-metadata.json) is
    built per-request from resolve_public_base_url(request). No port
    is hardcoded, and the client_id is always the exact URL this
    document is served from — the property CIMD verification relies
    on.

  * redirect_uri is stored verbatim in oauth_state_store at flow
    start and replayed verbatim at token exchange. This prevents
    subtle mismatches when the request arrives via a different
    path (proxy, load balancer, etc.) than the start call did.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Literal, Optional

from fastapi import APIRouter, Body, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field

from omweb.extensions.auth_manager import (
    OAuthManager,
    PKCEPair,
    generate_pkce,
    oauth_manager,
    oauth_state_store,
    resolve_client_metadata_url,
    resolve_public_base_url,
)
from omweb.extensions.chat_mcp_auth import (
    MODE_CUSTOM,
    MODE_EPHEMERAL,
    MODE_USE_GLOBAL,
    get_chat_mcp_auth_store,
)
from omweb.extensions.registry import extension_registry

logger = logging.getLogger("omweb.routers.mcp_auth")

router = APIRouter()


# ─────────────────────────────────────────────────────────────────
# Pydantic request models
# ─────────────────────────────────────────────────────────────────

class SaveEnvPayload(BaseModel):
    """Payload for the /token and /env endpoints."""
    mode: Literal["custom"] = "custom"
    env: Dict[str, str] = Field(default_factory=dict)


class SaveEphemeralPayload(BaseModel):
    """Payload for the /ephemeral endpoint."""
    env: Dict[str, str] = Field(default_factory=dict)


class StartOAuthResponse(BaseModel):
    auth_url: str
    state: str
    server_id: str
    chat_id: str


# ─────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────

def _is_oauth_capable(manifest: Dict[str, Any]) -> bool:
    """
    An MCP server is OAuth-capable iff it is reachable over HTTP and
    declares a URL. stdio servers cannot participate in OAuth — the
    token has nowhere to go.
    """
    transport = str(manifest.get("transport") or "").lower()
    url = manifest.get("url")
    if not isinstance(url, str) or not url:
        return False
    return transport in ("sse", "http", "streamable-http", "")


def _load_manifest_or_404(server_id: str) -> Dict[str, Any]:
    manifest = extension_registry.get_effective_server(server_id)
    if not manifest:
        raise HTTPException(
            status_code=404,
            detail=f"MCP server '{server_id}' not found.",
        )
    return manifest


def _sanitize_return_to(value: Optional[str]) -> Optional[str]:
    """
    Return a same-origin path for the frontend to redirect back to.

    Rejects absolute URLs, protocol-relative URLs, and anything that
    looks like an external redirect. Only paths starting with a single
    "/" are allowed.
    """
    if not value:
        return None
    s = value.strip()
    if not s.startswith("/"):
        return None
    if s.startswith("//"):
        return None
    return s


def _postmessage_origin(request: Request) -> str:
    """
    The origin the popup was opened from.

    Both the popup and its opener are served by the same PELDRUN
    instance, so window.location.origin in the injected JS matches
    the base URL resolved here. Using an explicit origin in the
    postMessage call is a hard requirement — never "*".
    """
    try:
        base = str(getattr(request, "base_url", "")).rstrip("/")
        if base:
            # base_url includes the scheme and host[:port] — exactly
            # the origin value window.location.origin would produce.
            return base
    except Exception:  # noqa: BLE001
        pass
    try:
        return resolve_public_base_url(request)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "Could not resolve origin for postMessage; the popup will "
            "fall back to same-origin postMessage. (%s)",
            exc,
        )
        return ""


def _close_window_html(
    *,
    request: Request,
    chat_id: Optional[str],
    server_id: Optional[str],
    success: bool,
    error_code: Optional[str] = None,
    error_message: Optional[str] = None,
) -> str:
    """
    Return a self-contained HTML page that:
      1. Postmessages a structured result to window.opener.
      2. Closes the popup window after a brief delay.
      3. Renders a human-readable message for browsers that block
         window.close() (some browsers require the window to have
         been opened by script — which is always the case here, but
         defense in depth).

    The HTML intentionally contains no external resources, no external
    stylesheets, no fonts, and no scripts loaded from CDNs. It must
    render identically with the network offline.
    """
    origin = _postmessage_origin(request)
    payload = {
        "source": "peldrun-mcp-oauth",
        "status": "success" if success else "error",
        "chat_id": chat_id,
        "server_id": server_id,
    }
    if error_code:
        payload["error_code"] = error_code
    if error_message:
        payload["error_message"] = error_message

    payload_json = json.dumps(payload)
    # Both escaped forms of the payload are used: JS string literal
    # inside the script block, and HTML-escaped text for the visible
    # fallback message.
    import html as html_lib  # stdlib only

    title = "PELDRUN MCP Authentication"
    headline = "Authentication Successful" if success else "Authentication Failed"
    body_text = (
        "You can close this window and return to PELDRUN."
        if success
        else (error_message or "Please close this window and try again.")
    )
    accent = "#10b981" if success else "#dc2626"

    # Note: script only runs postMessage when window.opener exists.
    # window.close() is attempted after the message is sent; if the
    # browser refuses (rare in script-opened windows), the page stays
    # open with the success/failure message visible.
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>{html_lib.escape(title)}</title>
<style>
  html,body{{margin:0;padding:0;background:#0f0f10;color:#e5e7eb;
    font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;}}
  .wrap{{min-height:100vh;display:flex;align-items:center;justify-content:center;padding:24px;}}
  .card{{background:#18181b;border:1px solid #27272a;border-radius:12px;
    padding:32px 28px;max-width:420px;text-align:center;}}
  .dot{{width:44px;height:44px;border-radius:50%;background:{accent}22;
    border:2px solid {accent};display:flex;align-items:center;
    justify-content:center;margin:0 auto 18px;font-size:22px;color:{accent};}}
  h1{{font-size:16px;margin:0 0 10px;font-weight:600;}}
  p{{font-size:13px;line-height:1.6;color:#a1a1aa;margin:0;}}
  .hint{{margin-top:18px;font-size:11px;color:#52525b;}}
</style>
</head>
<body>
<div class="wrap">
  <div class="card">
    <div class="dot">{'✓' if success else '✕'}</div>
    <h1>{html_lib.escape(headline)}</h1>
    <p>{html_lib.escape(body_text)}</p>
    <div class="hint">This window closes automatically.</div>
  </div>
</div>
<script>
(function () {{
  var payload = {payload_json};
  var targetOrigin = "{origin}";
  try {{
    if (window.opener && !window.opener.closed) {{
      if (targetOrigin) {{
        window.opener.postMessage(payload, targetOrigin);
      }} else {{
        window.opener.postMessage(payload, window.location.origin);
      }}
    }}
  }} catch (e) {{
    /* swallow: postMessage failure must not block window.close() */
  }}
  setTimeout(function () {{
    try {{ window.close(); }} catch (e) {{ /* ignore */ }}
  }}, 150);
}})();
</script>
</body>
</html>"""


# ─────────────────────────────────────────────────────────────────
# OAuth: client metadata document (CIMD)
# ─────────────────────────────────────────────────────────────────

@router.get("/api/mcp/oauth/client-metadata.json")
async def get_client_metadata(request: Request):
    """
    Serve the Client ID Metadata Document for CIMD-capable servers.

    Per the CIMD draft, the document's ``client_id`` field MUST be the
    exact URL the document is served from. This endpoint enforces that
    invariant automatically by deriving every URL from the incoming
    request's base URL.

    Optional fields (logo_uri, client_uri, tos_uri, policy_uri,
    contacts) are intentionally omitted. The CIMD draft treats them as
    optional. Operators who want them can add a static override file
    served by a reverse proxy ahead of PELDRUN.
    """
    base = resolve_public_base_url(request)
    return JSONResponse(
        content={
            "client_id": f"{base}/api/mcp/oauth/client-metadata.json",
            "client_name": "PELDRUN MCP Client",
            "client_uri": base,
            "redirect_uris": [f"{base}/api/mcp/oauth/callback"],
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "none",
        },
        headers={
            # CIMD documents are content-addressed by URL; no caching
            # beyond the current request is desired in development.
            "Cache-Control": "no-store",
        },
    )


# ─────────────────────────────────────────────────────────────────
# OAuth: start flow
# ─────────────────────────────────────────────────────────────────

@router.post(
    "/api/chats/{chat_id}/mcp-auth/{server_id}/start",
    response_model=StartOAuthResponse,
)
async def start_oauth(chat_id: str, server_id: str, request: Request):
    """
    Begin an OAuth 2.1 authorization flow for a (chat, server) pair.

    Returns the authorization URL the frontend must open in a popup,
    plus the state nonce for diagnostic logging. The state nonce is
    also stored server-side; the frontend does NOT need to send it
    back — the popup will carry it in the redirect URI.
    """
    manifest = _load_manifest_or_404(server_id)

    if not _is_oauth_capable(manifest):
        raise HTTPException(
            status_code=400,
            detail=(
                f"MCP server '{server_id}' does not support OAuth: it has "
                "no URL or uses a non-HTTP transport."
            ),
        )

    mcp_server_url = str(manifest["url"]).rstrip("/")

    # 1. Resolve the authorization server metadata (RFC 9728 or
    #    pre-registered fallback).
    try:
        metadata = await oauth_manager.get_metadata(mcp_server_url)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "OAuth metadata discovery failed for '%s': %s", server_id, exc
        )
        raise HTTPException(
            status_code=502,
            detail=(
                f"Failed to discover authorization server for '{server_id}': "
                f"{exc}"
            ),
        )

    # 2. Resolve client registration (pre-reg → CIMD → DCR).
    redirect_uri = f"{resolve_public_base_url(request)}/api/mcp/oauth/callback"
    manifest_metadata_url = manifest.get("client_metadata_url")
    pelrdun_metadata_url = resolve_client_metadata_url(request)

    try:
        registration = await oauth_manager.resolve_client(
            server_id=server_id,
            mcp_server_url=mcp_server_url,
            metadata=metadata,
            redirect_uri=redirect_uri,
            manifest_metadata_url=manifest_metadata_url,
            pelrdun_metadata_url=pelrdun_metadata_url,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "Client registration resolution failed for '%s': %s",
            server_id,
            exc,
        )
        raise HTTPException(
            status_code=502,
            detail=f"Failed to register OAuth client for '{server_id}': {exc}",
        )

    # 3. Generate PKCE (S256) and state nonce.
    pkce = generate_pkce()
    import secrets as _secrets

    state = _secrets.token_urlsafe(32)

    # 4. Choose scopes: manifest override, then AS-advertised defaults.
    scopes: List[str] = list(manifest.get("scopes") or metadata.scopes_supported)

    # 5. Persist transient flow state. The callback will pop it and
    #    complete the exchange. Verbatim redirect_uri is stored so
    #    the token request uses the exact value sent in step 3.
    oauth_state_store.put(
        state,
        {
            "chat_id": chat_id,
            "server_id": server_id,
            "verifier": pkce.verifier,
            "metadata": metadata,
            "client_id": registration.client_id,
            "client_secret": registration.client_secret,
            "redirect_uri": redirect_uri,
            "resource": mcp_server_url,
        },
    )

    # 6. Build and return the authorization URL.
    auth_url = oauth_manager.build_authorization_url(
        metadata=metadata,
        client_id=registration.client_id,
        redirect_uri=redirect_uri,
        pkce=pkce,
        scopes=scopes,
        state=state,
        resource=mcp_server_url,
    )

    logger.info(
        "OAuth flow started (chat=%s server=%s issuer=%s client_id=%s...)",
        chat_id,
        server_id,
        metadata.issuer,
        registration.client_id[:16],
    )

    return StartOAuthResponse(
        auth_url=auth_url,
        state=state,
        chat_id=chat_id,
        server_id=server_id,
    )


# ─────────────────────────────────────────────────────────────────
# OAuth: callback
# ─────────────────────────────────────────────────────────────────

@router.get("/api/mcp/oauth/callback")
async def oauth_callback(
    request: Request,
    code: str = "",
    state: str = "",
    error: str = "",
    error_description: str = "",
):
    """
    OAuth callback. Consumes the state, exchanges the code, persists
    the tokens, and closes the popup with a postMessage.

    Every code path returns an HTML response — the popup must never
    display a raw JSON error to the user. The structured result is
    delivered to the opener via postMessage.
    """
    # 1. Provider error early return.
    if error:
        # Try to pop the state so the entry does not linger.
        ctx = oauth_state_store.pop(state) if state else None
        logger.info(
            "OAuth provider returned error (code=%s): %s",
            error,
            error_description,
        )
        return HTMLResponse(
            _close_window_html(
                request=request,
                chat_id=(ctx or {}).get("chat_id"),
                server_id=(ctx or {}).get("server_id"),
                success=False,
                error_code=error,
                error_message=error_description or error,
            ),
            status_code=200,
        )

    # 2. State validation. Single-use by definition (pop).
    if not state:
        return HTMLResponse(
            _close_window_html(
                request=request,
                chat_id=None,
                server_id=None,
                success=False,
                error_code="missing_state",
                error_message="No state parameter received from the provider.",
            ),
            status_code=200,
        )

    ctx = oauth_state_store.pop(state)
    if not ctx:
        return HTMLResponse(
            _close_window_html(
                request=request,
                chat_id=None,
                server_id=None,
                success=False,
                error_code="invalid_state",
                error_message=(
                    "This OAuth session has expired or was already used. "
                    "Please start the authentication flow again."
                ),
            ),
            status_code=200,
        )

    if not code:
        return HTMLResponse(
            _close_window_html(
                request=request,
                chat_id=ctx["chat_id"],
                server_id=ctx["server_id"],
                success=False,
                error_code="missing_code",
                error_message="The provider did not return an authorization code.",
            ),
            status_code=200,
        )

    # 3. Exchange the code for tokens.
    try:
        pkce = PKCEPair(
            verifier=ctx["verifier"], challenge="", method="S256"
        )
        tokens = await oauth_manager.exchange_code(
            metadata=ctx["metadata"],
            client_id=ctx["client_id"],
            client_secret=ctx.get("client_secret"),
            code=code,
            redirect_uri=ctx["redirect_uri"],
            pkce=pkce,
            resource=ctx["resource"],
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception(
            "Token exchange failed (chat=%s server=%s)",
            ctx.get("chat_id"),
            ctx.get("server_id"),
        )
        return HTMLResponse(
            _close_window_html(
                request=request,
                chat_id=ctx["chat_id"],
                server_id=ctx["server_id"],
                success=False,
                error_code="exchange_failed",
                error_message=str(exc)[:500],
            ),
            status_code=200,
        )

    # 4. Persist the tokens for this (chat, server) pair.
    try:
        store = get_chat_mcp_auth_store()
        await store.save_oauth_tokens(
            chat_id=ctx["chat_id"],
            server_id=ctx["server_id"],
            access_token=tokens["access_token"],
            refresh_token=tokens.get("refresh_token"),
            expires_in=tokens.get("expires_in"),
            token_type=tokens.get("token_type", "Bearer"),
            resource=ctx["resource"],
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception(
            "Failed to persist OAuth tokens (chat=%s server=%s)",
            ctx.get("chat_id"),
            ctx.get("server_id"),
        )
        return HTMLResponse(
            _close_window_html(
                request=request,
                chat_id=ctx["chat_id"],
                server_id=ctx["server_id"],
                success=False,
                error_code="persist_failed",
                error_message=str(exc)[:500],
            ),
            status_code=200,
        )

    logger.info(
        "OAuth flow completed (chat=%s server=%s)",
        ctx["chat_id"],
        ctx["server_id"],
    )

    return HTMLResponse(
        _close_window_html(
            request=request,
            chat_id=ctx["chat_id"],
            server_id=ctx["server_id"],
            success=True,
        ),
        status_code=200,
    )


# ─────────────────────────────────────────────────────────────────
# Credential mode management
# ─────────────────────────────────────────────────────────────────

async def _save_custom_env(
    chat_id: str,
    server_id: str,
    env: Dict[str, str],
) -> Dict[str, Any]:
    """Persist custom env credentials for a (chat, server) pair."""
    _load_manifest_or_404(server_id)
    if not env:
        raise HTTPException(
            status_code=400,
            detail="No environment variables provided.",
        )
    store = get_chat_mcp_auth_store()
    await store.save_custom(chat_id, server_id, env)
    return {"status": "ok", "mode": MODE_CUSTOM}


@router.post("/api/chats/{chat_id}/mcp-auth/{server_id}/token")
async def save_token(chat_id: str, server_id: str, payload: SaveEnvPayload = Body(...)):
    """Persist custom env credentials (persistent, encrypted on disk)."""
    return await _save_custom_env(chat_id, server_id, payload.env)


@router.post("/api/chats/{chat_id}/mcp-auth/{server_id}/env")
async def save_env_alias(chat_id: str, server_id: str, payload: SaveEnvPayload = Body(...)):
    """Alias of /token — keeps the URL semantic explicit."""
    return await _save_custom_env(chat_id, server_id, payload.env)


@router.post("/api/chats/{chat_id}/mcp-auth/{server_id}/use-global")
async def save_use_global(chat_id: str, server_id: str):
    """Record the user's choice to fall back to the global settings."""
    _load_manifest_or_404(server_id)
    store = get_chat_mcp_auth_store()
    await store.save_use_global(chat_id, server_id)
    return {"status": "ok", "mode": MODE_USE_GLOBAL}


@router.post("/api/chats/{chat_id}/mcp-auth/{server_id}/ephemeral")
async def save_ephemeral(chat_id: str, server_id: str, payload: SaveEphemeralPayload = Body(...)):
    """
    Store one-shot credentials in memory only.

    These credentials are:
      * Never written to disk.
      * Dropped on process restart.
      * Superseded by any subsequent persistent save.
    """
    _load_manifest_or_404(server_id)
    if not payload.env:
        raise HTTPException(
            status_code=400,
            detail="No environment variables provided.",
        )
    store = get_chat_mcp_auth_store()
    await store.save_ephemeral(chat_id, server_id, env=payload.env)
    return {"status": "ok", "mode": MODE_EPHEMERAL}


@router.delete("/api/chats/{chat_id}/mcp-auth/{server_id}")
async def delete_credentials(chat_id: str, server_id: str):
    """Remove any persisted or ephemeral credentials for (chat, server)."""
    _load_manifest_or_404(server_id)
    store = get_chat_mcp_auth_store()
    removed = await store.delete(chat_id, server_id)
    return {"status": "ok", "removed": removed}


@router.get("/api/chats/{chat_id}/mcp-auth")
async def list_credentials(chat_id: str):
    """
    List the persisted credential entries for a chat.

    Only reports the presence of env / oauth secrets via boolean
    flags. The actual secret values are never returned by this
    endpoint under any circumstances.
    """
    store = get_chat_mcp_auth_store()
    entries = store.list_entries(chat_id)
    return {
        "status": "ok",
        "chat_id": chat_id,
        "entries": entries,
    }
