# backend/integrations/source_control/operations/idempotency.py
"""
Idempotency tracking for the Source Control Integration subsystem.

The idempotency registry prevents the same logical operation from
being executed twice, even if the caller retries after a transient
failure or an ambiguous timeout.

The registry is keyed by a deterministic `IdempotencyKey` derived
from the operation's parameters. The key is a SHA-256 digest of a
canonical JSON representation, which means:

    - The same inputs always produce the same key.
    - The key reveals nothing about the inputs (no secrets in the
      digest).
    - The key is a fixed 64-character hex string, safe to store in
      logs, in the journal, and in the UI.

The registry tracks a small state machine per key:

    IN_PROGRESS -> SUCCEEDED
                -> FAILED
                -> AMBIGUOUS -> SUCCEEDED | FAILED

Design rules:
    - `register` MUST be called before the operation starts. It
      refuses to proceed when the key is already in `IN_PROGRESS` or
      `SUCCEEDED`.
    - `lookup` never raises. It returns `None` for unknown keys.
    - Entries expire after `TTL_SECONDS` (default: 7 days) and are
      pruned automatically when the registry is written to.
    - The registry MUST NOT store secrets. Only identifiers and
      hashes are retained.
    - The registry is thread-safe. All mutations hold a reentrant
      lock.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Mapping, Optional, Sequence

from ..domain.enums import OperationStatus, ProviderId
from ..errors import ConflictError, ValidationError
from .models import _utc_now

__all__ = [
    "IdempotencyKey",
    "IdempotencyEntry",
    "IdempotencyOutcome",
    "IdempotencyRegistry",
    "IdempotencyManager",
    "IdempotencyCheckResult",
    "compute_key",
    "generate_key_from_operation",
    "compute_request_hash",
    "get_default_idempotency_registry",
    "set_default_idempotency_registry",
    "reset_default_idempotency_registry",
    "get_default_idempotency_manager",
    "set_default_idempotency_manager",
    "reset_default_idempotency_manager",
    "TTL_SECONDS",
    "MAX_ENTRIES",
]


logger = logging.getLogger(__name__)


#: How long an entry remains valid. After this window, the same
#: logical operation MAY be retried. Seven days is long enough to
#: cover realistic user sessions and short enough to bound the
#: registry's growth.
TTL_SECONDS: int = 7 * 24 * 60 * 60

#: Maximum number of entries retained in memory. When the cap is
#: exceeded, the oldest entries are evicted.
MAX_ENTRIES: int = 10_000


# ---------------------------------------------------------------------------
# Outcome enum
# ---------------------------------------------------------------------------


class IdempotencyOutcome(str):
    """
    Lifecycle state of an idempotency entry.

    Defined as a `str` subclass so that it serializes transparently
    and compares to literal strings without importing this module.
    """

    IN_PROGRESS = "in_progress"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    AMBIGUOUS = "ambiguous"


# ---------------------------------------------------------------------------
# Key
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class IdempotencyKey:
    """
    Deterministic identifier for a logical operation.

    The `value` field is a 64-character lowercase hex digest. It is
    computed from the canonical JSON of `fields`. Two `IdempotencyKey`
    instances are equal when their `value` matches, regardless of the
    exact shape of `fields`.

    Fields:
        value:  The SHA-256 digest.
        kind:   Operation kind (e.g. `"push_branch"`).
        provider: Provider identifier, when applicable.
        repository_id: Repository identifier, when applicable.
    """

    value: str
    kind: str
    provider: Optional[ProviderId] = None
    repository_id: Optional[str] = None

    def __post_init__(self) -> None:
        if not self.value or len(self.value) != 64:
            raise ValidationError(
                "IdempotencyKey.value must be a 64-character hex digest.",
                details={"length": len(self.value or "")},
            )
        if not self.kind:
            raise ValidationError("IdempotencyKey.kind must not be empty.")

    def to_dict(self) -> Dict[str, Any]:
        """Serialize for logs and API responses (no secrets)."""
        return {
            "value": self.value,
            "kind": self.kind,
            "provider": self.provider.value if self.provider else None,
            "repository_id": self.repository_id,
        }


def compute_key(
    *,
    kind: str,
    provider: Optional[ProviderId] = None,
    repository_id: Optional[str] = None,
    source_branch: Optional[str] = None,
    target_branch: Optional[str] = None,
    base_sha: Optional[str] = None,
    diff_sha256: Optional[str] = None,
    extra: Optional[Mapping[str, Any]] = None,
) -> IdempotencyKey:
    """
    Compute an `IdempotencyKey` from the given fields.

    The digest is `SHA-256(canonical_json({fields}))`. Fields that
    are `None` are omitted, so that callers may supply only the
    parameters relevant to their operation.

    The function is pure and deterministic. Calling it twice with the
    same arguments produces the same key, on every platform and
    Python version.

    Args:
        kind: Operation kind. Required and non-empty.
        provider: Provider identifier.
        repository_id: Repository identifier.
        source_branch: Source branch name.
        target_branch: Target branch name.
        base_sha: Base commit SHA.
        diff_sha256: Canonical diff hash.
        extra: Optional mapping of additional fields to include.

    Raises:
        ValidationError: If `kind` is empty, or if `extra` contains
            non-serializable values.
    """
    if not kind or not isinstance(kind, str):
        raise ValidationError("kind must be a non-empty string.")

    payload: Dict[str, Any] = {"kind": kind.strip()}
    if provider is not None:
        payload["provider"] = (
            provider.value if isinstance(provider, ProviderId) else str(provider)
        )
    if repository_id:
        payload["repository_id"] = str(repository_id)
    if source_branch:
        payload["source_branch"] = str(source_branch)
    if target_branch:
        payload["target_branch"] = str(target_branch)
    if base_sha:
        payload["base_sha"] = str(base_sha)
    if diff_sha256:
        payload["diff_sha256"] = str(diff_sha256)
    if extra:
        for key, value in extra.items():
            if not isinstance(key, str):
                raise ValidationError(
                    "extra keys must be strings.",
                    details={"type": type(key).__name__},
                )
            payload[key] = value

    try:
        canonical = json.dumps(
            payload, sort_keys=True, separators=(",", ":"), default=str
        )
    except (TypeError, ValueError) as exc:
        raise ValidationError(
            "Failed to serialize idempotency payload.",
            details={"reason": type(exc).__name__},
        ) from exc

    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return IdempotencyKey(
        value=digest,
        kind=payload["kind"],
        provider=provider if isinstance(provider, ProviderId) else None,
        repository_id=repository_id,
    )


def generate_key_from_operation(operation: Any) -> IdempotencyKey:
    """
    Build an `IdempotencyKey` from an `OperationRecord`.

    Convenience helper for the operation service. The operation's
    `kind`, `provider`, `repository_id`, `source_branch`,
    `target_branch`, and `diff_sha256` are used. `base_sha` is
    included when present.
    """
    kind = getattr(operation, "kind", None)
    if not kind:
        raise ValidationError(
            "Operation is missing a kind and cannot be keyed."
        )
    return compute_key(
        kind=str(kind),
        provider=getattr(operation, "provider", None),
        repository_id=getattr(operation, "repository_id", None),
        source_branch=getattr(operation, "source_branch", None),
        target_branch=getattr(operation, "target_branch", None),
        base_sha=getattr(operation, "base_sha", None) or None,
        diff_sha256=getattr(operation, "diff_sha256", None) or None,
    )


# ---------------------------------------------------------------------------
# Entry
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class IdempotencyEntry:
    """
    A record of one idempotency key.

    Fields:
        key:          The `IdempotencyKey`.
        operation_id: The operation that produced this entry.
        outcome:      Current outcome.
        created_at:   When the entry was created.
        updated_at:   When the entry was last updated.
        expires_at:   When the entry should be considered stale.
        remote_ref:   Optional remote reference produced by the
                      operation (e.g. a change request URL). MUST NOT
                      contain secrets.
        error_code:   Optional error code when the outcome is
                      `FAILED`.
        message:      Optional human-readable detail.
    """

    key: IdempotencyKey
    operation_id: str
    outcome: str = IdempotencyOutcome.IN_PROGRESS
    created_at: datetime = field(default_factory=_utc_now)
    updated_at: datetime = field(default_factory=_utc_now)
    expires_at: Optional[datetime] = None
    remote_ref: Optional[str] = None
    error_code: Optional[str] = None
    message: Optional[str] = None

    def is_expired(self, now: Optional[datetime] = None) -> bool:
        """Return True when the entry has passed its TTL."""
        if self.expires_at is None:
            return False
        moment = now or _utc_now()
        return moment >= self.expires_at

    def is_terminal(self) -> bool:
        """Return True when the outcome is `SUCCEEDED` or `FAILED`."""
        return self.outcome in (
            IdempotencyOutcome.SUCCEEDED,
            IdempotencyOutcome.FAILED,
        )

    def to_dict(self) -> Dict[str, Any]:
        """Serialize for logs and API responses (no secrets)."""
        return {
            "key": self.key.to_dict(),
            "operation_id": self.operation_id,
            "outcome": self.outcome,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "expires_at": (
                self.expires_at.isoformat() if self.expires_at else None
            ),
            "remote_ref": self.remote_ref,
            "error_code": self.error_code,
            "message": self.message,
        }


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


class IdempotencyRegistry:
    """
    Thread-safe registry of idempotency entries.

    The registry holds every live entry in memory and enforces the
    TTL and the capacity constraints. It does not persist entries to
    disk; durability is provided by the operation journal, which the
    application layer consults during recovery.
    """

    def __init__(
        self,
        *,
        ttl_seconds: int = TTL_SECONDS,
        max_entries: int = MAX_ENTRIES,
    ) -> None:
        if ttl_seconds <= 0:
            raise ValidationError("ttl_seconds must be positive.")
        if max_entries <= 0:
            raise ValidationError("max_entries must be positive.")
        self._entries: Dict[str, IdempotencyEntry] = {}
        self._lock = threading.RLock()
        self._ttl_seconds = int(ttl_seconds)
        self._max_entries = int(max_entries)

    # --- Registration ---------------------------------------------------

    def register(
        self,
        key: IdempotencyKey,
        operation_id: str,
        *,
        force: bool = False,
    ) -> IdempotencyEntry:
        """
        Claim an idempotency key for a new operation.

        Creates an entry with outcome `IN_PROGRESS`. Refuses to
        proceed when the key already exists in `IN_PROGRESS` or
        `SUCCEEDED`, unless `force=True` is passed.

        Args:
            key: The idempotency key.
            operation_id: The operation that will perform the work.
            force: When True, an existing entry is overwritten. Use
                with caution; `force` is intended for tests and for
                explicit user-driven retries after a failure.

        Raises:
            ValidationError: If inputs are malformed.
            ConflictError: If the key already exists and `force` is
                False.
        """
        if not isinstance(key, IdempotencyKey):
            raise ValidationError(
                "key must be an IdempotencyKey.",
                details={"type": type(key).__name__},
            )
        if not operation_id or not isinstance(operation_id, str):
            raise ValidationError("operation_id must be a non-empty string.")

        now = _utc_now()
        expires_at = now + timedelta(seconds=self._ttl_seconds)

        with self._lock:
            self._prune_locked(now)

            existing = self._entries.get(key.value)
            if existing is not None and not force:
                if existing.outcome in (
                    IdempotencyOutcome.IN_PROGRESS,
                    IdempotencyOutcome.SUCCEEDED,
                ):
                    raise ConflictError(
                        "An operation with the same idempotency key "
                        "already exists.",
                        details={
                            "key": key.value,
                            "existing_operation_id": existing.operation_id,
                            "existing_outcome": existing.outcome,
                        },
                    )
                # An entry with outcome FAILED or AMBIGUOUS may be
                # replaced only when explicitly forced. We refuse
                # AMBIGUOUS without force because the state is
                # genuinely unknown.
                if existing.outcome == IdempotencyOutcome.AMBIGUOUS:
                    raise ConflictError(
                        "The idempotency key is in an ambiguous state. "
                        "Reconcile the previous operation before "
                        "retrying.",
                        details={
                            "key": key.value,
                            "existing_operation_id": existing.operation_id,
                        },
                    )

            entry = IdempotencyEntry(
                key=key,
                operation_id=operation_id,
                outcome=IdempotencyOutcome.IN_PROGRESS,
                created_at=now,
                updated_at=now,
                expires_at=expires_at,
            )
            self._entries[key.value] = entry
            self._enforce_capacity_locked()

        return entry

    # --- Lookup ---------------------------------------------------------

    def lookup(self, key: IdempotencyKey) -> Optional[IdempotencyEntry]:
        """
        Return the entry for `key`, or `None` when absent.

        Expired entries are treated as absent.
        """
        if not isinstance(key, IdempotencyKey):
            return None
        now = _utc_now()
        with self._lock:
            entry = self._entries.get(key.value)
            if entry is None:
                return None
            if entry.is_expired(now):
                return None
            return entry

    def is_duplicate(self, key: IdempotencyKey) -> bool:
        """
        Return True when the key has already been claimed.

        A key is considered a duplicate when an entry exists with
        outcome `IN_PROGRESS` or `SUCCEEDED`. `FAILED` and `AMBIGUOUS`
        entries are NOT considered duplicates; the caller is expected
        to reconcile or retry.
        """
        entry = self.lookup(key)
        if entry is None:
            return False
        return entry.outcome in (
            IdempotencyOutcome.IN_PROGRESS,
            IdempotencyOutcome.SUCCEEDED,
        )

    # --- Outcome updates ------------------------------------------------

    def complete_success(
        self,
        key: IdempotencyKey,
        *,
        remote_ref: Optional[str] = None,
        message: Optional[str] = None,
    ) -> IdempotencyEntry:
        """
        Mark the entry as `SUCCEEDED`.

        `remote_ref` carries a provider-neutral reference to the
        remote artifact produced by the operation, such as a change
        request URL. It MUST NOT contain secrets.
        """
        return self._set_outcome(
            key,
            outcome=IdempotencyOutcome.SUCCEEDED,
            remote_ref=remote_ref,
            message=message,
        )

    def complete_failure(
        self,
        key: IdempotencyKey,
        *,
        error_code: Optional[str] = None,
        message: Optional[str] = None,
    ) -> IdempotencyEntry:
        """Mark the entry as `FAILED`."""
        return self._set_outcome(
            key,
            outcome=IdempotencyOutcome.FAILED,
            error_code=error_code,
            message=message,
        )

    def mark_ambiguous(
        self,
        key: IdempotencyKey,
        *,
        message: Optional[str] = None,
    ) -> IdempotencyEntry:
        """
        Mark the entry as `AMBIGUOUS`.

        Used when the caller cannot determine whether the remote
        operation succeeded, for example after a timeout. An
        ambiguous entry blocks new registrations for the same key
        until it is reconciled via `complete_success` or
        `complete_failure`.
        """
        return self._set_outcome(
            key,
            outcome=IdempotencyOutcome.AMBIGUOUS,
            message=message,
        )

    # --- Maintenance ----------------------------------------------------

    def prune_expired(self) -> int:
        """
        Remove every expired entry. Returns the number removed.
        """
        now = _utc_now()
        with self._lock:
            return self._prune_locked(now)

    def clear(self) -> None:
        """Remove every entry. Intended for tests."""
        with self._lock:
            self._entries.clear()

    def list_recent(
        self, *, limit: int = 100
    ) -> Sequence[IdempotencyEntry]:
        """
        Return the most recently updated entries, newest first.
        """
        if limit <= 0:
            return ()
        with self._lock:
            entries = list(self._entries.values())
        entries.sort(key=lambda e: e.updated_at, reverse=True)
        return tuple(entries[:limit])

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)

    def __contains__(self, key: object) -> bool:
        if not isinstance(key, IdempotencyKey):
            return False
        return self.lookup(key) is not None

    # --- Internal -------------------------------------------------------

    def _set_outcome(
        self,
        key: IdempotencyKey,
        *,
        outcome: str,
        remote_ref: Optional[str] = None,
        error_code: Optional[str] = None,
        message: Optional[str] = None,
    ) -> IdempotencyEntry:
        if not isinstance(key, IdempotencyKey):
            raise ValidationError("key must be an IdempotencyKey.")

        now = _utc_now()
        with self._lock:
            entry = self._entries.get(key.value)
            if entry is None:
                raise ValidationError(
                    "No idempotency entry exists for the given key.",
                    details={"key": key.value},
                )
            if entry.is_terminal() and not entry.is_expired(now):
                # Allow transition from AMBIGUOUS, but refuse to
                # flip a terminal outcome to a different terminal
                # outcome.
                if outcome != entry.outcome:
                    raise ConflictError(
                        "The idempotency entry is already in a "
                        "terminal state.",
                        details={
                            "key": key.value,
                            "current_outcome": entry.outcome,
                            "requested_outcome": outcome,
                        },
                    )
            updated = replace(
                entry,
                outcome=outcome,
                updated_at=now,
                remote_ref=remote_ref or entry.remote_ref,
                error_code=error_code or entry.error_code,
                message=message or entry.message,
            )
            self._entries[key.value] = updated
            return updated

    def _prune_locked(self, now: datetime) -> int:
        """
        Remove expired entries. Caller MUST hold the lock.
        """
        stale = [
            key
            for key, entry in self._entries.items()
            if entry.is_expired(now)
        ]
        for key in stale:
            self._entries.pop(key, None)
        return len(stale)

    def _enforce_capacity_locked(self) -> None:
        """
        Bound the number of entries. Caller MUST hold the lock.

        When the cap is exceeded, the oldest entries are evicted.
        Entries with outcome `IN_PROGRESS` are preferred for
        eviction last, because removing them could allow a duplicate
        execution.
        """
        if len(self._entries) <= self._max_entries:
            return

        # Order candidates: terminal entries first (they are safe to
        # remove), then ambiguous, then in-progress last.
        priority = {
            IdempotencyOutcome.SUCCEEDED: 0,
            IdempotencyOutcome.FAILED: 0,
            IdempotencyOutcome.AMBIGUOUS: 1,
            IdempotencyOutcome.IN_PROGRESS: 2,
        }
        overflow = len(self._entries) - self._max_entries
        candidates = sorted(
            self._entries.items(),
            key=lambda item: (
                priority.get(item[1].outcome, 1),
                item[1].updated_at,
            ),
        )
        for key, _entry in candidates[:overflow]:
            self._entries.pop(key, None)


# ---------------------------------------------------------------------------
# Default registry singleton
# ---------------------------------------------------------------------------


_DEFAULT_REGISTRY: Optional[IdempotencyRegistry] = None
_DEFAULT_LOCK = threading.RLock()


def get_default_idempotency_registry() -> IdempotencyRegistry:
    """
    Return the process-wide default idempotency registry, creating it
    on first use.
    """
    global _DEFAULT_REGISTRY
    with _DEFAULT_LOCK:
        if _DEFAULT_REGISTRY is None:
            _DEFAULT_REGISTRY = IdempotencyRegistry()
        return _DEFAULT_REGISTRY


def set_default_idempotency_registry(
    registry: IdempotencyRegistry,
) -> None:
    """
    Replace the process-wide default registry.

    Intended for application startup and for tests.
    """
    if not isinstance(registry, IdempotencyRegistry):
        raise TypeError(
            "set_default_idempotency_registry expects an "
            "IdempotencyRegistry."
        )
    global _DEFAULT_REGISTRY
    with _DEFAULT_LOCK:
        _DEFAULT_REGISTRY = registry


def reset_default_idempotency_registry() -> IdempotencyRegistry:
    """
    Replace the default registry with a fresh one.

    Used by tests to guarantee isolation between test cases.
    """
    global _DEFAULT_REGISTRY
    with _DEFAULT_LOCK:
        _DEFAULT_REGISTRY = IdempotencyRegistry()
        return _DEFAULT_REGISTRY

# ---------------------------------------------------------------------------
# Backward-compatibility aliases and helper functions
# ---------------------------------------------------------------------------
#
# The `operations/__init__.py` package exports a "manager"-flavoured
# vocabulary that predates this module. The names below map to the
# canonical classes and functions defined above; they exist so that
# callers using either vocabulary work without changes.

#: Alias for `IdempotencyRegistry`.
IdempotencyManager = IdempotencyRegistry

#: Alias for `get_default_idempotency_registry`.
get_default_idempotency_manager = get_default_idempotency_registry

#: Alias for `set_default_idempotency_registry`.
set_default_idempotency_manager = set_default_idempotency_registry

#: Alias for `reset_default_idempotency_registry`.
reset_default_idempotency_manager = reset_default_idempotency_registry


def compute_request_hash(
    *,
    kind: str,
    provider: Optional[ProviderId] = None,
    repository_id: Optional[str] = None,
    source_branch: Optional[str] = None,
    target_branch: Optional[str] = None,
    base_sha: Optional[str] = None,
    diff_sha256: Optional[str] = None,
    extra: Optional[Mapping[str, Any]] = None,
) -> str:
    """
    Return the 64-character hex digest for a request.

    Thin wrapper around `compute_key` that returns just the digest
    string instead of a full `IdempotencyKey`. Useful for callers
    that only need to persist or compare the hash.
    """
    return compute_key(
        kind=kind,
        provider=provider,
        repository_id=repository_id,
        source_branch=source_branch,
        target_branch=target_branch,
        base_sha=base_sha,
        diff_sha256=diff_sha256,
        extra=extra,
    ).value


@dataclass(frozen=True)
class IdempotencyCheckResult:
    """
    Outcome of checking whether a request is a duplicate.

    Fields:
        is_duplicate:    True when a live entry exists with an
                         outcome of `IN_PROGRESS` or `SUCCEEDED`.
        key:             The idempotency key that was checked. `None`
                         when the caller did not supply one.
        existing_entry:  The matching entry, when present. `None`
                         when the request is not a duplicate.
        reason:          Optional human-readable explanation of the
                         decision (useful for logs and the UI).
    """

    is_duplicate: bool
    key: Optional[IdempotencyKey] = None
    existing_entry: Optional[IdempotencyEntry] = None
    reason: Optional[str] = None
