// Path: frontend/src/components/skills/skill-editor-serialization.ts

/**
 * Markdown <-> Lexical serialization helpers for the Skill editor.
 *
 * This module is intentionally self-contained: it does NOT import
 * anything from `components/chat/composer/`. The composer and the skill
 * editor use different transformer sets (the editor additionally needs
 * fenced code blocks and ordered/unordered lists, which the composer
 * deliberately omits), so keeping them separate prevents any
 * cross-contamination when one side evolves.
 *
 * Public exports:
 *
 *   SKILL_EDITOR_TRANSFORMERS  — full markdown transformer list used by
 *                                both MarkdownShortcutPlugin (live typing)
 *                                and $convertFrom/ToMarkdownString.
 *   extractSkillMarkdown       — serialize the current editor state to
 *                                a markdown string (trimmed).
 *   loadSkillMarkdown          — replace the editor state with the parsed
 *                                form of a markdown string. Must be
 *                                called inside `editor.update(...)`.
 */

import {
  $convertFromMarkdownString,
  $convertToMarkdownString,
  CODE,
  HEADING,
  LINK,
  ORDERED_LIST,
  QUOTE,
  TEXT_FORMAT_TRANSFORMERS,
  UNORDERED_LIST,
  type Transformer,
} from '@lexical/markdown';
import type { LexicalEditor } from 'lexical';

/**
 * Full transformer set used by the Skill editor.
 *
 * Block-level (order matters — more specific patterns first):
 *   - CODE             fenced ``` code blocks
 *   - HEADING          # ## ### (and lower levels)
 *   - QUOTE            > blockquote
 *   - ORDERED_LIST     1. 2. 3.
 *   - UNORDERED_LIST   - * +
 *
 * Inline (order matters — bold+italic before bold before italic):
 *   - LINK             [label](url)
 *   - TEXT_FORMAT_TRANSFORMERS   bold, italic, strikethrough, code
 *
 * Note: the composer's custom inline regex transformers (the
 * BOLD_STAR_MATCH family in `serialization.ts`) are deliberately NOT
 * reused here — they implement composer-specific behavior (trailing
 * space insertion after links, cursor placement, etc.) that would
 * produce different markdown output on round-trip. The standard
 * TEXT_FORMAT_TRANSFORMERS from @lexical/markdown are sufficient for
 * SKILL.md authoring, where predictable round-trip fidelity matters
 * more than live-typing UX niceties.
 */
export const SKILL_EDITOR_TRANSFORMERS: ReadonlyArray<Transformer> = [
  CODE,
  HEADING,
  QUOTE,
  ORDERED_LIST,
  UNORDERED_LIST,
  LINK,
  ...TEXT_FORMAT_TRANSFORMERS,
];

/**
 * Serialize the current editor state to a trimmed markdown string.
 *
 * Returns an empty string when the editor is empty. The caller is
 * expected to treat "" as "nothing to save" — the server rejects empty
 * bodies with a 422, so the client should short-circuit before issuing
 * the PUT.
 */
export function extractSkillMarkdown(editor: LexicalEditor): string {
  let markdownText = '';
  editor.getEditorState().read(() => {
    markdownText = $convertToMarkdownString(
      SKILL_EDITOR_TRANSFORMERS as Transformer[],
    );
  });
  return markdownText.trim();
}

/**
 * Replace the current editor state with the parsed form of ``markdown``.
 *
 * MUST be called from inside ``editor.update(() => ...)`` — Lexical
 * requires all node-mutating operations to run within a transaction.
 *
 * Empty input is a no-op for the parser; the editor ends up with a
 * single empty paragraph, which is the canonical "empty editor" state.
 */
export function loadSkillMarkdown(
  markdown: string,
  transformers: ReadonlyArray<Transformer> = SKILL_EDITOR_TRANSFORMERS,
): void {
  $convertFromMarkdownString(
    markdown || '',
    transformers as Transformer[],
  );
}
