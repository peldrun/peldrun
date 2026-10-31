// Path: frontend/src/components/chat/composer/plugins/image-paste-plugin.tsx
'use client';

/**
 * Lexical plugin that intercepts image pastes in the composer and dispatches
 * them to the host component instead of letting the browser insert a base64
 * data URI directly into the editor state.
 *
 * Why this exists
 * ---------------
 * The composer intentionally does NOT model images as Lexical nodes:
 *
 *   - Base64 data URIs would inflate the editor state with hundreds of
 *     kilobytes per image, which the OnChangePlugin would reserialize on
 *     every keystroke.
 *   - The composer's draft persistence (sessionStorage) would be polluted
 *     with binary payloads.
 *   - The upload pipeline (POST /api/files/upload-temp) needs to be an
 *     explicit, observable step so the UI can show progress and handle
 *     failures — not a silent side-effect of the browser's default paste.
 *
 * Instead, images are treated as opaque File objects owned by the host's
 * state. This plugin is the bridge between the browser's clipboard event
 * and that state.
 *
 * Scope boundaries
 * ----------------
 * - TEXT pastes are not intercepted; Lexical's default handling applies.
 * - DRAG AND DROP is not handled here. The composer and the chat-landing
 *   component already provide their own container-level onDrop handlers,
 *   and duplicating that logic inside this plugin would cause conflicting
 *   handlers on the same drop target.
 * - HTML-embedded images (`<img src="...">` inside `text/html` clipboard
 *   payloads, e.g. from a web page copy) are NOT supported, because the
 *   clipboard exposes them as HTML markup rather than binary file data.
 *   The correct user flow is a direct image copy or screenshot (which
 *   places binary `image/*` data in the clipboard).
 */

import { useEffect } from 'react';
import { useLexicalComposerContext } from '@lexical/react/LexicalComposerContext';
import { COMMAND_PRIORITY_HIGH, PASTE_COMMAND } from 'lexical';

export interface ImagePastePluginProps {
  /**
   * Invoked once per pasted image file, in clipboard order.
   *
   * May return a promise; the plugin does NOT await it — the host is
   * responsible for tracking per-file upload state. Rejections are
   * caught and logged so they never crash the paste handler.
   */
  onImagePasted: (file: File) => void | Promise<void>;
}

/**
 * Collect image files from a clipboard DataTransfer in a browser-portable
 * way.
 *
 * Precedence:
 *   1. `clipboardData.items` — populated by Chrome, Edge, Firefox, and
 *      recent Safari. Preserves paste order and item kind.
 *   2. `clipboardData.files` — the only populated source in older Safari
 *      and some OS clipboard bridges.
 *
 * Both are checked so that no browser silently drops a pasted image.
 */
function collectImageFiles(clipboardData: DataTransfer): File[] {
  const result: File[] = [];

  const items = clipboardData.items;
  if (items) {
    for (const item of items) {
      if (item.kind === 'file' && item.type.startsWith('image/')) {
        const file = item.getAsFile();
        if (file) result.push(file);
      }
    }
  }

  if (result.length === 0 && clipboardData.files) {
    for (const file of clipboardData.files) {
      if (file.type.startsWith('image/')) {
        result.push(file);
      }
    }
  }

  return result;
}

export function ImagePastePlugin({
  onImagePasted,
}: ImagePastePluginProps): null {
  const [editor] = useLexicalComposerContext();

  useEffect(() => {
    return editor.registerCommand(
      PASTE_COMMAND,
      (event) => {
        // Read-only editor: `DynamicEditablePlugin` sets this when the
        // composer is disabled (e.g. an in-flight run). Never intercept.
        if (!editor.isEditable()) {
          return false;
        }

        if (!event || !('clipboardData' in event) || !event.clipboardData) {
          return false;
        }

        const clipboardData = event.clipboardData;
        const imageFiles = collectImageFiles(clipboardData);
        if (imageFiles.length === 0) {
          // No image content: let the default text paste proceed.
          return false;
        }

        // Consume the paste so the browser never inserts an inline
        // data-URI <img> into the contenteditable. Doing so would
        // (a) inflate the editor state, and (b) silently bypass the
        // upload pipeline.
        event.preventDefault();
        event.stopPropagation();

        for (const file of imageFiles) {
          try {
            const result = onImagePasted(file);
            if (
              result &&
              typeof (result as Promise<void>).catch === 'function'
            ) {
              (result as Promise<void>).catch((err) => {
                console.error(
                  '[ImagePastePlugin] onImagePasted rejected for',
                  file.name,
                  err,
                );
              });
            }
          } catch (err) {
            console.error(
              '[ImagePastePlugin] onImagePasted threw for',
              file.name,
              err,
            );
          }
        }

        return true;
      },
      COMMAND_PRIORITY_HIGH,
    );
  }, [editor, onImagePasted]);

  return null;
}

export default ImagePastePlugin;
