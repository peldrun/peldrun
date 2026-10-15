from __future__ import annotations

import hashlib
import json
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Mapping, Optional

from ..errors import ConflictError, ValidationError
from .journal import OperationJournal, get_default_operation_journal
from .models import IdempotencyRecord


def compute_request_hash(payload: Any) -> str:
    """Computes a deterministic SHA-256 fingerprint for arbitrary mutation payloads."""
    if payload is None:
        return hashlib.sha256(b"").hexdigest()

    if isinstance(payload, bytes):
        raw_bytes = payload
    elif isinstance(payload, str):
        raw_bytes = payload.encode("utf-8")
    elif isinstance(payload, Mapping):
        # Canonical deterministic JSON sorting
        serialized = json.dumps(payload, sort_keys=True, ensure_ascii=False)
        raw_bytes = serialized.encode("utf-8")
    else:
        raw_bytes = str(payload).encode("utf-8")

    return hashlib.sha256(raw_bytes).hexdigest()


@dataclass(frozen=True)
class IdempotencyCheckResult:
    """Outcome of evaluating an idempotency key prior to mutation execution."""

    is_new: bool
    is_completed: bool
    record: IdempotencyRecord
    cached_response: Optional[dict[str, Any]] = None


class IdempotencyManager:
    """Thread-safe manager coordinating mutation deduplication, request fingerprinting, and response replay."""

    def __init__(
        self,
        journal: Optional[OperationJournal] = None,
        default_ttl_minutes: int = 1440,  # 24 hours
    ) -> None:
        self._journal = journal
        self.default_ttl_minutes = default_ttl_minutes
        self._lock = threading.Lock()

    @property
    def journal(self) -> OperationJournal:
        if self._journal is None:
            self._journal = get_default_operation_journal()
        return self._journal

    @staticmethod
    def generate_key(
        operation_id: str, action_type: str, disambiguator: str = ""
    ) -> str:
        """Generates a stable, scoped idempotency key."""
        clean_op = operation_id.strip()
        clean_act = action_type.strip().lower()
        if disambiguator:
            clean_dis = disambiguator.strip()
            return f"sc_idem:{clean_op}:{clean_act}:{clean_dis}"
        return f"sc_idem:{clean_op}:{clean_act}"

    def check_or_reserve(
        self,
        idempotency_key: str,
        operation_id: str,
        action_type: str,
        request_payload: Any,
        ttl_minutes: Optional[int] = None,
    ) -> IdempotencyCheckResult:
        """Atomically checks an idempotency key or reserves it if new.

        Raises:
            ValidationError: If key or payload is invalid.
            ConflictError: If the key was previously used with a different request payload.
        """
        if not idempotency_key or not idempotency_key.strip():
            raise ValidationError("Idempotency key cannot be empty.")

        request_hash = compute_request_hash(request_payload)
        now = datetime.utcnow()
        ttl = ttl_minutes if ttl_minutes is not None else self.default_ttl_minutes
        expires_at = now + timedelta(minutes=ttl)

        with self._lock:
            existing = self.journal.get_idempotency_record(idempotency_key)

            if existing is not None:
                # Check expiration
                if existing.expires_at is not None and now >= existing.expires_at:
                    # Expired record can be replaced
                    new_record = IdempotencyRecord(
                        idempotency_key=idempotency_key,
                        operation_id=operation_id,
                        action_type=action_type,
                        request_hash=request_hash,
                        response_payload=None,
                        created_at=now,
                        expires_at=expires_at,
                    )
                    self.journal.save_idempotency_record(new_record)
                    return IdempotencyCheckResult(
                        is_new=True,
                        is_completed=False,
                        record=new_record,
                    )

                # Validate payload fingerprint
                if existing.request_hash != request_hash:
                    raise ConflictError(
                        f"Idempotency key '{idempotency_key}' was already registered with a "
                        "different request fingerprint. Mutating payload under identical key is forbidden.",
                        details={
                            "idempotency_key": idempotency_key,
                            "existing_hash": existing.request_hash,
                            "submitted_hash": request_hash,
                        },
                    )

                # If completed with payload, return cached response
                if existing.response_payload is not None:
                    return IdempotencyCheckResult(
                        is_new=False,
                        is_completed=True,
                        record=existing,
                        cached_response=dict(existing.response_payload),
                    )

                # Still in-flight or timed out ambiguously
                return IdempotencyCheckResult(
                    is_new=False,
                    is_completed=False,
                    record=existing,
                )

            # Completely new record
            record = IdempotencyRecord(
                idempotency_key=idempotency_key,
                operation_id=operation_id,
                action_type=action_type,
                request_hash=request_hash,
                response_payload=None,
                created_at=now,
                expires_at=expires_at,
            )
            self.journal.save_idempotency_record(record)
            return IdempotencyCheckResult(
                is_new=True,
                is_completed=False,
                record=record,
            )

    def complete(
        self,
        idempotency_key: str,
        response_payload: Mapping[str, Any],
    ) -> IdempotencyRecord:
        """Stores the response payload for a reserved idempotency record upon successful mutation."""
        now = datetime.utcnow()
        with self._lock:
            existing = self.journal.get_idempotency_record(idempotency_key)
            if existing is None:
                raise ValidationError(
                    f"Cannot complete unreserved idempotency key '{idempotency_key}'."
                )

            updated = IdempotencyRecord(
                idempotency_key=existing.idempotency_key,
                operation_id=existing.operation_id,
                action_type=existing.action_type,
                request_hash=existing.request_hash,
                response_payload=dict(response_payload),
                created_at=existing.created_at,
                expires_at=existing.expires_at,
            )
            self.journal.save_idempotency_record(updated)
            return updated


# ============================================================
# Default IdempotencyManager Singleton Accessors
# ============================================================

_DEFAULT_IDEMPOTENCY_MANAGER: Optional[IdempotencyManager] = None
_IDEM_LOCK = threading.Lock()


def get_default_idempotency_manager(
    journal: Optional[OperationJournal] = None,
) -> IdempotencyManager:
    """Returns the process-wide default IdempotencyManager singleton."""
    global _DEFAULT_IDEMPOTENCY_MANAGER
    if _DEFAULT_IDEMPOTENCY_MANAGER is not None:
        return _DEFAULT_IDEMPOTENCY_MANAGER

    with _IDEM_LOCK:
        if _DEFAULT_IDEMPOTENCY_MANAGER is not None:
            return _DEFAULT_IDEMPOTENCY_MANAGER

        _DEFAULT_IDEMPOTENCY_MANAGER = IdempotencyManager(journal=journal)
        return _DEFAULT_IDEMPOTENCY_MANAGER


def set_default_idempotency_manager(manager: IdempotencyManager) -> None:
    """Explicitly injects an IdempotencyManager instance (used during testing)."""
    global _DEFAULT_IDEMPOTENCY_MANAGER
    with _IDEM_LOCK:
        _DEFAULT_IDEMPOTENCY_MANAGER = manager


def reset_default_idempotency_manager() -> None:
    """Resets the process-wide default IdempotencyManager (used during testing)."""
    global _DEFAULT_IDEMPOTENCY_MANAGER
    with _IDEM_LOCK:
        _DEFAULT_IDEMPOTENCY_MANAGER = None
