# backend/integrations/source_control/security/oauth_flow.py
"""
OAuth 2.0 Authorization Code flow with PKCE.

This module implements the authorization handshake for provider
connections. It is the only component allowed to exchange an
authorization code for tokens; token storage is delegated to
`TokenService`.

Flow:
    1. The UI calls `begin_authorization(provider)`.
    2. The coordinator generates a cryptographically random `state`
       and a PKCE `code_verifier` (S256 challenge).
    3. The provider-specific `build_authorization_url` callback
       produces the URL the UI opens in the system browser.
    4. The provider redirects back to a loopback URI
       (`http://127.0.0.1:<port>/...`) with `code` and `state`.
    5. The UI calls `complete_authorization(...)` with those values.
    6. The coordinator validates `state`, exchanges `code` for tokens
       via the provider-specific `exchange_code_for_token` callback,
       stores the tokens via `TokenService`, and returns an
       `AuthResult`.

Design rules:
    - PKCE S256 is MANDATORY. The `plain` challenge method is not
      supported.
    - The redirect URI MUST use a loopback host (`127.0.0.1` or
      `::1`). `localhost` and `0.0.0.0` are rejected.
    - `state` values are single-use and expire after
      `SESSION_TTL_SECONDS`.
    - Tokens never leave the `TokenService`/vault. `AuthResult`
      carries only metadata.
    - All callbacks are registered per provider at application
      startup. This module has no provider-specific knowledge.

Singleton accessors:
    - `get_default_oauth_flow_coordinator()` returns the process-wide
      coordinator.
    - `get_default_oauth_session_manager()` returns the coordinator's
      session store, so that callers inspecting or pruning sessions
      observe exactly the sessions the coordinator manages. Both
      accessors operate on the same underlying object.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import secrets
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Awaitable, Callable, Dict, List, Optional, Sequence, Tuple
from urllib.parse import urlparse

from ..domain.enums import ConnectionStatus, ProviderId
from ..domain.models import ProviderAccount
from ..errors import AuthRequiredError, ProviderError, ValidationError
from ..ports.auth import (
    AuthResult,
    OAuthFlowPort,
    OAuthStartResult,
    TokenInfo,
)
from .audit import (
    AuditEventType,
    AuditLogger,
    AuditSeverity,
    get_default_audit_logger,
)
from .redaction import redact_text, redact_url
from .token_service import (
    RefreshCallback,
    TokenRefreshResult,
    TokenService,
    get_default_token_service,
)

__all__ = [
    "OAuthFlowCoordinator",
    "OAuthSessionManager",
    "OAuthProviderCallbacks",
    "AuthorizationUrlBuilder",
    "TokenExchanger",
    "TokenRevoker",
    "get_default_oauth_flow_coordinator",
    "set_default_oauth_flow_coordinator",
    "reset_default_oauth_flow_coordinator",
    "get_default_oauth_session_manager",
    "set_default_oauth_session_manager",
    "reset_default_oauth_session_manager",
    "SESSION_TTL_SECONDS",
    "LOOPBACK_HOSTS",
    "generate_pkce_verifier",
    "compute_pkce_challenge",
    "generate_state_token",
]


logger = logging.getLogger(__name__)


#: How long a `state` value remains valid.
SESSION_TTL_SECONDS: int = 10 * 60  # 10 minutes

#: Allowed loopback hosts for the redirect URI. `localhost` is
#: deliberately excluded because its resolution depends on the host
#: configuration and may point to a non-loopback interface.
LOOPBACK_HOSTS: frozenset[str] = frozenset({"127.0.0.1", "::1", "[::1]"})

#: Maximum number of pending sessions retained in memory.
MAX_PENDING_SESSIONS: int = 64


def _utc_now() -> datetime:
    """Return the current time as a timezone-aware UTC datetime."""
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Provider callbacks
# ---------------------------------------------------------------------------


#: Build the authorization URL. Receives the provider, client_id, the
#: redirect URI, the state, the PKCE challenge, and optional scopes.
#: Returns the fully qualified authorization URL.
AuthorizationUrlBuilder = Callable[
    [ProviderId, str, str, str, str, Optional[Sequence[str]]],
    str,
]


#: Exchange an authorization code for tokens. Receives the provider,
#: the authorization code, the PKCE code_verifier, the redirect URI,
#: the client_id, and an optional base URL. Returns a
#: `TokenRefreshResult`-shaped object (same fields).
TokenExchanger = Callable[
    [ProviderId, str, str, str, str, Optional[str]],
    Awaitable[TokenRefreshResult],
]


#: Revoke a token at the provider. Best-effort. Receives the
#: provider, the access token, and an optional base URL.
TokenRevoker = Callable[
    [ProviderId, str, Optional[str]],
    Awaitable[None],
]


@dataclass(frozen=True)
class OAuthProviderCallbacks:
    """
    Provider-specific OAuth callbacks.

    All callbacks are registered at application startup, once per
    provider. The coordinator never assumes anything about a provider
    beyond what these callbacks express.

    Fields:
        client_id:      Public client identifier. For public clients
                        this is not secret.
        authorization_url_builder: Build the authorization URL.
        token_exchanger: Exchange the code for tokens.
        token_revoker:  Optional. Best-effort revocation at the
                        provider.
        default_scopes: Scopes requested when the caller does not
                        supply them.
        pkce_required:  When True (default), the coordinator enforces
                        PKCE for this provider. All three supported
                        providers support PKCE.
    """

    client_id: str
    authorization_url_builder: AuthorizationUrlBuilder
    token_exchanger: TokenExchanger
    token_revoker: Optional[TokenRevoker] = None
    default_scopes: Sequence[str] = field(default_factory=tuple)
    pkce_required: bool = True


# ---------------------------------------------------------------------------
# Session manager
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _PendingSession:
    """Internal representation of a pending OAuth session."""

    state: str
    provider: ProviderId
    code_verifier: str
    redirect_uri: str
    base_url: Optional[str]
    created_at: datetime
    expires_at: datetime
    scopes: Tuple[str, ...] = ()
    client_id: str = ""


class OAuthSessionManager:
    """
    Bounded, thread-safe store for pending OAuth sessions.

    Sessions are keyed by the opaque `state` value. Each session is
    single-use: once consumed, it is removed from the store. Sessions
    expire after `SESSION_TTL_SECONDS`.
    """

    def __init__(self, max_sessions: int = MAX_PENDING_SESSIONS) -> None:
        if max_sessions <= 0:
            raise ValueError("max_sessions must be positive.")
        self._sessions: Dict[str, _PendingSession] = {}
        self._lock = threading.RLock()
        self._max_sessions = max_sessions

    def add(self, session: _PendingSession) -> None:
        """Insert a session, evicting the oldest if necessary."""
        with self._lock:
            if len(self._sessions) >= self._max_sessions:
                oldest = min(
                    self._sessions,
                    key=lambda k: self._sessions[k].created_at,
                )
                self._sessions.pop(oldest, None)
            self._sessions[session.state] = session

    def consume(self, state: str) -> Optional[_PendingSession]:
        """
        Return and remove the session for `state`.

        Returns `None` if the state is unknown. Callers MUST check
        the session's expiry before using it.
        """
        if not state:
            return None
        with self._lock:
            return self._sessions.pop(state, None)

    def get(self, state: str) -> Optional[_PendingSession]:
        """Return the session without removing it."""
        if not state:
            return None
        with self._lock:
            return self._sessions.get(state)

    def prune_expired(self) -> int:
        """Remove expired sessions. Returns the number removed."""
        now = _utc_now()
        removed = 0
        with self._lock:
            for state in list(self._sessions.keys()):
                if self._sessions[state].expires_at <= now:
                    self._sessions.pop(state, None)
                    removed += 1
        return removed

    def clear(self) -> None:
        """Remove every session. Intended for tests."""
        with self._lock:
            self._sessions.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._sessions)


# ---------------------------------------------------------------------------
# Coordinator
# ---------------------------------------------------------------------------


class OAuthFlowCoordinator(OAuthFlowPort):
    """
    Default implementation of `OAuthFlowPort`.

    Provider-specific behaviour is injected via
    `OAuthProviderCallbacks`, registered once per provider. The
    coordinator itself has no provider knowledge.
    """

    def __init__(
        self,
        *,
        token_service: Optional[TokenService] = None,
        audit_logger: Optional[AuditLogger] = None,
        session_manager: Optional[OAuthSessionManager] = None,
        session_ttl_seconds: int = SESSION_TTL_SECONDS,
    ) -> None:
        self._token_service = token_service or get_default_token_service()
        self._audit = audit_logger or get_default_audit_logger()
        self._sessions = session_manager or OAuthSessionManager()
        self._providers: Dict[str, OAuthProviderCallbacks] = {}
        # Account resolvers are registered per provider at startup.
        # Initializing the mapping here (rather than lazily via
        # `object.__setattr__`) keeps the coordinator's state complete
        # after construction, avoids a race with `_resolve_account`,
        # and makes the type checker happy.
        self._account_resolvers: Dict[
            str,
            Callable[
                [ProviderId, TokenRefreshResult, Optional[str]],
                Awaitable[ProviderAccount],
            ],
        ] = {}
        self._lock = threading.RLock()
        self._session_ttl_seconds = max(1, int(session_ttl_seconds))

    # --- Public accessors ----------------------------------------------

    @property
    def sessions(self) -> OAuthSessionManager:
        """
        Return the session manager backing this coordinator.

        Callers that want to inspect, prune, or clear pending OAuth
        sessions SHOULD use this property rather than accessing the
        private attribute. The returned object is the same instance
        the coordinator uses internally; there is a single source of
        truth for pending sessions.
        """
        return self._sessions

    @property
    def session_ttl_seconds(self) -> int:
        """Return the configured TTL for new pending sessions."""
        return self._session_ttl_seconds

    # --- Provider registration -----------------------------------------

    def register_provider(
        self,
        provider: ProviderId | str,
        callbacks: OAuthProviderCallbacks,
        *,
        overwrite: bool = False,
    ) -> None:
        """
        Register the OAuth callbacks for a provider.

        Raises:
            ValidationError: If inputs are invalid, or a provider is
                already registered and `overwrite` is False.
        """
        key = _provider_key(provider)
        if not isinstance(callbacks, OAuthProviderCallbacks):
            raise ValidationError(
                "register_provider expects OAuthProviderCallbacks.",
                details={"provider": key},
            )
        if not callbacks.client_id:
            raise ValidationError(
                "client_id must not be empty.",
                details={"provider": key},
            )
        with self._lock:
            if key in self._providers and not overwrite:
                raise ValidationError(
                    f"Provider '{key}' is already registered.",
                    details={"provider": key},
                )
            self._providers[key] = callbacks

    def unregister_provider(self, provider: ProviderId | str) -> bool:
        """Remove a provider's callbacks. Returns True if removed."""
        key = _provider_key(provider)
        with self._lock:
            return self._providers.pop(key, None) is not None

    def list_providers(self) -> Sequence[str]:
        """Return a snapshot of registered provider identifiers."""
        with self._lock:
            return tuple(sorted(self._providers.keys()))

    # --- Flow: begin ----------------------------------------------------

    def begin_authorization(
        self,
        provider: ProviderId,
        *,
        base_url: Optional[str] = None,
        scopes: Optional[Sequence[str]] = None,
        redirect_uri: Optional[str] = None,
    ) -> OAuthStartResult:
        """
        Start an OAuth authorization attempt.

        Generates a cryptographically random `state`, a PKCE
        `code_verifier`, and derives the S256 challenge. Stores the
        pending session and returns the URL the UI must open in the
        system browser.

        The `redirect_uri` MUST use a loopback host. When omitted, a
        loopback URI is chosen automatically; the UI is responsible
        for receiving the callback on that port.

        Raises:
            ValidationError: If the provider is unknown or if the
                redirect URI is not a loopback URI.
        """
        callbacks = self._get_callbacks(provider)
        self._sessions.prune_expired()

        code_verifier = _generate_code_verifier()
        code_challenge = _derive_code_challenge(code_verifier)
        state = _generate_state()

        resolved_redirect = redirect_uri or _default_redirect_uri()
        _validate_callback_url(resolved_redirect)

        resolved_scopes = tuple(scopes or callbacks.default_scopes)
        now = _utc_now()

        session = _PendingSession(
            state=state,
            provider=provider,
            code_verifier=code_verifier,
            redirect_uri=resolved_redirect,
            base_url=base_url,
            created_at=now,
            expires_at=now + timedelta(seconds=self._session_ttl_seconds),
            scopes=resolved_scopes,
            client_id=callbacks.client_id,
        )
        self._sessions.add(session)

        try:
            authorization_url = callbacks.authorization_url_builder(
                provider,
                callbacks.client_id,
                resolved_redirect,
                state,
                code_challenge,
                resolved_scopes or None,
            )
        except Exception as exc:  # noqa: BLE001
            # Do not leak a potentially broken URL.
            self._sessions.consume(state)
            raise ProviderError(
                "Failed to build the authorization URL.",
                details={
                    "provider": _provider_value(provider),
                    "reason": type(exc).__name__,
                },
                cause=exc,
            ) from exc

        self._audit.record(
            AuditEventType.AUTH_BEGIN,
            severity=AuditSeverity.INFO,
            action="begin_authorization",
            message=(
                f"OAuth authorization started for provider "
                f"'{_provider_value(provider)}'."
            ),
            payload={
                "provider": _provider_value(provider),
                "redirect_uri": redact_url(resolved_redirect),
                "scopes": list(resolved_scopes),
                "state_fingerprint": _fingerprint(state),
            },
            actor="user",
        )

        return OAuthStartResult(
            authorization_url=authorization_url,
            state=state,
            provider=provider,
            redirect_uri=resolved_redirect,
            expires_at=session.expires_at,
        )

    # --- Flow: complete -------------------------------------------------

    async def complete_authorization(
        self,
        provider: ProviderId,
        *,
        state: str,
        code: str,
        redirect_uri: str,
    ) -> AuthResult:
        """
        Complete an OAuth authorization attempt.

        Validates the state, exchanges the code for tokens via the
        provider callback, stores the tokens in the vault, and
        returns the resulting `AuthResult`.

        Raises:
            ValidationError: If `state` is unknown or expired.
            AuthRequiredError: If the token exchange fails.
            ProviderError: On unexpected provider failures.
        """
        if not code:
            raise ValidationError("Authorization code must not be empty.")
        if not state:
            raise ValidationError("State must not be empty.")

        session = self._sessions.consume(state)
        if session is None:
            self._audit.record(
                AuditEventType.AUTH_FAILURE,
                severity=AuditSeverity.WARNING,
                action="complete_authorization",
                message="OAuth callback received with unknown state.",
                payload={
                    "provider": _provider_value(provider),
                    "state_fingerprint": _fingerprint(state),
                },
                actor="user",
            )
            raise ValidationError(
                "OAuth state is unknown or has already been used.",
                details={"state_fingerprint": _fingerprint(state)},
            )

        if session.provider != provider:
            raise ValidationError(
                "OAuth state was issued for a different provider.",
                details={
                    "expected_provider": session.provider.value,
                    "received_provider": _provider_value(provider),
                },
            )

        if session.expires_at <= _utc_now():
            self._audit.record(
                AuditEventType.AUTH_FAILURE,
                severity=AuditSeverity.WARNING,
                action="complete_authorization",
                message="OAuth callback received after session expiry.",
                payload={
                    "provider": _provider_value(provider),
                    "state_fingerprint": _fingerprint(state),
                },
                actor="user",
            )
            raise ValidationError(
                "OAuth session has expired. Please restart the flow."
            )

        # Validate the caller-provided redirect_uri as a loopback URL,
        # then verify that it matches the value registered in the
        # session. The provider will reject a mismatched redirect_uri
        # with `redirect_uri_mismatch` during the token exchange, but
        # catching the mismatch here is defense in depth: it surfaces
        # a clear, actionable error, and it prevents a caller that
        # forgot to read `session.redirect_uri` (which is what the
        # HTTP facade currently does) from silently accepting a
        # different loopback URL — a scenario that would otherwise
        # allow an attacker who knows the victim's `state` value to
        # swap in a code issued for their own redirect_uri.
        _validate_callback_url(redirect_uri)
        if redirect_uri != session.redirect_uri:
            raise ValidationError(
                "redirect_uri does not match the value registered "
                "for this OAuth session. Restart the authorization "
                "flow.",
                details={
                    "expected": redact_url(session.redirect_uri),
                    "received": redact_url(redirect_uri),
                },
            )

        callbacks = self._get_callbacks(provider)

        try:
            token_result = await callbacks.token_exchanger(
                provider,
                code,
                session.code_verifier,
                redirect_uri,
                callbacks.client_id,
                session.base_url,
            )
        except Exception as exc:  # noqa: BLE001
            self._audit.record(
                AuditEventType.AUTH_FAILURE,
                severity=AuditSeverity.ERROR,
                action="complete_authorization",
                message=(
                    f"Token exchange failed for provider "
                    f"'{_provider_value(provider)}'."
                ),
                payload={
                    "provider": _provider_value(provider),
                    "reason": type(exc).__name__,
                },
                actor="user",
            )
            raise AuthRequiredError(
                "Failed to exchange the authorization code for tokens.",
                details={
                    "provider": _provider_value(provider),
                    "reason": type(exc).__name__,
                },
                cause=exc,
            ) from exc

        account = await self._resolve_account(
            provider, token_result, session.base_url
        )

        self._token_service.store_token(
            provider=provider,
            account_id=account.account_id,
            base_url=session.base_url or account.base_url or "default",
            access_token=token_result.access_token,
            refresh_token=token_result.refresh_token,
            expires_at=token_result.expires_at,
            scopes=token_result.scopes or session.scopes,
        )

        self._audit.record(
            AuditEventType.AUTH_COMPLETE,
            severity=AuditSeverity.INFO,
            action="complete_authorization",
            message=(
                f"OAuth authorization completed for provider "
                f"'{_provider_value(provider)}'."
            ),
            payload={
                "provider": _provider_value(provider),
                "account_id": account.account_id,
                "username": account.username,
                "scopes": list(token_result.scopes or session.scopes),
            },
            actor="user",
        )

        return AuthResult(
            provider=provider,
            account_id=account.account_id,
            username=account.username,
            # Apply the same fallback used by `store_token` above so
            # that the reported base_url and the base_url actually
            # stored in the vault are byte-for-byte identical. Without
            # this, a resolver that returns an empty `base_url` would
            # cause `AuthResult.base_url` to be `""` while the vault
            # entry was keyed under `"default"`.
            base_url=(
                session.base_url
                or account.base_url
                or "default"
            ),
            status=ConnectionStatus.CONNECTED,
            scopes=tuple(token_result.scopes or session.scopes),
            expires_at=token_result.expires_at,
            validated_at=_utc_now(),
            message=None,
        )

    # --- Flow: revoke ---------------------------------------------------

    async def revoke(
        self,
        provider: ProviderId,
        account_id: str,
        base_url: str,
    ) -> None:
        """
        Best-effort revocation of a provider token.

        First attempts provider-side revocation via the registered
        callback, then removes the local copy from the vault
        regardless of the remote outcome.
        """
        callbacks = self._get_callbacks(provider, required=False)
        access_token = self._token_service.get_access_token(
            provider, account_id, base_url
        )

        if callbacks and callbacks.token_revoker and access_token:
            try:
                await callbacks.token_revoker(provider, access_token, base_url)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "Provider-side token revocation failed for "
                    "provider '%s': %s",
                    _provider_value(provider),
                    type(exc).__name__,
                )

        self._token_service.revoke(provider, account_id, base_url)

        self._audit.record(
            AuditEventType.AUTH_REVOKE,
            severity=AuditSeverity.INFO,
            action="revoke",
            message=(
                f"OAuth credentials revoked for provider "
                f"'{_provider_value(provider)}'."
            ),
            payload={
                "provider": _provider_value(provider),
                "account_id": account_id,
            },
            actor="user",
        )

    # --- Internal helpers ----------------------------------------------

    def _get_callbacks(
        self,
        provider: ProviderId,
        *,
        required: bool = True,
    ) -> Optional[OAuthProviderCallbacks]:
        key = _provider_key(provider)
        with self._lock:
            callbacks = self._providers.get(key)
        if callbacks is None and required:
            raise ValidationError(
                f"OAuth is not configured for provider '{key}'.",
                details={"provider": key},
            )
        return callbacks

    async def _resolve_account(
        self,
        provider: ProviderId,
        token_result: TokenRefreshResult,
        base_url: Optional[str],
    ) -> ProviderAccount:
        """
        Resolve the provider account after a successful token
        exchange.

        Provider adapters that support `get_authenticated_account`
        expose the identity via that method. The coordinator does not
        call the provider directly; instead, it relies on the
        provider to have registered an account-resolver callback
        under the same name. When no resolver is available, a
        placeholder account is returned to allow the flow to
        complete, and the UI can refresh the identity later.
        """
        # Read the resolver reference under the lock so that a
        # concurrent `register_account_resolver` cannot observe a
        # half-initialized mapping. The call itself is awaited
        # outside the lock so that a slow resolver does not block
        # registration of other providers.
        with self._lock:
            resolver = self._account_resolvers.get(
                _provider_key(provider)
            )
        if resolver is not None:
            return await resolver(provider, token_result, base_url)

        # Placeholder identity; the caller is expected to refresh it
        # via the provider adapter's `get_authenticated_account`.
        return ProviderAccount(
            provider=provider,
            account_id="unknown",
            username="unknown",
            base_url=base_url or "default",
            connected=True,
        )

    def register_account_resolver(
        self,
        provider: ProviderId | str,
        resolver: Callable[
            [ProviderId, TokenRefreshResult, Optional[str]],
            Awaitable[ProviderAccount],
        ],
    ) -> None:
        """
        Register an account resolver for a provider.

        The resolver is invoked during `complete_authorization` to
        determine the account identity associated with the newly
        issued token. It typically calls the provider's
        `get_authenticated_account` API.
        """
        key = _provider_key(provider)
        if not callable(resolver):
            raise ValidationError(
                "account resolver must be callable.",
                details={"provider": key},
            )
        with self._lock:
            self._account_resolvers[key] = resolver


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Public aliases for the internal PKCE and state helpers. These are
# used by external callers (tests, custom authentication flows, and
# provider adapters) that need to build or verify PKCE parameters
# without instantiating the full coordinator.
#
# The aliases are simple references; the underlying implementations
# remain the single source of truth.
def _generate_state() -> str:
    """
    Generate a cryptographically random state value.

    The value is URL-safe and long enough (32 bytes of entropy) to
    make guessing infeasible.
    """
    return secrets.token_urlsafe(32)


def _generate_code_verifier() -> str:
    """
    Generate a PKCE `code_verifier` per RFC 7636.

    The verifier is 43-128 characters of `[A-Za-z0-9-._~]`. We use
    64 bytes of entropy base64url-encoded, yielding 86 characters.
    """
    return secrets.token_urlsafe(64)


def _derive_code_challenge(code_verifier: str) -> str:
    """
    Derive the S256 `code_challenge` from a `code_verifier`.

    The challenge is `BASE64URL(SHA256(ASCII(code_verifier)))` with
    padding removed, as mandated by RFC 7636.
    """
    digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def _fingerprint(value: str) -> str:
    """
    Return a short, non-reversible fingerprint of a secret value.

    Used to correlate audit events without ever storing the raw
    `state` value.
    """
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


def _default_redirect_uri() -> str:
    """
    Return the default loopback redirect URI.

    The port is chosen by the caller (the UI). Returning a fixed URI
    here would prevent the UI from binding an ephemeral port. This
    default is used only when the caller does not supply a URI, and
    it targets an unlikely port.
    """
    return "http://127.0.0.1:8765/oauth/callback"


def _validate_callback_url(url: str) -> None:
    """
    Validate that `url` is a loopback callback URI.

    Rules:
        - Scheme MUST be `http` (loopback does not need TLS).
        - Host MUST be `127.0.0.1` or `::1`.
        - A port MUST be present.
        - No userinfo (username/password) is allowed.
    """
    if not url:
        raise ValidationError("redirect_uri must not be empty.")
    try:
        parsed = urlparse(url)
    except ValueError as exc:
        raise ValidationError(
            "redirect_uri is not a valid URL.",
            details={"reason": str(exc)},
        ) from exc

    if parsed.scheme.lower() != "http":
        raise ValidationError(
            "redirect_uri MUST use the http scheme.",
            details={"scheme": parsed.scheme},
        )
    if parsed.hostname not in {"127.0.0.1", "::1"}:
        raise ValidationError(
            "redirect_uri MUST use a loopback host (127.0.0.1 or ::1).",
            details={"host": parsed.hostname},
        )
    if not parsed.port:
        raise ValidationError(
            "redirect_uri MUST include an explicit port.",
            details={"url": redact_url(url)},
        )
    if parsed.username or parsed.password:
        raise ValidationError(
            "redirect_uri MUST NOT include userinfo."
        )


def _provider_key(provider: ProviderId | str) -> str:
    """Normalize a provider identifier to a lowercase string."""
    if isinstance(provider, ProviderId):
        return provider.value
    if isinstance(provider, str):
        normalized = provider.strip().lower()
        if not normalized:
            raise ValidationError("provider must not be blank.")
        return normalized
    raise ValidationError(
        "provider must be a ProviderId enum member or a string.",
        details={"type": type(provider).__name__},
    )


def _provider_value(provider: ProviderId | str) -> str:
    """Return the string form of a provider identifier."""
    return _provider_key(provider)

# ---------------------------------------------------------------------------
# Public PKCE and state helpers
# ---------------------------------------------------------------------------
#
# These are aliases for the internal helpers above. They are part of
# the public API because external callers (tests, custom auth flows,
# provider adapters) frequently need to generate a PKCE verifier,
# derive its S256 challenge, or generate a random state token without
# instantiating the full coordinator.

#: Generate a PKCE `code_verifier` per RFC 7636.
generate_pkce_verifier = _generate_code_verifier

#: Derive the S256 `code_challenge` from a `code_verifier`.
compute_pkce_challenge = _derive_code_challenge

#: Generate a cryptographically random OAuth `state` value.
generate_state_token = _generate_state

# ---------------------------------------------------------------------------
# Default coordinator singleton
# ---------------------------------------------------------------------------


_DEFAULT_COORDINATOR: Optional[OAuthFlowCoordinator] = None
_DEFAULT_LOCK = threading.RLock()


def get_default_oauth_flow_coordinator() -> OAuthFlowCoordinator:
    """
    Return the process-wide default OAuth coordinator, creating it on
    first use.
    """
    global _DEFAULT_COORDINATOR
    with _DEFAULT_LOCK:
        if _DEFAULT_COORDINATOR is None:
            _DEFAULT_COORDINATOR = OAuthFlowCoordinator()
        return _DEFAULT_COORDINATOR


def set_default_oauth_flow_coordinator(
    coordinator: OAuthFlowCoordinator,
) -> None:
    """
    Replace the process-wide default OAuth coordinator.

    Intended for application startup and for tests.
    """
    if not isinstance(coordinator, OAuthFlowCoordinator):
        raise TypeError(
            "set_default_oauth_flow_coordinator expects an "
            "OAuthFlowCoordinator instance."
        )
    global _DEFAULT_COORDINATOR
    with _DEFAULT_LOCK:
        _DEFAULT_COORDINATOR = coordinator


def reset_default_oauth_flow_coordinator() -> OAuthFlowCoordinator:
    """
    Replace the default coordinator with a fresh one.

    Used by tests to guarantee isolation between test cases.
    """
    global _DEFAULT_COORDINATOR
    with _DEFAULT_LOCK:
        _DEFAULT_COORDINATOR = OAuthFlowCoordinator()
        return _DEFAULT_COORDINATOR

# ---------------------------------------------------------------------------
# Backward-compatibility aliases for the session manager accessors
# ---------------------------------------------------------------------------
#
# `get_default_oauth_session_manager` is the only accessor for the
# session manager (see its docstring above for why there is no
# separate setter). The `set_*` and `reset_*` aliases below are kept
# for callers that expect a symmetric API. They intentionally operate
# on the coordinator, because the session manager is a property of
# the coordinator and cannot be replaced in isolation.

def set_default_oauth_session_manager(
    manager: OAuthSessionManager,
) -> None:
    """
    Replace the coordinator's session manager.

    This is a convenience wrapper that constructs a new coordinator
    around the supplied session manager and installs it as the
    default. It exists so that callers familiar with the
    `get_default_*` / `set_default_*` pattern can swap the manager
    without worrying about the coordinator's identity.
    """
    if not isinstance(manager, OAuthSessionManager):
        raise TypeError(
            "set_default_oauth_session_manager expects an "
            "OAuthSessionManager instance."
        )
    global _DEFAULT_COORDINATOR
    with _DEFAULT_LOCK:
        previous = _DEFAULT_COORDINATOR
        coordinator = OAuthFlowCoordinator(session_manager=manager)
        if previous is not None:
            # Preserve the provider registrations and the account
            # resolvers so that the swap does not lose configuration.
            with previous._lock:
                coordinator._providers = dict(previous._providers)
                if hasattr(previous, "_account_resolvers"):
                    coordinator._account_resolvers = dict(
                        previous._account_resolvers
                    )
        _DEFAULT_COORDINATOR = coordinator


def reset_default_oauth_session_manager() -> OAuthSessionManager:
    """
    Replace the default coordinator's session manager with a fresh
    one and return it.

    Used by tests to guarantee isolation between test cases. The
    coordinator itself is replaced as well, so that no stale
    registrations leak across tests.
    """
    global _DEFAULT_COORDINATOR
    with _DEFAULT_LOCK:
        coordinator = OAuthFlowCoordinator()
        _DEFAULT_COORDINATOR = coordinator
        return coordinator.sessions
# ---------------------------------------------------------------------------
# Default session manager accessor
# ---------------------------------------------------------------------------


def get_default_oauth_session_manager() -> OAuthSessionManager:
    """
    Return the session manager used by the default OAuth coordinator.

    This is the SAME instance the coordinator uses internally, so
    sessions created via `get_default_oauth_flow_coordinator().begin_authorization(...)`
    are immediately visible through this accessor. Callers that need
    to inspect, prune, or clear pending sessions SHOULD use this
    function rather than accessing coordinator internals.

    There is no separate setter: replacing the coordinator via
    `set_default_oauth_flow_coordinator` also replaces the session
    manager returned here, because the manager is a property of the
    coordinator.
    """
    return get_default_oauth_flow_coordinator().sessions
