from __future__ import annotations

from .idempotency import (
    IdempotencyCheckResult,
    IdempotencyManager,
    compute_request_hash,
    get_default_idempotency_manager,
    reset_default_idempotency_manager,
    set_default_idempotency_manager,
)
from .journal import (
    InMemoryOperationJournal,
    JournalError,
    OperationJournal,
    SQLiteOperationJournal,
    get_default_operation_journal,
    reset_default_operation_journal,
    set_default_operation_journal,
)
from .models import (
    VALID_TRANSITIONS,
    IdempotencyRecord,
    OperationEvent,
    OperationRecord,
)
from .recovery import (
    RecoveryActionKind,
    RecoveryEngine,
    RecoveryReport,
    get_default_recovery_engine,
    reset_default_recovery_engine,
    set_default_recovery_engine,
)
from .service import (
    OperationService,
    get_default_operation_service,
    reset_default_operation_service,
    set_default_operation_service,
)

__all__ = [
    # Durable Operation and Transition Models
    "VALID_TRANSITIONS",
    "OperationRecord",
    "OperationEvent",
    "IdempotencyRecord",
    # Operation Journal Persistence
    "OperationJournal",
    "InMemoryOperationJournal",
    "SQLiteOperationJournal",
    "JournalError",
    "get_default_operation_journal",
    "set_default_operation_journal",
    "reset_default_operation_journal",
    # Idempotency and Deduplication Engine
    "compute_request_hash",
    "IdempotencyCheckResult",
    "IdempotencyManager",
    "get_default_idempotency_manager",
    "set_default_idempotency_manager",
    "reset_default_idempotency_manager",
    # Crash Recovery and Remote Reconciliation
    "RecoveryActionKind",
    "RecoveryReport",
    "RecoveryEngine",
    "get_default_recovery_engine",
    "set_default_recovery_engine",
    "reset_default_recovery_engine",
    # Operation Lifecycle Orchestration Service
    "OperationService",
    "get_default_operation_service",
    "set_default_operation_service",
    "reset_default_operation_service",
]
