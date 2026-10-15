from __future__ import annotations

import base64
import hashlib
import secrets
import threading
from datetime import datetime, timedelta
from typing import Optional

from ..domain.enums import ProviderId
from ..errors import ValidationError
from ..ports.auth import AuthorizationSession


def generate_pkce_verifier(num_bytes: int = 32) -> str:
    """Generates a high-entropy cryptographic code verifier for PKCE (RFC 7636).

    Returns an unpadded base64url-encoded string between 43 and 128 characters.
    """
    raw_bytes = secrets.token_bytes(num_bytes)
    return base64.urlsafe_b64encode(raw_bytes).decode("ascii").rstrip("=")


def compute_pkce_challenge(code_verifier: str) -> str:
    """Computes the S256 code challenge from a code verifier (RFC 7636)."""
    if not code_verifier:
        raise ValidationError("PKCE code verifier cannot be empty.")
    digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def generate_state_token() -> str:
    """Generates an unguessable cryptographic state token for OAuth CSRF protection."""
    return secrets.token_urlsafe(32)


class OAuthSessionManager:
    """Thread-safe manager for ephemeral OAuth authorization sessions."""

    def __init__(self, default_ttl_minutes: int = 15) -> None:
        self.default_ttl_minutes = default_ttl_minutes
        self._sessions_by_state: dict[str, AuthorizationSession] = {}
        self._lock = threading.Lock()

    def create_session(
        self,
        provider_id: ProviderId,
        redirect_uri: str,
        ttl_minutes: Optional[int] = None,
        use_pkce: bool = True,
    ) -> AuthorizationSession:
        """Creates and registers a new ephemeral authorization session."""
        session_id = f"sc_sess_{secrets.token_hex(16)}"
        state = generate_state_token()
        code_verifier = generate_pkce_verifier() if use_pkce else None

        ttl = ttl_minutes if ttl_minutes is not None else self.default_ttl_minutes
        now = datetime.utcnow()
        expires_at = now + timedelta(minutes=ttl)

        session = AuthorizationSession(
            session_id=session_id,
            provider_id=provider_id,
            state=state,
            code_verifier=code_verifier,
            redirect_uri=redirect_uri,
            created_at=now,
            expires_at=expires_at,
        )

        with self._lock:
            self._cleanup_expired_locked(now)
            self._sessions_by_state[state] = session

        return session

    def verify_and_consume(
        self, state: str, expected_provider: Optional[ProviderId] = None
    ) -> AuthorizationSession:
        """Atomically verifies and consumes a session by its state token.

        Raises ValidationError if the state is missing, expired, already consumed,
        or belongs to an unexpected provider.
        """
        if not state:
            raise ValidationError("Missing OAuth state parameter in callback.")

        now = datetime.utcnow()
        with self._lock:
            session = self._sessions_by_state.pop(state, None)
            if session is None:
                raise ValidationError(
                    "Invalid or expired OAuth state token. Possible CSRF attempt or expired session."
                )

            if now >= session.expires_at:
                raise ValidationError(
                    f"OAuth authorization session expired at {session.expires_at.isoformat()}."
                )

            if expected_provider and session.provider_id != expected_provider:
                raise ValidationError(
                    f"OAuth callback provider mismatch. Expected '{expected_provider.value}', "
                    f"got session provider '{session.provider_id.value}'."
                )

            return session

    def peek_session(self, state: str) -> Optional[AuthorizationSession]:
        """Retrieves an active session by state without consuming it (diagnostic helper)."""
        with self._lock:
            session = self._sessions_by_state.get(state)
            if session and datetime.utcnow() < session.expires_at:
                return session
            return None

    def _cleanup_expired_locked(self, now: datetime) -> None:
        """Removes expired sessions. Assumes caller holds self._lock."""
        expired_states = [
            s for s, sess in self._sessions_by_state.items() if now >= sess.expires_at
        ]
        for s in expired_states:
            del self._sessions_by_state[s]

    def clear(self) -> None:
        """Clears all active sessions (isolated test teardown helper)."""
        with self._lock:
            self._sessions_by_state.clear()


# ============================================================
# OAuthSessionManager Singleton Accessors
# ============================================================

_DEFAULT_SESSION_MANAGER: Optional[OAuthSessionManager] = None
_OAUTH_LOCK = threading.Lock()


def get_default_oauth_session_manager() -> OAuthSessionManager:
    """Returns the process-wide default OAuthSessionManager singleton."""
    global _DEFAULT_SESSION_MANAGER
    if _DEFAULT_SESSION_MANAGER is None:
        with _OAUTH_LOCK:
            if _DEFAULT_SESSION_MANAGER is None:
                _DEFAULT_SESSION_MANAGER = OAuthSessionManager()
    return _DEFAULT_SESSION_MANAGER


def set_default_oauth_session_manager(manager: OAuthSessionManager) -> None:
    """Explicitly injects an OAuthSessionManager instance (used during testing)."""
    global _DEFAULT_SESSION_MANAGER
    with _OAUTH_LOCK:
        _DEFAULT_SESSION_MANAGER = manager


def reset_default_oauth_session_manager() -> None:
    """Resets the process-wide default OAuthSessionManager (used during testing)."""
    global _DEFAULT_SESSION_MANAGER
    with _OAUTH_LOCK:
        if _DEFAULT_SESSION_MANAGER is not None:
            _DEFAULT_SESSION_MANAGER.clear()
        _DEFAULT_SESSION_MANAGER = None
