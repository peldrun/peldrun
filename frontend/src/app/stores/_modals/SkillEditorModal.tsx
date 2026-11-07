// Path: frontend/src/app/stores/_modals/SkillEditorModal.tsx

"use client";

/**
 * Full-page modal that lets the operator edit the Markdown body of a
 * SKILL.md file through a Lexical editor.
 *
 * Scope boundaries:
 *
 *   - Only the BODY is edited. The YAML frontmatter block is shown
 *     read-only in a dedicated panel and is preserved server-side
 *     verbatim by ``SkillManager.update_skill_instructions``.
 *
 *   - This module is deliberately self-contained. It imports NOTHING
 *     from ``components/chat/composer/`` — the composer and the skill
 *     editor use different transformer sets and different node trees.
 *     Any coupling between the two would risk cross-contamination when
 *     either side evolves.
 *
 *   - The editor's initial state is loaded exactly once per open (see
 *     ``LoadMarkdownPlugin``). Re-rendering the parent does NOT reset
 *     the cursor or lose typed characters.
 */

import React, {
  useCallback,
  useEffect,
  useRef,
  useState,
} from "react";
import {
  AlertTriangle,
  Bold,
  Code,
  Heading1,
  Heading2,
  Heading3,
  Italic,
  Link as LinkIcon,
  List,
  ListOrdered,
  Loader2,
  Lock,
  Quote,
  Redo2,
  Save,
  Strikethrough,
  Underline,
  Undo2,
  X,
} from "lucide-react";
import { LexicalComposer } from "@lexical/react/LexicalComposer";
import { RichTextPlugin } from "@lexical/react/LexicalRichTextPlugin";
import { ContentEditable } from "@lexical/react/LexicalContentEditable";
import { HistoryPlugin } from "@lexical/react/LexicalHistoryPlugin";
import { OnChangePlugin } from "@lexical/react/LexicalOnChangePlugin";
import { LexicalErrorBoundary } from "@lexical/react/LexicalErrorBoundary";
import { MarkdownShortcutPlugin } from "@lexical/react/LexicalMarkdownShortcutPlugin";
import { ListPlugin } from "@lexical/react/LexicalListPlugin";
import { LinkPlugin } from "@lexical/react/LexicalLinkPlugin";
import { useLexicalComposerContext } from "@lexical/react/LexicalComposerContext";
import {
  $createHeadingNode,
  $createQuoteNode,
  $isHeadingNode,
  $isQuoteNode,
  HeadingNode,
  QuoteNode,
  type HeadingTagType,
} from "@lexical/rich-text";
import {
  $createListItemNode,
  $createListNode,
  $isListNode,
  INSERT_ORDERED_LIST_COMMAND,
  INSERT_UNORDERED_LIST_COMMAND,
  ListNode,
  ListItemNode,
  REMOVE_LIST_COMMAND,
} from "@lexical/list";
import {
  $createCodeNode,
  $isCodeNode,
  CodeNode,
  CodeHighlightNode,
} from "@lexical/code";
import { LinkNode, AutoLinkNode } from "@lexical/link";
import {
  $createParagraphNode,
  $getSelection,
  $isRangeSelection,
  $isRootOrShadowRoot,
  CAN_REDO_COMMAND,
  CAN_UNDO_COMMAND,
  COMMAND_PRIORITY_CRITICAL,
  FORMAT_TEXT_COMMAND,
  REDO_COMMAND,
  SELECTION_CHANGE_COMMAND,
  UNDO_COMMAND,
  type EditorState,
  type LexicalEditor,
} from "lexical";
import { $setBlocksType } from "@lexical/selection";
import { $findMatchingParent, mergeRegister } from "@lexical/utils";

import { showToast } from "@/components/ui/ToastNotification";
import type { SkillPreview, SkillSummary } from "../_types/store";
import { getApiUrl } from "../_hooks/useStoreData";
import { SkillIcon } from "../_components/SkillIcon";
import {
  SKILL_EDITOR_TRANSFORMERS,
  extractSkillMarkdown,
  loadSkillMarkdown,
} from "@/components/skills/skill-editor-serialization";
import { SkillFloatingToolbarPlugin } from "@/components/skills/editor/skill-floating-toolbar-plugin";


// ---------------------------------------------------------------------------
// Props
// ---------------------------------------------------------------------------

export interface SkillEditorModalProps {
  open: boolean;
  /** The skill (parent router) being edited. Null when closed. */
  skill: SkillSummary | null;
  /**
   * The sub-skill being edited, or ``null`` to edit the parent body.
   *
   * When set, the modal:
   *   * fetches its body from ``GET /api/skills/{id}/sub-skills/{sub}/preview``
   *   * saves via ``PUT /api/skills/{id}/sub-skills/{sub}/instructions``
   *   * shows the sub-skill ID in the title breadcrumb
   *   * preserves the sub-skill's own YAML frontmatter (server-side)
   *
   * Defaults to ``null`` for backward compatibility with callers that
   * only edit the parent body.
   */
  subSkillId?: string | null;
  onClose: () => void;
  /**
   * Called after a successful save with the updated parent skill so
   * the parent can merge it into its ``skills`` list without
   * refetching. For sub-skill edits, the parent's metadata is
   * unchanged — the callback receives the same record and the merge
   * is idempotent.
   */
  onSaved?: (updatedSkill: SkillSummary) => void;
}


// ---------------------------------------------------------------------------
// Internal plugin: load initial markdown exactly once per mount
// ---------------------------------------------------------------------------

interface LoadMarkdownPluginProps {
  initialMarkdown: string;
}

function LoadMarkdownPlugin({ initialMarkdown }: LoadMarkdownPluginProps) {
  const [editor] = useLexicalComposerContext();
  const loadedRef = useRef(false);

  useEffect(() => {
    if (loadedRef.current) return;
    loadedRef.current = true;
    editor.update(() => {
      loadSkillMarkdown(initialMarkdown);
    });
  }, [editor, initialMarkdown]);

  return null;
}


// ---------------------------------------------------------------------------
// Internal plugin: fixed toolbar with formatting buttons
// ---------------------------------------------------------------------------

interface ToolbarButtonProps {
  onClick: () => void;
  isActive?: boolean;
  disabled?: boolean;
  title: string;
  children: React.ReactNode;
}

function ToolbarButton({
  onClick,
  isActive = false,
  disabled = false,
  title,
  children,
}: ToolbarButtonProps) {
  return (
    <button
      type="button"
      onMouseDown={(e) => e.preventDefault()}
      onClick={onClick}
      disabled={disabled}
      title={title}
      aria-label={title}
      className={`p-1.5 rounded transition-colors cursor-pointer disabled:opacity-40 disabled:cursor-not-allowed ${
        isActive
          ? "bg-primary/15 text-primary"
          : "text-muted-foreground hover:text-foreground hover:bg-muted"
      }`}
    >
      {children}
    </button>
  );
}

function Divider() {
  return <div className="w-px h-4 bg-border/60 mx-1" aria-hidden="true" />;
}

function SkillEditorToolbarPlugin() {
  const [editor] = useLexicalComposerContext();

  const [isBold, setIsBold] = React.useState(false);
  const [isItalic, setIsItalic] = React.useState(false);
  const [isUnderline, setIsUnderline] = React.useState(false);
  const [isStrikethrough, setIsStrikethrough] = React.useState(false);
  const [isCode, setIsCode] = React.useState(false);
  const [blockType, setBlockType] = React.useState<string>("paragraph");
  const [canUndo, setCanUndo] = React.useState(false);
  const [canRedo, setCanRedo] = React.useState(false);

  const updateToolbar = React.useCallback(() => {
    const selection = $getSelection();
    if (!$isRangeSelection(selection)) {
      return;
    }

    setIsBold(selection.hasFormat("bold"));
    setIsItalic(selection.hasFormat("italic"));
    setIsUnderline(selection.hasFormat("underline"));
    setIsStrikethrough(selection.hasFormat("strikethrough"));
    setIsCode(selection.hasFormat("code"));

    const anchorNode = selection.anchor.getNode();
    const element =
      anchorNode.getKey() === "root"
        ? anchorNode
        : $findMatchingParent(anchorNode, (e) => {
            const parent = e.getParent();
            return parent !== null && $isRootOrShadowRoot(parent);
          });

    if (!element) return;

    if ($isHeadingNode(element)) {
      setBlockType(element.getTag());
    } else if ($isQuoteNode(element)) {
      setBlockType("quote");
    } else if ($isCodeNode(element)) {
      setBlockType("code");
    } else if ($isListNode(element)) {
      setBlockType(element.getListType());
    } else {
      setBlockType("paragraph");
    }
  }, []);

  React.useEffect(() => {
    return mergeRegister(
      editor.registerUpdateListener(({ editorState }) => {
        editorState.read(() => {
          updateToolbar();
        });
      }),
      editor.registerCommand(
        SELECTION_CHANGE_COMMAND,
        () => {
          updateToolbar();
          return false;
        },
        COMMAND_PRIORITY_CRITICAL,
      ),
      editor.registerCommand(
        CAN_UNDO_COMMAND,
        (payload) => {
          setCanUndo(payload);
          return false;
        },
        COMMAND_PRIORITY_CRITICAL,
      ),
      editor.registerCommand(
        CAN_REDO_COMMAND,
        (payload) => {
          setCanRedo(payload);
          return false;
        },
        COMMAND_PRIORITY_CRITICAL,
      ),
    );
  }, [editor, updateToolbar]);

  const formatText = (
    format: "bold" | "italic" | "underline" | "strikethrough" | "code",
  ) => {
    editor.dispatchCommand(FORMAT_TEXT_COMMAND, format);
  };

  const setParagraph = () => {
    editor.update(() => {
      const selection = $getSelection();
      if (!$isRangeSelection(selection)) return;
      $setBlocksType(selection, () => $createParagraphNode());
    });
  };

  const setHeading = (tag: HeadingTagType) => {
    editor.update(() => {
      const selection = $getSelection();
      if (!$isRangeSelection(selection)) return;
      $setBlocksType(selection, () => $createHeadingNode(tag));
    });
  };

  const setQuote = () => {
    editor.update(() => {
      const selection = $getSelection();
      if (!$isRangeSelection(selection)) return;
      $setBlocksType(selection, () => $createQuoteNode());
    });
  };

  const setCodeBlock = () => {
    editor.update(() => {
      const selection = $getSelection();
      if (!$isRangeSelection(selection)) return;
      $setBlocksType(selection, () => $createCodeNode());
    });
  };

  const toggleList = (type: "bullet" | "number") => {
    if (type === "bullet") {
      if (blockType === "bullet") {
        editor.dispatchCommand(REMOVE_LIST_COMMAND, undefined);
      } else {
        editor.dispatchCommand(INSERT_UNORDERED_LIST_COMMAND, undefined);
      }
    } else {
      if (blockType === "number") {
        editor.dispatchCommand(REMOVE_LIST_COMMAND, undefined);
      } else {
        editor.dispatchCommand(INSERT_ORDERED_LIST_COMMAND, undefined);
      }
    }
  };

  const insertLink = () => {
    const url = window.prompt("Enter link URL:");
    if (!url) return;
    editor.update(() => {
      const selection = $getSelection();
      if ($isRangeSelection(selection)) {
        const text = selection.getTextContent() || url;
        selection.insertText(`[${text}](${url})`);
      }
    });
  };

  return (
    <div className="px-6 py-2 border-b border-border bg-muted/20 flex items-center gap-0.5 flex-wrap shrink-0">
      {/* Undo / Redo */}
      <ToolbarButton
        onClick={() => editor.dispatchCommand(UNDO_COMMAND, undefined)}
        disabled={!canUndo}
        title="Undo (Ctrl+Z)"
      >
        <Undo2 size={14} />
      </ToolbarButton>
      <ToolbarButton
        onClick={() => editor.dispatchCommand(REDO_COMMAND, undefined)}
        disabled={!canRedo}
        title="Redo (Ctrl+Y)"
      >
        <Redo2 size={14} />
      </ToolbarButton>

      <Divider />

      {/* Block type */}
      <ToolbarButton
        onClick={setParagraph}
        isActive={blockType === "paragraph"}
        title="Paragraph"
      >
        <span className="text-xs font-mono px-0.5">P</span>
      </ToolbarButton>
      <ToolbarButton
        onClick={() => setHeading("h1")}
        isActive={blockType === "h1"}
        title="Heading 1"
      >
        <Heading1 size={14} />
      </ToolbarButton>
      <ToolbarButton
        onClick={() => setHeading("h2")}
        isActive={blockType === "h2"}
        title="Heading 2"
      >
        <Heading2 size={14} />
      </ToolbarButton>
      <ToolbarButton
        onClick={() => setHeading("h3")}
        isActive={blockType === "h3"}
        title="Heading 3"
      >
        <Heading3 size={14} />
      </ToolbarButton>

      <Divider />

      {/* Inline formatting */}
      <ToolbarButton
        onClick={() => formatText("bold")}
        isActive={isBold}
        title="Bold (Ctrl+B)"
      >
        <Bold size={14} />
      </ToolbarButton>
      <ToolbarButton
        onClick={() => formatText("italic")}
        isActive={isItalic}
        title="Italic (Ctrl+I)"
      >
        <Italic size={14} />
      </ToolbarButton>
      <ToolbarButton
        onClick={() => formatText("underline")}
        isActive={isUnderline}
        title="Underline (Ctrl+U)"
      >
        <Underline size={14} />
      </ToolbarButton>
      <ToolbarButton
        onClick={() => formatText("strikethrough")}
        isActive={isStrikethrough}
        title="Strikethrough"
      >
        <Strikethrough size={14} />
      </ToolbarButton>
      <ToolbarButton
        onClick={() => formatText("code")}
        isActive={isCode}
        title="Inline code"
      >
        <Code size={14} />
      </ToolbarButton>

      <Divider />

      {/* Lists */}
      <ToolbarButton
        onClick={() => toggleList("bullet")}
        isActive={blockType === "bullet"}
        title="Bullet list"
      >
        <List size={14} />
      </ToolbarButton>
      <ToolbarButton
        onClick={() => toggleList("number")}
        isActive={blockType === "number"}
        title="Numbered list"
      >
        <ListOrdered size={14} />
      </ToolbarButton>

      <Divider />

      {/* Quote / Code block / Link */}
      <ToolbarButton
        onClick={setQuote}
        isActive={blockType === "quote"}
        title="Quote"
      >
        <Quote size={14} />
      </ToolbarButton>
      <ToolbarButton
        onClick={setCodeBlock}
        isActive={blockType === "code"}
        title="Code block"
      >
        <Code size={14} />
      </ToolbarButton>
      <ToolbarButton onClick={insertLink} title="Insert link">
        <LinkIcon size={14} />
      </ToolbarButton>
    </div>
  );
}


// ---------------------------------------------------------------------------
// Main component
// ---------------------------------------------------------------------------

export function SkillEditorModal({
  open,
  skill,
  subSkillId = null,
  onClose,
  onSaved,
}: SkillEditorModalProps) {
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [preview, setPreview] = useState<SkillPreview | null>(null);
  const [markdown, setMarkdown] = useState("");
  const [initialMarkdown, setInitialMarkdown] = useState("");
  const [dirty, setDirty] = useState(false);

  const editorRef = useRef<LexicalEditor | null>(null);

  // ── Fetch SKILL.md body on open ────────────────────────────────────
  useEffect(() => {
    if (!open || !skill) return;
    let cancelled = false;

    setLoading(true);
    setError(null);
    setPreview(null);
    setMarkdown("");
    setInitialMarkdown("");
    setDirty(false);

    // Switch between parent-body and sub-skill endpoints based on
    // the subSkillId prop. The two endpoints return identical
    // payload shapes (``SkillPreview``), so downstream logic does
    // not need to branch further.
    const previewUrl = subSkillId
      ? `/api/skills/${encodeURIComponent(skill.id)}/sub-skills/${encodeURIComponent(subSkillId)}/preview`
      : `/api/skills/${encodeURIComponent(skill.id)}/preview`;

    (async () => {
      try {
        const res = await fetch(getApiUrl(previewUrl));
        if (cancelled) return;

        if (!res.ok) {
          const body = await res.json().catch(() => ({}));
          const detail =
            body && typeof body.detail === "object" && body.detail !== null
              ? (body.detail as { error?: string }).error
              : typeof body.detail === "string"
                ? body.detail
                : `Failed to load instructions (HTTP ${res.status}).`;
          setError(detail || `HTTP ${res.status}`);
          return;
        }

        const data: SkillPreview = await res.json();
        const body = data.instructions || "";
        if (cancelled) return;

        setPreview(data);
        setInitialMarkdown(body);
        setMarkdown(body);
      } catch (exc) {
        if (cancelled) return;
        const msg =
          exc instanceof Error ? exc.message : "Network error while loading.";
        setError(msg);
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();

    return () => {
      cancelled = true;
    };
  }, [open, skill, subSkillId]);

  // ── Escape closes (with unsaved-changes guard) ─────────────────────
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "Escape" || saving) return;
      if (dirty && !confirm("Discard unsaved changes?")) return;
      onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, saving, dirty, onClose]);

  // ── Body scroll lock while open ────────────────────────────────────
  useEffect(() => {
    if (!open) return;
    const prev = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      document.body.style.overflow = prev;
    };
  }, [open]);

  // ── Editor change handler ──────────────────────────────────────────
  const handleEditorChange = useCallback(
    (_state: EditorState, editor: LexicalEditor) => {
      editorRef.current = editor;
      const md = extractSkillMarkdown(editor);
      setMarkdown(md);
      setDirty(md !== initialMarkdown.trim());
    },
    [initialMarkdown],
  );

  // ── Save ──────────────────────────────────────────────────────────
  const handleSave = useCallback(async () => {
    if (!skill) return;
    const trimmed = markdown.trim();
    if (!trimmed) {
      showToast.warning("Empty body", "Instructions cannot be empty.");
      return;
    }

    const bytes = new TextEncoder().encode(trimmed).length;
    const maxBytes = Number(
      (skill as unknown as { limits?: { max_instructions_bytes?: number } })
        .limits?.max_instructions_bytes ?? 262144,
    );
    if (bytes > maxBytes) {
      showToast.error(
        "Content too large",
        `${bytes} bytes exceeds the ${maxBytes}-byte limit.`,
      );
      return;
    }

    setSaving(true);
    setError(null);

    try {
      // Switch between parent-body and sub-skill endpoints. Both
      // accept the same ``{ instructions: string }`` payload and
      // return the parent skill summary in ``data.skill``.
      const saveUrl = subSkillId
        ? `/api/skills/${encodeURIComponent(skill.id)}/sub-skills/${encodeURIComponent(subSkillId)}/instructions`
        : `/api/skills/${encodeURIComponent(skill.id)}/instructions`;

      const res = await fetch(getApiUrl(saveUrl), {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ instructions: trimmed }),
      });

      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        const detail =
          body && typeof body.detail === "object" && body.detail !== null
            ? (body.detail as { error?: string; code?: string }).error ||
              (body.detail as { error?: string; code?: string }).code ||
              "Save rejected."
            : typeof body.detail === "string"
              ? body.detail
              : `HTTP ${res.status}`;
        setError(detail);
        showToast.error("Save rejected", detail);
        return;
      }

      const data: { status: string; skill: SkillSummary } = await res.json();
      showToast.success(
        subSkillId ? "Sub-skill updated" : "Skill updated",
        subSkillId
          ? `${data.skill.name} › ${subSkillId} instructions saved.`
          : `${data.skill.name} instructions saved.`,
      );

      if (typeof window !== "undefined") {
        window.dispatchEvent(new CustomEvent("peldrun:skills-changed"));
      }

      onSaved?.(data.skill);
      onClose();
    } catch (exc) {
      const msg =
        exc instanceof Error ? exc.message : "Network error while saving.";
      setError(msg);
      showToast.error("Save failed", msg);
    } finally {
      setSaving(false);
    }
  }, [skill, markdown, onSaved, onClose, subSkillId]);

  // ── Reset to the loaded state ──────────────────────────────────────
  const handleReset = useCallback(() => {
    setMarkdown(initialMarkdown);
    setDirty(false);
    if (editorRef.current) {
      editorRef.current.update(() => {
        loadSkillMarkdown(initialMarkdown);
      });
    }
  }, [initialMarkdown]);

  // ── Render guard ──────────────────────────────────────────────────
  if (!open || !skill) return null;

  const bytes = new TextEncoder().encode(markdown).length;
  const maxBytes = Number(
    (skill as unknown as { limits?: { max_instructions_bytes?: number } })
      .limits?.max_instructions_bytes ?? 262144,
  );
  const overLimit = bytes > maxBytes;

  // Lexical initial configuration. ``null`` type on ``theme`` is
  // acceptable — Lexical falls back to default rendering when a theme
  // slot is omitted.
  const initialConfig = {
    namespace: "SkillEditor",
    nodes: [
      HeadingNode,
      QuoteNode,
      ListNode,
      ListItemNode,
      CodeNode,
      CodeHighlightNode,
      LinkNode,
      AutoLinkNode,
    ],
    theme: {
      paragraph: "mb-2 leading-relaxed text-foreground",
      heading: {
        h1: "text-2xl font-bold mt-4 mb-2",
        h2: "text-xl font-bold mt-3 mb-2",
        h3: "text-lg font-semibold mt-2 mb-1",
      },
      quote:
        "border-l-2 border-primary/40 ps-3 italic text-muted-foreground my-2",
      code: "block bg-muted/60 px-3 py-2 rounded font-mono text-xs text-foreground whitespace-pre-wrap",
      list: {
        ul: "list-disc ps-6 my-2",
        ol: "list-decimal ps-6 my-2",
        listitem: "my-0.5",
      },
      link: "text-primary underline",
      text: {
        bold: "font-bold",
        italic: "italic",
        underline: "underline",
        strikethrough: "line-through",
        code: "font-mono text-xs bg-muted/60 px-1 py-0.5 rounded",
      },
    },
    onError: (err: Error) => {
      // eslint-disable-next-line no-console
      console.error("[SkillEditor]", err);
    },
  };

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-background/80 backdrop-blur-sm p-4"
      role="dialog"
      aria-modal="true"
      aria-labelledby="skill-editor-title"
      onClick={(e) => {
        if (e.target === e.currentTarget && !saving) {
          if (dirty && !confirm("Discard unsaved changes?")) return;
          onClose();
        }
      }}
    >
      <div className="bg-card rounded-lg shadow-2xl w-full max-w-5xl h-[90vh] flex flex-col border border-border">
        {/* Header */}
        <div className="px-6 py-4 border-b border-border flex items-center justify-between shrink-0 bg-muted/30">
          <div className="flex items-center gap-2 min-w-0">
            <div className="p-1 rounded-lg bg-primary/10 text-primary shrink-0 flex items-center justify-center">
              <SkillIcon skill={skill} size={22} />
            </div>
            <div className="min-w-0">
              <h2
                id="skill-editor-title"
                className="text-base font-bold text-foreground truncate"
              >
                Edit — {skill.display_name || skill.name}
                {subSkillId && (
                  <span className="text-muted-foreground font-normal">
                    {" "}
                    › <code className="font-mono text-primary">{subSkillId}</code>
                  </span>
                )}
              </h2>
              <p className="text-xs text-muted-foreground font-mono">
                {skill.id} · v{skill.version}
                {subSkillId && (
                  <span className="ml-2 text-violet-600 dark:text-violet-400">
                    · editing sub-skill
                  </span>
                )}
              </p>
            </div>
          </div>
          <button
            type="button"
            onClick={() => {
              if (dirty && !confirm("Discard unsaved changes?")) return;
              onClose();
            }}
            disabled={saving}
            className="p-1.5 rounded text-muted-foreground hover:text-foreground hover:bg-muted/50 disabled:opacity-50 transition"
            aria-label="Close editor"
          >
            <X size={18} />
          </button>
        </div>

        {/* Frontmatter info banner */}
        {preview && Object.keys(preview.frontmatter).length > 0 && (
          <div className="px-6 py-3 border-b border-border bg-muted/10 shrink-0">
            <div className="flex items-center gap-2 text-[11px] text-muted-foreground mb-1">
              <Lock size={11} />
              <span className="font-semibold uppercase tracking-wider">
                Frontmatter (read-only)
              </span>
            </div>
            <div className="grid grid-cols-2 gap-x-4 gap-y-1 text-[11px]">
              {Object.entries(preview.frontmatter).map(([k, v]) => (
                <div key={k} className="font-mono">
                  <span className="text-muted-foreground">{k}:</span>{" "}
                  <span className="text-foreground">
                    {String(v).slice(0, 80)}
                  </span>
                </div>
              ))}
            </div>
          </div>
        )}

        {/* Body */}
        <div className="flex-1 overflow-y-auto scrollbar-thin">
          {loading && (
            <div className="flex items-center justify-center h-full text-muted-foreground gap-2 text-sm">
              <Loader2 size={18} className="animate-spin text-primary" />
              Loading instructions…
            </div>
          )}

          {!loading && error && (
            <div className="m-6 flex items-start gap-3 p-4 bg-destructive/10 border border-destructive/30 rounded-md">
              <AlertTriangle
                size={18}
                className="text-destructive shrink-0 mt-0.5"
              />
              <div className="text-sm text-foreground">{error}</div>
            </div>
          )}

          {!loading && !error && preview && (
            <LexicalComposer initialConfig={initialConfig}>
              <LoadMarkdownPlugin initialMarkdown={initialMarkdown} />
              <SkillEditorToolbarPlugin />
              <div className="relative min-h-full px-6 py-4">
                <RichTextPlugin
                  contentEditable={
                    <ContentEditable className="min-h-[400px] w-full outline-none text-sm text-foreground font-sans leading-relaxed" />
                  }
                  placeholder={
                    <div className="absolute top-4 left-6 text-muted-foreground/40 text-sm pointer-events-none">
                      Write your skill instructions here…
                    </div>
                  }
                  ErrorBoundary={LexicalErrorBoundary}
                />
              </div>
              <HistoryPlugin />
              <ListPlugin />
              <LinkPlugin />
              <MarkdownShortcutPlugin
                transformers={SKILL_EDITOR_TRANSFORMERS as never}
              />
              <OnChangePlugin onChange={handleEditorChange} />
              <SkillFloatingToolbarPlugin />
            </LexicalComposer>
          )}
        </div>

        {/* Footer */}
        <div className="px-6 py-3 border-t border-border flex items-center justify-between gap-2 shrink-0 bg-muted/20">
          <div className="text-[11px] font-mono text-muted-foreground">
            {bytes.toLocaleString()} / {maxBytes.toLocaleString()} bytes
            {overLimit && (
              <span className="text-destructive ms-2">(over limit)</span>
            )}
            {dirty && (
              <span className="ms-3 text-amber-600 dark:text-amber-400">
                ● unsaved
              </span>
            )}
          </div>

          <div className="flex items-center gap-2">
            <button
              type="button"
              onClick={handleReset}
              disabled={!dirty || saving}
              className="px-3 py-1.5 text-xs text-muted-foreground hover:text-foreground transition disabled:opacity-40"
            >
              Reset
            </button>
            <button
              type="button"
              onClick={() => {
                if (dirty && !confirm("Discard unsaved changes?")) return;
                onClose();
              }}
              disabled={saving}
              className="px-3 py-1.5 text-xs text-muted-foreground hover:text-foreground transition disabled:opacity-50"
            >
              Cancel
            </button>
            <button
              type="button"
              onClick={handleSave}
              disabled={saving || !dirty || overLimit || loading}
              className="inline-flex items-center gap-1.5 px-4 py-1.5 bg-primary text-primary-foreground rounded-md text-xs font-medium hover:opacity-90 disabled:opacity-40 disabled:cursor-not-allowed transition"
            >
              {saving ? (
                <>
                  <Loader2 size={13} className="animate-spin" />
                  Saving…
                </>
              ) : (
                <>
                  <Save size={13} />
                  Save Changes
                </>
              )}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}

export default SkillEditorModal;
