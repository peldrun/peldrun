# backend/peldrun/memory/domain/entry.py
"""
PELDRUN Memory Platform - Domain Entity: MemoryEntry.

Defines the PERSISTENT memory entry (the durable record that survives
across turns, runs, chats, and restarts) together with its mandatory
provenance descriptor.

Design invariants
-----------------

1. **Pydantic v2, strict schema.**
   ``extra="forbid"`` rejects unknown fields at construction time so
   any drift between producers and consumers surfaces immediately
   (rather than silently landing in ``model_extra`` and being lost
   on save). ``validate_assignment=True`` ensures mutations like
   ``entry.content_hash = ...`` re-run the field validators.

2. **Enum types, not bare strings.**
   The model stores ``MemoryType``, ``MemoryScope``, ``MemoryStatus``
   enum members directly. Pydantic v2's str-Enum handling gives us
   three benefits simultaneously:
     * JSON serialization emits the string value (``"semantic"``),
     * SQLite binding uses ``.value`` (the repository does this
       explicitly in P1.2),
     * field validators enforce the closed vocabulary at construction
       time -- an unknown type is rejected before it reaches storage.

3. **Hash-based dedupe is explicit, never implicit.**
   ``compute_hash()`` is the ONLY mechanism that produces the
   ``content_hash`` used by the SQLite UNIQUE constraint. It must
   run AFTER content normalization, and the repository (P1.2) is
   responsible for calling ``with_normalized_content()`` first so
   that "Hello" and "hello" produce the same hash. The standalone
   fallback (lowercase + strip) exists for unit tests and direct
   construction; production code paths go through the repository.

4. **Timezone-aware datetimes.**
   All timestamps are stored as aware ``datetime`` in UTC. Naive
   datetimes passed by legacy callers are coerced to UTC-aware in
   the field validator (``_ensure_utc_aware``). This eliminates the
   "silent TZ bug" that plagues naive-utc code: comparisons,
   sorting, and serialization all behave deterministically.

5. **Immutable-update helpers.**
   ``with_updated_timestamp()``, ``mark_used()``, ``archive()``,
   ``supersede()`` return a COPY with the field updated, leaving the
   original untouched. This makes audit / rollback trivial: the old
   entry stays referenceable, and callers can build new entries from
   old ones without aliasing bugs.

6. **Bounded sizes, enforced at the boundary.**
   Content, tags, scope ids, and hashes are all length-capped.
   This is not cosmetic -- an unbounded ``content`` would be a DoS
   vector via a single oversized ``memory_save`` tool call and would
   blow the token budget arithmetic in P1.4. All caps are named
   constants, defined once at the top of this module.

7. **Provenance is mandatory.**
   Every entry records WHERE it came from (source type + the
   specific chat/run/turn/import) so the audit log can always
   reconstruct origin. ``MemoryProvenance`` is ``extra="forbid"``
   for the same reason as the entry itself.

8. **No I/O, no framework imports.**
   This module imports ONLY ``hashlib``, ``datetime``, ``typing``,
   ``uuid``, and ``pydantic``. It does NOT import the repository,
   normalizer, provider, or any omweb module. Domain stays pure;
   storage lives in P1.2.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Any, List, Optional
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator

from peldrun.memory.domain.enums import (
    MemoryScope,
    MemorySourceType,
    MemoryStatus,
    MemoryType,
)


# ---------------------------------------------------------------------------
# Size limits (single source of truth)
# ---------------------------------------------------------------------------
#
# Every cap below is enforced by a field validator. Changing a value
# is a schema change that must be accompanied by a data migration if
# historical entries already exceed the new limit. Keeping the values
# in named constants makes the coupling explicit.

_MAX_CONTENT_CHARS: int = 10_000
"""Hard ceiling on ``content`` and ``normalized_content`` length.

Chosen to comfortably fit a paragraph, a code snippet, or a small
document fragment while remaining far below the token budget's
sensible ceiling. Oversized entries are rejected at construction;
the caller must split them if they genuinely need more.
"""

_MAX_SCOPE_ID_CHARS: int = 256
"""Hard ceiling on ``scope_id`` and ``memory_id`` length.

256 is generous enough to hold UUIDs, slugified paths, emails, and
composite identifiers like ``project:acme/backend`` while still
bounding the storage cost per row.
"""

_MAX_TAGS: int = 32
"""Maximum number of tags per entry."""

_MAX_TAG_CHARS: int = 64
"""Maximum length of a single tag."""

_MAX_HASH_CHARS: int = 64
"""Maximum length of ``content_hash`` (well above the 32 we produce)."""

_PREFIX_MEMORY_ID: str = "mem_"
"""Prefix for auto-generated memory identifiers.

Combined with a 16-hex-char UUID fragment, produces a 20-char id
with negligible collision risk and a readable shape for logs.
"""


# ---------------------------------------------------------------------------
# Datetime helpers
# ---------------------------------------------------------------------------


def _utc_now() -> datetime:
    """Return the current time as a timezone-aware UTC ``datetime``.

    Replaces the deprecated ``datetime.utcnow()`` pattern. Every
    timestamp field on ``MemoryEntry`` uses this factory, so all
    persisted entries carry consistent aware-UTC timestamps.
    """
    return datetime.now(timezone.utc)


def _ensure_utc_aware(value: Optional[datetime]) -> Optional[datetime]:
    """Coerce ``value`` to timezone-aware UTC.

    * ``None`` passes through unchanged.
    * Naive datetimes are assumed to be UTC and get ``tzinfo`` attached.
    * Aware datetimes are converted to UTC (preserving the instant).

    This is the ONE place where legacy naive-utc input is repaired.
    Every downstream consumer (repository, ranker, API layer) can
    then safely assume aware-UTC.
    """
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


# ---------------------------------------------------------------------------
# MemoryProvenance
# ---------------------------------------------------------------------------


class MemoryProvenance(BaseModel):
    """Origin of a memory entry.

    Every ``MemoryEntry`` MUST carry provenance. This is the audit
    trail that lets a future user ask "why does the agent think
    that?" and get a concrete answer: which chat, which run, which
    turn, or which imported package.

    The shape is deliberately narrow. If a producer needs to record
    arbitrary auxiliary data, it belongs in the entry's content or
    in a future ``metadata`` field -- NOT smuggled through provenance
    as an opaque blob (which would defeat the point of having
    provenance at all).
    """

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    source_type: MemorySourceType = Field(
        ...,
        description=(
            "Who or what created this entry. Drives write gating "
            "(IMPORT is the only source allowed in READ_ONLY mode) "
            "and trust weighting in the ranker."
        ),
    )
    source_chat_id: Optional[str] = Field(
        default=None,
        description="Chat session id the memory was created in, if any.",
    )
    source_run_id: Optional[str] = Field(
        default=None,
        description="Agent run id that produced the memory, if any.",
    )
    source_turn_id: Optional[str] = Field(
        default=None,
        description="Specific turn id within the chat, if known.",
    )
    imported_from: Optional[str] = Field(
        default=None,
        description=(
            "Free-form origin label for imports (e.g. 'chatgpt-export', "
            "'legacy-markdown-notes'). Only meaningful when "
            "source_type == IMPORT."
        ),
    )

    @field_validator(
        "source_chat_id",
        "source_run_id",
        "source_turn_id",
        "imported_from",
        mode="after",
    )
    @classmethod
    def _strip_optional_strings(cls, v: Optional[str]) -> Optional[str]:
        """Trim optional identifier strings; empty-after-trim -> None.

        Treating ``""`` and ``"   "`` as absent (rather than as
        legitimate non-empty strings) prevents a subtle class of
        bugs: a producer that emits ``source_run_id=""`` when the
        field is unknown would otherwise look like a valid, but
        meaningless, identifier.
        """
        if v is None:
            return None
        stripped = v.strip()
        return stripped or None


# ---------------------------------------------------------------------------
# MemoryEntry
# ---------------------------------------------------------------------------


class MemoryEntry(BaseModel):
    """Durable memory entry.

    Immutable-by-convention: mutation helpers (``mark_used``,
    ``archive``, etc.) return copies rather than mutating in place.
    Direct field assignment is technically legal (``frozen=False``,
    ``validate_assignment=True``) but reserved for the repository's
    internal update paths -- application code SHOULD prefer the
    ``with_*`` helpers so the audit trail of who-changed-what stays
    obvious.
    """

    model_config = ConfigDict(
        extra="forbid",
        validate_assignment=True,
        populate_by_name=True,
    )

    # ------------------------- identity ------------------------------------

    memory_id: str = Field(
        default_factory=lambda: f"{_PREFIX_MEMORY_ID}{uuid4().hex[:16]}",
        description=(
            "Stable, globally unique identifier for this memory. "
            "Generated locally at construction time (prefix 'mem_' + "
            "16 hex chars). Not reused across entries."
        ),
    )

    # ------------------------- classification ------------------------------

    memory_type: MemoryType = Field(
        ...,
        description=(
            "Semantic classification (semantic/episodic/procedural/"
            "preference/decision). Surfaced to the LLM as a soft label "
            "and used by the ranker's intent-matching heuristics."
        ),
    )
    scope_type: MemoryScope = Field(
        ...,
        description=(
            "Ownership boundary. Determines isolation, ranking "
            "precedence, and token-budget reservation."
        ),
    )
    scope_id: str = Field(
        ...,
        description=(
            "The concrete owner of this entry. For USER scope this is "
            "the user id; for PROJECT it is the project id; for CHAT "
            "it is the chat id; for AGENT it is the agent id. "
            "Paired with ``scope_type``, defines the isolation bucket."
        ),
    )

    # ------------------------- content -------------------------------------

    content: str = Field(
        ...,
        description=(
            "Human-readable, original text of the memory. This is "
            "what the user sees in the UI and what the LLM eventually "
            "reads (via context injection)."
        ),
    )
    normalized_content: str = Field(
        default="",
        description=(
            "Language-aware normalized form of ``content`` (Arabic/"
            "Russian/English normalization via P1.2's normalizer). "
            "Used exclusively for FTS indexing and the dedupe hash. "
            "Empty until the repository populates it."
        ),
    )
    tags: List[str] = Field(
        default_factory=list,
        description=(
            "Free-form topic markers. Normalized to lowercase, "
            "trimmed, deduplicated, and bounded at the validator. "
            "Used by FTS and by the ranker's tag-overlap heuristic."
        ),
    )

    # ------------------------- metadata ------------------------------------

    importance: int = Field(
        default=50,
        ge=0,
        le=100,
        description=(
            "User-assigned or policy-assigned importance score "
            "(0=trivial, 100=critical). Ranker adds this as a "
            "tie-breaking and boosting signal; budget eviction "
            "prefers to keep high-importance entries."
        ),
    )
    confidence: float = Field(
        default=1.0,
        ge=0.0,
        le=1.0,
        description=(
            "How confident the producer is that this memory is "
            "accurate. Extraction-produced entries typically have "
            "<1.0; explicit user entries are 1.0."
        ),
    )
    status: MemoryStatus = Field(
        default=MemoryStatus.ACTIVE,
        description=(
            "Lifecycle status. ACTIVE entries are retrievable; "
            "ARCHIVED and SUPERSEDED are excluded from search and "
            "retained only for audit/restoration."
        ),
    )
    is_pinned: bool = Field(
        default=False,
        description=(
            "When True the ranker applies the ``boost_pinned`` "
            "multiplier from ``MemorySettings``. Pinned entries "
            "surface even for weak queries; only explicit unpin "
            "removes the boost."
        ),
    )

    # ------------------------- provenance ----------------------------------

    provenance: MemoryProvenance = Field(
        ...,
        description=(
            "Mandatory origin descriptor. Every memory records HOW it "
            "came to exist (explicit user / agent tool / import / "
            "extraction) and the specific chat/run/turn that produced "
            "it, so the audit trail is complete."
        ),
    )

    # ------------------------- lifecycle -----------------------------------

    created_at: datetime = Field(
        default_factory=_utc_now,
        description="Creation timestamp (aware UTC).",
    )
    updated_at: datetime = Field(
        default_factory=_utc_now,
        description=(
            "Last modification timestamp (aware UTC). Updated "
            "automatically by the repository on every write."
        ),
    )
    last_used_at: Optional[datetime] = Field(
        default=None,
        description=(
            "Last time this entry was injected into a prompt (aware "
            "UTC). Populated in bulk by the repository after a run "
            "so the ranker can apply recency decay without an N-write "
            "cost per run."
        ),
    )
    expires_at: Optional[datetime] = Field(
        default=None,
        description=(
            "Optional hard expiration (aware UTC). Expired entries "
            "are excluded from retrieval even if still ACTIVE. "
            "Used for ephemeral facts like 'current sprint'."
        ),
    )

    # ------------------------- computed ------------------------------------

    content_hash: str = Field(
        default="",
        description=(
            "Deterministic dedupe hash derived from "
            "(scope_type, scope_id, normalized_content). Populated by "
            "the repository (P1.2) immediately before INSERT so the "
            "UNIQUE(scope_type, scope_id, content_hash) constraint "
            "catches re-saves. Empty until computed."
        ),
    )

    # ------------------------- field validators ----------------------------

    @field_validator("memory_id", "scope_id", mode="after")
    @classmethod
    def _validate_required_short_string(cls, v: str, info: Any) -> str:
        """Trim and length-check identity / scope identifiers."""
        if not isinstance(v, str):
            raise ValueError(
                f"{info.field_name} must be a string, got {type(v).__name__}"
            )
        stripped = v.strip()
        if not stripped:
            raise ValueError(f"{info.field_name} must not be empty")
        if len(stripped) > _MAX_SCOPE_ID_CHARS:
            raise ValueError(
                f"{info.field_name} exceeds {_MAX_SCOPE_ID_CHARS} chars "
                f"(got {len(stripped)})"
            )
        return stripped

    @field_validator("content", mode="after")
    @classmethod
    def _validate_content(cls, v: str) -> str:
        """Trim, non-empty-check, and length-cap raw content."""
        if not isinstance(v, str):
            raise ValueError(
                f"content must be a string, got {type(v).__name__}"
            )
        stripped = v.strip()
        if not stripped:
            raise ValueError("content must not be empty or whitespace-only")
        if len(stripped) > _MAX_CONTENT_CHARS:
            raise ValueError(
                f"content exceeds {_MAX_CONTENT_CHARS} chars "
                f"(got {len(stripped)}); split the entry if you need more"
            )
        return stripped

    @field_validator("normalized_content", mode="after")
    @classmethod
    def _validate_normalized_content(cls, v: str) -> str:
        """Trim (allow empty) and length-cap normalized content.

        Empty is a valid transient state -- the repository fills it
        before persisting. Length check protects against a caller
        passing a pathological normalization that exceeds the raw
        content ceiling (unusual but possible if the normalizer
        expands abbreviations).
        """
        if not isinstance(v, str):
            raise ValueError(
                f"normalized_content must be a string, "
                f"got {type(v).__name__}"
            )
        stripped = v.strip()
        if len(stripped) > _MAX_CONTENT_CHARS:
            raise ValueError(
                f"normalized_content exceeds {_MAX_CONTENT_CHARS} chars "
                f"(got {len(stripped)})"
            )
        return stripped

    @field_validator("tags", mode="after")
    @classmethod
    def _normalize_tags(cls, v: List[str]) -> List[str]:
        """Lowercase, trim, dedupe (preserve first-seen order), cap.

        Dedup preserves order rather than using ``set()`` so that
        deterministic serialization (needed by the package format
        in P1.6) is preserved across saves/loads.
        """
        if not v:
            return []

        seen: set[str] = set()
        result: List[str] = []
        for raw in v:
            if not isinstance(raw, str):
                raise ValueError(
                    f"tags must be strings, got {type(raw).__name__}"
                )
            normalized = raw.strip().lower()
            if not normalized:
                continue
            if len(normalized) > _MAX_TAG_CHARS:
                raise ValueError(
                    f"tag exceeds {_MAX_TAG_CHARS} chars: "
                    f"{normalized[:20]}..."
                )
            if normalized in seen:
                continue
            seen.add(normalized)
            result.append(normalized)

        if len(result) > _MAX_TAGS:
            raise ValueError(
                f"tags list exceeds {_MAX_TAGS} items (got {len(result)})"
            )
        return result

    @field_validator("content_hash", mode="after")
    @classmethod
    def _validate_content_hash(cls, v: str) -> str:
        """Trim (allow empty) and length-cap the dedupe hash."""
        if not isinstance(v, str):
            raise ValueError(
                f"content_hash must be a string, got {type(v).__name__}"
            )
        stripped = v.strip()
        if len(stripped) > _MAX_HASH_CHARS:
            raise ValueError(
                f"content_hash exceeds {_MAX_HASH_CHARS} chars "
                f"(got {len(stripped)})"
            )
        return stripped

    @field_validator(
        "created_at",
        "updated_at",
        "last_used_at",
        "expires_at",
        mode="after",
    )
    @classmethod
    def _ensure_utc_aware(
        cls, v: Optional[datetime]
    ) -> Optional[datetime]:
        """Coerce every datetime field to aware UTC."""
        return _ensure_utc_aware(v)

    # ------------------------- derived properties --------------------------

    @property
    def is_active(self) -> bool:
        """True when the entry participates in retrieval by status."""
        return self.status is MemoryStatus.ACTIVE

    @property
    def is_expired(self) -> bool:
        """True when ``expires_at`` is set and has passed.

        Returns False when ``expires_at`` is None (no expiration).
        Uses ``_utc_now()`` so a test that mocks the clock via
        module-level ``_utc_now`` sees the change here too.
        """
        if self.expires_at is None:
            return False
        return _utc_now() >= self.expires_at

    @property
    def is_retrievable(self) -> bool:
        """True when the entry may participate in retrieval.

        Combines status and expiration: an ACTIVE entry that has
        expired is NOT retrievable. Centralizing this rule here
        prevents every caller from re-deriving it (and forgetting
        the expiration check).
        """
        return self.status.is_retrievable and not self.is_expired

    # ------------------------- hash & normalization ------------------------

    def compute_hash(self) -> str:
        """Compute the deterministic dedupe hash (32-char prefix of SHA-256).

        Uses ``normalized_content`` when populated (the production
        path, filled by the repository's normalizer). Falls back to
        a basic ``lower().strip()`` of raw content for standalone
        construction (unit tests, direct ``MemoryEntry(...)`` calls).

        The hash intentionally EXCLUDES timestamps, ``memory_id``,
        and every metadata field: two entries with identical
        ``(scope_type, scope_id, normalized_content)`` MUST collide
        here so the SQLite UNIQUE constraint in P1.2 rejects the
        duplicate at INSERT time.

        Raises
        ------
        ValueError
            When both ``normalized_content`` and ``content`` are
            empty (a state the field validators should have already
            rejected, but defensive here because a caller might
            construct an invalid entry by bypassing validation).
        """
        source = self.normalized_content or self.content.lower().strip()
        if not source:
            raise ValueError(
                "Cannot compute hash: both normalized_content and "
                "content are empty"
            )
        # ``.value`` is explicit: str-Enum members stringify
        # inconsistently across Python versions and Pydantic
        # releases. ``.value`` is the stable, documented accessor.
        raw = f"{self.scope_type.value}:{self.scope_id}:{source}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]

    def with_normalized_content(self, normalized: str) -> "MemoryEntry":
        """Return a copy with ``normalized_content`` and ``content_hash`` set.

        This is THE method the repository (P1.2) calls after running
        the language-aware normalizer. It atomically updates both
        fields so callers can never accidentally save an entry with
        a hash that does not match its normalized_content.

        The normalizer's output MUST be idempotent (running it twice
        produces the same string). Otherwise two saves of the same
        content would produce different hashes and the dedupe would
        silently fail.
        """
        stripped = normalized.strip()
        if not stripped:
            raise ValueError(
                "normalized content must not be empty (the repository "
                "must produce a non-empty normalization)"
            )
        if len(stripped) > _MAX_CONTENT_CHARS:
            raise ValueError(
                f"normalized content exceeds {_MAX_CONTENT_CHARS} chars "
                f"(got {len(stripped)})"
            )
        raw = f"{self.scope_type.value}:{self.scope_id}:{stripped}"
        new_hash = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]
        return self.model_copy(
            update={
                "normalized_content": stripped,
                "content_hash": new_hash,
            }
        )

    # ------------------------- immutable mutators --------------------------

    def with_updated_timestamp(
        self, now: Optional[datetime] = None
    ) -> "MemoryEntry":
        """Return a copy with ``updated_at`` bumped to ``now`` (default: UTC now)."""
        return self.model_copy(
            update={"updated_at": _ensure_utc_aware(now) or _utc_now()}
        )

    def mark_used(
        self, now: Optional[datetime] = None
    ) -> "MemoryEntry":
        """Return a copy with ``last_used_at`` set to ``now``.

        Called in bulk by ``SqliteMemoryRepository.mark_used`` after
        a retrieval, so a single UPDATE ... WHERE memory_id IN (...)
        covers every retrieved entry without an N-row write storm.
        """
        return self.model_copy(
            update={"last_used_at": _ensure_utc_aware(now) or _utc_now()}
        )

    def archive(
        self, now: Optional[datetime] = None
    ) -> "MemoryEntry":
        """Return a copy with ``status`` set to ARCHIVED.

        Soft-delete semantics: the entry remains in the database for
        restoration, but is excluded from retrieval via
        ``MemoryStatus.is_retrievable``.
        """
        return self.model_copy(
            update={
                "status": MemoryStatus.ARCHIVED,
                "updated_at": _ensure_utc_aware(now) or _utc_now(),
            }
        )

    def supersede(
        self, now: Optional[datetime] = None
    ) -> "MemoryEntry":
        """Return a copy with ``status`` set to SUPERSEDED.

        Used by the conflict-resolution policy (P1.4) when a newer
        entry replaces this one about the same topic. The older
        entry is kept for audit; only the newer one participates in
        retrieval.
        """
        return self.model_copy(
            update={
                "status": MemoryStatus.SUPERSEDED,
                "updated_at": _ensure_utc_aware(now) or _utc_now(),
            }
        )

    # ------------------------- convenience constructor ---------------------

    @classmethod
    def create(
        cls,
        *,
        content: str,
        memory_type: MemoryType,
        scope_type: MemoryScope,
        scope_id: str,
        provenance: MemoryProvenance,
        normalized_content: str = "",
        **kwargs: Any,
    ) -> "MemoryEntry":
        """Construct a fully-populated entry with ``content_hash`` computed.

        This is the RECOMMENDED way to create a new entry from
        application code and tests, because it:

          1. Runs full field validation (length caps, enum coercion,
             tag normalization, datetime awareness).
          2. Populates ``normalized_content`` (using the caller's
             value if provided, else ``content.lower().strip()``).
          3. Computes ``content_hash`` atomically with the
             normalization so the two can never diverge.

        The repository (P1.2) uses this method internally and then
        OVERRIDES ``normalized_content`` via ``with_normalized_content``
        after running its language-aware normalizer. That two-step
        flow guarantees the entry is valid at every intermediate
        state -- no window where ``content_hash`` could be stale.
        """
        entry = cls(
            content=content,
            memory_type=memory_type,
            scope_type=scope_type,
            scope_id=scope_id,
            provenance=provenance,
            **kwargs,
        )
        effective_normalized = (
            normalized_content.strip()
            if normalized_content
            else content.strip().lower()
        )
        return entry.with_normalized_content(effective_normalized)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


__all__ = [
    "MemoryEntry",
    "MemoryProvenance",
]
