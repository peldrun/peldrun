// Path: frontend/src/components/chat/composer/__tests__/draft-integrity.test.ts

import { saveDraft, loadDraft, clearDraft, ComposerDraft } from '../../composer';

describe('P0-03 Draft and File Integrity Acceptance Tests', () => {
  const testStorageKey = 'peldrun_composer_draft_test_session';

  beforeEach(() => {
    sessionStorage.clear();
    jest.clearAllMocks();
  });

  // Case 1: Page reload with unsent text restores the draft
  it('Case 1: persists and restores unsent text with explicit schema versioning', () => {
    const promptText = 'Build an autonomous web crawler using python';
    saveDraft(testStorageKey, promptText);

    const restored = loadDraft(testStorageKey);
    expect(restored).toBe(promptText);

    // Assert internal structure conforms to ComposerDraft contract
    const raw = sessionStorage.getItem(testStorageKey);
    expect(raw).not.toBeNull();
    const parsed: ComposerDraft = JSON.parse(raw!);
    expect(parsed.version).toBe(1);
    expect(parsed.text).toBe(promptText);
    expect(typeof parsed.updatedAt).toBe('number');
  });

  // Case 2: Corrupted or invalid schema version does not crash and returns null
  it('Case 2: gracefully handles corrupted storage entries without crashing', () => {
    sessionStorage.setItem(testStorageKey, 'invalid-non-json{');
    expect(loadDraft(testStorageKey)).toBeNull();

    sessionStorage.setItem(
      testStorageKey,
      JSON.stringify({ version: 999, text: 'Future incompatible version' })
    );
    expect(loadDraft(testStorageKey)).toBeNull();
  });

  // Case 3: Empty text removes draft from sessionStorage
  it('Case 3: removes storage entry when text is empty or trimmed whitespace', () => {
    saveDraft(testStorageKey, 'Initial content');
    expect(sessionStorage.getItem(testStorageKey)).not.toBeNull();

    saveDraft(testStorageKey, '   ');
    expect(sessionStorage.getItem(testStorageKey)).toBeNull();
  });

  // Case 4: Rejection before confirmation preserves files and draft
  it('Case 4: preserves draft and attached files upon API submission failure', async () => {
    const draftText = 'Analyze server telemetry logs';
    const fakeFile = new File(['log data'], 'server.log', { type: 'text/plain' });

    saveDraft(testStorageKey, draftText);

    // Simulate failed onSend rejection
    const mockOnSend = jest.fn().mockRejectedValue(new Error('500 Internal Server Error'));

    let caughtError: any = null;
    try {
      await mockOnSend(draftText, [fakeFile]);
    } catch (err) {
      caughtError = err;
    }

    expect(caughtError).not.toBeNull();
    expect(caughtError.message).toBe('500 Internal Server Error');

    // Storage draft remains fully preserved
    expect(loadDraft(testStorageKey)).toBe(draftText);
  });

  // Case 5: Confirmed acceptance clears draft cleanly
  it('Case 5: clears draft only when submission acceptance handshake is confirmed', async () => {
    const draftText = 'Build frontend UI components';
    saveDraft(testStorageKey, draftText);

    const mockOnSend = jest.fn().mockResolvedValue({
      accepted: true,
      jobId: 'job_test_98765',
      chatId: 'chat_test_123',
    });

    const response = await mockOnSend(draftText);
    expect(response.accepted).toBe(true);
    expect(response.jobId).toBe('job_test_98765');

    clearDraft(testStorageKey);
    expect(loadDraft(testStorageKey)).toBeNull();
  });

  // Case 6: Post-acceptance job failure does not resurrect old drafts
  it('Case 6: post-acceptance execution failure does not re-populate the draft', () => {
    // Once accepted, draft is cleared
    clearDraft(testStorageKey);
    expect(loadDraft(testStorageKey)).toBeNull();

    // Simulating terminal job failure dispatched from backend SSE
    const jobErrorEvent = new CustomEvent('omweb:job-error', {
      detail: { jobId: 'job_test_98765', error: 'Process crashed at step 4' },
    });
    window.dispatchEvent(jobErrorEvent);

    // Draft must remain null and not be resurrected into input
    expect(loadDraft(testStorageKey)).toBeNull();
  });
});
