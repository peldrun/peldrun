import sys, asyncio
sys.path.insert(0, "backend")
from backend.integrations.source_control.operations.service import OperationService
from backend.integrations.source_control.operations.journal import InMemoryOperationJournal
from backend.integrations.source_control.domain.enums import OperationStatus

async def main():
    journal = InMemoryOperationJournal()
    service = OperationService(journal=journal)

    # path 1: نجاح
    op1 = await service.create_operation(repository_id="repo_t", kind="push_branch")
    failed = await service.mark_failed(
        op1.operation_id,
        error_code="NETWORK_ERROR",
        error_message="Connection reset",
        retryable=True,
    )
    assert failed.status == OperationStatus.FAILED_RETRYABLE
    assert failed.error_code == "NETWORK_ERROR"
    assert failed.error_message == "Connection reset"
    print("mark_failed success: OK")

    # path 2: عملية نهائية — يجب ألا يحفظ error_code
    op2 = await service.create_operation(repository_id="repo_t", kind="push_branch")
    for state in [
        OperationStatus.REPOSITORY_RESOLVED,
        OperationStatus.WORKSPACE_READY,
        OperationStatus.EDITING,
        OperationStatus.CHECKING,
        OperationStatus.REVIEW_REQUIRED,
        OperationStatus.REJECTED,
    ]:
        await service.transition(op2.operation_id, state, actor="system")

    try:
        await service.mark_failed(
            op2.operation_id,
            error_code="SHOULD_NOT_PERSIST",
            error_message="should not persist",
            retryable=True,
        )
        print("FAIL: should have raised")
    except Exception as exc:
        record = await service.get_operation(op2.operation_id)
        assert record.error_code != "SHOULD_NOT_PERSIST", \
            f"BUG: error_code persisted: {record.error_code}"
        assert record.status == OperationStatus.REJECTED
        print(f"mark_failed on terminal: {type(exc).__name__} OK")
        print(f"  error_code = {record.error_code!r} (unchanged)")

    # path 3: extra_fields تجاوز محظور
    try:
        await service._transition_internal(
            op1.operation_id,
            OperationStatus.FAILED_RETRYABLE,
            extra_fields={"status": OperationStatus.CHANGED},
        )
        print("FAIL: should have raised on extra_fields['status']")
    except Exception as exc:
        print(f"extra_fields override rejected: {type(exc).__name__} OK")

asyncio.run(main())
