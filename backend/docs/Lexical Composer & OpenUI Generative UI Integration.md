# PELDRUN Engineering Plan

## Lexical Composer & OpenUI Generative UI Integration

**Document type:** Engineering Implementation Plan
**Date:** 9 October 2026
**Repository:** `peldrun/peldrun`
**Scope:** Frontend architecture, Composer, generative UI, integration contracts, testing and regression prevention.

---

## 1. Project Objective

Integrate Lexical and OpenUI into PELDRUN without replacing the existing application architecture or breaking any working functionality.

The objective is to improve user interaction and enable the agent to produce structured, interactive results while maintaining:

* Existing chat and agent execution behavior.
* Existing API request and response contracts.
* Existing streaming and execution event handling.
* Existing attachment upload and workspace file behavior.
* Existing agent, engine, provider, model and tool selection.
* Existing stop, approval, resume and error-handling flows.
* Existing chat history, artifacts, usage metrics and workspace navigation.
* Local-first operation and compatibility with the existing backend.

The integration must also establish a maintainable frontend architecture in which individual capabilities can be changed, tested, debugged and repaired without modifying a single oversized component.

## 2. Product Requirements

### 2.1 Lexical: Intelligent Composer

Lexical will replace the current textarea-based text-entry surface incrementally.

The Composer must continue to support:

1. Plain-text task and chat input.
2. Enter to send and Shift+Enter for a new line.
3. File selection, multiple attachments, drag-and-drop and attachment removal.
4. Agent and Chat execution modes.
5. Engine, model, provider and agent selection.
6. Active tools and reasoning-effort settings.
7. Running-task stop behavior.
8. Disabled states while execution is active.
9. Existing landing-page and ongoing-conversation workflows.
10. Keyboard accessibility and clear focus behavior.

After the basic integration is stable, the Composer may support:

* `/` commands for actions and task shortcuts.
* `@` mentions for agents, tools or supported context references.
* Explicit references to files or workspace artifacts.
* Structured attachment and context chips.
* Paste handling and future contextual input capabilities.

These advanced capabilities must be introduced incrementally. They must not be prerequisites for the initial Lexical release.

Lexical owns editing behavior and editor state. The application owns execution configuration, attachments, model selection, agent selection, permissions and task submission.

### 2.2 OpenUI: Generative UI Results

OpenUI will provide an additional rendering capability inside the existing PELDRUN interface.

The intended use cases include:

* Structured forms and questionnaires.
* Tables and data summaries.
* Charts and comparisons.
* Test results and validation summaries.
* Approval and confirmation panels.
* Structured task outputs that benefit from interaction.
* Domain-specific result components introduced in the future.

OpenUI must not replace:

* The main chat shell.
* The existing conversation timeline.
* The execution trace and tool activity display.
* The Composer.
* WorkspacePanel.
* File management and artifact navigation.
* Existing settings and configuration interfaces.

Ordinary assistant text should continue to use the existing Markdown rendering. Code should continue to use the existing code-rendering path. Existing artifacts should continue to use the existing artifact and workspace interfaces.

OpenUI should be selected only when a structured interactive result provides a meaningful benefit.

### 2.3 Non-goals

This integration does not include:

* Rebuilding the frontend from scratch.
* Replacing the Core execution engine.
* Introducing a new agent orchestration system.
* Implementing Multi-Agent functionality.
* Replacing all existing UI components.
* Migrating all messages to OpenUI.
* Introducing a mandatory cloud dependency.
* Building a full document editor comparable to Notion.
* Allowing unrestricted model-generated executable code.
* Rewriting working backend endpoints without a demonstrated requirement.

---

## 3. Architectural Principles

### 3.1 Separation of responsibilities

The architecture must preserve these boundaries:

**Lexical**

* Editor lifecycle.
* Text editing.
* Editor-specific nodes and plugins.
* Commands and mention interactions.
* Editor-to-message normalization.

**Composer application layer**

* Attachments and their metadata.
* Agent and model configuration.
* Execution mode.
* Validation of submitted messages.
* Submission to the existing task workflow.

**PELDRUN application layer**

* Chat history.
* Run state.
* Streaming event processing.
* Workspace and artifacts.
* Human approval and resume.
* Existing API communication.

**OpenUI adapter**

* Converts a supported structured result into the input required by the selected OpenUI renderer.
* Validates result types and schemas.
* Handles renderer lifecycle and failures.
* Maps approved interactions to application-owned callbacks.

**OpenUI renderer**

* Renders only supported components.
* Manages rendering of the selected OpenUI format.
* Does not own PELDRUN task execution or persistence.

**PELDRUN Core**

* Remains independent of React, Next.js, Lexical and OpenUI.
* Owns or exposes the relevant execution contracts and events.
* Does not import frontend packages.

### 3.2 Required data flow

User input:

`Lexical Editor → Composer Normalizer → ComposerMessage → Existing Submission Flow → Existing API → PELDRUN Execution`

Agent result:

`Core / Existing Backend → Existing Event Adapter → Normalized Result → Result Router → Markdown / Artifact UI / OpenUI`

OpenUI interactions:

`Rendered Component → Validated UI Action → Application Handler → Existing API or Task Workflow`

No renderer may bypass application-owned execution handlers.

### 3.3 Backward compatibility

The integration must preserve the current task submission behavior unless a separate, explicitly approved change is required.

The initial Lexical adapter should produce the same effective text and attachment inputs that the existing flow expects.

Do not change the `/api/run` contract simply to accommodate the editor.

Do not make OpenUI a requirement for receiving or displaying an ordinary execution result.

Existing SSE event handling and persisted chat data must remain readable. Any required event or result normalization should be introduced as a compatibility layer rather than an unrelated backend rewrite.

---

## 4. Proposed Frontend Directory Structure

The following is a proposed target structure under `frontend/src`. It is not an instruction to move every existing file immediately. First map the actual imports and responsibilities, then migrate in small, reviewable steps.

```text
frontend/src/
├── components/
│   ├── chat/
│   │   ├── composer/
│   │   │   ├── composer.tsx
│   │   │   ├── composer-toolbar.tsx
│   │   │   ├── composer-attachments.tsx
│   │   │   ├── composer-controls.tsx
│   │   │   ├── composer-submit.ts
│   │   │   ├── composer.types.ts
│   │   │   └── index.ts
│   │   │
│   │   ├── chat-container.tsx
│   │   ├── chat-thread.tsx
│   │   ├── chat-timeline.tsx
│   │   └── chat-deliverable.tsx
│   │
│   ├── workspace/
│   │   ├── workspace-panel.tsx
│   │   ├── files/
│   │   ├── artifacts/
│   │   └── preview/
│   │
│   └── ui-results/
│       ├── result-router.tsx
│       ├── result-boundary.tsx
│       ├── result-fallback.tsx
│       ├── result.types.ts
│       └── renderers/
│           ├── markdown-result.tsx
│           ├── artifact-result.tsx
│           └── openui-result.tsx
│
├── features/
│   ├── composer/
│   │   ├── components/
│   │   │   ├── lexical-composer.tsx
│   │   │   ├── composer-placeholder.tsx
│   │   │   └── composer-error-boundary.tsx
│   │   │
│   │   ├── plugins/
│   │   │   ├── send-command.plugin.tsx
│   │   │   ├── clear-editor.plugin.tsx
│   │   │   ├── attachment-reference.plugin.tsx
│   │   │   ├── slash-command.plugin.tsx
│   │   │   └── mention.plugin.tsx
│   │   │
│   │   ├── nodes/
│   │   │   ├── mention.node.ts
│   │   │   └── context-reference.node.ts
│   │   │
│   │   ├── model/
│   │   │   ├── composer-message.types.ts
│   │   │   ├── composer-normalizer.ts
│   │   │   └── composer-validation.ts
│   │   │
│   │   ├── hooks/
│   │   │   └── use-composer-controller.ts
│   │   │
│   │   └── index.ts
│   │
│   └── generative-ui/
│       ├── components/
│       │   ├── openui-renderer.tsx
│       │   ├── openui-fallback.tsx
│       │   └── interactive-result.tsx
│       │
│       ├── adapters/
│       │   ├── openui-adapter.ts
│       │   ├── result-normalizer.ts
│       │   └── ui-action-adapter.ts
│       │
│       ├── contracts/
│       │   ├── ui-result.types.ts
│       │   ├── ui-action.types.ts
│       │   └── result-schemas.ts
│       │
│       ├── library/
│       │   ├── index.ts
│       │   ├── forms.tsx
│       │   ├── tables.tsx
│       │   ├── charts.tsx
│       │   └── test-results.tsx
│       │
│       ├── security/
│       │   ├── validate-result.ts
│       │   ├── validate-action.ts
│       │   └── url-policy.ts
│       │
│       └── index.ts
│
├── lib/
│   ├── api/
│   ├── events/
│   └── validation/
│
├── stores/
│   ├── chat-store.ts
│   └── ...
│
└── test/
    ├── fixtures/
    └── helpers/
```

### Directory rules

1. Keep each component focused on one responsibility.
2. Put reusable types in dedicated type files when they are shared across modules.
3. Keep editor plugins independent instead of adding all behaviors to one Lexical component.
4. Keep OpenUI parsing, validation, rendering and action dispatch separate.
5. Avoid circular dependencies between features.
6. Avoid barrel exports that create circular imports or load unnecessary code.
7. Do not create a file for every trivial expression; split by responsibility and meaningful testability.
8. Preserve existing file locations when moving a file adds risk without improving ownership.
9. Do not create duplicate implementations of existing workspace or chat components.
10. Keep the Core package free of frontend dependencies.

---

## 5. Proposed Contracts

The final definitions must be reconciled with existing application types before implementation.

### 5.1 ComposerMessage

```ts
type ComposerMessage = {
  version: 1;
  text: string;
  mentions: MentionReference[];
  commands: ComposerCommand[];
  attachments: AttachmentReference[];
  references: ContextReference[];
};
```

`MentionReference`, `ComposerCommand`, `AttachmentReference` and `ContextReference` must have explicit schemas and stable identifiers.

The contract must not contain raw File objects as persisted conversation data. Browser File objects may exist temporarily in the upload layer, while the normalized message uses identifiers and metadata appropriate to the existing upload workflow.

### 5.2 RunOptions

Execution options must remain separate from editor content.

```ts
type RunOptions = {
  mode: "chat" | "agent";
  agentId: string;
  model?: string;
  provider?: string;
  reasoningEffort?: string;
};
```

This is illustrative. The actual contract must preserve current fields, provider-specific configuration and compatibility requirements. Do not remove existing fields merely because they are absent from this example.

### 5.3 UIResult

```ts
type UIResult = {
  id: string;
  schemaVersion: number;
  kind:
    | "text"
    | "form"
    | "table"
    | "chart"
    | "test-results"
    | "artifact";
  data: unknown;
  actions?: UIAction[];
};
```

The application must validate `kind`, `schemaVersion`, `data` and `actions` before rendering.

Use a discriminated union and schema validation in the production implementation. The `unknown` type above is intentional: untrusted payloads must not be treated as valid application objects before validation.

### 5.4 UIAction

Actions should describe intent, not executable code.

Examples include submitting a validated form, opening an existing artifact, refreshing an approved result or requesting confirmation.

Each action must be mapped to a known application handler. Unknown action types must be rejected. Sensitive operations must require the same authorization and confirmation rules as their existing execution paths.

---

## 6. Detailed Implementation Tasks

## Phase 0 — Baseline Audit and Regression Protection

**Objective:** Establish a reliable record of existing behavior before changing the Composer.

Tasks:

* [ ] P0-01. Confirm the exact base commit and create a dedicated integration branch.
* [ ] P0-02. Inspect the current `frontend/package.json` and lockfile.
* [ ] P0-03. Confirm actual Next.js, React, TypeScript and build-tool versions.
* [ ] P0-04. Inspect `Composer`, `ChatLanding` and `ChatContainer`.
* [ ] P0-05. Trace the complete submit flow into `/api/run`.
* [ ] P0-06. Trace attachment upload, removal, drag-and-drop and upload failure behavior.
* [ ] P0-07. Document current Agent/Chat, engine, provider, model and tool-selection behavior.
* [ ] P0-08. Document stop, error, human response, resume and final-result flows.
* [ ] P0-09. Trace SSE event names and the mapping from raw payloads to UI state.
* [ ] P0-10. Document artifact creation and WorkspacePanel navigation.
* [ ] P0-11. Identify duplicated input state between landing and conversation surfaces.
* [ ] P0-12. Capture current behavior with automated tests or reproducible manual scenarios.
* [ ] P0-13. Record baseline TypeScript, lint, build and existing test results.
* [ ] P0-14. Identify existing technical debt separately from regressions introduced by this work.

**Acceptance criteria:**

The team can explain the current request, upload, execution, event, history and artifact flows. Baseline failures are recorded, and the existing golden path has a reproducible test procedure.

No production behavior changes in this phase.

## Phase 1 — Dependency and Integration Spike

**Objective:** Verify the actual libraries and integration approach before committing to an implementation.

Tasks:

* [ ] P1-01. Review the official Lexical documentation and supported React integration.
* [ ] P1-02. Select the minimum required Lexical packages.
* [ ] P1-03. Confirm compatible versions across Lexical packages.
* [ ] P1-04. Review the official OpenUI repository, package structure, API and license.
* [ ] P1-05. Identify the specific OpenUI renderer and language packages required.
* [ ] P1-06. Confirm whether the intended OpenUI integration can consume results from the existing PELDRUN backend.
* [ ] P1-07. Determine how streaming results, validation failures and user interactions are handled.
* [ ] P1-08. Review package dependencies, browser requirements and client-bundle impact.
* [ ] P1-09. Verify whether any OpenUI features require external services; exclude them from the required local execution path.
* [ ] P1-10. Build a minimal isolated Lexical proof of concept.
* [ ] P1-11. Build a separate OpenUI proof of concept using a small, application-owned component library.
* [ ] P1-12. Test malformed and incomplete OpenUI output.
* [ ] P1-13. Document the selected versions and integration decisions.
* [ ] P1-14. Record a go/no-go decision before production integration.

**Acceptance criteria:**

Both proof-of-concept integrations work independently. Their dependency and licensing implications are documented. No existing Composer or chat behavior has been replaced.

## Phase 2 — Composer Contracts and State Ownership

**Objective:** Define a stable boundary between editor state and application state.

Tasks:

* [ ] P2-01. Define the `ComposerMessage` contract.
* [ ] P2-02. Define attachment and context-reference types.
* [ ] P2-03. Define command and mention types.
* [ ] P2-04. Define validation rules for normalized messages.
* [ ] P2-05. Identify which state remains in the existing Zustand stores.
* [ ] P2-06. Keep execution mode, model, provider, agent and tool settings outside Lexical.
* [ ] P2-07. Define a single submission adapter for landing and ongoing-chat workflows.
* [ ] P2-08. Preserve the existing API payload and its compatibility behavior.
* [ ] P2-09. Define editor clear/reset behavior after successful submission.
* [ ] P2-10. Define behavior when submission fails.
* [ ] P2-11. Define behavior for attachment-only submissions.
* [ ] P2-12. Define how existing attachments and context references are represented.
* [ ] P2-13. Add unit tests for normalization and validation.
* [ ] P2-14. Document the contract and its versioning policy.

**Acceptance criteria:**

The editor does not own application configuration. Both entry points use the same submission boundary, and the existing API receives equivalent effective input.

## Phase 3 — Lexical Foundation

**Objective:** Introduce Lexical while preserving the current Composer behavior.

Tasks:

* [ ] P3-01. Create the dedicated Composer feature directory.
* [ ] P3-02. Implement `LexicalComposer` with a minimal configuration.
* [ ] P3-03. Implement the editable surface and placeholder.
* [ ] P3-04. Reproduce existing layout, spacing, focus and disabled states.
* [ ] P3-05. Implement the send command.
* [ ] P3-06. Implement Enter and Shift+Enter behavior.
* [ ] P3-07. Implement safe clearing after successful submission.
* [ ] P3-08. Preserve text when validation or submission fails.
* [ ] P3-09. Add cleanup for listeners and editor plugins.
* [ ] P3-10. Verify keyboard navigation and screen-reader behavior.
* [ ] P3-11. Verify IME input and multilingual text entry.
* [ ] P3-12. Test long prompts and large pasted content.
* [ ] P3-13. Add component tests for the editor lifecycle.
* [ ] P3-14. Integrate behind a controlled feature switch if practical.

**Acceptance criteria:**

Plain-text entry, keyboard behavior, reset behavior and error handling work without altering the execution layer.

## Phase 4 — Composer Attachments and Application Controls

**Objective:** Integrate the editor with the existing application controls without coupling them to Lexical internals.

Tasks:

* [ ] P4-01. Extract the attachment display into `composer-attachments.tsx`.
* [ ] P4-02. Preserve file selection and multiple-file upload.
* [ ] P4-03. Preserve drag-and-drop behavior.
* [ ] P4-04. Preserve attachment removal and file-size display.
* [ ] P4-05. Preserve the existing file upload endpoint and payload.
* [ ] P4-06. Preserve the Agent/Chat toggle.
* [ ] P4-07. Preserve agent, engine, model, provider and active-tool controls.
* [ ] P4-08. Preserve reasoning-effort selection and model capability handling.
* [ ] P4-09. Preserve stop and disabled behavior during execution.
* [ ] P4-10. Ensure controls remain available and usable on narrow screens.
* [ ] P4-11. Ensure attachment state is not accidentally duplicated in Lexical and application state.
* [ ] P4-12. Test upload failures, repeated selection and duplicate filenames.
* [ ] P4-13. Verify that landing-page and ongoing-chat submissions behave consistently.

**Acceptance criteria:**

All existing Composer controls remain functional. No duplicate execution or lost attachment behavior is introduced.

## Phase 5 — Advanced Lexical Features

**Objective:** Add structured input capabilities only after the basic Composer is stable.

Tasks:

* [ ] P5-01. Implement the slash-command registry.
* [ ] P5-02. Define command identifiers and validation rules.
* [ ] P5-03. Add the slash-command suggestion interface.
* [ ] P5-04. Implement mention suggestions for supported agents or context items.
* [ ] P5-05. Create custom mention nodes only where structured representation is required.
* [ ] P5-06. Define how mentions are normalized into `ComposerMessage`.
* [ ] P5-07. Define how context references are resolved and validated.
* [ ] P5-08. Preserve sensible behavior for pasted plain text.
* [ ] P5-09. Prevent unresolved mention tokens from silently becoming executable actions.
* [ ] P5-10. Add tests for selection, deletion, editing, copy/paste and submission.
* [ ] P5-11. Verify compatibility with right-to-left and multilingual text.
* [ ] P5-12. Keep advanced features optional if they are not yet supported by the backend.

**Acceptance criteria:**

Commands and mentions produce validated structured data. They do not bypass existing authorization or execution pathways.

## Phase 6 — Generative UI Contracts

**Objective:** Define the supported result types before introducing OpenUI into the main conversation.

Tasks:

* [ ] P6-01. Define the `UIResult` discriminated union.
* [ ] P6-02. Define a schema versioning strategy.
* [ ] P6-03. Define schemas for forms, tables, charts and test results.
* [ ] P6-04. Define how artifacts reference existing workspace files.
* [ ] P6-05. Define supported interaction types.
* [ ] P6-06. Define which interactions are local and which require a backend request.
* [ ] P6-07. Define validation and rejection behavior.
* [ ] P6-08. Define a fallback for unknown result kinds.
* [ ] P6-09. Define whether the result is transient or persisted in chat history.
* [ ] P6-10. Define loading, partial, complete and failed rendering states.
* [ ] P6-11. Decide how results are linked to their originating run or message.
* [ ] P6-12. Document the result contract and add schema tests.

**Acceptance criteria:**

The application can distinguish text, artifact and structured results without depending on OpenUI-specific objects in the core chat model.

## Phase 7 — OpenUI Renderer Integration

**Objective:** Add OpenUI as an isolated rendering capability inside PELDRUN.

Tasks:

* [ ] P7-01. Create the dedicated `generative-ui` feature.
* [ ] P7-02. Implement the OpenUI adapter.
* [ ] P7-03. Implement the result normalizer and schema validator.
* [ ] P7-04. Build an application-owned component library.
* [ ] P7-05. Start with one or two bounded result types rather than a broad catalog.
* [ ] P7-06. Implement the OpenUI renderer wrapper.
* [ ] P7-07. Keep rendering isolated from the chat shell.
* [ ] P7-08. Add a result-level error boundary and fallback.
* [ ] P7-09. Ensure a rendering failure cannot terminate an agent run.
* [ ] P7-10. Validate all actions before dispatch.
* [ ] P7-11. Reuse existing artifact and workspace handlers where applicable.
* [ ] P7-12. Ensure unknown components and unsupported actions are rejected.
* [ ] P7-13. Verify that ordinary Markdown responses bypass OpenUI.
* [ ] P7-14. Verify that the integration does not require OpenUI Gateway for local operation.
* [ ] P7-15. Review dependency size and load the renderer only where appropriate.
* [ ] P7-16. Add unit and integration tests for the renderer.

**Acceptance criteria:**

OpenUI renders supported results within the existing PELDRUN shell. Disabling or failing OpenUI does not prevent chat, execution or artifact access.

## Phase 8 — Event and Result Normalization

**Objective:** Keep the frontend independent of inconsistent raw event payloads.

Tasks:

* [ ] P8-01. Inventory current SSE event types and payload shapes.
* [ ] P8-02. Define normalized frontend event types where necessary.
* [ ] P8-03. Centralize mapping from raw payloads to normalized events.
* [ ] P8-04. Avoid parsing raw `ask_human` JSON independently in presentation components.
* [ ] P8-05. Normalize artifact references and result identifiers.
* [ ] P8-06. Preserve existing event names and compatibility.
* [ ] P8-07. Handle unknown event types safely.
* [ ] P8-08. Handle duplicate, delayed and malformed events.
* [ ] P8-09. Keep event transport separate from rendering logic.
* [ ] P8-10. Add regression tests for final, error, approval and artifact events.

**Acceptance criteria:**

Presentation components consume stable normalized objects. Existing backend event behavior remains supported.

## Phase 9 — Regression, Security and End-to-End Testing

**Objective:** Verify that the new frontend works with real PELDRUN execution paths.

Tasks:

* [ ] P9-01. Run TypeScript checks.
* [ ] P9-02. Run lint checks using the repository's supported command.
* [ ] P9-03. Run the production build.
* [ ] P9-04. Run unit tests for Composer normalization and validation.
* [ ] P9-05. Run Lexical component tests.
* [ ] P9-06. Run OpenUI schema and renderer tests.
* [ ] P9-07. Run integration tests for file uploads and submission.
* [ ] P9-08. Run end-to-end tests in a production-like environment.
* [ ] P9-09. Verify chat history and workspace behavior.
* [ ] P9-10. Verify human input, approval and resume behavior.
* [ ] P9-11. Verify tool failure and retry behavior.
* [ ] P9-12. Verify artifact creation and navigation.
* [ ] P9-13. Verify malformed UI payloads and unknown actions.
* [ ] P9-14. Verify that malicious or untrusted content cannot execute arbitrary frontend code.
* [ ] P9-15. Verify renderer failure and recovery.
* [ ] P9-16. Compare key behaviors against the Phase 0 baseline.
* [ ] P9-17. Record test results and unresolved limitations.

**Acceptance criteria:**

All critical regression tests pass. Any pre-existing failure is documented separately. No release proceeds with an unexplained regression in a core workflow.

## Phase 10 — Controlled Release and Maintenance

Tasks:

* [ ] P10-01. Enable Lexical first, without requiring OpenUI.
* [ ] P10-02. Validate the core Composer workflow.
* [ ] P10-03. Enable OpenUI for a limited set of result types.
* [ ] P10-04. Keep a way to disable the OpenUI renderer independently.
* [ ] P10-05. Monitor rendering failures, invalid payloads and frontend exceptions.
* [ ] P10-06. Document dependency versions and upgrade procedures.
* [ ] P10-07. Document how to add a new Lexical plugin.
* [ ] P10-08. Document how to add a new OpenUI component safely.
* [ ] P10-09. Document result-schema changes and backward compatibility.
* [ ] P10-10. Remove temporary compatibility code only after proving it is no longer required.

**Acceptance criteria:**

The team can modify or disable either integration without rebuilding the chat shell or changing the Core execution engine.

---

## 7. Security Requirements

The following requirements are mandatory:

1. Treat all model-generated UI content as untrusted.
2. Validate structured payloads before rendering.
3. Use allow-listed component types.
4. Do not execute arbitrary JavaScript, dynamic imports or model-generated React components.
5. Do not trust client-provided action identifiers or parameters.
6. Validate action payloads again at the application/backend boundary.
7. Apply explicit URL and navigation policies to links generated from untrusted content.
8. Sanitize untrusted HTML if an HTML rendering path is introduced.
9. Enforce existing user permissions and confirmation requirements.
10. Avoid exposing secrets, API keys or internal execution configuration in generated UI.
11. Define size limits for result payloads and arrays.
12. Test malformed, incomplete and adversarial inputs.

OpenUI is a rendering and generation mechanism, not an authorization system.

## 8. Testing Strategy

Use four complementary test layers.

### Unit tests

Test pure functions and contracts:

* Composer normalization.
* Command and mention validation.
* Result schema validation.
* Action validation.
* Result-type routing.
* Error and fallback selection.

### Component tests

Test:

* Lexical input and keyboard behavior.
* Attachment controls.
* Composer disabled and running states.
* OpenUI rendering of valid and invalid results.
* Result error boundaries.
* Interaction callbacks.

### Integration tests

Test:

* Composer to existing submission flow.
* Attachment upload to task submission.
* Event normalization to ChatThread.
* OpenUI action dispatch to application handlers.
* Artifact results to WorkspacePanel.

### End-to-end tests

The minimum golden path is:

`Create conversation → Enter request → Attach file → Start agent → Receive progress events → Execute tools → Handle error or clarification → Resume or retry → Create artifact → Display final result`

Also verify:

* Chat mode still works independently.
* Stop behavior still works.
* Invalid generated UI does not break the conversation.
* Existing history remains readable.
* The app remains useful when OpenUI is unavailable.

---

## 9. Engineering Rules for the Team

1. Do not combine Lexical and OpenUI integration into one large pull request.
2. Do not rewrite unrelated files during this project.
3. Do not change the Core execution lifecycle as part of a frontend-only task.
4. Do not introduce new global state when an existing owner already exists.
5. Do not duplicate attachment or submission logic across landing and chat components.
6. Do not couple domain contracts to third-party library types.
7. Do not silently swallow validation or rendering errors.
8. Do not remove a compatibility path until tests demonstrate it is unnecessary.
9. Do not mark a task complete solely because TypeScript compiles.
10. Every phase must have an explicit acceptance result.
11. Every new module must have a clear owner and responsibility.
12. Every public contract must have documented compatibility expectations.
13. Dependencies must be pinned or constrained consistently with the project's package-management policy.
14. Keep changes small enough to review and revert independently.
15. Record architectural decisions that would otherwise be lost in implementation details.

## 10. Definition of Done

The project is complete only when:

* Lexical replaces the existing textarea without losing core Composer behavior.
* Landing and conversation input use a consistent submission boundary.
* Advanced commands and mentions are structured and validated.
* OpenUI renders selected structured results inside the existing PELDRUN interface.
* Existing Markdown, code, artifacts, workspace and execution views continue to work.
* Core and backend execution contracts remain compatible.
* The integration is divided into maintainable modules.
* Invalid generated UI cannot execute arbitrary code or break the chat.
* A renderer failure does not stop task execution.
* Critical unit, integration and end-to-end tests pass.
* Dependency, security and maintenance decisions are documented.
* The team can update Lexical or OpenUI independently of the Core engine.

## 11. Final Engineering Decision

Proceed in this order:

**Baseline and contracts → Lexical foundation → Composer regression testing → Advanced Composer capabilities → Generative UI contracts → OpenUI renderer → Security and end-to-end validation → Controlled release.**

Lexical is part of the primary input experience. OpenUI is an optional rendering capability for structured agent results.

The success criterion is not the number of new components. It is the ability to improve PELDRUN's user experience without compromising existing execution reliability, local-first operation, or maintainability.

**Core principle:** Build stable application contracts that allow the UI to evolve without requiring a rewrite of the execution system.
