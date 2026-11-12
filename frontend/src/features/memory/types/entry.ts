// src/features/memory/types/entry.ts

/**
 * The core entity types for the PELDRUN Memory subsystem.
 *
 * This module mirrors ``peldrun.memory.domain.entry`` one-to-one.
 * Field names are deliberately snake_case because they map directly
 * to the wire format produced by the backend's
 * ``model_dump(mode="json")`` -- there is NO transformation layer,
 * so a mismatch between a field name here and a field name on the
 * wire surfaces as a TypeScript compile error at the call site,
 * not as a silent ``undefined`` in production.
 *
 * Why snake_case rather than the usual TS camelCase convention:
 *
 *   * The spec's "Type safety end-to-end" principle puts the
 *     Pydantic model at the source of truth. Renaming on the
 *     boundary adds a mapping layer that is itself a bug magnet
 *     (esp. around nested objects like ``provenance``).
 *
 *   * The backend's ``_entry_to_dict`` helper does NOT rename
 *     fields -- it only appends a computed ``scope`` field. So
 *     the wire payload IS the model, snake_case included.
 *
 *   * Consumers who want camelCase in component code can create
 *     a view-model adapter; that adapter is easier to audit than
 *     a symmetric rename hidden inside the API client.
 *
 * If the backend ever renames a field, the compiler catches it on
 * the first build. No runtime surprises.
 */

import {
  isMemoryScope,
  type MemoryScope,
  type MemorySourceType,
  type MemoryStatus,
  type MemoryType,
} from './enums';

// ---------------------------------------------------------------------------
// Provenance
// ---------------------------------------------------------------------------

/**
 * Where a memory entry came from.
 *
 * Mirrors ``peldrun.memory.domain.entry.MemoryProvenance``.
 *
 * The ``source_type`` field is the security-relevant one: the
 * backend's user-scope write rule accepts ONLY
 * ``source_type='explicit_user'`` for ``scope_type='user'``. The
 * UI must therefore never offer another source type when the
 * target scope is ``user`` (see ``EntryEditor``).
 *
 * The ``source_chat_id`` and ``imported_from`` fields are audit
 * metadata -- they answer "which conversation produced this?" or
 * "which package was this restored from?". They are null when
 * not applicable (e.g. a manually-created entry has no source
 * chat).
 */
export interface MemoryProvenance {
  /** The write channel that created this entry. */
  source_type: MemorySourceType;

  /**
   * The originating chat, if the entry was extracted or saved
   * from a conversation. ``null`` for entries created without a
   * chat context (e.g. the settings page's "add entry" form).
   */
  source_chat_id: string | null;

  /**
   * The import source for restored entries, or a
   * ``"promoted_from:<scope>"`` marker for entries created via
   * ``POST /{id}/promote``. ``null`` otherwise.
   */
  imported_from: string | null;
}

// ---------------------------------------------------------------------------
// Scope reference
// ---------------------------------------------------------------------------

/**
 * A structured memory-scope identifier.
 *
 * Pairs a ``MemoryScope`` enum value with a concrete owner id.
 * This is the form the UI uses to build requests and to parse
 * URL query parameters like ``?scope=project:acme``.
 *
 * The wire format for a scope reference is the STRING form
 * (``"<scope_type>:<scope_id>"``). Use ``formatScopeRef`` /
 * ``parseScopeRef`` to move between the two representations.
 */
export interface MemoryScopeRef {
  scope_type: MemoryScope;
  scope_id: string;
}

/**
 * The serialized form of a ``MemoryScopeRef``: ``"<type>:<id>"``.
 *
 * This is what appears in every endpoint's ``scope`` field
 * (e.g. ``"project:acme"``) and in ``?scope=`` query parameters.
 * It is a plain string so that it can flow through URLs, query
 * keys, and React props without object-identity concerns.
 */
export type MemoryScopeRefString = string;

// ---------------------------------------------------------------------------
// MemoryEntry
// ---------------------------------------------------------------------------

/**
 * A single, atomic unit of stored knowledge.
 *
 * Mirrors ``peldrun.memory.domain.entry.MemoryEntry``. This is
 * NOT a conversation log entry -- it is a durable fact, decision,
 * preference, or procedure that survives across turns and, in
 * the ``user`` scope, across projects.
 *
 * Field-by-field contract:
 *
 *   * ``memory_id``      -- stable identifier prefixed ``mem_``.
 *                           Treat as opaque; never parse.
 *
 *   * ``content``        -- the memory's text. May contain
 *                           Markdown; render with react-markdown
 *                           in display contexts (never ``innerHTML``).
 *
 *   * ``memory_type``    -- the KIND of knowledge (see
 *                           ``MemoryType``). Immutable via the
 *                           generic PATCH? No -- the backend's
 *                           ``_UpdateBody`` DOES accept
 *                           ``memory_type``, so it can be changed.
 *
 *   * ``scope_type``     -- one of user/project/chat/agent. The
 *                           backend forbids moving an entry
 *                           between scopes; use ``/promote`` to
 *                           COPY into the user scope instead.
 *
 *   * ``scope_id``       -- the concrete owner (project id, chat
 *                           id, user id, agent id). Together with
 *                           ``scope_type`` it uniquely locates
 *                           the entry.
 *
 *   * ``importance``     -- 0..100. Feeds the ranker's
 *                           ``importance/100`` signal (weight
 *                           0.20). Not a quality score; a
 *                           criticality score.
 *
 *   * ``confidence``     -- 0.0..1.0. Set by the write pipeline
 *                           for auto-extracted entries; always
 *                           1.0 for explicit user writes. Feeds
 *                           an auxiliary quality signal in the
 *                           ranker.
 *
 *   * ``status``         -- lifecycle state (active / archived /
 *                           superseded). ``active`` entries are
 *                           the only ones retrieval considers.
 *
 *   * ``is_pinned``      -- when true, the ranker's final score
 *                           is multiplied by the configured
 *                           ``boost_pinned`` factor. Reversible
 *                           via ``POST /{id}/pin``.
 *
 *   * ``tags``           -- user-supplied labels for filtering
 *                           and grouping. Max 32 tags per entry,
 *                           each ≤64 chars (enforced at the API
 *                           boundary).
 *
 *   * ``provenance``     -- see ``MemoryProvenance``.
 *
 *   * ``created_at`` /
 *     ``updated_at``     -- ISO-8601 timestamps with a ``Z`` suffix.
 *                           ``updated_at`` advances on any field
 *                           change, including pin/unpin.
 *
 *   * ``last_used_at``   -- last time this entry was retrieved
 *                           into a prompt. ``null`` until first
 *                           retrieval. Written by the usage
 *                           tracking layer, not by user actions.
 *
 *   * ``expires_at``     -- optional auto-expiry timestamp. When
 *                           ``null``, the entry never auto-expires.
 *
 *   * ``content_hash``   -- sha256 of the normalized content.
 *                           INTERNAL: used by the write pipeline
 *                           to detect duplicates. Present in the
 *                           payload for transparency but should
 *                           NOT be displayed in the UI.
 *
 *   * ``scope``          -- computed convenience field:
 *                           ``"<scope_type>:<scope_id>"``. Present
 *                           on every endpoint that returns entries
 *                           via the ``_entry_to_dict`` helper
 *                           (list, get, create, update, promote,
 *                           pin, search results). MAY BE ABSENT
 *                           when an entry is nested inside a
 *                           wrapper payload that serializes
 *                           through Pydantic directly (e.g. the
 *                           import preview's ``candidates``).
 *                           Always derive from ``scope_type`` and
 *                           ``scope_id`` when you need certainty
 *                           -- see ``entryScopeRef()``.
 */
export interface MemoryEntry {
  memory_id: string;
  content: string;
  memory_type: MemoryType;
  scope_type: MemoryScope;
  scope_id: string;
  importance: number;
  confidence: number;
  status: MemoryStatus;
  is_pinned: boolean;
  tags: string[];
  provenance: MemoryProvenance;
  created_at: string;
  updated_at: string;
  last_used_at: string | null;
  expires_at: string | null;
  content_hash: string;
  scope?: MemoryScopeRefString;
}

// ---------------------------------------------------------------------------
// Scope ref helpers
// ---------------------------------------------------------------------------

/**
 * Serialize a ``MemoryScopeRef`` to the wire form ``"<type>:<id>"``.
 *
 * The only correct way to build a scope-ref string in the
 * frontend. Do NOT concatenate ``scope_type + ":" + scope_id``
 * manually -- a stray colon in the id (which ``MemoryScopeRef``
 * validation rejects on the backend, but which can arrive from a
 * URL parameter) would produce an ambiguous reference that the
 * API rejects with a confusing error.
 */
export function formatScopeRef(ref: MemoryScopeRef): MemoryScopeRefString {
  return `${ref.scope_type}:${ref.scope_id}`;
}

/**
 * Parse a wire-form scope ref back into a structured ref.
 *
 * Returns ``null`` for anything that cannot be a valid
 * ``<scope_type>:<scope_id>`` pair (no colon, empty parts, or an
 * unknown scope type). Used by URL-param parsers and by
 * defensive code that receives a scope ref from an untrusted
 * source (URL, localStorage, a query-string on a link).
 */
export function parseScopeRef(
  ref: string | null | undefined,
): MemoryScopeRef | null {
  if (!ref || typeof ref !== 'string') return null;
  const idx = ref.indexOf(':');
  if (idx <= 0 || idx === ref.length - 1) return null;
  const scope_type = ref.slice(0, idx);
  if (!isMemoryScope(scope_type)) return null;
  return {
    scope_type,
    scope_id: ref.slice(idx + 1),
  };
}

/**
 * Return the canonical scope ref for an entry.
 *
 * Prefers the computed ``scope`` field when present (avoids a
 * string allocation and matches what the backend sent). Falls
 * back to building the ref from ``scope_type`` and ``scope_id``
 * -- which is required for entries nested inside wrapper
 * payloads where the backend did not append the convenience
 * field.
 */
export function entryScopeRef(entry: MemoryEntry): MemoryScopeRefString {
  return entry.scope ?? `${entry.scope_type}:${entry.scope_id}`;
}

// ---------------------------------------------------------------------------
// Draft (client-side editing shape)
// ---------------------------------------------------------------------------

/**
 * The shape of an in-progress create/edit form.
 *
 * Deliberately SEPARATE from ``MemoryEntry``. Reasons:
 *
 *   * Drafts are partially-filled (every field optional); the
 *     entry is always fully-populated.
 *
 *   * Drafts carry UI-only state (e.g. ``contentTouched``) that
 *     must never leak to the API.
 *
 *   * Server-side mutations always construct a fresh object from
 *     the draft -- they never send the draft verbatim. This
 *     prevents the classic "sent the whole server object back"
 *     bug where a stale ``updated_at`` or ``content_hash``
 *     causes a PATCH conflict.
 *
 * The store that owns drafts lives in
 * ``features/memory/stores/memory-draft-store.ts``.
 */
export interface MemoryEntryDraft {
  content: string;
  memory_type: MemoryType;
  scope_type: MemoryScope;
  scope_id: string;
  importance: number;
  tags: string[];
  is_pinned: boolean;

  /**
   * Tracks whether the user has touched the ``content`` textarea.
   * Used by ``EntryEditor`` to decide when to fire the
   * "similar memories" preview (only after a real edit, not on
   * mount). Never sent to the API.
   */
  contentTouched: boolean;
}
