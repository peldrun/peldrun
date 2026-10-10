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

export interface MentionItem {
  id: string;
  label: string;
  description?: string;
  category: MentionCategory;
  icon?: string;
  insertText: string;
  metadata?: Record<string, unknown>;
}

export interface MentionContext {
  chatId?: string;
}

export type DynamicMentionProvider = (
  query: string,
  context?: MentionContext
) => Promise<MentionItem[]> | MentionItem[];

/**
 * Client-side Workspace Files Provider.
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
        const isThisChatFile = normalizedPath.includes(`chats/${targetChatId}/files/`);
        const isWorkspaceFile = f.origin === 'workspace' || normalizedPath.startsWith('workspace/');
        if (!isThisChatFile && !isWorkspaceFile) return false;
      } else {
        const isWorkspaceFile = f.origin === 'workspace' || normalizedPath.startsWith('workspace/');
        if (!isWorkspaceFile) return false;
      }

      return !lower || f.name.toLowerCase().includes(lower);
    });

    return filtered.map((f) => ({
      id: `file-${f.name}`,
      label: `@${f.name}`,
      description: f.path ? `Workspace: ${f.path}` : 'Workspace file',
      category: 'file' as MentionCategory,
      insertText: `@${f.name} `,
      icon: 'FileText',
    }));
  } catch (err) {
    console.error('[CommandRegistry] Error fetching workspace files:', err);
    return [];
  }
}

/**
 * Dynamic Store & Core Agents Provider:
 * Automatically loads built-in agents and store-installed agents into the @ selector.
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

    return filtered.map((a) => ({
      id: `agent-${a.id}`,
      label: `@${a.id}`,
      description: a.name ? `${a.name} —${a.description || 'Agent'}` : a.description || 'Agent',
      category: 'agent' as MentionCategory,
      insertText: `@${a.id} `,
      icon: 'Bot',
      metadata: {
        agentId: a.id,
        name: a.name,
        systemPrompt: a.system_prompt,
        tools: a.tools,
      },
    }));
  } catch (err) {
    console.debug('[CommandRegistry] Notice: Could not load store agents:', err);
    return [];
  }
}

/**
 * Dynamic Store & Tools/MCP Provider:
 * Automatically queries active tools, plugins, and MCP capabilities.
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
      const toolId = t.id || t.name;
      return {
        id: `tool-${toolId}`,
        label: `@${toolId}`,
        description: t.description || 'Callable tool capability',
        category: 'tool' as MentionCategory,
        insertText: `@${toolId} `,
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

/**
 * Utility to extract operational directives (@agent and @tool) from the prompt text
 * to inject directly into the execution engine payload.
 */
export function parsePromptDirectives(text: string): {
  targetAgentId?: string;
  requestedTools: string[];
} {
  const agentMatches = text.match(/@([a-zA-Z0-9_-]+)/g) || [];
  const requestedTools: string[] = [];
  let targetAgentId: string | undefined = undefined;

  for (const match of agentMatches) {
    const id = match.slice(1);
    if (['peldrun', 'code_architect', 'data_scientist', 'deep_researcher'].includes(id)) {
      targetAgentId = id;
    } else if (
      ['bash', 'browser_use', 'web_search', 'str_replace_editor', 'file_saver', 'python_execute'].includes(id)
    ) {
      requestedTools.push(id);
    }
  }

  return { targetAgentId, requestedTools };
}
