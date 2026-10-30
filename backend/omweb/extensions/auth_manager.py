# backend/omweb/extensions/auth_manager.py
"""
OAuth 2.1 engine for MCP server authentication.

Conforms to:
  * MCP Authorization (2026-07-28) — OAuth 2.1 + PKCE S256 mandatory
  * OAuth 2.1 (draft-ietf-oauth-v2-1) — no implicit, no ROPC
  * RFC 9700 (OAuth 2.0 Security BCP) — PKCE for public clients
  * RFC 8252 (Native Apps) — loopback redirect, no client secret required
  * RFC 9728 (Protected Resource Metadata) — discovery from /.well-known
  * RFC 8707 (Resource Indicators) — resource parameter mandatory
  * Client ID Metadata Documents (CIMD) — preferred registration
  * Dynamic Client Registration (DCR) — fallback
  * Pre-registration — for providers with pre-existing relationships

This module is STATELESS with respect to authorization flows: it
provides pure primitives (discovery, registration resolution, URL
building, exchange, refresh). The transient state (state nonce, PKCE
verifier, in-flight metadata) is held by OAuthStateStore, which is
in-memory only and TTL-bounded.

Public base URL resolution does NOT hardcode any port. Precedence:
  1. PELDRUN_BASE_URL env var (explicit override)
  2. FastAPI Request.base_url (per-request, exact as the client sees it)
  3. PELDRUN_PORT / PORT env var
  4. sys.argv --port / --port=N (uvicorn CLI)
  5. RuntimeError — never silently fall back to 127.0.0.1:8000
"""

from __future__ import annotations

import base64
import hashlib
import logging
import os
import secrets
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
from urllib.parse import urlencode, urlparse

import httpx

logger = logging.getLogger("omweb.extensions.auth_manager")


# ─────────────────────────────────────────────────────────────────
# Pre-registered clients
# ─────────────────────────────────────────────────────────────────
#
# These are providers where PELDRUN's relationship is established
# manually (registered OAuth app), so client_id and — when required —
# client_secret come from environment variables. NOTHING sensitive is
# committed to the repository.
#
# The key is a hostname fragment used for substring matching against
# the issuer URL. First match wins.
#
PRE_REGISTERED_CLIENTS: Dict[str, Dict[str, Any]] = {
    "github.com": {
        "client_id_env": "PELDRUN_GITHUB_CLIENT_ID",
        "client_secret_env": "PELDRUN_GITHUB_CLIENT_SECRET",
        "authorization_endpoint": "https://github.com/login/oauth/authorize",
        "token_endpoint": "https://github.com/login/oauth/access_token",
        "scopes": ["repo", "read:user"],
        "pkce": True,
    },
    # WordPress.com is handled via DCR — no pre-registration entry needed.
}


# ─────────────────────────────────────────────────────────────────
# Data containers
# ─────────────────────────────────────────────────────────────────

@dataclass
class PKCEPair:
    """A PKCE verifier / challenge pair (S256 only per OAuth 2.1)."""
    verifier: str
    challenge: str
    method: str = "S256"


@dataclass
class RegistrationResult:
    """
    Outcome of client registration resolution.

    ``client_secret`` is only present for pre-registered providers
    whose token endpoint requires it (e.g. GitHub classic OAuth apps).
    Public clients (CIMD/DCR) never carry a secret.
    """
    client_id: str
    client_secret: Optional[str] = None
    token_endpoint_auth_method: str = "none"
    raw: Dict[str, Any] = field(default_factory=dict)


@dataclass
class AuthServerMetadata:
    """Normalized OAuth 2.0 Authorization Server metadata (RFC 8414)."""
    issuer: str
    authorization_endpoint: str
    token_endpoint: str
    registration_endpoint: Optional[str] = None
    client_id_metadata_document_supported: bool = False
    scopes_supported: List[str] = field(default_factory=list)


# ─────────────────────────────────────────────────────────────────
# Base64url helper (RFC 4648 §5, unpadded)
# ─────────────────────────────────────────────────────────────────

def _b64url_no_pad(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _is_publicly_reachable_url(url: str) -> bool:
    """
    Return True if a CIMD (Client ID Metadata Document) URL could
    plausibly be fetched by a remote authorization server.

    CIMD's client_id is a URL that the authorization server must
    fetch over the network. When PELDRUN is running on localhost or
    a private network, that URL is unreachable — the server will
    reject it with "Invalid client_id". In that case the correct
    fallback is DCR (Dynamic Client Registration), which returns a
    real client_id string that the AS can trust without fetching
    anything.

    CIMD also requires HTTPS (draft §4.1). Plain HTTP is never
    considered publicly reachable.
    """
    try:
        parsed = urlparse(url)
    except Exception:
        return False

    if parsed.scheme != "https":
        return False

    host = (parsed.hostname or "").lower()
    if not host:
        return False

    # Loopback / localhost
    if host in ("localhost", "127.0.0.1", "::1"):
        return False

    # IPv4 private ranges (RFC 1918) and link-local.
    if host.startswith("127.") or host.startswith("10."):
        return False
    if host.startswith("192.168."):
        return False
    if host.startswith("169.254."):
        return False
    if host.startswith("172."):
        try:
            second = int(host.split(".")[1])
            if 16 <= second <= 31:
                return False
        except (ValueError, IndexError):
            pass

    return True


# ─────────────────────────────────────────────────────────────────
# PKCE (S256)
# ─────────────────────────────────────────────────────────────────

def generate_pkce() -> PKCEPair:
    """
    Generate a PKCE verifier / challenge pair.

    Per RFC 7636, the verifier is a high-entropy 43-128 character
    string drawn from [A-Za-z0-9._~-]. We use 32 random bytes
    (=> 43 base64url chars) which is at the top of the recommended
    entropy range.
    """
    verifier = _b64url_no_pad(os.urandom(32))
    challenge = _b64url_no_pad(hashlib.sha256(verifier.encode("ascii")).digest())
    return PKCEPair(verifier=verifier, challenge=challenge, method="S256")


# ─────────────────────────────────────────────────────────────────
# Public base URL resolution (no hardcoded ports)
# ─────────────────────────────────────────────────────────────────

def resolve_public_base_url(request: Optional[Any] = None) -> str:
    """
    Resolve the base URL PELDRUN is reachable at, for use in OAuth
    redirect_uri and client_metadata_url construction.

    Precedence:
      1. ``PELDRUN_BASE_URL`` env var — explicit override, no probing.
      2. FastAPI ``Request.base_url`` if a request is passed in — this
         is the exact host:port the client sees, so it is the safest
         dynamic source when available.
      3. ``PELDRUN_PORT`` / ``PORT`` env var — common in containerized
         deployments where uvicorn is behind an orchestrator.
      4. ``sys.argv`` — uvicorn CLI invocation (``--port 8080`` or
         ``--port=8080``).
      5. RuntimeError — we never silently fall back to a hardcoded
         port, because a wrong redirect_uri would break the callback.
    """
    explicit = (os.environ.get("PELDRUN_BASE_URL") or "").strip().rstrip("/")
    if explicit:
        return explicit

    if request is not None:
        try:
            base = str(getattr(request, "base_url", "")).rstrip("/")
            if base:
                return base
        except Exception as exc:  # noqa: BLE001
            logger.debug("Request.base_url access failed: %s", exc)

    port = (
        (os.environ.get("PELDRUN_PORT") or "").strip()
        or (os.environ.get("PORT") or "").strip()
    )
    if port:
        host = (os.environ.get("PELDRUN_HOST") or "127.0.0.1").strip()
        return f"http://{host}:{port}"

    # uvicorn --port N / --port=N
    for i, arg in enumerate(sys.argv):
        if arg in ("--port", "-p") and i + 1 < len(sys.argv):
            return f"http://127.0.0.1:{sys.argv[i + 1]}"
        if arg.startswith("--port="):
            return f"http://127.0.0.1:{arg.split('=', 1)[1]}"

    raise RuntimeError(
        "Cannot determine PELDRUN's public base URL. "
        "Set the PELDRUN_BASE_URL environment variable (e.g. "
        "http://127.0.0.1:8080) or pass a FastAPI Request object."
    )


def resolve_redirect_uri(request: Optional[Any] = None) -> str:
    """
    Return the OAuth redirect URI for the current PELDRUN instance.

    Kept in lockstep with the callback route in routers/mcp_auth.py.
    """
    return f"{resolve_public_base_url(request)}/api/mcp/oauth/callback"


def resolve_client_metadata_url(request: Optional[Any] = None) -> str:
    """
    Return the URL of PELDRUN's hosted Client ID Metadata Document.

    Used when a provider supports CIMD but the manifest does not
    supply its own metadata URL. The endpoint is served by
    routers/mcp_auth.py at GET /api/mcp/oauth/client-metadata.json.
    """
    return f"{resolve_public_base_url(request)}/api/mcp/oauth/client-metadata.json"


# ─────────────────────────────────────────────────────────────────
# OAuth state store (TTL-bounded, in-memory only)
# ─────────────────────────────────────────────────────────────────

class OAuthStateStore:
    """
    Transient storage for in-flight authorization flows.

    Each entry carries everything the callback needs to complete the
    exchange: the PKCE verifier, the resolved metadata, the client
    credentials, the resource indicator, and the redirect URI.

    Entries expire after ``ttl_seconds`` and are cleaned on access —
    no background thread is required for a store whose working set
    is bounded by user activity.
    """

    DEFAULT_TTL_SECONDS = 600  # 10 minutes

    def __init__(self, ttl_seconds: int = DEFAULT_TTL_SECONDS) -> None:
        self._ttl = int(ttl_seconds)
        self._data: Dict[str, Dict[str, Any]] = {}

    def _prune(self) -> None:
        now = time.monotonic()
        expired = [
            key for key, entry in self._data.items()
            if now - entry.get("_created_at", 0) > self._ttl
        ]
        for key in expired:
            self._data.pop(key, None)

    def put(self, state: str, payload: Dict[str, Any]) -> None:
        self._prune()
        entry = dict(payload)
        entry["_created_at"] = time.monotonic()
        self._data[state] = entry

    def pop(self, state: str) -> Optional[Dict[str, Any]]:
        self._prune()
        entry = self._data.pop(state, None)
        if entry is None:
            return None
        entry.pop("_created_at", None)
        return entry

    def __len__(self) -> int:
        self._prune()
        return len(self._data)


# ─────────────────────────────────────────────────────────────────
# OAuth manager
# ─────────────────────────────────────────────────────────────────

class OAuthManager:
    """Stateless OAuth 2.1 engine. All flow state lives in OAuthStateStore."""

    def __init__(self, http_client: Optional[httpx.AsyncClient] = None) -> None:
        self._http = http_client or httpx.AsyncClient(
            timeout=httpx.Timeout(30.0, connect=10.0),
            follow_redirects=False,
        )
        self._owns_http_client = http_client is None

    async def close(self) -> None:
        """Shut down the HTTP client on application shutdown."""
        if self._owns_http_client:
            try:
                await self._http.aclose()
            except Exception as exc:  # noqa: BLE001
                logger.debug("Error closing OAuth HTTP client: %s", exc)

    # ─────────────────────────────────────────────────────────────
    # Discovery
    # ─────────────────────────────────────────────────────────────

    async def discover_auth_server(self, mcp_server_url: str) -> AuthServerMetadata:
        """
        RFC 9728 discovery from an MCP server URL.

        The well-known URL is constructed by INSERTING
        ``.well-known/oauth-protected-resource`` between the host and
        the resource path — NOT by appending it to the path. For
        example:

            Resource:   https://mcp.notion.com/sse
            Candidate 1: https://mcp.notion.com/.well-known/oauth-protected-resource/sse
            Candidate 2: https://mcp.notion.com/.well-known/oauth-protected-resource

        We try candidate 1 first (per RFC 9728 §3.1's path-scoped form),
        then candidate 2 (host-scoped fallback). Only when BOTH fail do
        we raise — a single 404 should not be treated as "no discovery
        support" when the other form might work.

        Common MCP servers and their discovery support:

          * Notion (https://mcp.notion.com/sse)        → host-scoped
          * Linear, Atlassian Rovo                     → host-scoped
          * Most DCR/CIMD servers                      → path-scoped or host

        The subsequent authorization-server metadata fetch tries BOTH
        the RFC 8414 form (``/.well-known/oauth-authorization-server``)
        and the OpenID Connect discovery form
        (``/.well-known/openid-configuration``), since some providers
        only publish one.
        """
        from urllib.parse import urlparse as _urlparse

        parsed = _urlparse(mcp_server_url)
        scheme = parsed.scheme or "https"
        host = parsed.netloc
        path = (parsed.path or "").strip("/")

        if not host:
            raise ValueError(
                f"Cannot parse host from mcp_server_url: {mcp_server_url!r}"
            )

        # Candidate list, ordered by specificity (RFC 9728 §3.1).
        prm_candidates: list[str] = []
        if path:
            prm_candidates.append(
                f"{scheme}://{host}/.well-known/oauth-protected-resource/{path}"
            )
        prm_candidates.append(
            f"{scheme}://{host}/.well-known/oauth-protected-resource"
        )

        prm: Optional[dict] = None
        prm_url_used: Optional[str] = None
        last_error: Optional[Exception] = None

        for cand in prm_candidates:
            try:
                r = await self._http.get(cand)
                if r.status_code != 200:
                    last_error = ValueError(
                        f"HTTP {r.status_code} from {cand}"
                    )
                    continue
                prm = r.json()
                prm_url_used = cand
                logger.info(
                    "RFC 9728 discovery succeeded at %s (resource=%s)",
                    cand,
                    mcp_server_url,
                )
                break
            except (httpx.HTTPError, ValueError) as exc:
                last_error = exc
                logger.debug(
                    "RFC 9728 discovery attempt failed at %s: %s",
                    cand,
                    exc,
                )
                continue

        if prm is None or prm_url_used is None:
            raise ValueError(
                f"Protected resource metadata discovery failed for "
                f"{mcp_server_url!r}. Tried {prm_candidates}. "
                f"Last error: {last_error}"
            )

        auth_servers = prm.get("authorization_servers") or []
        if not isinstance(auth_servers, list) or not auth_servers:
            raise ValueError(
                f"No authorization_servers advertised at {prm_url_used}: "
                f"{prm}"
            )

        issuer = str(auth_servers[0]).rstrip("/")

        # Try both RFC 8414 and OIDC discovery metadata URLs. Providers
        # usually support one of the two; we do not know which in
        # advance, so try both in sequence.
        as_meta: Optional[dict] = None
        as_url_used: Optional[str] = None
        as_candidates = [
            f"{issuer}/.well-known/oauth-authorization-server",
            f"{issuer}/.well-known/openid-configuration",
        ]
        last_as_error: Optional[Exception] = None

        for cand in as_candidates:
            try:
                r = await self._http.get(cand)
                if r.status_code != 200:
                    last_as_error = ValueError(
                        f"HTTP {r.status_code} from {cand}"
                    )
                    continue
                meta = r.json()
                if isinstance(meta, dict):
                    as_meta = meta
                    as_url_used = cand
                    break
            except (httpx.HTTPError, ValueError) as exc:
                last_as_error = exc
                continue

        if as_meta is None or as_url_used is None:
            raise ValueError(
                f"Authorization server metadata discovery failed for "
                f"issuer {issuer!r}. Tried {as_candidates}. "
                f"Last error: {last_as_error}"
            )

        try:
            return AuthServerMetadata(
                issuer=issuer,
                authorization_endpoint=as_meta["authorization_endpoint"],
                token_endpoint=as_meta["token_endpoint"],
                registration_endpoint=as_meta.get("registration_endpoint"),
                client_id_metadata_document_supported=bool(
                    as_meta.get("client_id_metadata_document_supported")
                ),
                scopes_supported=list(as_meta.get("scopes_supported") or []),
            )
        except KeyError as exc:
            raise ValueError(
                f"Authorization server metadata at {as_url_used} is "
                f"missing required field {exc}"
            ) from exc

    async def get_metadata(self, mcp_server_url: str) -> AuthServerMetadata:
        """
        Resolve AS metadata for a server, falling back to pre-registered
        data when RFC 9728 discovery is unavailable.

        GitHub (and some other legacy providers) do not expose
        /.well-known/oauth-protected-resource at all, so discovery
        always fails for them. In that case we synthesize metadata
        from the pre-registered table.
        """
        try:
            return await self.discover_auth_server(mcp_server_url)
        except Exception as exc:  # noqa: BLE001
            logger.info(
                "RFC 9728 discovery failed for '%s' (%s) — trying pre-registered fallback.",
                mcp_server_url,
                exc,
            )

        host = urlparse(mcp_server_url).hostname or ""
        for match_host, cfg in PRE_REGISTERED_CLIENTS.items():
            if match_host in mcp_server_url or match_host == host:
                logger.info(
                    "Using pre-registered metadata for host '%s'.", match_host
                )
                return AuthServerMetadata(
                    issuer=f"https://{match_host}",
                    authorization_endpoint=cfg["authorization_endpoint"],
                    token_endpoint=cfg["token_endpoint"],
                    registration_endpoint=None,
                    client_id_metadata_document_supported=False,
                    scopes_supported=list(cfg.get("scopes", [])),
                )

        raise ValueError(
            f"No authorization server metadata available for '{mcp_server_url}'. "
            "The server does not support RFC 9728 discovery and no "
            "pre-registered client matches its hostname."
        )

    # ─────────────────────────────────────────────────────────────
    # Client registration resolution
    # ─────────────────────────────────────────────────────────────

    async def resolve_client(
        self,
        *,
        server_id: str,
        mcp_server_url: str,
        metadata: AuthServerMetadata,
        redirect_uri: str,
        manifest_metadata_url: Optional[str] = None,
        pelrdun_metadata_url: Optional[str] = None,
    ) -> RegistrationResult:
        """
        Resolve client registration for an authorization flow.

        Precedence:
          1. Pre-registered hostname match — explicit override. Chosen
             because the developer registered an OAuth app out-of-band
             and expects PELDRUN to use that specific client_id.
          2. CIMD (RFC 7591 alternative) — server advertises support.
             URL preference: manifest → PELDRUN hosted document.
          3. DCR (RFC 7591) — server exposes a registration endpoint.
          4. Raise — the caller must fix configuration.
        """
        # 1. Pre-registered hostname match
        host = urlparse(metadata.issuer).hostname or urlparse(mcp_server_url).hostname or ""
        for match_host, cfg in PRE_REGISTERED_CLIENTS.items():
            if match_host in metadata.issuer or match_host == host or match_host in mcp_server_url:
                client_id = (os.environ.get(cfg["client_id_env"]) or "").strip()
                if not client_id:
                    raise ValueError(
                        f"Pre-registered MCP client for '{match_host}' is "
                        f"missing environment variable {cfg['client_id_env']}. "
                        f"Register an OAuth app and export the client_id."
                    )
                client_secret = (
                    os.environ.get(cfg["client_secret_env"]) or ""
                ).strip() or None

                auth_method = "client_secret_post" if client_secret else "none"
                logger.info(
                    "Resolved pre-registered client for '%s' (server_id=%s, auth=%s)",
                    match_host,
                    server_id,
                    auth_method,
                )
                return RegistrationResult(
                    client_id=client_id,
                    client_secret=client_secret,
                    token_endpoint_auth_method=auth_method,
                    raw=dict(cfg),
                )

        # 2. CIMD — only when the metadata URL is publicly reachable.
        #
        # CIMD's client_id is a URL that the authorization server
        # must fetch over the network to verify the client. On
        # localhost / private networks that URL is unreachable, so
        # servers like Notion reject it with "Invalid client_id".
        # Falling through to DCR (which returns a real client_id
        # string) is the correct behavior — the AS does not need to
        # fetch anything.
        if metadata.client_id_metadata_document_supported:
            chosen_url = (manifest_metadata_url or "").strip() or (
                pelrdun_metadata_url or ""
            ).strip()
            if chosen_url and _is_publicly_reachable_url(chosen_url):
                logger.info(
                    "Resolved CIMD client for server '%s' using %s",
                    server_id,
                    chosen_url,
                )
                return RegistrationResult(
                    client_id=chosen_url,
                    token_endpoint_auth_method="none",
                    raw={"source": "cimd", "url": chosen_url},
                )
            # Fall through to DCR below.
            logger.info(
                "CIMD skipped for '%s': client metadata URL %r is not "
                "publicly reachable (localhost, private IP, or HTTP). "
                "Falling back to DCR.",
                server_id,
                chosen_url,
            )

        # 3. DCR — works with localhost because the AS returns a real
        #    client_id string (no fetch required).
        if metadata.registration_endpoint:
            # RFC 7591 §2: "web" is the default application_type.
            # RFC 8252 §7.3: native apps with loopback redirect URIs
            # should declare "native". PELDRUN is a web app but during
            # local development the redirect_uri is loopback, so we
            # declare "native" for that case — most servers (including
            # Notion) accept it and skip the HTTPS requirement.
            rd = urlparse(redirect_uri)
            is_loopback = (rd.hostname or "").lower() in (
                "localhost",
                "127.0.0.1",
                "::1",
            )
            body: Dict[str, Any] = {
                "client_name": "PELDRUN MCP Client",
                "redirect_uris": [redirect_uri],
                "grant_types": ["authorization_code", "refresh_token"],
                "response_types": ["code"],
                "token_endpoint_auth_method": "none",
                "application_type": "native" if is_loopback else "web",
            }
            try:
                r = await self._http.post(
                    metadata.registration_endpoint, json=body
                )
                r.raise_for_status()
                reg = r.json()
            except httpx.HTTPStatusError as dc_exc:
                detail = ""
                try:
                    detail = dc_exc.response.text[:500]
                except Exception:  # noqa: BLE001
                    pass
                raise ValueError(
                    f"Dynamic client registration failed at "
                    f"{metadata.registration_endpoint} "
                    f"(HTTP {dc_exc.response.status_code}): {detail}"
                ) from dc_exc
            except httpx.HTTPError as exc:
                raise ValueError(
                    f"Dynamic client registration failed at "
                    f"{metadata.registration_endpoint}: {exc}"
                ) from exc
            except ValueError as exc:
                raise ValueError(
                    f"Invalid JSON from registration endpoint: {exc}"
                ) from exc

            if not isinstance(reg, dict) or not reg.get("client_id"):
                raise ValueError(
                    "Registration endpoint returned no client_id."
                )
            logger.info(
                "Resolved DCR client for server '%s' "
                "(client_id=%s..., application_type=%s)",
                server_id,
                str(reg["client_id"])[:8],
                body["application_type"],
            )
            return RegistrationResult(
                client_id=reg["client_id"],
                client_secret=reg.get("client_secret"),
                token_endpoint_auth_method=reg.get(
                    "token_endpoint_auth_method", "none"
                ),
                raw=reg,
            )

        # 4. No mechanism available
        raise ValueError(
            f"No client registration mechanism available for issuer "
            f"'{metadata.issuer}' (server_id={server_id}). "
            "The provider does not support CIMD or DCR, and no "
            "pre-registered client matches its hostname."
        )

    # ─────────────────────────────────────────────────────────────
    # Authorization URL construction
    # ─────────────────────────────────────────────────────────────

    def build_authorization_url(
        self,
        *,
        metadata: AuthServerMetadata,
        client_id: str,
        redirect_uri: str,
        pkce: PKCEPair,
        scopes: List[str],
        state: str,
        resource: str,
    ) -> str:
        """
        Build an OAuth 2.1 authorization URL.

        ``resource`` (RFC 8707) is always included so the resulting
        token is bound to this specific MCP server audience.
        """
        params = {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "code_challenge": pkce.challenge,
            "code_challenge_method": pkce.method,
            "state": state,
            "resource": resource,
        }
        if scopes:
            params["scope"] = " ".join(scopes)

        sep = "&" if "?" in metadata.authorization_endpoint else "?"
        return f"{metadata.authorization_endpoint}{sep}{urlencode(params)}"

    # ─────────────────────────────────────────────────────────────
    # Token exchange
    # ─────────────────────────────────────────────────────────────

    async def exchange_code(
        self,
        *,
        metadata: AuthServerMetadata,
        client_id: str,
        client_secret: Optional[str],
        code: str,
        redirect_uri: str,
        pkce: PKCEPair,
        resource: str,
    ) -> Dict[str, Any]:
        """
        Exchange an authorization code for tokens.

        Sends ``resource`` per RFC 8707 so the AS can bind the token
        audience. GitHub ignores this parameter (it is not RFC 8707
        compliant), but sending it is harmless there.
        """
        body: Dict[str, Any] = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "client_id": client_id,
            "code_verifier": pkce.verifier,
        }
        # GitHub is not RFC 8707 compliant and will reject unknown
        # parameters in some configurations — but it also silently
        # ignores them. We send resource only to well-behaved servers.
        host = urlparse(metadata.issuer).hostname or ""
        if host not in PRE_REGISTERED_CLIENTS:
            body["resource"] = resource
        else:
            body["resource"] = resource  # RFC 8707 — include unconditionally

        if client_secret:
            body["client_secret"] = client_secret

        headers = {"Accept": "application/json"}

        try:
            r = await self._http.post(
                metadata.token_endpoint, data=body, headers=headers
            )
            r.raise_for_status()
        except httpx.HTTPStatusError as exc:
            # Surface the AS-provided error body — invaluable when a
            # redirect_uri mismatch or invalid grant occurs.
            detail = ""
            try:
                detail = exc.response.text[:500]
            except Exception:  # noqa: BLE001
                pass
            raise ValueError(
                f"Token exchange rejected by {metadata.token_endpoint} "
                f"(HTTP {exc.response.status_code}): {detail}"
            ) from exc
        except httpx.HTTPError as exc:
            raise ValueError(
                f"Token exchange failed at {metadata.token_endpoint}: {exc}"
            ) from exc

        try:
            payload = r.json()
        except ValueError as exc:
            raise ValueError(
                f"Token endpoint returned non-JSON: {r.text[:200]}"
            ) from exc

        if not isinstance(payload, dict):
            raise ValueError("Token endpoint returned a non-object payload.")
        if "error" in payload:
            raise ValueError(
                f"Token endpoint error: {payload.get('error')} "
                f"— {payload.get('error_description', '')}"
            )
        if not payload.get("access_token"):
            raise ValueError("Token endpoint returned no access_token.")

        return payload

    async def refresh(
        self,
        *,
        metadata: AuthServerMetadata,
        client_id: str,
        client_secret: Optional[str],
        refresh_token: str,
        resource: str,
    ) -> Dict[str, Any]:
        """
        Refresh an access token (with rotation semantics per OAuth 2.1).

        The AS is expected to return a NEW refresh_token. Callers must
        overwrite the stored one on every successful refresh.
        """
        body: Dict[str, Any] = {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": client_id,
            "resource": resource,
        }
        if client_secret:
            body["client_secret"] = client_secret

        try:
            r = await self._http.post(metadata.token_endpoint, data=body)
            r.raise_for_status()
        except httpx.HTTPStatusError as exc:
            detail = ""
            try:
                detail = exc.response.text[:500]
            except Exception:  # noqa: BLE001
                pass
            raise ValueError(
                f"Token refresh rejected (HTTP {exc.response.status_code}): {detail}"
            ) from exc
        except httpx.HTTPError as exc:
            raise ValueError(f"Token refresh failed: {exc}") from exc

        try:
            payload = r.json()
        except ValueError as exc:
            raise ValueError(
                f"Refresh endpoint returned non-JSON: {r.text[:200]}"
            ) from exc

        if not isinstance(payload, dict) or not payload.get("access_token"):
            raise ValueError("Refresh endpoint returned no access_token.")

        return payload


# ─────────────────────────────────────────────────────────────────
# Process-wide singletons
# ─────────────────────────────────────────────────────────────────

oauth_manager = OAuthManager()
oauth_state_store = OAuthStateStore()


__all__ = [
    "PRE_REGISTERED_CLIENTS",
    "PKCEPair",
    "RegistrationResult",
    "AuthServerMetadata",
    "generate_pkce",
    "resolve_public_base_url",
    "resolve_redirect_uri",
    "resolve_client_metadata_url",
    "OAuthStateStore",
    "OAuthManager",
    "oauth_manager",
    "oauth_state_store",
]
