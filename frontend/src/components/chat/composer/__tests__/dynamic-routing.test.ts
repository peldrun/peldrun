// Path: frontend/src/components/chat/composer/registry/__tests__/dynamic-routing.test.ts

import {
  getMentionId,
  containsMention,
  resolveExecutionDirectives,
  MentionItem,
} from '../registry/command-registry';

describe('P0-01 Dynamic Routing Acceptance Tests', () => {
  // Case 1: Built-in agent selection returns correct agent_id
  it('Case 1: resolves built-in agent selection to its canonical agent_id', () => {
    const builtinAgent: MentionItem = {
      id: 'agent-peldrun',
      label: '@peldrun',
      category: 'agent',
      insertText: '@peldrun ',
      metadata: { agentId: 'peldrun' },
    };

    const text = '@peldrun please plan this task';
    const directives = resolveExecutionDirectives(text, [builtinAgent]);

    expect(directives.agentId).toBe('peldrun');
    expect(directives.toolIds).toEqual([]);
  });

  // Case 2: Custom store/local agent selection returns its true runtime identifier
  it('Case 2: resolves custom store/local agent to its true runtime identifier', () => {
    const customAgent: MentionItem = {
      id: 'agent-custom_analyst_v2',
      label: '@custom_analyst_v2',
      category: 'agent',
      insertText: '@custom_analyst_v2 ',
      metadata: { agentId: 'custom_analyst_v2' },
    };

    const text = '@custom_analyst_v2 analyze the dataset';
    const directives = resolveExecutionDirectives(text, [customAgent]);

    expect(directives.agentId).toBe('custom_analyst_v2');
  });

  // Case 3: Custom tool selection includes its identifier in tools
  it('Case 3: includes custom tool identifier in execution tools', () => {
    const customTool: MentionItem = {
      id: 'tool-mcp_database_query',
      label: '@mcp_database_query',
      category: 'tool',
      insertText: '@mcp_database_query ',
      metadata: { toolId: 'mcp_database_query' },
    };

    const text = 'Run query with @mcp_database_query';
    const directives = resolveExecutionDirectives(text, [customTool]);

    expect(directives.toolIds).toEqual(['mcp_database_query']);
  });

  // Case 4: Multiple tool selection includes both tools without duplication
  it('Case 4: deduplicates tools when multiple are selected or repeated', () => {
    const toolBash: MentionItem = {
      id: 'tool-bash',
      label: '@bash',
      category: 'tool',
      insertText: '@bash ',
      metadata: { toolId: 'bash' },
    };

    const toolBrowser: MentionItem = {
      id: 'tool-browser_use',
      label: '@browser_use',
      category: 'tool',
      insertText: '@browser_use ',
      metadata: { toolId: 'browser_use' },
    };

    const text = 'Use @bash and @browser_use and again @bash';
    const directives = resolveExecutionDirectives(text, [toolBash, toolBrowser, toolBash]);

    expect(directives.toolIds).toEqual(['bash', 'browser_use']);
  });

  // Case 5: Deleted tool mention is not dispatched
  it('Case 5: excludes tool from execution if its token is deleted from prompt text', () => {
    const toolBash: MentionItem = {
      id: 'tool-bash',
      label: '@bash',
      category: 'tool',
      insertText: '@bash ',
      metadata: { toolId: 'bash' },
    };

    // Prompt no longer contains @bash
    const text = 'Use the standard file editor instead';
    const directives = resolveExecutionDirectives(text, [toolBash]);

    expect(directives.toolIds).toEqual([]);
  });

  // Case 6: Manually typed @unknown is not converted to an unverified tool
  it('Case 6: does not convert manually typed @unknown into an unverified directive', () => {
    const text = 'Hello @unknown_capability and @fake_agent';
    // User never picked anything from typeahead menu
    const selectedMentions: MentionItem[] = [];

    const directives = resolveExecutionDirectives(text, selectedMentions);

    expect(directives.agentId).toBeUndefined();
    expect(directives.toolIds).toEqual([]);
  });

  // Case 7: Single agent policy applied (latest selected agent wins)
  it('Case 7: enforces single target agent policy where the latest selected agent wins', () => {
    const agentFirst: MentionItem = {
      id: 'agent-peldrun',
      label: '@peldrun',
      category: 'agent',
      insertText: '@peldrun ',
      metadata: { agentId: 'peldrun' },
    };

    const agentSecond: MentionItem = {
      id: 'agent-deep_researcher',
      label: '@deep_researcher',
      category: 'agent',
      insertText: '@deep_researcher ',
      metadata: { agentId: 'deep_researcher' },
    };

    const text = 'Hand over from @peldrun to @deep_researcher';
    const directives = resolveExecutionDirectives(text, [agentFirst, agentSecond]);

    expect(directives.agentId).toBe('deep_researcher');
  });

  // Case 8: Tool list query failure does not claim unavailable tools
  it('Case 8: handles missing or invalid metadata gracefully without false positives', () => {
    const brokenTool: MentionItem = {
      id: 'custom-broken',
      label: '@broken',
      category: 'tool',
      insertText: '@broken ',
      // metadata is missing or empty
    };

    expect(getMentionId(brokenTool)).toBeUndefined();

    const text = 'Execute @broken';
    const directives = resolveExecutionDirectives(text, [brokenTool]);

    expect(directives.toolIds).toEqual([]);
  });
});
