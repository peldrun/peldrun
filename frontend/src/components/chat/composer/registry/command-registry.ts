// Path: frontend/src/components/chat/composer/registry/command-registry.ts

export type CommandCategory = 'system' | 'agent' | 'tool' | 'skill' | 'custom';

export interface SlashCommandExecutionContext {
  clearEditor: () => void;
  insertText: (text: string) => void;
  dispatchCustomEvent: (name: string, detail?: unknown) => void;
}

export interface SlashCommandItem {
  id: string;
  trigger: string;
  label: string;
  description: string;
  category: CommandCategory;
  icon?: string;
  execute: (context: SlashCommandExecutionContext) => void | Promise<void>;
}

export type MentionCategory = 'agent' | 'tool' | 'skill' | 'file' | 'custom';

// [FIX-1.1] Add explicit mentionToken field for reliable matching.
// `insertText` is what gets inserted into the editor (no @ prefix).
// `mentionToken` is the bare identifier used for existence checks.
// `label` stays user-facing (with @) for display in the menu.
// `metadata` carries canonical file info (relPath, fileName, origin).
export interface MentionItem {
  id: string;
  label: string;
  description?: string;
  category: MentionCategory;
  icon?: string;
  insertText: string;
  /**
   * Optional bare identifier used for existence checks.
   * When omitted, `containsMention` falls back to `insertText` with
   * @/backtick characters stripped.
   *
   * New code (as of the mention-refactor) SHOULD always populate this.
   * Legacy call sites may omit it during the transition period.
   */
  mentionToken?: string;   // ← [FIX-1.1b] أصبح اختياريًا
  metadata?: Record<string, unknown>;
}

export interface MentionContext {
  chatId?: string;
}

export type DynamicMentionProvider = (
  query: string,
  context?: MentionContext
) => Promise<MentionItem[]> | MentionItem[];

export interface ExecutionDirectives {
  agentId?: string;
  toolIds: string[];
}

/**
 * Extracts the canonical runtime execution identifier from a mention item.
 * Strictly uses source metadata identifiers rather than UI labels or descriptions.
 *
 * [FIX-1.7] Extended to support `category === 'file'`. Returns the canonical
 * relative path within the workspace, which is what the backend needs to
 * resolve and inject the file content.
 */
export function getMentionId(item: MentionItem): string | undefined {
  if (item.category === 'agent') {
    const id = item.metadata?.agentId;
    if (typeof id === 'string' && id.trim()) {
      return id.trim();
    }
    if (typeof item.id === 'string' && item.id.startsWith('agent-')) {
      return item.id.replace(/^agent-/, '');
    }
    return undefined;
  }

  if (item.category === 'tool') {
    const id = item.metadata?.toolId;
    if (typeof id === 'string' && id.trim()) {
      return id.trim();
    }
    if (typeof item.id === 'string' && item.id.startsWith('tool-')) {
      return item.id.replace(/^tool-/, '');
    }
    return undefined;
  }

  // [FIX-1.7] File mentions: return the canonical relative path.
  // Preferred source: metadata.relPath (set by the workspace files provider).
  // Fallback: strip the `file-` prefix from the auto-generated id.
  if (item.category === 'file') {
    const relPath = item.metadata?.relPath;
    if (typeof relPath === 'string' && relPath.trim()) {
      return relPath.trim();
    }
    if (typeof item.id === 'string' && item.id.startsWith('file-')) {
      return item.id.replace(/^file-/, '');
    }
    return undefined;
  }

  return undefined;
}

/**
 * Checks whether the draft prompt text currently contains the mention token.
 *
 * [FIX-1.6] Now matches THREE representations for backward compatibility:
 *   1. New bare form:      "report.md"
 *   2. Legacy @-form:      "@report.md"
 *   3. Backtick form:      "`report.md`"
 *
 * This ensures that:
 *   - Prompts produced by the new composer (no @) still resolve correctly.
 *   - Prompts copied from older chats / pasted from other apps still resolve.
 *   - Prompts where the user typed backticks manually still resolve.
 */
export function containsMention(text: string, item: MentionItem): boolean {
  const token = (item.mentionToken || item.insertText || '').trim();
  if (!token) return false;

  // Strip any leading/trailing @, `, [, or ] characters from the token.
  // Also strip a leading `@[` and trailing `]` for the bracketed form.
  const bare = token
    .replace(/^@\[|\]$/g, '')       // strip @[ ... ] wrapper if present
    .replace(/^[@`]+|[@`]+$/g, '')   // strip any residual @ or ` chars
    .trim();
  if (!bare) return false;

  const words = text.split(/\s+/);
  return (
    words.includes(bare) ||
    words.includes(`@${bare}`) ||
    words.includes(`\`${bare}\``) ||
    words.includes(`\`${bare}`) ||
    words.includes(`${bare}\``) ||
    words.includes(`@[${bare}]`)   // [FIX-5.5] bracketed marker
  );
}

/**
 * Resolves verified execution directives from active user selections still present in the text.
 * Enforces single target agent policy (latest selected) and deduplicated tools.
 *
 * NOTE: Files are NOT resolved here — they travel separately via `context_refs`
 * in the run payload. This function remains the source of truth for agent/tool
 * directives only.
 */
export function resolveExecutionDirectives(
  text: string,
  mentions: MentionItem[]
): ExecutionDirectives {
  const active = mentions.filter((item) => containsMention(text, item));

  const agents = active.filter((item) => item.category === 'agent');

  const tools = active
    .filter((item) => item.category === 'tool')
    .map((item) => getMentionId(item))
    .filter((id): id is string => Boolean(id));

  const lastAgent = agents.at(-1);

  return {
    agentId: lastAgent ? getMentionId(lastAgent) : undefined,
    toolIds: [...new Set(tools)],
  };
}

/**
 * Client-side Workspace Files Provider.
 *
 * [FIX-1.2]
 *   - `insertText` now uses the bare filename wrapped in backticks, WITHOUT
 *     the @ prefix. The editor stores a plain, human-readable token that the
 *     LLM will treat as a literal filename reference.
 *   - `mentionToken` carries the bare filename for existence checks.
 *   - `metadata` carries canonical file information (relPath, fileName, origin)
 *     so the composer can build a `context_refs` array without re-parsing.
 *   - `id` now uses the full path (not just the name) to disambiguate files
 *     with identical basenames in different subdirectories.
 */
async function sessionWorkspaceFilesProvider(
  query = '',
  context?: MentionContext
): Promise<MentionItem[]> {
  if (typeof window === 'undefined') return [];

  try {
    const res = await fetch('/api/files');
    if (!res.ok) return [];

    const data = await res.json();
    const filesList: Array<{ name: string; path?: string; rel_root?: string; origin?: string }> =
      Array.isArray(data) ? data : data.files || [];

    const lower = query.toLowerCase();
    const targetChatId = context?.chatId;

    const filtered = filesList.filter((f) => {
      const normalizedPath = (f.path || f.rel_root || '').replace(/\\/g, '/');

      if (targetChatId) {
        // [FIX-5.3] When a chat is active, restrict the @ mention menu to
        // files belonging to this specific chat. This prevents legacy
        // workspace/ duplicates (created by the old mirror-upload behavior)
        // from appearing twice.
        //
        // Legacy files that were uploaded BEFORE the mirror-upload removal
        // exist in BOTH `workspace/` and `chats/<cid>/files/`. The chat
        // copy is canonical; the workspace copy is discarded here.
        const isThisChatFile = normalizedPath.includes(
          `chats/${targetChatId}/files/`
        );
        if (!isThisChatFile) return false;
      } else {
        // No chat context yet (fresh session): fall back to workspace files
        // so the user can reference previously uploaded material.
        const isWorkspaceFile =
          f.origin === 'workspace' || normalizedPath.startsWith('workspace/');
        if (!isWorkspaceFile) return false;
      }

      return !lower || f.name.toLowerCase().includes(lower);
    });

    return filtered.map((f) => {
      const normalizedRelPath = (f.path || f.rel_root || f.name).replace(/\\/g, '/');
      return {
        // [FIX-1.2a] Use full relative path as the unique id.
        id: `file-${normalizedRelPath}`,
        // [FIX-1.2b] Label stays with @ for the menu UI.
        label: `@${f.name}`,
        description: f.path ? `Workspace: ${f.path}` : 'Workspace file',
        category: 'file' as MentionCategory,
        // [FIX-1.2c] Insert the bare filename in backticks — NO @ prefix.
        insertText: `@[${f.name}] `,
        // [FIX-1.2d] Bare token for existence checks.
        mentionToken: f.name,
        icon: 'FileText',
        // [FIX-1.2e] Canonical metadata for downstream context_refs.
        metadata: {
          relPath: normalizedRelPath,
          fileName: f.name,
          origin: f.origin || 'unknown',
        },
      };
    });
  } catch (err) {
    console.error('[CommandRegistry] Error fetching workspace files:', err);
    return [];
  }
}

/**
 * Dynamic Store & Core Agents Provider:
 * Automatically loads built-in agents and store-installed agents into the @ selector.
 *
 * [FIX-1.3] `insertText` no longer contains the @ prefix.
 * The user sees "@peldrun" in the menu, but the editor receives "peldrun".
 */
async function storeAgentsProvider(query = ''): Promise<MentionItem[]> {
  if (typeof window === 'undefined') return [];

  try {
    let agentsList: any[] = [];
    const res = await fetch('/api/agents');
    if (res.ok) {
      const data = await res.json();
      agentsList = Array.isArray(data) ? data : data.agents || [];
    } else {
      const fallbackRes = await fetch('/api/store/agents');
      if (fallbackRes.ok) {
        const fallbackData = await fallbackRes.json();
        agentsList = Array.isArray(fallbackData) ? fallbackData : fallbackData.agents || [];
      }
    }

    const lower = query.toLowerCase();
    const filtered = agentsList.filter((a) => {
      const id = String(a.id || '').toLowerCase();
      const name = String(a.name || '').toLowerCase();
      return !lower || id.includes(lower) || name.includes(lower);
    });

    return filtered.map((a) => {
      const bareId = String(a.id);
      return {
        id: `agent-${bareId}`,
        label: `@${bareId}`,
        description: a.name ? `${a.name} — ${a.description || 'Agent'}` : a.description || 'Agent',
        category: 'agent' as MentionCategory,
        // [FIX-1.3] No @ prefix; bare token in backticks.
        insertText: `\`${bareId}\` `,
        mentionToken: bareId,
        icon: 'Bot',
        metadata: {
          agentId: bareId,
          name: a.name,
          systemPrompt: a.system_prompt,
          tools: a.tools,
        },
      };
    });
  } catch (err) {
    console.debug('[CommandRegistry] Notice: Could not load store agents:', err);
    return [];
  }
}

/**
 * Dynamic Store & Tools/MCP Provider:
 * Automatically queries active tools, plugins, and MCP capabilities.
 *
 * [FIX-1.4] `insertText` no longer contains the @ prefix.
 */
async function storeToolsProvider(query = ''): Promise<MentionItem[]> {
  if (typeof window === 'undefined') return [];

  try {
    let toolsList: any[] = [];
    const res = await fetch('/api/tools');
    if (res.ok) {
      const data = await res.json();
      toolsList = Array.isArray(data) ? data : data.tools || [];
    } else {
      const storeRes = await fetch('/api/store/tools');
      if (storeRes.ok) {
        const storeData = await storeRes.json();
        toolsList = Array.isArray(storeData) ? storeData : storeData.tools || [];
      }
    }

    const lower = query.toLowerCase();
    const filtered = toolsList.filter((t) => {
      const id = String(t.id || t.name || '').toLowerCase();
      const desc = String(t.description || '').toLowerCase();
      return !lower || id.includes(lower) || desc.includes(lower);
    });

    return filtered.map((t) => {
      const toolId = String(t.id || t.name || '');
      return {
        id: `tool-${toolId}`,
        label: `@${toolId}`,
        description: t.description || 'Callable tool capability',
        category: 'tool' as MentionCategory,
        // [FIX-1.4] No @ prefix; bare token in backticks.
        insertText: `\`${toolId}\` `,
        mentionToken: toolId,
        icon: 'Wrench',
        metadata: {
          toolId: toolId,
          name: t.name,
          parameters: t.parameters,
        },
      };
    });
  } catch (err) {
    console.debug('[CommandRegistry] Notice: Could not load store tools:', err);
    return [];
  }
}

class CommandRegistryStore {
  private slashCommands: Map<string, SlashCommandItem> = new Map();
  private staticMentions: Map<string, MentionItem> = new Map();
  private dynamicProviders: Set<DynamicMentionProvider> = new Set();
  private listeners: Set<() => void> = new Set();

  constructor() {
    this.registerDefaultCommands();
    this.registerMentionProvider(sessionWorkspaceFilesProvider);
    this.registerMentionProvider(storeAgentsProvider);
    this.registerMentionProvider(storeToolsProvider);
    this.setupExternalEventListener();
  }

  private setupExternalEventListener(): void {
    if (typeof window === 'undefined') return;

    window.addEventListener('peldrun:register-slash-command', ((
      event: CustomEvent<SlashCommandItem>
    ) => {
      if (event.detail && event.detail.id) {
        this.registerSlashCommand(event.detail);
      }
    }) as EventListener);

    window.addEventListener('peldrun:register-mention-item', ((
      event: CustomEvent<MentionItem>
    ) => {
      if (event.detail && event.detail.id) {
        this.registerMentionItem(event.detail);
      }
    }) as EventListener);

    window.addEventListener('peldrun:register-mention-provider', ((
      event: CustomEvent<{ provider: DynamicMentionProvider }>
    ) => {
      if (event.detail && typeof event.detail.provider === 'function') {
        this.registerMentionProvider(event.detail.provider);
      }
    }) as EventListener);
  }

  private registerDefaultCommands(): void {
    this.registerSlashCommand({
      id: 'cmd-clear',
      trigger: 'clear',
      label: '/clear',
      description: 'Reset conversation session and clear editor',
      category: 'system',
      icon: 'Trash2',
      execute: ({ clearEditor, dispatchCustomEvent }) => {
        clearEditor();
        dispatchCustomEvent('peldrun:new-session');
      },
    });

    this.registerSlashCommand({
      id: 'cmd-agent-mode',
      trigger: 'agent',
      label: '/agent',
      description: 'Switch execution mode to Autonomous Agent',
      category: 'system',
      icon: 'Bot',
      execute: ({ clearEditor, dispatchCustomEvent }) => {
        clearEditor();
        dispatchCustomEvent('omweb:mode-change', 'agent');
      },
    });

    this.registerSlashCommand({
      id: 'cmd-chat-mode',
      trigger: 'chat',
      label: '/chat',
      description: 'Switch execution mode to Direct Chat',
      category: 'system',
      icon: 'MessageSquare',
      execute: ({ clearEditor, dispatchCustomEvent }) => {
        clearEditor();
        dispatchCustomEvent('omweb:mode-change', 'chat');
      },
    });
  }

  public registerSlashCommand(command: SlashCommandItem): () => void {
    this.slashCommands.set(command.id, command);
    this.notify();
    return () => {
      this.slashCommands.delete(command.id);
      this.notify();
    };
  }

  public registerMentionItem(mention: MentionItem): () => void {
    this.staticMentions.set(mention.id, mention);
    this.notify();
    return () => {
      this.staticMentions.delete(mention.id);
      this.notify();
    };
  }

  public registerMentionProvider(provider: DynamicMentionProvider): () => void {
    this.dynamicProviders.add(provider);
    this.notify();
    return () => {
      this.dynamicProviders.delete(provider);
      this.notify();
    };
  }

  public getSlashCommands(query = ''): SlashCommandItem[] {
    const list = Array.from(this.slashCommands.values());
    if (!query) return list;
    const lower = query.toLowerCase();
    return list.filter(
      (c) => c.trigger.toLowerCase().includes(lower) || c.label.toLowerCase().includes(lower)
    );
  }

  public async getMentionItems(
    query = '',
    context?: MentionContext
  ): Promise<MentionItem[]> {
    const staticList = Array.from(this.staticMentions.values());
    const lower = query.toLowerCase();

    const filteredStatic = query
      ? staticList.filter(
          (m) =>
            m.label.toLowerCase().includes(lower) ||
            m.id.toLowerCase().includes(lower) ||
            (m.description && m.description.toLowerCase().includes(lower))
        )
      : staticList;

    if (this.dynamicProviders.size === 0) {
      return filteredStatic;
    }

    const dynamicPromises = Array.from(this.dynamicProviders).map(async (provider) => {
      try {
        return await provider(query, context);
      } catch (err) {
        console.debug('[CommandRegistry] Dynamic mention provider exception:', err);
        return [];
      }
    });

    const dynamicResults = await Promise.all(dynamicPromises);
    const combined = [...filteredStatic, ...dynamicResults.flat()];

    const uniqueMap = new Map<string, MentionItem>();
    for (const item of combined) {
      uniqueMap.set(item.id, item);
    }

    return Array.from(uniqueMap.values());
  }

  public subscribe(listener: () => void): () => void {
    this.listeners.add(listener);
    return () => {
      this.listeners.delete(listener);
    };
  }

  private notify(): void {
    this.listeners.forEach((listener) => listener());
  }
}

export const commandRegistry = new CommandRegistryStore();
