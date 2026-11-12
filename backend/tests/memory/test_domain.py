# backend/tests/memory/test_domain.py
"""
P1.1 Smoke Tests -- Domain & Contracts.

Validates that the entire public surface of ``peldrun.memory.domain``
is importable, structurally sound, and enforces the invariants
promised by the engineering plan:

  * every promised public symbol exists,
  * no private name leaked into ``__all__``,
  * the two fail-safe classes structurally satisfy their protocols,
  * scope resolution produces distinct canonical refs per project,
  * budget overflow is rejected at construction time,
  * the exception hierarchy maps to the documented HTTP intents.

These tests do NOT exercise storage, retrieval, or any I/O. They
are pure domain sanity checks.
"""

from __future__ import annotations

import pytest

import peldrun.memory.domain as dom


# ---------------------------------------------------------------------------
# Public surface completeness
# ---------------------------------------------------------------------------


def test_public_surface_is_complete():
    """Every name listed in __all__ must exist on the package."""
    missing = [name for name in dom.__all__ if not hasattr(dom, name)]
    assert not missing, f"missing public exports: {missing}"


def test_no_private_names_in_all():
    """No name in __all__ may start with an underscore."""
    leaked = [name for name in dom.__all__ if name.startswith("_")]
    assert not leaked, f"private names leaked into __all__: {leaked}"


def test_public_surface_has_no_duplicates():
    """__all__ must not contain duplicate entries."""
    assert len(dom.__all__) == len(set(dom.__all__)), (
        "__all__ contains duplicate entries"
    )


# ---------------------------------------------------------------------------
# Enum sanity
# ---------------------------------------------------------------------------


def test_memory_scope_precedence_is_ordered():
    """USER > PROJECT > CHAT > AGENT (higher precedence wins ties)."""
    assert (
        dom.MemoryScope.USER.precedence
        > dom.MemoryScope.PROJECT.precedence
        > dom.MemoryScope.CHAT.precedence
        > dom.MemoryScope.AGENT.precedence
    )


def test_memory_scope_only_user_is_globally_visible():
    """Only USER scope crosses project boundaries."""
    assert dom.MemoryScope.USER.is_globally_visible is True
    for scope in (
        dom.MemoryScope.PROJECT,
        dom.MemoryScope.CHAT,
        dom.MemoryScope.AGENT,
    ):
        assert scope.is_globally_visible is False


def test_memory_status_only_active_is_retrievable():
    """ACTIVE is the sole retrievable status."""
    assert dom.MemoryStatus.ACTIVE.is_retrievable is True
    assert dom.MemoryStatus.ARCHIVED.is_retrievable is False
    assert dom.MemoryStatus.SUPERSEDED.is_retrievable is False


def test_memory_mode_can_read_write():
    """DISABLED rejects both; READ_ONLY rejects only writes."""
    assert dom.MemoryMode.DISABLED.can_read is False
    assert dom.MemoryMode.DISABLED.can_write is False
    assert dom.MemoryMode.READ_ONLY.can_read is True
    assert dom.MemoryMode.READ_ONLY.can_write is False
    assert dom.MemoryMode.READ_WRITE.can_read is True
    assert dom.MemoryMode.READ_WRITE.can_write is True


def test_memory_source_is_automatic():
    """EXTRACTION and IMPORT are automatic; the other two are not."""
    assert dom.MemorySourceType.EXTRACTION.is_automatic is True
    assert dom.MemorySourceType.IMPORT.is_automatic is True
    assert dom.MemorySourceType.EXPLICIT_USER.is_automatic is False
    assert dom.MemorySourceType.AGENT_TOOL.is_automatic is False


# ---------------------------------------------------------------------------
# MemoryScopeRef & scope resolution (SECURITY critical)
# ---------------------------------------------------------------------------


def test_scope_ref_rejects_colon_in_id():
    """A colon inside scope_id would make parsing ambiguous -- rejected."""
    with pytest.raises(ValueError, match="separator"):
        dom.MemoryScopeRef(
            scope_type=dom.MemoryScope.PROJECT,
            scope_id="a:b",
        )


def test_scope_ref_rejects_empty_id():
    with pytest.raises(ValueError, match="must not be empty"):
        dom.MemoryScopeRef(
            scope_type=dom.MemoryScope.PROJECT,
            scope_id="   ",
        )


def test_scope_ref_roundtrip():
    """str(ref) -> from_string -> ref must be identity."""
    ref = dom.MemoryScopeRef(
        scope_type=dom.MemoryScope.PROJECT,
        scope_id="acme",
    )
    assert str(ref) == "project:acme"
    assert dom.MemoryScopeRef.from_string("project:acme") == ref


def test_resolve_scope_refs_isolates_projects():
    """Acceptance #2: project A cannot see project B's refs."""
    refs_a = dom.resolve_scope_refs(chat_id="c1", project_id="A")
    refs_b = dom.resolve_scope_refs(chat_id="c1", project_id="B")
    assert "project:A" in refs_a
    assert "project:A" not in refs_b
    assert "project:B" in refs_b
    assert "project:B" not in refs_a


def test_resolve_scope_refs_includes_user_scope():
    """Acceptance #3: user scope is visible to every project."""
    refs = dom.resolve_scope_refs(chat_id="c1", project_id="A")
    assert "user:default" in refs


def test_resolve_scope_refs_empty_project_omits_project_scope():
    """Empty project_id must NOT fall back to a phantom project scope."""
    refs = dom.resolve_scope_refs(chat_id="c1", project_id="")
    assert not any(r.startswith("project:") for r in refs)


def test_resolve_scope_returns_innermost():
    """resolve_scope prefers PROJECT when both ids are present."""
    ref = dom.resolve_scope(chat_id="c1", project_id="A")
    assert ref.scope_type is dom.MemoryScope.PROJECT
    assert ref.scope_id == "A"


def test_resolve_scope_rejects_empty_context():
    with pytest.raises(ValueError, match="at least one"):
        dom.resolve_scope(chat_id="", project_id="")


# ---------------------------------------------------------------------------
# MemoryEntry invariants
# ---------------------------------------------------------------------------


def _make_provenance() -> dom.MemoryProvenance:
    return dom.MemoryProvenance(
        source_type=dom.MemorySourceType.EXPLICIT_USER,
    )


def test_entry_create_computes_hash():
    """MemoryEntry.create must populate content_hash."""
    entry = dom.MemoryEntry.create(
        content="The user prefers dark mode.",
        memory_type=dom.MemoryType.PREFERENCE,
        scope_type=dom.MemoryScope.USER,
        scope_id="default",
        provenance=_make_provenance(),
    )
    assert entry.content_hash, "content_hash must be populated"
    assert len(entry.content_hash) == 32


def test_entry_hash_deterministic_for_case_variants():
    """'Hello' and 'hello' produce the same hash (dedupe contract)."""
    a = dom.MemoryEntry.create(
        content="Hello",
        memory_type=dom.MemoryType.SEMANTIC,
        scope_type=dom.MemoryScope.PROJECT,
        scope_id="p1",
        provenance=_make_provenance(),
    )
    b = dom.MemoryEntry.create(
        content="hello",
        memory_type=dom.MemoryType.SEMANTIC,
        scope_type=dom.MemoryScope.PROJECT,
        scope_id="p1",
        provenance=_make_provenance(),
    )
    assert a.content_hash == b.content_hash


def test_entry_hash_differs_across_scopes():
    """Same content in different scopes must produce different hashes."""
    a = dom.MemoryEntry.create(
        content="fact",
        memory_type=dom.MemoryType.SEMANTIC,
        scope_type=dom.MemoryScope.PROJECT,
        scope_id="p1",
        provenance=_make_provenance(),
    )
    b = dom.MemoryEntry.create(
        content="fact",
        memory_type=dom.MemoryType.SEMANTIC,
        scope_type=dom.MemoryScope.PROJECT,
        scope_id="p2",
        provenance=_make_provenance(),
    )
    assert a.content_hash != b.content_hash


def test_entry_rejects_oversized_content():
    with pytest.raises(ValueError, match="exceeds"):
        dom.MemoryEntry.create(
            content="x" * 20_000,
            memory_type=dom.MemoryType.SEMANTIC,
            scope_type=dom.MemoryScope.USER,
            scope_id="default",
            provenance=_make_provenance(),
        )


def test_entry_tags_normalized_to_lowercase_and_deduped():
    entry = dom.MemoryEntry.create(
        content="test",
        memory_type=dom.MemoryType.SEMANTIC,
        scope_type=dom.MemoryScope.USER,
        scope_id="default",
        provenance=_make_provenance(),
        tags=["Python", "python", "  ML  "],
    )
    assert entry.tags == ["python", "ml"]


def test_entry_archive_returns_new_instance():
    """Mutators must be immutable -- original stays unchanged."""
    original = dom.MemoryEntry.create(
        content="test",
        memory_type=dom.MemoryType.SEMANTIC,
        scope_type=dom.MemoryScope.USER,
        scope_id="default",
        provenance=_make_provenance(),
    )
    archived = original.archive()
    assert original.status is dom.MemoryStatus.ACTIVE
    assert archived.status is dom.MemoryStatus.ARCHIVED
    assert original is not archived


def test_entry_is_retrievable_checks_status_and_expiry():
    entry = dom.MemoryEntry.create(
        content="test",
        memory_type=dom.MemoryType.SEMANTIC,
        scope_type=dom.MemoryScope.USER,
        scope_id="default",
        provenance=_make_provenance(),
    )
    assert entry.is_retrievable is True
    assert entry.archive().is_retrievable is False


# ---------------------------------------------------------------------------
# MemoryContext invariants
# ---------------------------------------------------------------------------


def test_context_empty_is_empty():
    ctx = dom.MemoryContext.empty(query="hi", token_budget=1024)
    assert ctx.is_empty is True
    assert ctx.used_tokens == 0
    assert ctx.dropped_for_budget == 0


def test_context_rejects_budget_overflow():
    """Acceptance #10 groundwork: used_tokens must never exceed budget."""
    with pytest.raises(ValueError, match="budget violation"):
        dom.MemoryContext(
            query="test",
            scope_refs=[],
            memories=[],
            token_budget=100,
            used_tokens=150,
        )


def test_context_rejects_unknown_router_decision():
    with pytest.raises(ValueError, match="not a valid RouteDecision"):
        dom.MemoryContext(
            query="test",
            scope_refs=[],
            router_decision="no-need",  # hyphen, not underscore
        )


def test_context_rejects_malformed_scope_ref():
    with pytest.raises(ValueError, match="not in canonical"):
        dom.MemoryContext(
            query="test",
            scope_refs=["default"],  # missing ':'
        )


def test_context_utilization_math():
    ctx = dom.MemoryContext(
        query="test",
        scope_refs=["user:default"],
        token_budget=1000,
        used_tokens=800,
    )
    assert ctx.utilization == pytest.approx(0.8)


# ---------------------------------------------------------------------------
# MemorySettings invariants
# ---------------------------------------------------------------------------


def test_settings_default_mode_is_read_write():
    s = dom.MemorySettings()
    assert s.mode is dom.MemoryMode.READ_WRITE


def test_settings_rejects_reservation_exceeding_budget():
    with pytest.raises(ValueError, match="must not exceed max_tokens"):
        dom.MemoryTokenBudget(max_tokens=100, reserved_for_user_scope=200)


def test_settings_rejects_nested_overrides():
    inner = dom.MemorySettings()
    with pytest.raises(ValueError, match="nested overrides"):
        dom.MemorySettings(
            project_overrides={"p1": inner.with_project_override("p2", inner)}
        )


def test_settings_effective_for_project_no_override():
    """No override -> returns the base instance unchanged."""
    base = dom.MemorySettings()
    assert base.effective_for_project("nonexistent") is base


def test_settings_effective_for_project_delta_merge():
    """Override only changes fields it explicitly set."""
    base = dom.MemorySettings()
    # Override ONLY the mode; everything else must inherit.
    override = dom.MemorySettings(mode=dom.MemoryMode.READ_ONLY)
    # ``model_fields_set`` includes only explicitly-passed fields.
    with_override = base.with_project_override("p1", override)
    effective = with_override.effective_for_project("p1")
    assert effective.mode is dom.MemoryMode.READ_ONLY
    # Untouched fields inherited from base.
    assert effective.default_limit == base.default_limit
    assert effective.boost_pinned == base.boost_pinned


# ---------------------------------------------------------------------------
# MemoryUsage invariants
# ---------------------------------------------------------------------------


def test_usage_scope_breakdown_must_sum():
    """Acceptance #14: user + project + chat + agent == context_tokens."""
    with pytest.raises(ValueError, match="does not sum"):
        dom.MemoryUsage(
            run_id="r1",
            user_tokens=100,
            project_tokens=100,
            context_tokens=500,  # 200 != 500
            entries_retrieved=1,
            entries_used=1,
            entries_dropped=0,
        )


def test_usage_entry_counts_must_sum():
    with pytest.raises(ValueError, match="entry count mismatch"):
        dom.MemoryUsage(
            run_id="r1",
            entries_retrieved=5,
            entries_used=3,
            entries_dropped=1,  # 3+1=4 != 5
        )


def test_usage_budget_respected():
    with pytest.raises(ValueError, match="memory budget violated"):
        dom.MemoryUsage(
            run_id="r1",
            context_tokens=900,
            overhead_tokens=200,
            budget_tokens=1000,  # 900+200=1100 > 1000
            user_tokens=900,
            entries_retrieved=1,
            entries_used=1,
            entries_dropped=0,
        )


def test_usage_valid_construction():
    usage = dom.MemoryUsage(
        run_id="r1",
        user_tokens=150,
        project_tokens=200,
        chat_tokens=100,
        agent_tokens=0,
        context_tokens=450,
        overhead_tokens=25,
        budget_tokens=1024,
        retrieval_tokens=0,
        entries_retrieved=5,
        entries_used=4,
        entries_dropped=1,
    )
    assert usage.total_tokens == 475
    assert usage.drop_rate == pytest.approx(0.2)
    assert usage.budget_utilization == pytest.approx(475 / 1024)


# ---------------------------------------------------------------------------
# Exception hierarchy
# ---------------------------------------------------------------------------


def test_exception_hierarchy():
    assert issubclass(dom.MemoryUnavailableError, dom.MemoryError)
    assert issubclass(dom.MemoryOperationError, dom.MemoryError)
    assert issubclass(dom.MemoryValidationError, dom.MemoryOperationError)
    assert issubclass(dom.MemoryNotFoundError, dom.MemoryOperationError)
    assert issubclass(dom.ScopeViolationError, dom.MemoryError)


def test_exception_retryable_flags():
    assert dom.MemoryUnavailableError.retryable is True
    assert dom.MemoryValidationError.retryable is False
    assert dom.MemoryNotFoundError.retryable is False
    assert dom.ScopeViolationError.retryable is False


def test_exception_client_facing_flags():
    assert dom.MemoryUnavailableError.client_facing is False
    assert dom.MemoryValidationError.client_facing is True
    assert dom.MemoryNotFoundError.client_facing is True
    assert dom.ScopeViolationError.client_facing is True


def test_exception_to_dict_shape():
    exc = dom.MemoryValidationError("bad content", field_name="content")
    d = exc.to_dict()
    assert d["error"] == "MemoryValidationError"
    assert d["message"] == "bad content"
    assert d["context"]["field_name"] == "content"
    assert d["client_facing"] is True


# ---------------------------------------------------------------------------
# Protocol structural compliance
# ---------------------------------------------------------------------------


def test_null_memory_port_satisfies_protocol():
    assert isinstance(dom.NullMemoryPort(), dom.MemoryPort)


def test_null_memory_provider_satisfies_protocol():
    assert isinstance(dom.NullMemoryProvider(), dom.MemoryProvider)


@pytest.mark.asyncio
async def test_null_memory_port_read_semantics():
    port = dom.NullMemoryPort()
    assert await port.search("q", ["user:default"]) == []
    assert await port.get("mem_xxx", "user:default") is None
    assert await port.delete("mem_xxx", "user:default") is False
    await port.mark_used(["mem_xxx"])  # must not raise


@pytest.mark.asyncio
async def test_null_memory_port_write_semantics():
    port = dom.NullMemoryPort()
    entry = dom.MemoryEntry.create(
        content="test",
        memory_type=dom.MemoryType.SEMANTIC,
        scope_type=dom.MemoryScope.USER,
        scope_id="default",
        provenance=_make_provenance(),
    )
    with pytest.raises(dom.MemoryUnavailableError):
        await port.create(entry)
    with pytest.raises(dom.MemoryUnavailableError):
        await port.update(entry)


# ---------------------------------------------------------------------------
# canonical empty context helper
# ---------------------------------------------------------------------------


def test_empty_memory_context_helper():
    ctx = dom.empty_memory_context("hi", ["user:default"], token_budget=512)
    assert ctx.is_empty is True
    assert ctx.token_budget == 512
    assert ctx.scope_refs == ["user:default"]
