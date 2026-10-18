

# PELDRUN Source Control — Frontend Engineering & Architecture Guide

**Version:** 2.0 (October 2026)
**Status:** Final Architecture Approved
**Target Audience:** Frontend Developers, UI/UX Designers, Full-Stack Engineers

---

## 1. Executive Summary & Core Principles

The PELDRUN Source Control Integration (SCI) frontend is not a traditional developer dashboard. It is a **context-aware, local-first workbench** designed for diverse users (non-developers included). It bridges the gap between conversational AI agents and deterministic, secure Git operations.

### Core Principles:
1. **"OAuth ≠ Consent":** Connecting an account (Provider Hub) is separate from approving a specific remote write (Approval Ticket).
2. **Three-Column Workbench:** Left (Navigation/Threads), Center (Chat), Right (Context Panel). The Source Control UI lives in the Right Panel as a dynamic, collapsible state.
3. **Progressive Disclosure:** Users who don't need Git never see it. It is gated by the **Sovereign Capability Store** and only surfaces when the agent performs an action or the user explicitly opens it.
4. **Zero-Trust Frontend:** The frontend never stores tokens. It is a pure rendering layer for the backend's state machine.

---

## 2. Architectural Decisions & Edge Cases

### 2.1 The Three-Column Workbench
- **Left:** Thread History / Navigation.
- **Center:** Chat Interface.
- **Right:** Dynamic Context Panel (Tabs: `Preview`, `Files`, `Source Control`, `Logs`).
- **The Rule:** The Right Panel is `collapsed` by default during standard chat. It auto-expands ONLY when an Approval is requested or the user clicks a Context Card in the chat.

### 2.2 The "Ephemeral Credentials" Edge Case (Local Git / One-off Push)
**Scenario:** A user has a local Git folder and wants the agent to edit it. They do NOT need remote provider authorization. Alternatively, they want to push to a random GitHub account *once* without saving credentials.
**Solution:**
- **Local-Only Workspaces:** If `provider == "local"`, no OAuth is triggered. The agent operates strictly locally. Pushes are disabled.
- **Ephemeral Session Tokens:** If a remote push is needed for a one-off, the UI provides a "One-Time Push" modal. The user pastes a PAT (Personal Access Token) or completes an ephemeral OAuth flow. The token is passed directly to the backend's `GitPushManager` via memory, **never saved to the OS Vault**, and discarded immediately after the push completes or fails.

### 2.3 The Provider Hub (`/integrations`)
- This is a dedicated, full-page settings route.
- **Purpose:** The ONLY place where OAuth PKCE flows are initiated, accounts are connected/disconnected, and default policies are set.
- **GitHub App:** We will use a GitHub App (not just OAuth) for fine-grained repository access, short-lived tokens, and better security posture.
- **UI:** A clean list of cards (GitHub, GitLab, Local Folder) with connection status and permission scopes.

### 2.4 Integration with Sovereign Capability Store
- The JSON manifest (`source_control.json`) is registered in the store.
- If the toggle is **OFF**: The agent cannot call the tool. The Right Panel hides the `Source Control` tab entirely.
- If the toggle is **ON**: The tool is available. The Right Panel shows the `Source Control` tab.

---

## 3. The User Flow (Approval & Execution)

We are adopting the **B-Hybrid** model (Agent coordinates, UI executes the exact push via backend endpoints).

```text
[Chat] User: "Refactor main.py and push it"
  ↓
[Agent] Calls: source_control(action="create_workspace", depth=1) // Shallow clone!
  ↓
[Agent] Edits file, runs diff, calls: source_control(action="request_operation_approval")
  ↓
[Chat UI] Renders an **Operation Card**:
  "⏳ Ready for review. [Open Source Control]"
  ↓
[User] Clicks the card.
  ↓
[Right Panel] Auto-expands, switches to `Source Control` mode.
  - Shows DiffViewer, FileChangeList, Checks, Warnings.
  - Shows explicit button: **[Approve & Push Branch]**
  ↓
[User] Clicks [Approve & Push Branch].
  ↓
[Frontend Client]
  1. POST /operations/{id}/approve
  2. POST /operations/{id}/push
  ↓
[Backend] Validates ticket binding, consumes it, executes `git push`, updates state.
  ↓
[Chat UI] Updates Operation Card to:
  "✅ Pushed successfully. [Create Pull Request]"
```

---

## 4. Frontend Module Breakdown

The frontend feature is isolated under `src/features/source-control/`.

### 4.1 API & Validation (`/api`)
- **`schemas.ts`**: Zod schemas mirroring the backend's Pydantic models exactly (e.g., `OperationResponse`, `ApproveOperationRequest`, `DiffSummary`, `ApprovalTicket`).
- **`source-control-client.ts`**: Typed fetch wrapper. Implements the strict error mapping:
  - `401` → Trigger `useProviders` to show reconnect prompt.
  - `403` → Show Permission Denied UI.
  - `412` → Handle `RemoteStateChangedError` (show diff re-fetch dialog).
  - `422` → Validation errors.

### 4.2 State Management (`/state`)
- **`source-control-store.ts`** (Zustand):
  - Tracks: Active operation, active repository, current diff, panel state (`isOpen`, `activeTab`).
  - **Security:** Never stores OAuth tokens or raw secrets.

### 4.3 Hooks (`/hooks`)
- **`useProviders.ts`**: Manages `/integrations` logic (OAuth start, status check, disconnect).
- **`useRepositories.ts`**: Fetch and search authorized repos.
- **`useOperation.ts`**: Polls `/operations/{id}` (or listens via SSE/WebSocket) to sync the Chat Operation Card with the Right Panel state.

### 4.4 Components (`/components`)

**Provider Hub Components:**
- `ProviderConnectDialog.tsx`: Explains scopes, opens system browser.
- `ProviderConnectionCard.tsx`: Shows connected account, repo count, disconnect button.

**Workbench / Panel Components:**
- `SourceControlPanel.tsx`: The main container for the Right Panel.
- `OperationTimeline.tsx`: Vertical stepper showing state transitions (`Created` -> `Review Required` -> `Pushed`).
- `FileChangeList.tsx`: Tree view of changed files, with red warning badges for `.github/workflows/*` or binary files.

**Approval Components (P0):**
- `ApprovalHeader.tsx`: Shows Repo, Source -> Target branch, Base SHA.
- `DiffViewer.tsx`: Syntax-highlighted unified diff. Handles horizontal scrolling and line numbers.
- `CheckResults.tsx`: Shows CI/CD status (Passed/Failed/Pending).
- `ApprovalDialog.tsx`: The final action block.
  - **Rule:** No generic "Approve" button.
  - Buttons: `[Reject]` | `[Keep Local]` | `[Approve & Push Branch]`.

**Chat Integration Components:**
- `ChatOperationCard.tsx`: The inline chat component that acts as a deep-link trigger to open the Right Panel.

---

## 5. Handling Edge Cases & Backend Constraints

1. **Shallow Clones (`--depth=1`):** The UI must pass `clone_depth: 1` by default when creating a workspace for a single-file edit to ensure instant execution. Full clones are an advanced option.
2. **Stale Reviews:** If the remote branch moves while the user is reviewing the diff, the next fetch will yield a `diff_sha256` mismatch or `RemoteStateChangedError`. The UI MUST intercept `412 Precondition Failed` and show: *"The remote branch has changed since you started reviewing. Please re-review the latest changes."*
3. **Background Approvals:** If the user closes the chat but leaves the app open, the operation remains in the journal. If they open `/source-control/operations/{id}` directly, the UI must function exactly the same (read from the backend state).
4. **Accessibility:** The Right Panel must be fully keyboard navigable (`Tab` to navigate, `Enter` to approve/reject). Destructive actions (like discarding changes) require a secondary confirmation modal.

---

## 6. Phased Implementation Roadmap

We will build the frontend in the following sequential phases. No phase begins until the previous one is tested and verified.

### Phase 1: Contracts & Foundations (The API Layer)
- [ ] Create `schemas.ts` (Zod) matching all Backend Pydantic DTOs.
- [ ] Create `source-control-client.ts` with rigorous error handling (401, 412, etc.).
- [ ] Define `types/index.ts` ensuring state enums are lowercase.

### Phase 2: State & Hooks
- [ ] Implement `source-control-store.ts` (Zustand).
- [ ] Implement `useProviders.ts` (for the OAuth flow).
- [ ] Implement `useOperation.ts` (for polling state updates).

### Phase 3: The Provider Hub (`/integrations`)
- [ ] Build the `/integrations` page layout.
- [ ] Build `ProviderConnectionCard` and `ProviderConnectDialog`.
- [ ] Wire up OAuth flow (redirect to backend `/begin`, handle callback).

### Phase 4: Right Panel & Chat Integration
- [ ] Create `SourceControlPanel.tsx` and mount it in the existing `workspace` Side Panel.
- [ ] Build `ChatOperationCard` to render in the chat thread.
- [ ] Wire the Chat Card click to auto-expand the Right Panel.

### Phase 5: The Review & Approval Engine (P0)
- [ ] Build `DiffViewer` (syntax highlighting, unified diff parsing).
- [ ] Build `FileChangeList` with warning badges.
- [ ] Build `ApprovalHeader` and `CheckResults`.
- [ ] Build `ApprovalDialog` with explicit action buttons.
- [ ] Wire the `[Approve & Push Branch]` button to the sequential API calls.

### Phase 6: Operation Timeline & Polish
- [ ] Build `OperationTimeline` for full context history.
- [ ] Implement skeleton loaders, empty states, and error toasts.
- [ ] Add keyboard accessibility and responsive design for the Right Panel.

---

## 7. Appendix: Critical API Endpoints (Frontend Reference)

- `POST /api/source-control/connections/{provider}/begin` -> Returns OAuth URL.
- `GET /api/source-control/repositories` -> Lists repos.
- `POST /api/source-control/operations` -> Creates operation.
- `POST /api/source-control/operations/{id}/approval` -> Agent requests ticket.
- `POST /api/source-control/operations/{id}/approve` -> User grants ticket.
- `POST /api/source-control/operations/{id}/push` -> Consumes ticket & pushes.
- `GET /api/source-control/operations/{id}` -> Fetches state, events, and `current_state` (lowercase).

---
---
==============================================================================
---
**"مساحة العمل يجب أن تكون هي مساحة الجلسة"**

هذا يعني أن الوكيل عندما يستدعي `source_control` (مثل `clone` أو `create_workspace`)، فإنه سيعمل مباشرة داخل مجلد `storage/.../chats/<chat_id>/files/`. هذا يدمج أدوات الملفات (`str_replace_editor`) مع أدوات Git (`source_control`) في نفس الصندوق الرملي (Sandbox)، ويجعل تبويب `Files` في اللوحة اليمنى يعرض نفس الملفات التي يعدلها Git بالضبط.

بناءً على هذا القرار المعماري النهائي، إليك قائمة الملفات التي سنعمل عليها بالترتيب، تليها قائمة المهام التنفيذية. **بدون كتابة أي كود الآن.**

---

### أولاً: قائمة الملفات المطلوبة (بالترتيب)

سنبني الواجهة الأمامية تحت المسار `frontend/src/features/source-control/` بترتيب يضمن تسلسل الاعتماديات (Dependencies).

#### المرحلة 1: العقود والبيانات (Contracts & API)
1. `api/schemas.ts` (تعريفات Zod ومطابقتها مع Pydantic).
2. `types/index.ts` (واجهات TypeScript العامة).
3. `api/source-control-client.ts` (عميل HTTP ومعالجة الأخطاء 401/412/422).

#### المرحلة 2: الحالة والخطافات (State & Hooks)
4. `state/source-control-store.ts` (مخزن Zustand للتحكم في اللوحة والعملية الحالية).
5. `hooks/useProviders.ts` (إدارة الاتصالات والمصادقة).
6. `hooks/useRepositories.ts` (جلب المستودعات).
7. `hooks/useOperation.ts` (مزامنة حالة العملية مع الشات واللوحة).

#### المرحلة 3: الأدوات المساعدة (Utilities)
8. `utils/diff-format.ts` (تحليل وعرض نصوص الـ Diff).
9. `utils/permission-labels.ts` (ترجمة الصلاحيات التقنية لنصوص مفهومة).

#### المرحلة 4: مركز المزودين (Provider Hub - `/integrations`)
10. `components/ProviderConnectionCard.tsx` (بطاقة عرض حالة المزود).
11. `components/ProviderConnectDialog.tsx` (نافذة بدء OAuth).
12. `components/DisconnectProviderDialog.tsx` (تأكيد قطع الاتصال).
13. `app/integrations/page.tsx` (الصفحة المستقلة لمركز المزودين).

#### المرحلة 5: تكامل الشات واللوحة اليمنى (Workbench Integration)
14. `components/ChatOperationCard.tsx` (البطاقة التي تظهر في الشات لطلب المراجعة).
15. `components/SourceControlPanel.tsx` (حاوية تبويب Source Control في اللوحة اليمنى).
16. `components/OperationTimeline.tsx` (مخطط الأحداث الزمني).

#### المرحلة 6: محرك المراجعة والاعتماد (Review & Approval - P0)
17. `components/FileChangeList.tsx` (قائمة الملفات المعدلة مع شارات التحذير).
18. `components/CheckResults.tsx` (عرض حالة الفحوصات والاختبارات).
19. `components/DiffViewer.tsx` (مستعرض الفروقات الموحد).
20. `components/ApprovalHeader.tsx` (بيانات الفرع والمستودع والـ SHA).
21. `components/ApprovalDialog.tsx` (أزرار الموافقة والرفض الصريحة).
22. `app/source-control/page.tsx` (الصفحة الكاملة للمراجعة لمن لا يستخدم اللوحة الجانبية).

---

### ثانياً: قائمة المهام التنفيذية (Task List)

هذه هي المهام التي سأقوم بتنفيذها خطوة بخطوة. لن أنتقل لمهمة حتى ننتهي من التي قبلها ونختبرها.

#### المهمة 1: تأسيس عقود البيانات (Contracts)
- [ ] بناء `schemas.ts` باستخدام Zod لضمان تطابق `OperationResponse`, `ApprovalTicket`, `DiffSummary` مع الباك إند.
- [ ] بناء `types/index.ts` لتعريف الحالات (Enums) وتضمينها بالحروف الصغيرة (lowercase) كما يتطلب الباك إند.

#### المهمة 2: بناء عميل الاتصال (API Client)
- [ ] بناء `source-control-client.ts` ليكون واجهة موحدة لجميع طلبات الـ HTTP.
- [ ] تطبيق معالجة الأخطاء الصارمة: 401 (إعادة اتصال)، 412 (تغيرت الحالة البعيدة - Stale Review)، 422 (خطأ في المدخلات).

#### المهمة 3: إدارة الحالة (State Management)
- [ ] بناء `source-control-store.ts` (باستخدام Zustand) للتحكم في حالة اللوحة اليمنى (مفتوحة/مغلقة، التبويب النشط).
- [ ] ربط `chat_id` الحالي بمعرف مساحة العمل (Workspace ID) لضمان أن كل العمليات تتم داخل `files/` الخاصة بالجلسة.

#### المهمة 4: بناء مركز المزودين (Provider Hub)
- [ ] تصميم صفحة `/integrations` المستقلة.
- [ ] بناء مكونات الاتصال (`ProviderConnectionCard`, `ProviderConnectDialog`) لبدء تدفق OAuth PKCE.
- [ ] ربط زر "اتصال" بفتح متصفح النظام واستقبال الـ Callback.

#### المهمة 5: دمج الشات مع اللوحة اليمنى (Workbench)
- [ ] بناء `ChatOperationCard.tsx` لتظهر داخل الشات عند طلب الوكيل موافقة.
- [ ] بناء `SourceControlPanel.tsx` في اللوحة اليمنى لتستقبل الأمر وتفتح وضع المراجعة (Approval Mode) تلقائياً.

#### المهمة 6: محرك المراجعة والاعتماد (The Core Security)
- [ ] بناء `DiffViewer.tsx` لعرض التغييرات مع دعم التلوين والتمرير الأفقي.
- [ ] بناء `FileChangeList.tsx` وإظهار التحذيرات الحمراء لملفات `.github/workflows` أو الملفات الثنائية.
- [ ] بناء `ApprovalDialog.tsx` مع الأزرار الصريحة جداً (مثل `[Approve & Push Branch]`) ومنع أي زر "موافقة" عام.
- [ ] برمجة تسلسل التنفيذ: عند الضغط على الموافقة، يتم إرسال طلب `approve` ثم `push` بالترتيب.

#### المهمة 7: التعامل مع الحالات الاستثنائية (Edge Cases)
- [ ] معالجة خطأ `412` (إذا تحرك الفرع البعيد): إظهار رسالة تطالب المستخدم بإعادة المراجعة وإعادة جلب الـ diff.
- [ ] معالجة "الاتصال المنقطع": إذا انتهت صلاحية الـ Token، يتم توجيه المستخدم لصفحة `/integrations` فوراً.
- [ ] دعم الاستخدام بدون اتصال (Local-Only): إذا كان المستودع محلياً، يتم تعطيل أزرار الدفع البعيد (Push) وإظهار رسالة توضيحية.

#### المهمة 8: الاختبارات والتحسين النهائي
- [ ] اختبار إمكانية الوصول (Accessibility): التنقل بالكيبورد داخل اللوحة اليمنى.
- [ ] إضافة Skeleton Loaders أثناء جلب البيانات.
- [ ] التأكد من أن زر "إغلاق اللوحة" لا يلغي العملية، بل يعيدها للخلفية (Background).

---
