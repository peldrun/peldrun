// Path: frontend/src/components/chat/composer/registry/command-registry.ts

import { useEffect, useState } from 'react';

export type CommandCategory = 'system' | 'agent' | 'tool' | 'custom';

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

export type MentionCategory = 'agent' | 'file' | 'tool' | 'custom';

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
 * Strict Client-side Files Filter:
 * Does NOT require modifying backend files.py.
 * Purely filters out external storage directories, limiting autocomplete
 * strictly to the active workspace folder or the active chat session's deliverables.
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

      // 1. If an active chat exists, allow its specific files folder OR workspace root
      if (targetChatId) {
        const isThisChatFile = normalizedPath.includes(`chats/${targetChatId}/files/`);
        const isWorkspaceFile = f.origin === 'workspace' || normalizedPath.startsWith('workspace/');
        if (!isThisChatFile && !isWorkspaceFile) return false;
      } else {
        // 2. Otherwise strictly restrict to workspace files only, completely hiding internal storage/chats
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

class CommandRegistryStore {
  private slashCommands: Map<string, SlashCommandItem> = new Map();
  private staticMentions: Map<string, MentionItem> = new Map();
  private dynamicProviders: Set<DynamicMentionProvider> = new Set();
  private listeners: Set<() => void> = new Set();

  constructor() {
    this.registerDefaultCommands();
    this.registerMentionProvider(sessionWorkspaceFilesProvider);
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
      execute: ({ dispatchCustomEvent }) => {
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
      execute: ({ dispatchCustomEvent }) => {
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
        console.error('[CommandRegistry] Error in dynamic mention provider:', err);
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
