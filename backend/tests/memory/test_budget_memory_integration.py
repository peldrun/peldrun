# backend/tests/memory/test_budget_memory_integration.py
"""
P1.5 integration tests for the memory section in the token budget controller.

Verifies that ``TokenBudgetController.fit_all`` correctly handles the
new ``memory_msgs`` parameter added under P1.5:

  * backward compatibility when ``memory_msgs`` is absent or empty,
  * merge of memory content into the first system message,
  * token accounting (``system_tokens`` and ``memory_tokens`` are
    disjoint slices; the merge separator contributes a small,
    bounded overhead to ``total_tokens``),
  * Phase-2 truncation when ``sections.memory`` is positive.

Runnable via:
    python -m pytest backend/tests/memory/test_budget_memory_integration.py -v

Also runnable without pytest:
    python backend/tests/memory/test_budget_memory_integration.py
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Standalone-run bootstrap
# ---------------------------------------------------------------------------
#
# This file must be runnable BOTH via pytest (which sets up sys.path
# correctly) AND via a direct ``python path/to/file.py`` invocation
# (used during P1.5 manual verification). When run directly, Python
# inserts the SCRIPT'S directory (``tests/memory/``) into sys.path,
# not the project root -- so ``import peldrun`` would fail with
# ModuleNotFoundError.
#
# The fix: when this file is executed as ``__main__`` (i.e. NOT
# imported by pytest), prepend the backend root (the ancestor at
# three levels up: memory -> tests -> backend) to sys.path.
#
# Why guards matter:
#   * ``__name__ == "__main__"`` -- only fires on direct execution,
#     never on pytest collection (which imports the module).
#   * The appended path is idempotent (``sys.path.insert`` with the
#     same path is a no-op at lookup time), so double-invocation in
#     the same interpreter is safe.
import sys as _sys_bootstrap
from pathlib import Path as _Path_bootstrap

if __name__ == "__main__":
    _BACKEND_ROOT = _Path_bootstrap(__file__).resolve().parents[2]
    if str(_BACKEND_ROOT) not in _sys_bootstrap.path:
        _sys_bootstrap.path.insert(0, str(_BACKEND_ROOT))

# --- Normal imports follow ----------------------------------------------

from peldrun.context.budget import (
    TokenBudgetController,
    count_messages_total,
)
from peldrun.context.schema import BudgetMetadata


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _populate_sections_from_ratios(meta: BudgetMetadata) -> BudgetMetadata:
    """Replicate what ``build_context_sidecar`` does before ``fit_all`` runs.

    Without this step, all section quotas are 0 (their Pydantic
    defaults), and ``fit_recent_conversation`` returns ``[]`` for
    every non-empty recent list. That is pre-existing behavior,
    unrelated to the P1.5 memory edit -- real code paths always
    populate sections from ratios x effective_limit before calling
    ``fit_all``. Test fixtures must do the same to exercise the
    pipeline the way production does.
    """
    eff = meta.effective_limit
    meta.sections.system = int(eff * meta.ratios.system / 100)
    meta.sections.summary = int(eff * meta.ratios.summary / 100)
    meta.sections.recent = int(eff * meta.ratios.recent / 100)
    meta.sections.execution = int(eff * meta.ratios.execution / 100)
    meta.sections.current = int(eff * meta.ratios.current / 100)
    # sections.memory stays at 0 in Phase 1 -- that is the point.
    return meta


def _fresh_meta() -> BudgetMetadata:
    return _populate_sections_from_ratios(BudgetMetadata())


SYS_MSGS = [
    {"role": "system", "content": "You are a helpful assistant."},
]
SUMMARY_MSGS = [
    {"role": "system", "content": "Earlier: discussed Python."},
]
RECENT_MSGS = [
    {"role": "user", "content": "Hi"},
    {"role": "assistant", "content": "Hello!"},
]
EXEC_MSGS = []
CUR_MSGS = [{"role": "user", "content": "What next?"}]
MEM_MSGS = [
    {
        "role": "system",
        "content": "[RETRIEVED MEMORY]\n1. User prefers dark mode.",
    },
]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_backward_compat_no_memory_msgs() -> None:
    """fit_all without memory_msgs matches the pre-P1.5 behavior exactly."""
    ctrl = TokenBudgetController(_fresh_meta())
    combined, usage = ctrl.fit_all(
        system_msgs=SYS_MSGS,
        summary_msgs=SUMMARY_MSGS,
        recent_msgs=RECENT_MSGS,
        exec_msgs=EXEC_MSGS,
        current_msgs=CUR_MSGS,
    )
    assert usage["memory_tokens"] == 0
    assert len(combined) == 5, [m["role"] for m in combined]
    assert combined[0]["content"] == "You are a helpful assistant."
    assert "[RETRIEVED MEMORY]" not in combined[0]["content"]


def test_memory_msgs_merged_into_first_system_message() -> None:
    """Memory content merges into message 0, not a separate system message."""
    ctrl = TokenBudgetController(_fresh_meta())
    combined, usage = ctrl.fit_all(
        system_msgs=SYS_MSGS,
        summary_msgs=SUMMARY_MSGS,
        recent_msgs=RECENT_MSGS,
        exec_msgs=EXEC_MSGS,
        current_msgs=CUR_MSGS,
        memory_msgs=MEM_MSGS,
    )
    assert usage["memory_tokens"] > 0
    # Merge keeps the message count identical to the no-memory case.
    assert len(combined) == 5, [m["role"] for m in combined]
    # Both parts land in the FIRST system message.
    first = combined[0]
    assert first["role"] == "system"
    assert "You are a helpful assistant." in first["content"]
    assert "[RETRIEVED MEMORY]" in first["content"]
    assert "User prefers dark mode." in first["content"]
    # There is only one system message in the whole list after the merge.
    # The summary is also a "system" role, so count that explicitly.
    role_sequence = [m["role"] for m in combined]
    # system (base+memory), system (summary), user, assistant, user
    assert role_sequence == ["system", "system", "user", "assistant", "user"]


def test_empty_memory_msgs_equivalent_to_none() -> None:
    """Passing memory_msgs=[] must not change output vs. omitting the arg."""
    ctrl = TokenBudgetController(_fresh_meta())
    combined, usage = ctrl.fit_all(
        system_msgs=SYS_MSGS,
        summary_msgs=SUMMARY_MSGS,
        recent_msgs=RECENT_MSGS,
        exec_msgs=EXEC_MSGS,
        current_msgs=CUR_MSGS,
        memory_msgs=[],
    )
    assert usage["memory_tokens"] == 0
    assert combined[0]["content"] == "You are a helpful assistant."


def test_memory_without_base_system_message() -> None:
    """When there is no base system message, memory forms a system message alone.

    Expected message composition (no base system, no summary, no exec):
      * 1 system message = the memory block itself
      * 2 recent messages = RECENT_MSGS
      * 1 current message = CUR_MSGS
    Total = 4 messages.
    """
    ctrl = TokenBudgetController(_fresh_meta())
    combined, _usage = ctrl.fit_all(
        system_msgs=[],
        summary_msgs=[],
        recent_msgs=RECENT_MSGS,
        exec_msgs=[],
        current_msgs=CUR_MSGS,
        memory_msgs=MEM_MSGS,
    )
    roles = [m["role"] for m in combined]
    assert len(combined) == 4, roles
    assert roles == ["system", "user", "assistant", "user"], roles
    assert combined[0]["role"] == "system"
    assert "[RETRIEVED MEMORY]" in combined[0]["content"]


def test_system_tokens_excludes_memory_tokens() -> None:
    """system_tokens and memory_tokens are DISJOINT (accounting invariant)."""
    ctrl = TokenBudgetController(_fresh_meta())
    _combined, usage = ctrl.fit_all(
        system_msgs=SYS_MSGS,
        summary_msgs=SUMMARY_MSGS,
        recent_msgs=RECENT_MSGS,
        exec_msgs=EXEC_MSGS,
        current_msgs=CUR_MSGS,
        memory_msgs=MEM_MSGS,
    )
    assert usage["system_tokens"] == count_messages_total(SYS_MSGS), (
        f"system_tokens should equal base system tokens, "
        f"got {usage['system_tokens']}"
    )
    assert usage["memory_tokens"] > 0


def test_total_tokens_after_merge_is_bounded() -> None:
    """total_tokens stays within a small delta of the pre-merge section sum.

    When memory is merged into the first system message, the merge
    changes the token total in two opposing directions:

      * SAVES the framing overhead of one message (approx. 5 tokens:
        4 metadata + ~1 role) because two messages become one.
      * COSTS up to a few content tokens for the '\\n\\n' separator,
        bounded by integer-division rounding (the '//4' heuristic
        absorbs the 2-char separator in most cases).

    Net effect: total_tokens is typically a few tokens SMALLER than
    the pre-merge section sum. The delta is bounded by
    ``[-(framing_overhead + 2), +2]`` in the worst case, i.e. a
    small window around zero.

    The invariant we actually care about is: the merge does not
    INFLATE the total. A surprisingly large positive delta would
    signal that the merge is duplicating content (a real bug).
    """
    ctrl = TokenBudgetController(_fresh_meta())
    _combined, usage = ctrl.fit_all(
        system_msgs=SYS_MSGS,
        summary_msgs=SUMMARY_MSGS,
        recent_msgs=RECENT_MSGS,
        exec_msgs=EXEC_MSGS,
        current_msgs=CUR_MSGS,
        memory_msgs=MEM_MSGS,
    )
    section_sum = (
        usage["system_tokens"]
        + usage["summary_tokens"]
        + usage["recent_tokens"]
        + usage["execution_tokens"]
        + usage["memory_tokens"]
        + usage["current_tokens"]
    )
    delta = usage["total_tokens"] - section_sum

    # The merge MUST NOT inflate the total. A small positive delta
    # (up to +2 tokens) is allowed for the rare case where the
    # separator crosses an integer-division boundary.
    assert delta <= 2, (
        f"merge must not inflate total_tokens: "
        f"total={usage['total_tokens']} section_sum={section_sum} "
        f"delta={delta}"
    )

    # The merge MUST NOT shrink the total by more than the framing
    # overhead of one message (4 metadata + 1 role = ~5) plus a
    # small content rounding margin. A delta more negative than -10
    # would indicate the merge dropped content -- another real bug.
    assert delta >= -10, (
        f"merge must not drop content: "
        f"total={usage['total_tokens']} section_sum={section_sum} "
        f"delta={delta}"
    )


def test_phase2_truncation_when_memory_quota_positive() -> None:
    """Phase-2 path: a positive sections.memory truncates oversized blocks.

    Phase 1 ships with sections.memory == 0 (see BudgetRatios
    docstring). This test proves the truncation branch is functional
    so that a future Phase 2 can enable it without code changes.
    """
    meta_p2 = _fresh_meta()
    meta_p2.sections.memory = 20  # tiny quota forces truncation
    ctrl_p2 = TokenBudgetController(meta_p2)
    long_mem = "x" * 400  # ~100 tokens by the char//4 heuristic
    combined_p2, _usage = ctrl_p2.fit_all(
        system_msgs=[],
        summary_msgs=[],
        recent_msgs=[],
        exec_msgs=[],
        current_msgs=[{"role": "user", "content": "q"}],
        memory_msgs=[{"role": "system", "content": long_mem}],
    )
    merged = combined_p2[0]["content"]
    assert len(merged) < len(long_mem)
    assert "truncated" in merged


# ---------------------------------------------------------------------------
# Standalone runner (no pytest required)
# ---------------------------------------------------------------------------


def _run_all() -> None:
    import traceback

    test_functions = [
        test_backward_compat_no_memory_msgs,
        test_memory_msgs_merged_into_first_system_message,
        test_empty_memory_msgs_equivalent_to_none,
        test_memory_without_base_system_message,
        test_system_tokens_excludes_memory_tokens,
        test_total_tokens_after_merge_is_bounded,
        test_phase2_truncation_when_memory_quota_positive,
    ]
    failures = 0
    for fn in test_functions:
        name = fn.__name__
        try:
            fn()
            print(f"PASS  {name}")
        except AssertionError as exc:
            failures += 1
            print(f"FAIL  {name}: {exc}")
            traceback.print_exc()
        except Exception as exc:  # noqa: BLE001
            failures += 1
            print(f"ERROR {name}: {exc}")
            traceback.print_exc()

    print()
    if failures:
        print(f"=== {failures} test(s) failed ===")
        raise SystemExit(1)
    print(f"=== all {len(test_functions)} tests passed ===")


if __name__ == "__main__":
    _run_all()
