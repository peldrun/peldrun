# backend/peldrun/memory/domain/enums.py
"""
PELDRUN Memory Platform - Domain Enumerations.

SINGLE SOURCE OF TRUTH for the closed vocabularies used throughout
the memory subsystem:

    * MemoryType       -- semantic classification of an entry.
    * MemoryScope      -- ownership boundary (isolation + ranking).
    * MemorySourceType -- origin of an entry (who / what created it).
    * MemoryStatus     -- lifecycle state (active / archived / superseded).
    * MemoryMode       -- global subsystem operating mode.

Design invariants
-----------------

1. Pure standard library.
   This module imports ONLY ``enum`` and ``typing``. It has no I/O,
   no Pydantic, no framework dependency. Every other module in
   ``peldrun.memory.domain`` may import from here; this module MUST
   NOT import from any of them (avoids cycles and preserves the
   "no I/O in domain" rule).

2. String-backed enums.
   Every enum subclasses ``str`` so its members:
     * serialize natively as their string value in JSON,
     * bind directly as SQLite TEXT column values,
     * compare equal to their raw str form (MemoryType.SEMANTIC
       == "semantic" is True).
   This eliminates the "coerce at every boundary" tax that
   non-str enums impose on storage and API layers.

3. Behavior on the enum, not on free functions.
   Predicates (can_read, can_write, is_retrievable, precedence,
   is_automatic, is_globally_visible) are properties on the enum
   classes rather than module-level functions. Rationale: a caller
   cannot accidentally re-implement a subtle rule (e.g. "which
   scopes are retrievable?") in a different way; the rule lives
   exactly once, on the member it applies to.

4. Closed sets, exported for validation.
   MEMORY_TYPES, MEMORY_SCOPES, MEMORY_SOURCE_TYPES,
   MEMORY_STATUSES, MEMORY_MODES are frozen views of the member
   set. Validators (Pydantic, API layers, import adapters) consume
   them instead of writing out the member list in their own files.

5. Stable string values.
   The string VALUE of each member is a persisted contract: it
   appears in SQLite rows, in peldrun-memory-package JSON files,
   and in API responses. Renaming a value is a data migration, not
   a code change. Never do it without an accompanying migration.

6. ASCII-only source.
   This file contains no non-ASCII characters. Docstrings use
   "->" where prose might want "->"; arrow glyphs and em-dashes
   are deliberately avoided so that any toolchain (including
   strict mypy, older Python versions on Windows, and IDE diff
   viewers) sees the same bytes we do.
"""

from __future__ import annotations

from enum import Enum
from typing import Final, Mapping


# ---------------------------------------------------------------------------
# MemoryType -- semantic classification
# ---------------------------------------------------------------------------


class MemoryType(str, Enum):
    """Semantic classification of a memory entry.

    The type is:
      * Surfaced to the LLM as a soft label inside the retrieved
        block (for example, ``[preference] ...``) so the model can
        weigh each entry differently from raw prose.
      * Used by ranking heuristics when the query's intent matches
        a specific type (e.g. "what did we decide?" boosts
        DECISION).
      * Used by the extraction pipeline to decide which merge
        strategy to apply (two PREFERENCE entries about the same
        topic supersede; two EPISODIC entries usually coexist).
    """

    SEMANTIC = "semantic"
    """Timeless facts about the world or the user's environment.

    Example: the company's primary domain is acme.example.
    """

    EPISODIC = "episodic"
    """Events and experiences bound to a moment.

    Example: on 2024-03-15 the staging deploy failed with HTTP 502.
    """

    PROCEDURAL = "procedural"
    """Reusable procedures, commands, or workflows.

    Example: run ``make test`` before committing.
    """

    PREFERENCE = "preference"
    """User tastes and defaults.

    Example: prefers dark mode; disallows emojis in commit messages.
    """

    DECISION = "decision"
    """Accepted decisions together with their rationale.

    Example: chose PostgreSQL 16 because of the JSONB query needs.
    """


# ---------------------------------------------------------------------------
# MemoryScope -- ownership boundary
# ---------------------------------------------------------------------------


class MemoryScope(str, Enum):
    """Ownership boundary of a memory entry.

    Scope determines three orthogonal behaviors:

    * Isolation
        A PROJECT entry is invisible to another project's retrieval.
        A USER entry is visible to every project owned by the same
        user. CHAT and AGENT narrow the visibility further.

    * Ranking precedence
        When two entries tie on relevance, the entry with the higher
        precedence wins. USER sits on top because user-scope entries
        (preferences, decisions) are the most durable signal in a
        workspace.

    * Token budget reservation
        MemoryTokenBudget.reserved_for_user_scope guarantees a
        minimum token slice for user-scope entries in every prompt,
        so a burst of project-scope matches cannot starve them.
    """

    USER = "user"
    """Shared across every project owned by the same user."""

    PROJECT = "project"
    """Scoped to one project; invisible to every other project."""

    CHAT = "chat"
    """Scoped to one chat session; disappears with the session."""

    AGENT = "agent"
    """Scoped to one agent instance. Reserved for future use."""

    @property
    def precedence(self) -> int:
        """Ranking precedence for relevance ties. Higher wins.

        Values are spaced by 1 so future scopes can be inserted
        between existing members without renumbering.
        """
        return _SCOPE_PRECEDENCE[self]

    @property
    def is_globally_visible(self) -> bool:
        """True when an entry in this scope crosses project boundaries.

        Only USER is globally visible. Every other scope is bounded
        by its owner (project id, chat id, agent id). Retrieval code
        uses this to decide whether to add the entry to a
        cross-project query without an explicit scope filter.
        """
        return self is MemoryScope.USER


# Defined AFTER MemoryScope so the dict keys are real enum members.
# The .precedence property evaluates _SCOPE_PRECEDENCE at call time,
# by which point this dict is fully populated.
_SCOPE_PRECEDENCE: Final[Mapping[MemoryScope, int]] = {
    MemoryScope.USER: 4,
    MemoryScope.PROJECT: 3,
    MemoryScope.CHAT: 2,
    MemoryScope.AGENT: 1,
}


# ---------------------------------------------------------------------------
# MemorySourceType -- origin of an entry
# ---------------------------------------------------------------------------


class MemorySourceType(str, Enum):
    """Origin of a memory entry.

    The source type drives two policies:

    * Write restrictions.
        MemoryMode.READ_ONLY forbids every source EXCEPT IMPORT --
        imports are allowed in read-only mode when the operator
        explicitly approves them via the preview-to-apply flow,
        because the operator IS the user confirming the write.

    * Trust weighting.
        EXPLICIT_USER entries outrank everything else when
        relevance ties, because the user typed the fact themselves.
        EXTRACTION entries are the lowest-trust source because the
        extraction pipeline is heuristic.
    """

    EXPLICIT_USER = "explicit_user"
    """The user explicitly asked the system to remember this.

    Example: typing "remember that ..." or clicking "Save to memory"
    in the UI.
    """

    AGENT_TOOL = "agent_tool"
    """An agent invoked the memory_save tool during a run.

    The user granted the agent the capability, but did not type the
    memory themselves.
    """

    IMPORT = "import"
    """Imported from an external source.

    Sources include a JSON package, a Markdown file, or an external
    SQLite database, imported after an explicit preview-to-apply
    confirmation.
    """

    EXTRACTION = "extraction"
    """Produced automatically by the post-run extraction pipeline.

    Lowest trust; typically requires a minimum confidence score.
    """

    @property
    def is_automatic(self) -> bool:
        """True for sources produced WITHOUT a per-entry user action.

        Used by the UI to visually distinguish "auto-extracted"
        entries from explicit ones, and by the extraction pipeline
        to filter out its own prior output when re-running on the
        same conversation.
        """
        return self in _AUTOMATIC_SOURCES

    @property
    def is_user_initiated(self) -> bool:
        """Inverse of is_automatic.

        Provided for symmetry and for callers that prefer
        affirmative phrasing.
        """
        return not self.is_automatic


_AUTOMATIC_SOURCES: Final[frozenset[MemorySourceType]] = frozenset(
    {
        MemorySourceType.EXTRACTION,
        MemorySourceType.IMPORT,
    }
)


# ---------------------------------------------------------------------------
# MemoryStatus -- lifecycle state
# ---------------------------------------------------------------------------


class MemoryStatus(str, Enum):
    """Lifecycle status of a memory entry.

    Three states, deliberately few:

    * ACTIVE
        Normal state. The entry is returned by search and injected
        into prompts.

    * ARCHIVED
        Soft-deleted. The entry remains in the database (for
        restoration / audit), but is excluded from retrieval.
        Produced by user delete, or by policy after a long period
        of non-use if archival is enabled.

    * SUPERSEDED
        Replaced by a newer entry about the same topic. The older
        entry stays for audit; retrieval returns only the newer
        one. Produced by the conflict-resolution policy when two
        entries are semantically equivalent.
    """

    ACTIVE = "active"
    """Normal, retrievable state."""

    ARCHIVED = "archived"
    """Soft-deleted; excluded from retrieval."""

    SUPERSEDED = "superseded"
    """Replaced by a newer entry; excluded from retrieval."""

    @property
    def is_retrievable(self) -> bool:
        """True when an entry in this status participates in search.

        Only ACTIVE entries are retrieved. ARCHIVED and SUPERSEDED
        entries remain in the database for audit and restoration,
        but are invisible to the router and ranker. Centralizing
        this rule here prevents the common bug where a new query
        forgets to filter on status = 'active'.
        """
        return self is MemoryStatus.ACTIVE

    @property
    def is_terminal(self) -> bool:
        """True when this status represents a final state.

        Both ARCHIVED and SUPERSEDED are terminal in the sense that
        a normal user action cannot resurrect an entry without an
        explicit restore operation. This distinction is used by the
        audit log and by the archive-cleanup sweeper.
        """
        return self in (MemoryStatus.ARCHIVED, MemoryStatus.SUPERSEDED)


# ---------------------------------------------------------------------------
# MemoryMode -- subsystem operating mode
# ---------------------------------------------------------------------------


class MemoryMode(str, Enum):
    """Global operating mode of the memory subsystem.

    Applies to the WHOLE service instance (per user, per
    deployment). Per-project overrides are modeled as a nested
    MemorySettings in MemorySettings.project_overrides, not as a
    different mode enum.

    * DISABLED
        No reads, no writes. MemoryService.retrieve returns an
        empty MemoryContext immediately (fast path). The
        memory_save and memory_search tools report unavailable.

    * READ_ONLY
        Retrieval works; writes via memory_save, agent tools, and
        extraction are rejected with MemoryUnavailableError.
        Imports through the preview-to-apply flow are still
        allowed because the operator has explicitly confirmed them
        (see MemorySourceType.IMPORT).

    * READ_WRITE
        Full functionality.
    """

    DISABLED = "disabled"
    """No reads, no writes. Fast path in retrieve()."""

    READ_ONLY = "read_only"
    """Reads allowed, writes rejected except explicit imports."""

    READ_WRITE = "read_write"
    """Full read/write access."""

    @property
    def can_read(self) -> bool:
        """True when retrieval may proceed in this mode."""
        return self in (MemoryMode.READ_ONLY, MemoryMode.READ_WRITE)

    @property
    def can_write(self) -> bool:
        """True when writes via the standard channels may proceed.

        Explicit imports are exempt (they go through a separate
        confirmation flow); this property reflects the general
        write path only.
        """
        return self is MemoryMode.READ_WRITE


# ---------------------------------------------------------------------------
# Canonical frozen sets -- for validators and API layer
# ---------------------------------------------------------------------------


MEMORY_TYPES: Final[frozenset[MemoryType]] = frozenset(MemoryType)
"""All valid MemoryType members, as a frozen set."""

MEMORY_SCOPES: Final[frozenset[MemoryScope]] = frozenset(MemoryScope)
"""All valid MemoryScope members, as a frozen set."""

MEMORY_SOURCE_TYPES: Final[frozenset[MemorySourceType]] = frozenset(
    MemorySourceType
)
"""All valid MemorySourceType members, as a frozen set."""

MEMORY_STATUSES: Final[frozenset[MemoryStatus]] = frozenset(MemoryStatus)
"""All valid MemoryStatus members, as a frozen set."""

MEMORY_MODES: Final[frozenset[MemoryMode]] = frozenset(MemoryMode)
"""All valid MemoryMode members, as a frozen set."""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


__all__ = [
    # Enums
    "MemoryType",
    "MemoryScope",
    "MemorySourceType",
    "MemoryStatus",
    "MemoryMode",
    # Canonical frozen sets
    "MEMORY_TYPES",
    "MEMORY_SCOPES",
    "MEMORY_SOURCE_TYPES",
    "MEMORY_STATUSES",
    "MEMORY_MODES",
]
