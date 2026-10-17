# backend/integrations/source_control/security/token_service.py
"""
Token lifecycle management for the Source Control Integration
subsystem.

The `TokenService` is the ONLY component allowed to read or write raw
access tokens and refresh tokens. It stores every token pair in the
OS credential vault as a single JSON blob and exposes a narrow API to
the rest of the subsystem:

    - `store_token`     : persist a freshly obtained token pair.
    - `get_access_token`: read the current access token.
    - `get_refresh_token`: read the current refresh token.
    - `get_token_info`  : read safe metadata (no token values).
    - `refresh`         : refresh via a provider-registered callback.
    - `revoke`          : delete the token pair from the vault.
    - `list_stored`     : enumerate known (provider, account, base_url)
                          triples without exposing secrets.

Design rules:
    - Tokens MUST NEVER be logged. Every read operation returns the
      raw value only to the caller; the value MUST NOT enter the
      audit log, the operation journal, or the model context.
    - The service MUST be provider-agnostic. Refresh is delegated to a
      callback registered by each provider adapter at startup.
    - The service MUST NOT raise on missing tokens. Missing tokens are
      returned as `None`. Only explicit refresh failures raise.
    - The service MUST tolerate corrupt vault entries by discarding
      them and logging a warning. A corrupt entry MUST NOT crash the
      application.
    - The service is thread-safe. All mutations hold a reentrant lock.
"""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from typing import (
    Awaitable,
    Callable,
    Dict,
    List,
    Optional,
    Sequence,
    Tuple,
)

from ..domain.enums import ProviderId
from ..errors import AuthExpiredError, ProviderError, ValidationError
from ..ports.auth import TokenInfo, TokenServicePort, make_vault_key
from .audit import (
    AuditEventType,
    AuditLogger,
    AuditSeverity,
    get_default_audit_logger,
)
from .redaction import redact_text
from .vault import CredentialVault, get_default_vault

__all__ = [
    "TokenService",
    "TokenRefreshResult",
    "RefreshCallback",
    "get_default_token_service",
    "set_default_token_service",
    "reset_default_token_service",
]


logger = logging.getLogger(__name__)


def _utc_now() -> datetime:
    """Return the current time as a timezone-aware UTC datetime."""
    return datetime.now(timezone.utc)


#: Tokens within this margin of expiry are treated as expired to
#: avoid mid-request failures. Sixty seconds is the industry norm.
_EXPIRY_MARGIN = timedelta(seconds=60)


# ---------------------------------------------------------------------------
# Refresh contract
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TokenRefreshResult:
    """
    Result of a refresh callback.

    The callback is registered per provider and is responsible for
    performing the actual HTTP exchange with the provider. The
    `TokenService` does not know the provider protocol; it only knows
    how to store and retrieve the result.
    """

    access_token: str
    refresh_token: Optional[str] = None
    expires_at: Optional[datetime] = None
    scopes: Optional[Sequence[str]] = None


#: A refresh callback receives the provider, the account ID, the base
#: URL, and the current refresh token. It returns a new
#: `TokenRefreshResult` or raises on failure.
RefreshCallback = Callable[
    [ProviderId, str, str, str],
    Awaitable[TokenRefreshResult],
]


# ---------------------------------------------------------------------------
# Internal record
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _TokenRecord:
    """
    Internal representation of a stored token pair.

    This record is serialized to JSON and stored as a single value in
    the vault. The JSON shape is versioned so that future changes can
    migrate without ambiguity.
    """

    access_token: str
    refresh_token: Optional[str] = None
    expires_at: Optional[datetime] = None
    scopes: Sequence[str] = field(default_factory=tuple)
    stored_at: datetime = field(default_factory=_utc_now)
    last_refreshed_at: Optional[datetime] = None
    schema_version: int = 1


def _serialize_record(record: _TokenRecord) -> str:
    """Serialize a `_TokenRecord` to a compact JSON string."""
    payload = {
        "v": record.schema_version,
        "access_token": record.access_token,
        "refresh_token": record.refresh_token,
        "expires_at": (
            record.expires_at.isoformat() if record.expires_at else None
        ),
        "scopes": list(record.scopes),
        "stored_at": record.stored_at.isoformat(),
        "last_refreshed_at": (
            record.last_refreshed_at.isoformat()
            if record.last_refreshed_at
            else None
        ),
    }
    return json.dumps(payload, separators=(",", ":"), sort_keys=True)


def _deserialize_record(raw: str) -> Optional[_TokenRecord]:
    """
    Deserialize a JSON string into a `_TokenRecord`.

    Returns `None` for malformed input; the caller is responsible for
    discarding the corresponding vault entry.
    """
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None

    access_token = data.get("access_token")
    if not isinstance(access_token, str) or not access_token:
        return None

    refresh_token = data.get("refresh_token")
    if refresh_token is not None and not isinstance(refresh_token, str):
        refresh_token = None

    scopes_raw = data.get("scopes")
    scopes: Tuple[str, ...] = ()
    if isinstance(scopes_raw, list):
        scopes = tuple(str(s) for s in scopes_raw if isinstance(s, str))

    return _TokenRecord(
        access_token=access_token,
        refresh_token=refresh_token,
        expires_at=_parse_datetime(data.get("expires_at")),
        scopes=scopes,
        stored_at=_parse_datetime(data.get("stored_at")) or _utc_now(),
        last_refreshed_at=_parse_datetime(data.get("last_refreshed_at")),
        schema_version=int(data.get("v") or 1),
    )


def _parse_datetime(value: object) -> Optional[datetime]:
    """
    Parse a value into a timezone-aware datetime.

    Accepts ISO 8601 strings, `datetime` instances, or `None`. Values
    without a timezone are assumed to be UTC, matching the convention
    used by most providers' API responses.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    return None


# ---------------------------------------------------------------------------
# Token service
# ---------------------------------------------------------------------------


class TokenService(TokenServicePort):
    """
    Default implementation of `TokenServicePort`.

    Backed by a `CredentialVault` and an in-process registry of
    refresh callbacks, one per provider. Refresh callbacks are
    registered at application startup by each provider adapter.
    """

    def __init__(
        self,
        vault: Optional[CredentialVault] = None,
        *,
        audit_logger: Optional[AuditLogger] = None,
    ) -> None:
        self._vault = vault if vault is not None else get_default_vault()
        self._audit = (
            audit_logger
            if audit_logger is not None
            else get_default_audit_logger()
        )
        self._refresh_callbacks: Dict[str, RefreshCallback] = {}
        self._record_cache: Dict[str, _TokenRecord] = {}
        self._lock = threading.RLock()

    # --- Refresh callback registration ----------------------------------

    def register_refresh_callback(
        self,
        provider: ProviderId | str,
        callback: RefreshCallback,
        *,
        overwrite: bool = True,
    ) -> None:
        """
        Register the refresh callback for a provider.

        The callback is invoked by `refresh()` when the stored access
        token is close to expiry. It MUST perform the HTTP exchange
        and return a `TokenRefreshResult`. Raising from the callback
        propagates out of `refresh()` as `AuthExpiredError`.
        """
        key = _provider_key(provider)
        if not callable(callback):
            raise ValidationError(
                "Refresh callback must be callable.",
                details={"provider": key},
            )
        with self._lock:
            if key in self._refresh_callbacks and not overwrite:
                raise ValidationError(
                    f"A refresh callback is already registered for "
                    f"provider '{key}'.",
                    details={"provider": key},
                )
            self._refresh_callbacks[key] = callback

    def unregister_refresh_callback(
        self, provider: ProviderId | str
    ) -> bool:
        """Remove a refresh callback. Returns True if one was present."""
        key = _provider_key(provider)
        with self._lock:
            return self._refresh_callbacks.pop(key, None) is not None

    # --- Public API (TokenServicePort) ----------------------------------

    def store_token(
        self,
        provider: ProviderId,
        account_id: str,
        base_url: str,
        *,
        access_token: str,
        refresh_token: Optional[str] = None,
        expires_at: Optional[datetime] = None,
        scopes: Optional[Sequence[str]] = None,
    ) -> None:
        """
        Persist a token pair for the given provider and account.

        Overwrites any existing entry. The stored value is a single
        JSON blob; the vault protects it at rest.
        """
        key = _vault_key(provider, base_url, account_id)
        if not access_token:
            raise ValidationError("access_token must not be empty.")

        record = _TokenRecord(
            access_token=access_token,
            refresh_token=refresh_token,
            expires_at=_parse_datetime(expires_at),
            scopes=tuple(scopes or ()),
            stored_at=_utc_now(),
        )

        with self._lock:
            self._vault.put(key, _serialize_record(record))
            self._record_cache[key] = record

        self._audit.record(
            AuditEventType.CREDENTIAL_STORE,
            severity=AuditSeverity.INFO,
            action="store_token",
            message=(
                f"Stored credential for provider '{_provider_value(provider)}'."
            ),
            payload={
                "provider": _provider_value(provider),
                "account_id": account_id,
                "has_refresh_token": bool(refresh_token),
                "expires_at": (
                    record.expires_at.isoformat()
                    if record.expires_at
                    else None
                ),
                "scopes": list(record.scopes),
            },
            actor="system",
        )

    def get_access_token(
        self,
        provider: ProviderId,
        account_id: str,
        base_url: str,
    ) -> Optional[str]:
        """
        Return the current access token, or `None` if absent.

        Callers MUST NOT log the returned value. Use `get_token_info`
        when only metadata is needed.
        """
        record = self._read_record(provider, account_id, base_url)
        return record.access_token if record else None

    def get_refresh_token(
        self,
        provider: ProviderId,
        account_id: str,
        base_url: str,
    ) -> Optional[str]:
        """Return the current refresh token, or `None` if absent."""
        record = self._read_record(provider, account_id, base_url)
        return record.refresh_token if record else None

    def get_token_info(
        self,
        provider: ProviderId,
        account_id: str,
        base_url: str,
    ) -> Optional[TokenInfo]:
        """
        Return safe metadata about the stored token, or `None`.

        The returned object carries no token values.
        """
        record = self._read_record(provider, account_id, base_url)
        if record is None:
            return None
        return TokenInfo(
            provider=provider,
            account_id=account_id,
            base_url=base_url,
            expires_at=record.expires_at,
            scopes=tuple(record.scopes),
            has_refresh_token=bool(record.refresh_token),
            last_refreshed_at=record.last_refreshed_at,
        )

    async def refresh(
        self,
        provider: ProviderId,
        account_id: str,
        base_url: str,
    ) -> Optional[TokenInfo]:
        """
        Refresh the access token using the registered callback.

        Returns the updated `TokenInfo` on success, or `None` when
        there is no refresh token to use. Raises `AuthExpiredError`
        when a refresh token exists but the callback fails.
        """
        record = self._read_record(provider, account_id, base_url)
        if record is None:
            return None
        if not record.refresh_token:
            return None

        key = _provider_key(provider)
        with self._lock:
            callback = self._refresh_callbacks.get(key)
        if callback is None:
            raise ProviderError(
                f"No refresh callback is registered for provider "
                f"'{key}'.",
                details={"provider": key},
            )

        try:
            result = await callback(
                provider, account_id, base_url, record.refresh_token
            )
        except AuthExpiredError:
            raise
        except Exception as exc:  # noqa: BLE001
            safe_message = redact_text(
                f"Token refresh failed for provider '{key}'."
            )
            raise AuthExpiredError(
                safe_message,
                details={
                    "provider": key,
                    "account_id": account_id,
                    "reason": type(exc).__name__,
                },
                cause=exc,
            ) from exc

        now = _utc_now()
        refreshed = _TokenRecord(
            access_token=result.access_token,
            refresh_token=result.refresh_token or record.refresh_token,
            expires_at=_parse_datetime(result.expires_at),
            scopes=tuple(result.scopes or record.scopes),
            stored_at=record.stored_at,
            last_refreshed_at=now,
        )

        vault_key = _vault_key(provider, base_url, account_id)
        with self._lock:
            self._vault.put(vault_key, _serialize_record(refreshed))
            self._record_cache[vault_key] = refreshed

        self._audit.record(
            AuditEventType.CREDENTIAL_REFRESH,
            severity=AuditSeverity.INFO,
            action="refresh_token",
            message=f"Refreshed credential for provider '{key}'.",
            payload={
                "provider": key,
                "account_id": account_id,
                "has_new_refresh_token": bool(result.refresh_token),
                "expires_at": (
                    refreshed.expires_at.isoformat()
                    if refreshed.expires_at
                    else None
                ),
            },
            actor="system",
        )

        return TokenInfo(
            provider=provider,
            account_id=account_id,
            base_url=base_url,
            expires_at=refreshed.expires_at,
            scopes=tuple(refreshed.scopes),
            has_refresh_token=bool(refreshed.refresh_token),
            last_refreshed_at=refreshed.last_refreshed_at,
        )

    def revoke(
        self,
        provider: ProviderId,
        account_id: str,
        base_url: str,
    ) -> None:
        """
        Remove the stored token pair from the vault.

        This is a local operation. Provider-side revocation is a
        separate best-effort call performed by the OAuth flow.
        """
        key = _vault_key(provider, base_url, account_id)
        with self._lock:
            self._vault.delete(key)
            self._record_cache.pop(key, None)

        self._audit.record(
            AuditEventType.CREDENTIAL_DELETE,
            severity=AuditSeverity.INFO,
            action="revoke_token",
            message=(
                f"Revoked credential for provider "
                f"'{_provider_value(provider)}'."
            ),
            payload={
                "provider": _provider_value(provider),
                "account_id": account_id,
            },
            actor="system",
        )

    # --- Extra operations ----------------------------------------------

    def list_stored(self) -> Sequence[TokenInfo]:
        """
        Return metadata for every token pair known to the service.

        Only accounts that have been stored since the service started
        are listed. This is a best-effort view of the in-memory cache;
        to enumerate the vault directly, use
        `CredentialVault.keys()`.

        No token values are ever returned.
        """
        with self._lock:
            items = list(self._record_cache.items())

        results: List[TokenInfo] = []
        for vault_key, record in items:
            parsed = _parse_vault_key(vault_key)
            if parsed is None:
                continue
            provider_value, base_url, account_id = parsed
            try:
                provider = ProviderId(provider_value)
            except ValueError:
                continue
            results.append(
                TokenInfo(
                    provider=provider,
                    account_id=account_id,
                    base_url=base_url,
                    expires_at=record.expires_at,
                    scopes=tuple(record.scopes),
                    has_refresh_token=bool(record.refresh_token),
                    last_refreshed_at=record.last_refreshed_at,
                )
            )
        results.sort(
            key=lambda info: (
                info.provider.value,
                info.base_url,
                info.account_id,
            )
        )
        return tuple(results)

    def clear_cache(self) -> None:
        """
        Drop the in-memory parsed-record cache.

        Useful in tests and after external modifications to the
        vault. Does NOT touch the vault itself.
        """
        with self._lock:
            self._record_cache.clear()

    # --- Internal helpers ----------------------------------------------

    def _read_record(
        self,
        provider: ProviderId,
        account_id: str,
        base_url: str,
    ) -> Optional[_TokenRecord]:
        """
        Read and cache the `_TokenRecord` for the given identity.

        Corrupt or unreadable entries are discarded and reported via
        `None`. This method never raises for missing data.
        """
        if not account_id or not base_url:
            return None
        key = _vault_key(provider, base_url, account_id)

        with self._lock:
            cached = self._record_cache.get(key)
            if cached is not None:
                return cached

        raw = self._vault.get(key)
        if raw is None:
            return None

        record = _deserialize_record(raw)
        if record is None:
            logger.warning(
                "Discarding corrupt credential entry for provider '%s'.",
                _provider_value(provider),
            )
            with self._lock:
                self._vault.delete(key)
                self._record_cache.pop(key, None)
            return None

        with self._lock:
            self._record_cache[key] = record
        return record


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _vault_key(
    provider: ProviderId, base_url: str, account_id: str
) -> str:
    """
    Build the canonical vault key for a token pair.

    The key format matches `make_vault_key` from `ports.auth` so that
    other components can address the same entry without duplicating
    the format string.
    """
    return make_vault_key(
        provider,
        base_url or "default",
        account_id,
        kind="oauth",
    )


def _parse_vault_key(
    key: str,
) -> Optional[Tuple[str, str, str]]:
    """
    Reverse the key format produced by `_vault_key`.

    Returns `(provider_value, base_url, account_id)` or `None` if the
    key does not match the expected shape. The account ID may itself
    contain dots, so the parser consumes the remainder of the string
    as the account ID.
    """
    prefix = "peldrun.sc.oauth."
    if not key.startswith(prefix):
        return None
    remainder = key[len(prefix):]
    parts = remainder.split(".", 2)
    if len(parts) < 3:
        return None
    provider_value, base_url, account_id = parts
    return provider_value, base_url, account_id


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
# Default service singleton
# ---------------------------------------------------------------------------


_DEFAULT_SERVICE: Optional[TokenService] = None
_DEFAULT_LOCK = threading.RLock()


def get_default_token_service() -> TokenService:
    """
    Return the process-wide default token service, creating it on
    first use.
    """
    global _DEFAULT_SERVICE
    with _DEFAULT_LOCK:
        if _DEFAULT_SERVICE is None:
            _DEFAULT_SERVICE = TokenService()
        return _DEFAULT_SERVICE


def set_default_token_service(service: TokenService) -> None:
    """
    Replace the process-wide default token service.

    Intended for application startup and for tests.
    """
    if not isinstance(service, TokenService):
        raise TypeError(
            "set_default_token_service expects a TokenService instance."
        )
    global _DEFAULT_SERVICE
    with _DEFAULT_LOCK:
        _DEFAULT_SERVICE = service


def reset_default_token_service(
    vault: Optional[CredentialVault] = None,
) -> TokenService:
    """
    Replace the default service with a fresh one.

    Used by tests to guarantee isolation. When `vault` is provided,
    the fresh service uses it; otherwise, it uses the current default
    vault.
    """
    global _DEFAULT_SERVICE
    with _DEFAULT_LOCK:
        _DEFAULT_SERVICE = TokenService(vault=vault)
        return _DEFAULT_SERVICE
