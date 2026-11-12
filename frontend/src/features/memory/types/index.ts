// src/features/memory/types/index.ts

/**
 * Public type surface for the PELDRUN Memory feature.
 *
 * This is the barrel file for every type, constant, class,
 * function, and type-guard the memory feature exposes to the
 * rest of the application. Consumers (API client, hooks,
 * components, pages) SHOULD import from this module:
 *
 *     import type { MemoryEntry, MemorySettings } from
 *       '@/features/memory/types';
 *     import { MemoryApiError, classifyMemoryError } from
 *       '@/features/memory/types';
 *
 * not from the individual files. This decouples consumers from
 * the internal file layout -- splitting or merging a types file
 * later requires no import changes outside this directory.
 *
 * What this module RE-EXPORTS
 * ---------------------------
 *
 *   * `./enums`         -- string-literal enums and their
 *                         guards (MemoryType, MemoryScope, ...).
 *
 *   * `./entry`         -- the core `MemoryEntry` entity,
 *                         `MemoryProvenance`, scope-ref helpers,
 *                         and `MemoryEntryDraft`.
 *
 *   * `./settings`      -- `MemorySettings` and its parts,
 *                         plus bounds, patch/override shapes,
 *                         and the merge/compare helpers.
 *
 *   * `./responses`     -- per-endpoint success envelopes.
 *
 *   * `./errors`        -- wire-shape types, `MemoryApiError`,
 *                         the classifier, and the UX policy.
 *
 *   * `./import-export` -- import/export request shapes,
 *                         file helpers, and wizard state.
 *
 * What this module does NOT re-export
 * -----------------------------------
 *
 *   * Internal constants and helpers inside each file (e.g.
 *     `_patchableFieldsAreExhaustive` in `settings.ts`,
 *     `normalizeFields` in `errors.ts`). These are intentionally
 *     module-private -- TypeScript's `export *` does not touch
 *     unexported symbols, and no consumer should depend on
 *     them.
 *
 *   * The wire-shape interfaces from `errors.ts` under
 *     conventional short names. Those interfaces
 *     (`MemoryErrorEnvelope`, `MemoryHttpExceptionResponse`,
 *     `MemoryFastApiValidationResponse`) are exposed because
 *     tests and diagnostics reference them, but consumers
 *     should reach for `MemoryApiError` and
 *     `classifyMemoryError` in normal code -- the wire shapes
 *     are the classifier's INPUT contract, not the error
 *     system's output.
 *
 * Ordering
 * --------
 *
 * Exports are listed in DEPENDENCY order (enums -> entry ->
 * settings -> responses -> errors -> import-export) so that
 * reading this file top-to-bottom follows the same layering the
 * individual modules use. It also makes accidental circular
 * re-exports easier to spot: a lower module must never need to
 * import from a higher one.
 */

// ---------------------------------------------------------------------------
// Enums and guards
// ---------------------------------------------------------------------------
//
// String-literal unions for every closed-set value on the wire.
// Import the CONST object when a runtime value is needed
// (select dropdowns, default props); import the TYPE when only
// compile-time narrowing is needed.

export * from './enums';

// ---------------------------------------------------------------------------
// Core entity
// ---------------------------------------------------------------------------
//
// `MemoryEntry` is the unit the whole feature orbits around.
// `MemoryEntryDraft` is the client-side editing shape; it is
// deliberately separate from `MemoryEntry` (see entry.ts).

export * from './entry';

// ---------------------------------------------------------------------------
// Settings
// ---------------------------------------------------------------------------
//
// The configuration surface, its bounds, patch/override shapes,
// and the pure helpers (`computeEffectiveOverride`,
// `mergeProjectSettings`, `isNoOpPatch`) that the settings form
// and project settings form rely on.

export * from './settings';

// ---------------------------------------------------------------------------
// HTTP response envelopes
// ---------------------------------------------------------------------------
//
// One interface per endpoint. Consumers narrow into these via
// `isOkEnvelope` when a union with `MemoryApiError` would
// otherwise force casts.

export * from './responses';

// ---------------------------------------------------------------------------
// Errors
// ---------------------------------------------------------------------------
//
// The class every API call throws (`MemoryApiError`), the
// classifier every API call uses (`classifyMemoryError`), the
// error-code constants (`MemoryErrorType`), and the UX policy
// helper (`uxHintFor`) plus its predicates. This is the ONLY
// module consumers may catch from.

export * from './errors';

// ---------------------------------------------------------------------------
// Import / export helpers
// ---------------------------------------------------------------------------
//
// Request shapes and client-only presentation helpers. Kept
// last because it depends on shapes from `./entry`,
// `./enums`, and `./responses` (the manifest type).

export * from './import-export';
