# PELDRUN

## Milestone P1 — Hardening, Usage Accounting & Observability

### الوثيقة الهندسية الموحدة للتنفيذ
---

### 1. التحليل الاستراتيجي والمعماري لأنظمة حساب التوكين (Deep Strategic Architecture Analysis)

بناء نظام شامل لحساب التوكين (Enterprise-Grade Token Metering & Observability) داخل **PELDRUN** يتطلب تصميماً سيادياً مستقلاً (Sovereign & Self-Contained) لا يعتمد على خدمات سحابية خارجية (مثل Langfuse أو Helicone أو Portkey)، مع الحفاظ على الأداء الفائق والسرية التامة للبيانات في بيئات العمل المحلية (Local-First).

```
                      [LLM Invocations (Agent Step / Direct Chat)]
                                           │
                                           ▼
             [Layer 1: Telemetry Extractor (Usage + Latency + Model + Provider)]
                                           │
               ┌───────────────────────────┴───────────────────────────┐
               ▼                                                       ▼
  [Layer 2A: Ephemeral Chat Storage]                     [Layer 2B: Immutable Global Ledger]
  - Saved in workspace session.json                      - Stored in SQLite (storage/peldrun_runtime.db)
  - Per-turn token usage                                 - Survives chat deletion permanently
  - Chat total running tokens & cost                     - Indexed by timestamp, provider, model, chat_id
               │                                                       │
               └───────────────────────────┬───────────────────────────┘
                                           ▼
                          [Layer 3: Pricing & Cost Engine]
                          - Dynamic Unit Economics (Input/Output price per 1M tokens)
                          - Configurable per model & provider
                                           │
                                           ▼
                        [Layer 4: Reporting & Analytics API]
                        - Daily, weekly, monthly aggregations
                        - Consumption breakdown by provider, model, and mode

```

---

#### الركائز المعمارية الأربع للنظام:

1. **نموذج التخزين ثنائي الطبقات (Dual-Layer Persistence Pattern):**
* **الطبقة الأولى (محلية سياقية - Context-Bound):** تُخزَّن بيانات التوكين لكل خطوة ورسالة مباشرة داخل ملف `session.json` في مجلد الشات المستقل (`project_manager`). هذا يضمن استرجاع استهلاك الشات فورياً داخل واجهة المحادثة.
* **الطبقة الثانية (دفتر أستاذ عام ودائم - Immutable Global Ledger):** تُسجَّل كل عملية استدعاء للنموذج ذرياً في جدول مستقل داخل قاعدة بيانات SQLite المركزية (`storage/peldrun_runtime.db`). **هذه البيانات لا تُحذف أبداً حتى لو قام المستخدم بمسح مجلد الشات بالكامل من لوحة التحكم**، مما يضمن الحفاظ على سجل المحاسبة التاريخي الدقيق.


2. **التكامل غير التدخلي (Non-Invasive Ingestion):**
* استخلاص بيانات الاستهلاك (`prompt_tokens`, `completion_tokens`, `total_tokens`) مباشرة من ردود مزودي الـ LLM (`OpenAICompatProvider` و `LLMResponse`).
* في حال كان الموديل المحلي (مثل بعض إصدارات Ollama القديمة أو LM Studio عند الـ Streaming) لا يرسل حقل `usage` في نهاية التدفق، يتم تفعيل **Tokenizer Fallback Estimator** لحساب التوكين تقديرياً بدقة رياضية عبر مكتبة الترميز (`peldrun/llm/tokenizer.py`).


3. **محرك التسعير والاقتصاديات المخصصة (Dynamic Unit Economics Engine):**
* تمكين المستخدم من تعريف تسعيرة مخصصة لكل موديل ومزود (`input_cost_per_million` و `output_cost_per_million`).
* النماذج المحلية (Local Models على LM Studio و Ollama) تسجل افتراضياً تكلفة قدرها `0.00$`.
* حساب التكلفة فوري لكل رسالة، مع إمكانية إعادة حساب التكلفة بأثر رجعي للتقارير عند تحديث الأسعار.


4. **محرك التقارير والتحليلات الزمنية (Time-Series Aggregation Engine):**
* استعلامات SQL مفهرسة وسريعة تتيح تجميع الاستهلاك على فترات زمنية محددة:
* تقارير يومية (`Daily Breakdown`)
* تقارير أسبوعية (`Weekly Trends`)
* تقارير شهرية (`Monthly Billing Cycles`)
* تقسيم تفصيلي حسب المزود (`Provider`) والموديل (`Model`) ونمط التشغيل (`Agent` مقابل `Direct Chat`).





---

### 2. توسيع وتحديث خارطة طريق Milestone P1 (Hardening & Observability)

تم تحديث وتوسيع مراحل **Milestone P1** لدمج نظام حساب التوكين الشامل ومحرك التسعير والتقارير المتقدمة دون المساس باستقرار النواة التي تم إنجازها في **P0**:

```
MILESTONE P1 — EXPANDED ROADMAP:
├── P1-01: Engine Resolver Decoupling & Explicit Selection
├── P1-02: Web-Core Architectural Isolation & Public Adapter
├── P1-03: Comprehensive Token Accounting & Cost Metering Subsystem
│    ├── P1-03A: Core LLM Usage Extraction & Tokenizer Fallback
│    ├── P1-03B: Dual-Layer Persistence (Chat session.json + Global SQLite Ledger)
│    ├── P1-03C: Dynamic Pricing & Cost Calculation Engine
│    ├── P1-03D: Time-Series Analytics & Aggregated Reporting APIs
│    └── P1-03E: Frontend Token Telemetry & Analytics Dashboard UI
├── P1-04: Persistent Artifact Metadata & History
├── P1-05: Structured Diagnostics, Tracing & Latency Telemetry
└── P1-06: Frontend Type Hardening & Operational Polish

```

---

### 3. خطة العمل التنفيذية التفصيلية لمرحلة P1-03 (Token Accounting Subsystem)

#### المرحلة P1-03A: استخلاص التوكين وتأمين البدائل (Ingestion & Extraction Layer)

* **الهدف:** التقاط استهلاك التوكين بدقة من كل استدعاء `LLM` في نمطي `Agent` و `Direct Chat`.
* **التنفيذ:**
1. ترقية نموذج `LLMResponse` في `backend/peldrun/llm/client.py` ليتضمن كائن `TokenUsage`:
```python
class TokenUsage(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cached_tokens: Optional[int] = 0
    estimated: bool = False

```


2. في `backend/peldrun/llm/providers/openai_compat.py`: استخلاص كائن `response.usage` المرفق مع استجابات المزود وتمريره في `LLMResponse`.
3. في حال غياب حقل `usage`: استدعاء `peldrun/llm/tokenizer.py` لحساب التوكين بناءً على محتوى الرسائل ومخرجات الموديل لضمان عدم وجود أي رسالة بتوكين مفقود (`estimated = True`).
4. في `ToolCallAgent.step()` و `runner.py`: ربط الـ tokens المستهلكة في كل خطوة مع كائن `ChatMessage.metadata["usage"]` وتحديث العداد التراكمي في `ExecutionState.metadata["total_usage"]`.



#### المرحلة P1-03B: التخزين ثنائي الطبقات (Dual-Layer Persistence Engine)

* **الهدف:** توثيق الاستهلاك في سجل الشات، وحفظ نسخة غير قابلة للحذف في قاعدة البيانات العامة.
* **التنفيذ:**
1. **الطبقة المحلية (Chat Workspace):**
* تحديث `project_manager.py` ومسار `save_chat_session` لتسجيل:
* في كل `turn`: حقل `usage: { prompt_tokens, completion_tokens, total_tokens, cost_usd }`.
* في أعلى ملف `session.json`: كائن تجميعي للشات كامل `usage_summary: { prompt_tokens, completion_tokens, total_tokens, total_cost_usd, turn_count }`.




2. **الطبقة الدائمة العامة (Global SQLite Ledger):**
* إضافة جدول `token_ledger` مفهرس بالكامل داخل `backend/peldrun/runtime/store.py` في قاعدة بيانات SQLite المركزية:
```sql
CREATE TABLE IF NOT EXISTS token_ledger (
    id TEXT PRIMARY KEY,
    timestamp REAL NOT NULL,
    chat_id TEXT NOT NULL,
    job_id TEXT NOT NULL,
    turn_id TEXT,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    mode TEXT NOT NULL, -- 'agent' or 'chat'
    prompt_tokens INTEGER NOT NULL,
    completion_tokens INTEGER NOT NULL,
    total_tokens INTEGER NOT NULL,
    cost_usd REAL DEFAULT 0.0,
    is_estimated INTEGER DEFAULT 0,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_token_ledger_chat ON token_ledger(chat_id);
CREATE INDEX IF NOT EXISTS idx_token_ledger_provider_model ON token_ledger(provider, model);
CREATE INDEX IF NOT EXISTS idx_token_ledger_ts ON token_ledger(timestamp);

```


* توفير دوال تسجيل ذرية داخل `SqliteRunStore`:
* `record_token_usage(entry: TokenLedgerEntry)`
* `get_chat_total_tokens(chat_id: str)`
* `get_global_token_summary()`







#### المرحلة P1-03C: محرك التسعير وحساب التكلفة (Dynamic Pricing & Unit Economics)

* **الهدف:** السماح للمستخدم بتعيين أسعار الإدخال والإخراج لكل نموذج وحساب التكلفة المالية تلقائياً.
* **التنفيذ:**
1. إنشاء جدول `model_pricing` في SQLite:
```sql
CREATE TABLE IF NOT EXISTS model_pricing (
    id TEXT PRIMARY KEY,
    provider TEXT,
    model_pattern TEXT NOT NULL, -- e.g. 'gpt-4o', 'qwen*', 'deepseek*'
    input_cost_per_million REAL NOT NULL DEFAULT 0.0,
    output_cost_per_million REAL NOT NULL DEFAULT 0.0,
    currency TEXT NOT NULL DEFAULT 'USD',
    updated_at REAL NOT NULL
);

```


2. تضمين أسعار افتراضية للنماذج الشائعة:
* النماذج المحلية (`LM Studio`, `Ollama`): `0.00$ / 1M`
* النماذج التجارية الشائعة (`gpt-4o`, `claude-3-5`, `deepseek-v3`): تسعيرة افتراضية يمكن تعديلها.


3. بناء محرك احتساب فوري:

$$\text{Cost} = \left(\frac{\text{Prompt Tokens}}{1,000,000} \times \text{Input Price}\right) + \left(\frac{\text{Completion Tokens}}{1,000,000} \times \text{Output Price}\right)$$


4. واجهات تحكم برمجية (Pricing APIs):
* `GET /api/telemetry/pricing`: استعراض قائمة الأسعار المسجلة.
* `POST /api/telemetry/pricing`: إضافة أو تحديث تسعيرة نموذج معين.
* `DELETE /api/telemetry/pricing/{id}`: حذف تسعيرة مخصصة.





#### المرحلة P1-03D: محرك التقارير والتحليلات التراكمية (Reporting & Analytics APIs)

* **الهدف:** إنتاج تحليلات زمنية دقيقة وتقارير استهلاك شاملة.
* **التنفيذ:**
1. بناء خدمة `backend/omweb/services/telemetry_service.py` لاستخراج البيانات:
* `GET /api/telemetry/dashboard`: مؤشرات الأداء الرئيسية (KPIs) الشاملة:
* إجمالي التوكين المستهلك تاريخياً.
* إجمالي التكلفة التقديرية بالدولار.
* أكثر النماذج استهلاكاً (Top Models).
* أكثر المزودين استخداماً (Top Providers).


* `GET /api/telemetry/reports`: تقارير زمنية مفصلة:
* يومي (`period=daily`): استهلاك كل يوم خلال آخر 30 يوماً.
* أسبوعي (`period=weekly`): استهلاك كل أسبوع خلال آخر 12 أسبوعاً.
* شهري (`period=monthly`): استهلاك كل شهر خلال العام.


* `GET /api/telemetry/chats/{chat_id}`: تقرير استهلاك خاص بجلسة معينة حتى بعد أرشفتها.
* `POST /api/telemetry/recalculate-costs`: إعادة احتساب التكاليف التاريخية بعد تعديل قائمة الأسعار.





#### المرحلة P1-03E: واجهة المستخدم وعرض التحليلات (Frontend Telemetry UI)

* **الهدف:** تمكين المستخدم من متابعة استهلاكه لحظياً وإدارة التكاليف من لوحة التحكم.
* **التنفيذ:**
1. **داخل واجهة الشات (`Chat View`):**
* إضافة شارة التوكين (`Token Usage Badge`) أسفل كل رسالة تعرض: `Tokens: 450 (Prompt: 320, Output: 130) | Cost: $0.0012 | Time: 1.2s`.
* في شريط رأس الشات (`Chat Header`): عرض عداد إجمالي التوكين التراكمي للجلسة الحالية وتكلفتها التقديرية.


2. **صفحة مخصصة للتحليلات (`Settings -> Usage & Cost Analytics`):**
* بطاقات ملخص (Total Tokens, Estimated Cost, Input Tokens, Output Tokens).
* مخططات بيانية زمنية تفاعلية (Daily / Weekly / Monthly Usage Charts).
* جدول توزيع الاستهلاك حسب النماذج والمزودين.
* جدول إدارة أسعار النماذج (`Model Pricing Editor`) يسمح بإدخال وتعديل الأسعار وحفظها فورياً.





---

### 4. مصفوفة الملفات المتأثرة والجديدة لمرحلة P1-03

| الملف | الحالة | الدور المعماري |
| --- | --- | --- |
| `backend/peldrun/llm/client.py` | تعديل | إضافة كائن `TokenUsage` داخل `LLMResponse`. |
| `backend/peldrun/llm/providers/openai_compat.py` | تعديل | استخلاص بيانات الـ `usage` من ردود الـ API والـ Streaming. |
| `backend/peldrun/runtime/store.py` | تعديل | إنشاء جدول `token_ledger` وجدول `model_pricing` ودوال التسجيل الذرية. |
| `backend/omweb/services/telemetry_service.py` | **جديد** | محرك الاستعلامات، التقارير الزمنية، وحساب التكاليف. |
| `backend/omweb/routers/telemetry.py` | **جديد** | مسارات الـ API للتقارير، الاستهلاك، وإعدادات الأسعار. |
| `backend/omweb/project_manager.py` | تعديل | حفظ ملخص التوكين في `session.json` وتفاصيل كل `turn`. |
| `backend/omweb/routers/run.py` | تعديل | التقاط استهلاك الـ direct chat وتمريره للتخزين المزدوج. |
| `backend/omweb/engines/peldrun_engine.py` | تعديل | تسجيل استهلاك مهام الـ Agent في دفتر الأستاذ العام عند اكتمال المهمة. |
| `frontend/src/lib/types.ts` | تعديل | إضافة واجهات `TokenUsage`, `TelemetrySummary`, `ModelPricing`. |
| `frontend/src/components/chat/chat-sub-header.tsx` | تعديل | عرض إجمالي توكين الشات وتكلفتها التقديرية. |
| `frontend/src/components/chat/thought-card.tsx` | تعديل | عرض تفاصيل التوكين المستهلك للرسالة الواحدة. |
| `frontend/src/app/settings/setup/tabs/UsageTab.tsx` | **جديد** | تبويب لوحة تحكم التوكين وإدارة الأسعار والتقارير. |

---

### 5. قائمة المهام التفصيلية للتنفيذ (Tasks Breakdown)

```
[ ] TASK P1-01: Engine Resolver Decoupling
    - Remove filesystem heuristic scanning in engine_resolver.py.
    - Enforce explicit engine selection ('peldrun' vs 'openmanus').

[ ] TASK P1-02: Web-Core Architectural Isolation
    - Refactor peldrun_engine.py to interact with peldrun-core strictly via RunRequest and Public Adapters.
    - Encapsulate internal engine state within Core boundaries.

[ ] TASK P1-03: Comprehensive Token Accounting & Cost Metering Subsystem
    [ ] Task P1-03A: Core LLM Usage Extraction & Tokenizer Fallback
        - Define TokenUsage model in peldrun/llm/client.py.
        - Extract API usage from provider responses in openai_compat.py.
        - Implement Tokenizer fallback estimation when provider returns empty usage.
    [ ] Task P1-03B: Dual-Layer Persistence (Chat session.json + Global SQLite Ledger)
        - Implement 'token_ledger' table and indexes in peldrun/runtime/store.py.
        - Persist atomic usage entries per turn into SQLite.
        - Update project_manager.py to save turn-level and session-level tokens in session.json.
    [ ] Task P1-03C: Dynamic Pricing & Cost Calculation Engine
        - Implement 'model_pricing' table with default pricing tiers.
        - Build cost calculation logic (Input/Output price per 1M tokens).
        - Add pricing management APIs (GET, POST, DELETE /api/telemetry/pricing).
    [ ] Task P1-03D: Time-Series Analytics & Aggregated Reporting APIs
        - Create omweb/services/telemetry_service.py with SQL aggregations.
        - Create omweb/routers/telemetry.py with endpoints for summary, daily, weekly, monthly reports.
        - Add chat-specific telemetry query endpoint.
    [ ] Task P1-03E: Frontend Token Telemetry & Analytics Dashboard UI
        - Update types.ts with telemetry data transfer objects.
        - Add token and cost pills to Chat Header and individual message turns.
        - Create Analytics & Usage dashboard tab with charts and pricing configuration.

[ ] TASK P1-04: Persistent Artifact Metadata & Checksum Tracking
    - Save artifact manifests and history in SQLite database to persist across restarts.

[ ] TASK P1-05: Observability, Logging & Latency Tracing
    - Track tool execution durations, LLM latency, and retry metrics in telemetry records.

[ ] TASK P1-06: Verification & End-to-End Regression Testing
    - Write unit and integration tests verifying token calculations, SQLite ledger durability, and reporting APIs.

```

---


---

# 1. الحكم الهندسي النهائي

الخطة الحالية **صحيحة من حيث الاتجاه**، ولكن النسخة الأولى منها تخلط بين أربعة مفاهيم مختلفة:

```text
Token Counting
Usage Accounting
Cost Calculation
Observability
```

الأفضل فصلها منطقيًا:

```text
                    LLM Invocation
                          │
                          ▼
                Usage Extraction Layer
                          │
              ┌───────────┴───────────┐
              ▼                       ▼
       Exact Provider Usage      Estimation
              │                       │
              └───────────┬───────────┘
                          ▼
                 Canonical Usage Fact
                          │
              ┌───────────┴───────────┐
              ▼                       ▼
        Immutable Ledger       Runtime Telemetry
              │                       │
              ▼                       ▼
        Pricing Engine          Latency/Tracing
              │
              ▼
         Cost Snapshot
              │
              ▼
       Reporting / Analytics
              │
       ┌──────┼─────────┐
       ▼      ▼         ▼
     Chat   Dashboard  Export
```

هذا التصميم يعطي PELDRUN:

* دقة أعلى
* عدم ازدواجية الحساب
* durability بعد restart
* إمكانية حذف chat دون فقد سجل الاستخدام
* إعادة بناء التقارير
* إعادة تسعير التقارير دون تغيير التاريخ
* دعم local models
* دعم cloud providers
* دعم OpenManus مستقبلًا
* إمكانية إضافة OpenTelemetry لاحقًا
* قابلية التوسع دون إعادة تصميم Core

---

# 2. ما الذي تغير في التقييم بعد مراجعة الكود الحالي؟

الحالة الحالية أفضل كثيرًا من التصميم القديم.

حاليًا:

```text
PELDRUN Core
    ├── RunRequest
    ├── AgentRunner
    ├── ExecutionState
    ├── EventStore
    ├── SQLite RunStore
    └── LLM Client

PELDRUN Web
    ├── Engine Registry
    ├── PeldrunEngine
    ├── project_manager
    ├── job_manager
    └── SSE
```

وهذا يسمح ببناء نظام telemetry فوق lifecycle موجود بالفعل بدل إضافة execution loop جديد.

لكن توجد أربع نقاط يجب أخذها في الاعتبار:

### 2.1 `LLMResponse.usage` حاليًا غير typed

في `backend/peldrun/llm/client.py` الاستخدام الحالي هو تقريبًا:

```python
usage: Optional[Dict[str, int]]
```

وهذا يجب تغييره إلى domain model واضح.

### 2.2 `OpenAICompatProvider` حاليًا thin wrapper

الاستخلاص الفعلي للـ response usage موجود عمليًا داخل `AsyncLLMClient.chat_completion()`.

لذلك لا ينبغي نقل المنطق إلى `openai_compat.py` بصورة مصطنعة.

الأفضل:

```text
Provider
    ↓
AsyncLLMClient
    ↓
Usage Normalizer
    ↓
TokenUsage
```

ويبقى provider مسؤولًا عن provider-specific normalization عندما يحتاج ذلك.

### 2.3 الـ streaming الحالي ليس streaming حقيقيًا

الكود الحالي:

```text
stream()
  ↓
chat_completion()
  ↓
stream=False
  ↓
single response
  ↓
StreamChunk
```

وهذا يجب تصحيحه.

النظام المستقبلي:

```text
stream=True
     ↓
chunk 1
chunk 2
chunk 3
...
final chunk / completion metadata
     ↓
exact usage if available
     ↓
fallback estimation otherwise
```

### 2.4 RunStore الحالي مناسب كبنية أساسية

الـ `SqliteRunStore` يستخدم:

```text
SQLite
WAL
thread-local connections
foreign_keys
asyncio.to_thread
```

وهذا جيد جدًا لتطبيق Local-First على جهاز واحد. WAL يسمح للقراءة والكتابة بالتزامن بدرجة أفضل، مع بقاء SQLite ذات writer واحد في اللحظة نفسها. توثيق SQLite يوصي بـ WAL في كثير من هذه الحالات، مع ملاحظة أن `synchronous=NORMAL` يعطي أداءً جيدًا لكنه لا يقدم نفس durability عند فقدان الطاقة مثل `FULL`.

لذلك لن نعيد بناء SQLite.

---

# 3. القرار المعماري الرئيسي

## لا نسجل usage من Web وCore وRouter وAgent في الوقت نفسه

هذا خطأ يجب منعه من البداية.

لا نريد:

```text
ToolCallAgent
       ↓
record_usage()

runner
       ↓
record_usage()

peldrun_engine
       ↓
record_usage()

run router
       ↓
record_usage()
```

لأن هذا سيؤدي إلى:

```text
double counting
triple counting
retry ambiguity
```

المكان canonical هو **LLM invocation boundary**.

أي:

```text
LLM request starts
       ↓
LLM response completes
       ↓
ONE UsageRecord
```

ثم باقي النظام يستهلك هذا السجل.

---

# 4. النموذج المستقبلي

## 4.1 LLM Invocation هو الوحدة المحاسبية الأساسية

لا نعتبر "message" هي الوحدة الأساسية.

لأن message واحدة قد تسبب:

```text
Turn 1
 ├── LLM call
 ├── tool call
 ├── LLM call
 ├── retry
 └── LLM call
```

إذن:

```text
User Turn
    └── multiple LLM Invocations
```

لذلك:

```text
Invocation
    ↓
Turn aggregation
    ↓
Chat aggregation
    ↓
Project aggregation
    ↓
Global aggregation
```

---

# 5. نموذج TokenUsage الصحيح

النموذج المقترح لا يجب أن يكون:

```python
prompt_tokens = 0
completion_tokens = 0
total_tokens = 0
```

لأن:

```text
0
```

لا يساوي:

```text
unknown
```

وهذه نقطة محاسبية مهمة.

النموذج:

```python
@dataclass(frozen=True)
class TokenUsage:
    input_tokens: Optional[int]
    output_tokens: Optional[int]
    total_tokens: Optional[int]

    cached_input_tokens: Optional[int] = None
    reasoning_output_tokens: Optional[int] = None

    source: str = "provider"
    estimated: bool = False

    tokenizer_id: Optional[str] = None
    tokenizer_version: Optional[str] = None
    estimation_method: Optional[str] = None
```

ويفضل الاحتفاظ مستقبلًا بتفاصيل إضافية:

```text
input_audio_tokens
input_image_tokens
output_audio_tokens
```

لأن نماذج GenAI الحديثة لم تعد Text-only.

OpenTelemetry الحالية تستخدم مفهوم input/output token usage، وتضم أيضًا model/provider/correlation concepts وTTFT وlatency، وأصبحت GenAI conventions معيارًا مفيدًا لتسمية هذه البيانات.

---

# 6. Exact vs Estimated Usage

نحتاج ثلاث حالات وليس حالتين فقط:

```text
EXACT
ESTIMATED
UNKNOWN
```

### EXACT

جاءت الأرقام من provider.

### ESTIMATED

تم حسابها باستخدام tokenizer متوافق.

### UNKNOWN

لا توجد usage ولا tokenizer صالح.

ولا يجب تحويل UNKNOWN إلى:

```text
0 tokens
```

لأن ذلك سيجعل التقارير تبدو دقيقة وهي ليست كذلك.

---

# 7. Tokenizer Strategy

هذه من أهم التصحيحات على الخطة الحالية.

لا يمكن اعتبار:

```text
tiktoken cl100k_base
```

حسابًا دقيقًا لكل النماذج.

`tiktoken` هو tokenizer سريع ومناسب لنماذج/encodings التي يدعمها، ويوفر `encoding_for_model()` عندما يكون model mapping معروفًا.

لكن النموذج المحلي قد يستخدم:

```text
SentencePiece
BPE
TikToken-compatible
custom tokenizer
```

لذلك ترتيب fallback الصحيح:

```text
1. Provider Usage
       ↓
2. Model-native local tokenizer
       ↓
3. Known tokenizer mapping
       ↓
4. tiktoken compatible encoding
       ↓
5. heuristic estimation
       ↓
6. UNKNOWN
```

والأفضل للنماذج المحلية:

```python
AutoTokenizer.from_pretrained(local_model_path)
```

عندما تتوفر ملفات tokenizer محليًا.

Hugging Face توضح أن `AutoTokenizer` يختار tokenizer المناسب من model configuration، وأن fast tokenizers مبنية على Rust وتناسب الاستخدام السريع.

### Local-First rule

لا يجب أن يتطلب حساب التوكين:

```text
internet connection
```

إذا كان النموذج محليًا.

إذن:

```text
local model
   ↓
local tokenizer path/cache
   ↓
exact estimation
```

بدون HTTP خارجي.

---

# 8. Tokenizer Registry

بدل وضع منطق tokenizer داخل `tokenizer.py` فقط، الأفضل:

```text
peldrun/llm/tokenization/
    __init__.py
    registry.py
    tiktoken_adapter.py
    huggingface_adapter.py
    heuristic.py
```

ويكون لدينا:

```python
TokenizerRegistry
```

يقرر:

```text
model
provider
model_path
tokenizer_id
```

ثم:

```text
TokenizerAdapter
```

ينفذ:

```python
count_messages()
count_text()
count_output()
```

وبالتالي يصبح النظام قابلًا لإضافة:

```text
Ollama tokenizer
LM Studio tokenizer
GGUF metadata tokenizer
SentencePiece
Tiktoken
HF
```

دون إعادة تصميم Core.

---

# 9. Usage Extraction

## المصدر الأول

```text
provider response usage
```

لا تعتمد على حساب النص إذا أعطاك provider usage حقيقي.

OpenAI APIs الحديثة تعرض input/output/total usage مع تفاصيل مثل cached input وreasoning tokens في بعض response types، لذلك يجب أن يكون النموذج الداخلي أوسع من `prompt_tokens/completion_tokens`.

---

# 10. Streaming Usage

يجب تصحيح المسار الحالي:

```text
AsyncLLMClient.stream()
```

ليكون:

```text
request(stream=True)
      ↓
consume chunks
      ↓
accumulate content
      ↓
capture final usage
      ↓
emit final LLMResponse / UsageRecord
```

ويجب أن يكون هناك:

```text
first_chunk_at
last_chunk_at
```

حتى نحسب:

```text
TTFT
Total latency
Generation duration
Tokens/sec
```

OpenTelemetry GenAI conventions الحالية تعرّف زمن الاستجابة الكلي وكذلك Time To First Token/Chunk كبيانات مفيدة في observability.

---

# 11. Invocation Record

هذا أهم كيان في النظام.

الاقتراح:

```text
llm_invocations
-----------------------------
invocation_id
run_id
job_id
chat_id
project_id
turn_id
step_id

mode
provider
model_requested
model_returned

request_id
response_id

attempt
status
finish_reason
failure_reason

input_tokens
output_tokens
total_tokens

cached_input_tokens
reasoning_output_tokens

usage_source
estimated
tokenizer_id
estimation_method

started_at
first_token_at
completed_at

latency_ms
ttft_ms

created_at
```

---

# 12. لماذا `attempt` مهم؟

لنفترض:

```text
LLM request attempt 1
   ↓
network failure

retry

LLM request attempt 2
   ↓
success
```

يجب ألا تظهر في التقرير كأنها request واحدة.

بل:

```text
Invocation A
attempt=1
status=failed

Invocation B
attempt=2
status=completed
```

أما turn aggregation فيعرض:

```text
LLM calls: 2
Successful calls: 1
Retries: 1
```

وإذا provider احتسب tokens للمحاولة الأولى، يتم تسجيلها أيضًا.

---

# 13. Ledger لا يجب أن يكون مجرد `token_ledger` بسيط

الاسم يمكن أن يبقى:

```text
token_ledger
```

لكن من الأفضل أن يكون fact table كاملًا:

```text
token_ledger
```

وكل صف = LLM invocation accounting fact.

المفتاح الأهم:

```text
invocation_id UNIQUE
```

وهذا يعطي idempotency.

إذا وقع retry في Web أو حدث reconnect:

```text
same invocation_id
      ↓
INSERT OR IGNORE
```

لا يوجد double accounting.

---

# 14. Schema المقترح

```sql
CREATE TABLE IF NOT EXISTS token_ledger (
    invocation_id TEXT PRIMARY KEY,

    run_id TEXT,
    job_id TEXT,
    project_id TEXT,
    chat_id TEXT,
    turn_id TEXT,
    step_id TEXT,

    mode TEXT NOT NULL,

    provider TEXT NOT NULL,
    model_requested TEXT NOT NULL,
    model_returned TEXT,

    request_id TEXT,
    response_id TEXT,

    attempt INTEGER NOT NULL DEFAULT 1,

    status TEXT NOT NULL,

    input_tokens INTEGER,
    output_tokens INTEGER,
    total_tokens INTEGER,

    cached_input_tokens INTEGER,
    reasoning_output_tokens INTEGER,

    usage_source TEXT NOT NULL,
    estimated INTEGER NOT NULL DEFAULT 0,

    tokenizer_id TEXT,
    tokenizer_version TEXT,
    estimation_method TEXT,

    started_at REAL NOT NULL,
    first_token_at REAL,
    completed_at REAL,

    latency_ms REAL,
    ttft_ms REAL,

    finish_reason TEXT,
    failure_reason TEXT,

    pricing_version_id TEXT,

    cost_nano_usd INTEGER,

    created_at REAL NOT NULL
);
```

---

# 15. لماذا `INTEGER cost_nano_usd` وليس `REAL cost_usd`؟

لا أنصح:

```sql
cost_usd REAL
```

لأن هذا يعرض الحسابات المالية لمشاكل floating-point.

الأفضل:

```text
nano USD
```

أو:

```text
micro USD
```

وتخزن:

```text
$0.0003075
```

كرقم صحيح بوحدة دقيقة جدًا.

مثلاً:

```text
307500 nano USD
```

ثم يعاد عرضه:

```text
$0.0003075
```

هذا يجعل:

```text
SUM()
```

آمنًا حسابيًا.

---

# 16. Pricing Architecture

الخطة الحالية تحتاج تصحيحًا جوهريًا.

لا يجب أن يكون:

```text
DELETE pricing
```

حذفًا حقيقيًا.

لأن التاريخ يحتاج إلى معرفة السعر الذي استُخدم وقتها.

الأفضل:

```text
pricing versioning
```

مثلاً:

```sql
model_pricing (
    pricing_id
    provider
    model_pattern

    input_price_nano_usd_per_million
    output_price_nano_usd_per_million

    cached_input_price_nano_usd_per_million

    effective_from
    effective_to

    priority

    active
    created_at
)
```

---

# 17. Pricing Resolution

يجب أن يكون deterministic.

الترتيب:

```text
provider + exact model
        ↓
provider + model pattern
        ↓
exact model
        ↓
global pattern
        ↓
local model default
        ↓
unknown price
```

مثلاً:

```text
openai + gpt-5.x
```

يتغلب على:

```text
gpt-*
```

وهكذا.

يجب أن يكون هناك:

```text
priority
```

لتجنب أي ambiguity.

---

# 18. Local Models

لا نقول:

```text
local model = actually free
```

بل:

```text
billing_cost = 0
cost_basis = local_zero
```

لأن:

```text
electricity
GPU time
hardware depreciation
```

ليست صفرًا فعليًا.

نظام P1 يحسب:

```text
API monetary cost
```

لكن يمكن لاحقًا إضافة:

```text
estimated_inference_cost
```

في P2.

---

# 19. أهم تعديل: لا نعيد كتابة الـ Ledger عند تغيير السعر

الخطة الحالية تقترح:

```text
POST /recalculate-costs
```

إذا كانت تعني:

```text
UPDATE historical ledger rows
```

فهذا **لا أنصح به**.

Ledger يجب أن يبقى immutable.

بدل ذلك:

```text
Original ledger
    ↓
pricing version used at time
    ↓
original cost snapshot
```

ثم عند طلب إعادة الحساب:

```text
historical usage
       +
new pricing rules
       ↓
recalculated report
```

بدون تغيير الحقيقة التاريخية.

وبذلك يستطيع المستخدم مقارنة:

```text
Original estimated cost
vs
Current pricing cost
```

وهذا أقوى بكثير.

---

# 20. Pricing Cost Formula

الأساس:

```text
input_cost =
input_tokens × input_price / 1,000,000

output_cost =
output_tokens × output_price / 1,000,000
```

لكن يجب توسيعها عند وجود cache pricing:

```text
non_cached_input
    × input_price

cached_input
    × cached_price

output
    × output_price
```

ولا يجوز خصم cached tokens من input tokens مرتين.

كذلك reasoning tokens يجب ألا يضاف تلقائيًا إلى output cost إذا كانت بالفعل جزءًا من output billing؛ هذه القاعدة يجب أن تكون provider/pricing-specific.

---

# 21. Dual-Layer Persistence

الفكرة ممتازة ولكن يجب تحديد مصدر الحقيقة:

```text
SQLite token ledger
        ↓
CANONICAL ACCOUNTING SOURCE
```

بينما:

```text
session.json
        ↓
CHAT PROJECTION / CACHE
```

أي:

```text
SQLite
   = truth

session.json
   = fast contextual projection
```

---

# 22. session.json

نضيف:

```json
{
  "version": "3.0.0",

  "usage_summary": {
    "input_tokens": 1200,
    "output_tokens": 800,
    "total_tokens": 2000,
    "estimated_tokens": 0,
    "total_cost_usd": 0.0124,
    "llm_call_count": 4
  },

  "turns": [
    {
      "turn_id": "turn_001",

      "usage": {
        "input_tokens": 500,
        "output_tokens": 300,
        "total_tokens": 800,
        "cost_usd": 0.0042,
        "estimated": false
      }
    }
  ]
}
```

لكن هذه المعلومات لا تُحسب من Web ad-hoc.

يفضل أن تأتي من:

```text
TelemetryService
```

أو من completed usage result returned by Core.

---

# 23. Atomic session persistence

لا نكتب:

```python
session_file.write_text(...)
```

مباشرة كل مرة.

بل:

```text
write temporary file
        ↓
flush
        ↓
replace
```

أي:

```text
session.json.tmp
      ↓
os.replace()
      ↓
session.json
```

حتى لا يظل لدينا ملف JSON نصف مكتوب بعد crash.

---

# 24. SQLite Migration Architecture

الحالي يستخدم:

```text
CREATE TABLE IF NOT EXISTS
```

وهذا جيد للـ initial schema لكنه غير كافٍ كنظام migrations طويل المدى.

نحتاج:

```text
schema version
```

مثلاً:

```sql
PRAGMA user_version;
```

ثم:

```text
Migration 1
Migration 2
Migration 3
...
```

مثلاً:

```text
P1.03.001
P1.03.002
```

وهذا سيكون ضروريًا جدًا لاحقًا عندما تتغير:

```text
token_ledger
model_pricing
artifact_history
telemetry
```

---

# 25. SQLite durability strategy

الـ Runtime الحالي:

```text
WAL
synchronous=NORMAL
```

جيد جدًا للأداء العام.

لكن الـ immutable accounting ledger له متطلبات مختلفة.

أقترح:

```text
General runtime store
    WAL + NORMAL

Accounting ledger transaction
    WAL + FULL
```

إذا قرر PELDRUN أن accounting history يجب أن يتحمل حتى power-loss scenarios.

SQLite توضح أن WAL + NORMAL يعطي balance ممتازًا للأداء والسلامة، لكن NORMAL في WAL لا يضمن نفس durability عند فقدان الطاقة الذي يوفره FULL.

هذا القرار يجب أن يكون explicit، لا accidental.

---

# 26. Correlation Model

كل invocation يجب أن يعرف:

```text
project_id
chat_id
job_id
run_id
turn_id
step_id
invocation_id
request_id
```

الهدف:

```text
User message
      ↓
Turn
      ↓
Agent run
      ↓
Step
      ↓
LLM invocation
```

وهكذا يمكن الضغط على رسالة في الواجهة والانتقال إلى:

```text
turn
   ↓
all LLM calls
   ↓
tools
   ↓
latency
   ↓
tokens
```

---

# 27. علاقة OpenTelemetry

لا أرى ضرورة لإضافة Langfuse أو Helicone أو Portkey.

وأيضًا لا أرى ضرورة لإجبار PELDRUN على تشغيل OpenTelemetry Collector خارجي في P1.

لكن يجب أن نصمم schema الداخلي **متوافقًا دلاليًا مع OpenTelemetry GenAI conventions**.

مثل:

```text
gen_ai.provider.name
gen_ai.request.model
gen_ai.response.model
gen_ai.conversation.id
gen_ai.operation.name
gen_ai.response.finish_reasons
gen_ai.response.time_to_first_chunk
gen_ai.usage.input_tokens
gen_ai.usage.output_tokens
```

وهذا يجعل PELDRUN مستقلًا الآن، لكنه قابلًا لاحقًا لتصدير telemetry إلى OTLP أو أدوات خارجية بدون إعادة بناء النموذج الداخلي. OpenTelemetry نفسها تركز حاليًا على model/provider/token/latency correlation في GenAI observability.

---

# 28. P1-05 يجب دمجه مع هذا النموذج

لا نبني نظام logging ثانيًا.

لدينا:

```text
TelemetryRecord
```

ينتج من نفس execution lifecycle.

### Run metrics

```text
run_duration_ms
step_count
tool_count
llm_call_count
retry_count
terminal_status
failure_reason
```

### LLM metrics

```text
latency_ms
ttft_ms
input_tokens
output_tokens
total_tokens
tokens_per_second
attempt
finish_reason
failure_reason
```

### Tool metrics

```text
tool_duration_ms
attempt
retry_count
success
failure_reason
```

---

# 29. Token throughput

نضيف:

```text
output_tokens / generation_seconds
```

لعرض:

```text
18.3 tok/s
```

لكن لا نحسب:

```text
total_tokens / wall_clock_time
```

لأن هذا يخلط input processing مع generation.

---

# 30. Reporting Architecture

بدل endpoint واحد كبير:

```text
/api/telemetry/dashboard
```

نقسم المسؤوليات منطقيًا.

## Summary

```http
GET /api/telemetry/summary
```

يعيد:

```text
total input
total output
total tokens
total cost
estimated %
llm calls
avg latency
avg ttft
retry count
```

## Time series

```http
GET /api/telemetry/timeseries
```

مع:

```text
granularity=day
granularity=week
granularity=month
```

## Breakdown

```http
GET /api/telemetry/breakdown
```

مثلاً:

```text
group_by=model
group_by=provider
group_by=mode
group_by=project
```

## Chat

```http
GET /api/telemetry/chats/{chat_id}
```

## Detailed invocations

```http
GET /api/telemetry/invocations
```

مع pagination.

---

# 31. Pagination

لا نعيد:

```text
100000 invocation rows
```

إلى React.

نستخدم:

```text
cursor pagination
```

أو على الأقل:

```text
limit + offset
```

والأفضل cursor:

```text
created_at
invocation_id
```

خصوصًا إذا أصبح ledger كبيرًا.

---

# 32. Indexing strategy

الحد الأدنى:

```sql
CREATE INDEX idx_token_ledger_created
ON token_ledger(created_at);

CREATE INDEX idx_token_ledger_chat_created
ON token_ledger(chat_id, created_at);

CREATE INDEX idx_token_ledger_project_created
ON token_ledger(project_id, created_at);

CREATE INDEX idx_token_ledger_provider_model_created
ON token_ledger(provider, model_returned, created_at);

CREATE INDEX idx_token_ledger_mode_created
ON token_ledger(mode, created_at);

CREATE INDEX idx_token_ledger_run
ON token_ledger(run_id);

CREATE INDEX idx_token_ledger_turn
ON token_ledger(turn_id);
```

لا نضع عشرات indexes بلا داعٍ.

كل index يعني write overhead.

---

# 33. Dashboard Query Strategy

لا نطلب:

```text
15 API calls
```

عند فتح Usage Dashboard.

يفضل:

```text
GET /api/telemetry/summary
```

يعيد:

```text
KPI
+
time series
+
top models
+
top providers
+
mode breakdown
```

في payload واحد.

أما التفاصيل فتُحمّل lazy.

---

# 34. Query caching

في dashboard:

```text
cache = 5–30 sec
```

يكفي غالبًا.

ليس من المنطقي تنفيذ عشرات SQL aggregation queries في كل render.

---

# 35. Pre-Aggregation

لا أنصح في P1 ببناء:

```text
daily_usage
weekly_usage
monthly_usage
```

كجداول materialized منفصلة.

SQLite يستطيع التعامل مع حجم usage محلي معقول جدًا عبر indexes.

نضيف pre-aggregation فقط عندما تثبت القياسات أن dataset أصبح كبيرًا.

أي:

```text
P1
  ↓
indexed live queries

P2
  ↓
optional rollups
```

---

# 36. Filters

النظام المطلوب يجب أن يدعم:

```text
Date range
Provider
Model
Project
Chat
Mode
Exact / Estimated
Status
Pricing basis
```

ويمكن مستقبلًا:

```text
Min cost
Max cost
Latency range
Retry only
Failed only
```

---

# 37. Reports

أقترح إضافة:

## P1-03F — Reporting, Export & Print

الصيغ:

```text
JSON
CSV
HTML Print Report
```

والـ PDF يمكن في البداية أن يأتي من:

```text
Print → Save as PDF
```

بدون إضافة dependency كبيرة للسيرفر.

لاحقًا:

```text
server-side PDF
```

يمكن إضافته إذا أصبح مطلوبًا.

---

# 38. Report model

الـ report ليس HTML فقط.

نبني:

```python
TelemetryReport
```

مثلاً:

```text
report_id
generated_at

from
to

filters

summary
time_series
breakdowns
top_models
top_providers
chat_rows
```

ثم:

```text
JSON Renderer
CSV Renderer
HTML Renderer
PDF Renderer
```

نفس data model.

وهذا يمنع اختلاف الأرقام بين:

```text
Dashboard
CSV
Print
PDF
```

---

# 39. Report print experience

واجهة الطباعة:

```text
PELDRUN Usage Report

Period:
01 Sep 2026 → 30 Sep 2026

Total Tokens
1,284,320

Input
892,110

Output
392,210

Estimated Cost
$4.821

LLM Calls
482

Average Latency
2.41s

Top Model
...

Top Provider
...

Daily Usage
[table]

Model Breakdown
[table]
```

مع:

```css
@media print
```

بحيث تختفي:

```text
sidebar
buttons
navigation
```

ويظهر:

```text
professional report
```

---

# 40. Frontend architecture

لا نضع الحساب المالي داخل React.

خطأ:

```ts
tokens * price / 1_000_000
```

داخل components.

الصحيح:

```text
Backend
   ↓
canonical cost
   ↓
Frontend display
```

يمكن للواجهة فقط format:

```text
$0.0012
```

---

# 41. Chat UI

كل turn:

```text
Tokens: 450
Input: 320
Output: 130
Cost: $0.0012
Time: 1.2s
```

لكن في Agent turn قد يكون:

```text
3 LLM invocations
2 tool calls
1 retry
```

إذن التفاصيل الأفضل:

```text
Turn summary
   1,480 tokens
   4 LLM calls
   2 tools
   $0.0081
   9.2s
```

ثم expandable details:

```text
LLM #1
LLM #2
Tool #1
LLM #3
Retry
LLM #4
```

---

# 42. Chat Header

نعرض:

```text
Tokens      Cost       Time
2.4K        $0.018     8.7s
```

وعند الضغط:

```text
Usage details
```

بدل ازدحام header.

---

# 43. Usage Dashboard

التبويب:

```text
Settings
   └── Usage & Cost
```

الأقسام:

```text
Overview
Timeline
Models
Providers
Chats
Pricing
Reports
```

---

# 44. Model Pricing Editor

الجدول:

```text
Provider
Model
Input / 1M
Output / 1M
Cached Input / 1M
Currency
Effective From
Status
```

الأفضل:

```text
Create
Edit
Retire
Clone
```

وليس:

```text
Delete
```

---

# 45. Historical pricing

مثلاً:

```text
GPT-X
v1
$2 / $8
valid until Sep 30

v2
$1.5 / $6
valid from Oct 1
```

السجلات القديمة تظل مرتبطة بـ:

```text
pricing_version=v1
```

ولا تتغير.

---

# 46. Repricing

بدل:

```text
POST /recalculate-costs
```

بصيغة update destructive.

الأفضل:

```http
POST /api/telemetry/reprice
```

مع:

```text
from
to
filters
pricing_version
```

والنتيجة:

```text
original_cost
recalculated_cost
difference
```

بدون تعديل الـ immutable ledger.

---

# 47. Privacy model

Ledger لا يجب أن يحتوي على:

```text
prompt
completion
tool result
API key
```

إلا إذا فعل المستخدم telemetry content logging صراحة في future.

الـ P1 ledger يحتاج:

```text
metadata
usage
timing
correlation
cost
```

فقط.

وهذا يحافظ على Local-First privacy.

---

# 48. Chat deletion semantics

المطلوب:

```text
Delete Chat
   ↓
remove session.json
remove events.json
remove workspace metadata
```

لكن:

```text
token_ledger
     ↓
UNCHANGED
```

بشرط وجود خيار واضح مستقبلاً:

```text
Delete all usage history
```

لأن immutable accounting لا ينبغي أن يعني "لا يمكن للمستخدم حذف بياناته أبدًا".

إذًا السياسة:

```text
Chat delete
    ≠
Ledger delete
```

لكن النظام يحتفظ بإمكانية:

```text
explicit global data purge
```

لاحقًا.

---

# 49. P1-02 Web/Core Boundary

هذا يجب أن يسبق telemetry implementation.

الهدف:

```text
WEB
 ↓
ExecutionEngine public interface
 ↓
Peldrun Adapter
 ↓
RunRequest
 ↓
CORE
```

ولا:

```text
WEB
 ↓
AgentRunner
ExecutionState
EventEmitter
ToolRegistry
```

مباشرة.

---

# 50. Telemetry داخل هذا boundary

Core يوفر public concepts:

```text
TokenUsage
LLMInvocationRecord
TelemetrySink
RunTelemetry
```

ولا يعرف:

```text
FastAPI
React
SSE
project_manager
```

أما Web فيأخذ:

```text
public telemetry data
```

ويعرضه.

---

# 51. Public Core API المقترح

```python
class TelemetrySink(Protocol):

    async def record_llm_invocation(
        self,
        record: LLMInvocationRecord,
    ) -> None:
        ...
```

ثم:

```python
class RunStore(TelemetrySink, Protocol):
    ...
```

أو الأفضل فصل protocols:

```text
RunStore
UsageStore
ArtifactStore
```

حتى لا يصبح `RunStore` God Object.

---

# 52. هذا مهم جدًا للمستقبل

حاليًا `RunStore` يحتوي على:

```text
runs
events
checkpoints
human_requests
```

ثم سنضيف:

```text
token_ledger
pricing
artifact_history
telemetry
```

إذا وضعنا كل شيء داخل `SqliteRunStore` بلا حدود، سيصبح:

```text
God Storage Object
```

لذلك:

```text
RunStore
UsageStore
PricingStore
ArtifactStore
```

لكنها يمكن أن تستخدم:

```text
same SQLite file
```

وهكذا:

```text
one database
multiple repositories
```

وهو أفضل معماريًا.

---

# 53. المقترح

```text
backend/peldrun/runtime/
    store.py
    usage_store.py
    pricing_store.py
    artifact_store.py
    migrations.py
```

وليس:

```text
store.py = 5000 lines
```

---

# 54. Telemetry service في Web

```text
backend/omweb/services/
    telemetry_service.py
```

لكن:

```text
TelemetryService
```

لا ينفذ SQL مباشرة.

يستخدم:

```text
UsageStore
PricingStore
```

public interfaces.

هذا يحافظ على P1-02.

---

# 55. API Layer

```text
backend/omweb/routers/telemetry.py
```

مسؤوليته:

```text
HTTP
validation
DTO
authentication/authorization if later
```

وليس:

```text
SQL
pricing logic
token calculation
```

---

# 56. Task Breakdown النهائي

## P1-01 — Engine Resolver Decoupling

```text
[ ] Remove runtime filesystem probing
[ ] Require explicit engine_id
[ ] Validate engine_id against EngineRegistry
[ ] Separate optional EngineDiscovery from runtime EngineSelection
[ ] Add invalid-engine tests
[ ] Add startup diagnostics
```

### Acceptance

```text
engine_id=peldrun
    → Peldrun

engine_id=openmanus
    → OpenManus

unknown
    → fail-fast

missing
    → explicit configuration error
```

ولا يوجد:

```text
search parent directory
search ~/.peldrun
search OpenManus directory
```

داخل runtime.

---

# P1-02 — Web/Core Isolation

```text
[ ] Define public Core execution adapter
[ ] Make RunRequest canonical execution request
[ ] Move AgentRunner ownership fully to Core
[ ] Hide ExecutionState from Web
[ ] Hide EventEmitter internals from Web
[ ] Restrict direct Core imports
[ ] Convert PeldrunEngine into thin adapter
[ ] Add architecture test preventing forbidden imports
```

### الهدف

```text
Web knows:
    RunRequest
    EngineResult
    PublicEvents
    PublicTelemetry

Web does NOT know:
    AgentRunner internals
    ExecutionState internals
    Core EventEmitter internals
```

---

# P1-03A — LLM Usage Extraction

```text
[ ] Add TokenUsage domain model
[ ] Add UsageSource enum
[ ] Add Exact / Estimated / Unknown semantics
[ ] Normalize provider usage
[ ] Normalize cached token details
[ ] Normalize reasoning token details
[ ] Add response_id/model correlation
[ ] Implement true streaming
[ ] Capture final stream usage
[ ] Capture first-token timestamp
[ ] Add tokenizer registry
[ ] Add local tokenizer support
[ ] Add tiktoken adapter
[ ] Add heuristic final fallback
[ ] Never treat unknown as zero
```

---

# P1-03B — Accounting & Persistence

```text
[ ] Define LLMInvocationRecord
[ ] Define token_ledger schema
[ ] Add invocation_id uniqueness
[ ] Add idempotent insert
[ ] Add indexes
[ ] Add schema migrations
[ ] Add UsageStore protocol
[ ] Add SQLite UsageStore
[ ] Add atomic ledger transaction
[ ] Add session.json usage projection
[ ] Add atomic session writer
[ ] Add turn aggregation
[ ] Add chat aggregation
```

---

# P1-03C — Pricing Engine

```text
[ ] Define PricingRule
[ ] Define PricingVersion
[ ] Add model_pricing table
[ ] Add exact model matching
[ ] Add provider+model matching
[ ] Add wildcard/pattern matching
[ ] Add priority
[ ] Add effective_from/effective_to
[ ] Add local-model zero-billing basis
[ ] Add integer currency representation
[ ] Add cached-input pricing
[ ] Add cost calculator
[ ] Add pricing resolution tests
[ ] Replace hard delete with retire/deactivate
```

---

# P1-03D — Reporting & Analytics

```text
[ ] Create TelemetryService
[ ] Create UsageStore queries
[ ] Create PricingStore queries
[ ] Summary endpoint
[ ] Time-series endpoint
[ ] Provider breakdown
[ ] Model breakdown
[ ] Mode breakdown
[ ] Project breakdown
[ ] Chat-specific endpoint
[ ] Invocation detail endpoint
[ ] Cursor pagination
[ ] Date filters
[ ] Provider/model filters
[ ] Exact/estimated filters
[ ] Failed/retry filters
```

---

# P1-03E — Frontend Usage UX

```text
[ ] Add TokenUsage DTOs
[ ] Add Cost DTOs
[ ] Add Latency DTOs
[ ] Add TurnUsage
[ ] Add ChatUsageSummary
[ ] Add Token badge
[ ] Add Cost badge
[ ] Add latency badge
[ ] Add expandable usage details
[ ] Add Chat Header summary
[ ] Add Usage dashboard
[ ] Add timeline charts
[ ] Add model breakdown
[ ] Add provider breakdown
[ ] Add pricing editor
[ ] Add exact/estimated indicators
```

---

# P1-03F — Reports, Export & Print

```text
[ ] Define TelemetryReport
[ ] Generate JSON report
[ ] Generate CSV report
[ ] Generate print HTML
[ ] Add print CSS
[ ] Add browser print action
[ ] Add date/filter preservation
[ ] Add report title and period
[ ] Add summary section
[ ] Add breakdown tables
[ ] Add generated timestamp
[ ] Add optional PDF later
```

---

# P1-04 — Persistent Artifact Metadata

```text
[ ] Make ArtifactManifest canonical
[ ] Define ArtifactStore
[ ] Add artifact_history
[ ] Persist SHA-256
[ ] Persist revisions
[ ] Persist size/mime
[ ] Persist mutation type
[ ] Persist timestamps
[ ] Persist run/chat/project correlation
[ ] Restore after restart
[ ] Use filesystem scan as reconciliation only
```

---

# P1-05 — Structured Diagnostics & Latency

```text
[ ] Add run duration
[ ] Add step duration
[ ] Add tool duration
[ ] Add LLM latency
[ ] Add TTFT
[ ] Add retry metrics
[ ] Add failure reason
[ ] Add finish reason
[ ] Add tokens/sec
[ ] Add terminal status
[ ] Align field names with OpenTelemetry GenAI conventions
[ ] Keep external exporters optional
```

---

# P1-06 — Verification & Regression

```text
[ ] Unit tests
[ ] SQLite persistence tests
[ ] Migration tests
[ ] Token extraction tests
[ ] Streaming usage tests
[ ] Tokenizer tests
[ ] Pricing tests
[ ] Cost precision tests
[ ] Idempotency tests
[ ] Retry accounting tests
[ ] Chat deletion tests
[ ] Restart recovery tests
[ ] Reporting tests
[ ] Filter tests
[ ] Export tests
[ ] Full Runtime V1 regression
[ ] Full frontend integration tests
```

---

# 57. Critical test matrix

## Provider usage

```text
Provider usage present
Provider usage missing
Provider usage partial
Provider usage malformed
```

## Tokenizer

```text
Known model tokenizer
Unknown model
Local tokenizer
tiktoken
heuristic
unavailable
```

## Streaming

```text
normal stream
empty stream
stream with usage
stream without usage
stream interrupted
stream retry
```

## Accounting

```text
one invocation
multiple invocations
retry
failed invocation
duplicate event
duplicate invocation_id
process restart
```

## Pricing

```text
exact model
wildcard model
provider-specific price
local zero price
expired price
future price
price change
retired rule
```

## Reporting

```text
day
week
month
empty date range
large date range
single chat
deleted chat
estimated-only
exact-only
model filter
provider filter
combined filters
```

---

# 58. أهم invariant في النظام

يجب أن نثبت هذه القاعدة:

```text
Every completed LLM invocation
must produce exactly one accounting identity.
```

أي:

```text
1 invocation
=
1 invocation_id
=
0 or 1 ledger row
```

ولا:

```text
1 invocation
=
multiple accounting rows
```

إلا إذا كانت هناك أسباب محاسبية واضحة مثل billing components منفصلة.

---

# 59. Invariant ثاني

```text
Ledger rows are append-only facts.
```

لا نسمح بـ:

```text
UPDATE historical usage
DELETE historical usage
```

في المسار الطبيعي.

---

# 60. Invariant ثالث

```text
session.json is reconstructible.
```

أي لو حذفنا:

```text
session.json
```

يمكن:

```text
SQLite ledger
    ↓
rebuild
    ↓
session usage summary
```

وهذه خاصية مهمة جدًا للاسترجاع.

---

# 61. Invariant رابع

```text
Dashboard numbers
CSV numbers
Print report numbers
Chat summary numbers
```

يجب أن تأتي من نفس aggregation service.

لا توجد:

```text
React calculation
Router calculation
SQL calculation
```

بأرقام مختلفة.

---

# 62. Invariant خامس

```text
Cost must be reproducible.
```

أي يجب أن يمكننا معرفة:

```text
usage
+
pricing version
+
pricing rules
=
original cost
```

---

# 63. Invariant سادس

```text
Unknown ≠ zero
Estimated ≠ exact
Local zero-billing ≠ zero resource cost
```

هذه ثلاث قواعد يجب أن تدخل الاختبارات.

---

# 64. API النهائي المقترح

```text
GET    /api/telemetry/summary

GET    /api/telemetry/timeseries

GET    /api/telemetry/breakdown

GET    /api/telemetry/invocations

GET    /api/telemetry/chats/{chat_id}

GET    /api/telemetry/pricing

POST   /api/telemetry/pricing

PUT    /api/telemetry/pricing/{id}

POST   /api/telemetry/pricing/{id}/retire

POST   /api/telemetry/reprice

GET    /api/telemetry/reports/export?format=json

GET    /api/telemetry/reports/export?format=csv

GET    /api/telemetry/reports/print
```

---

# 65. لا نحتاج endpoint منفصل لكل daily/weekly/monthly

بدل:

```text
/daily
/weekly
/monthly
```

أفضل:

```text
/timeseries?granularity=day
/timeseries?granularity=week
/timeseries?granularity=month
```

هذا يقلل API surface ويجعل المستقبل أبسط.

---

# 66. الأداء

الهدف:

```text
LLM call
   ↓
usage extraction
   ↓
ledger insert
```

يجب ألا يضيف latency ملحوظة إلى model generation.

لذلك:

```text
Do not:
calculate dashboard aggregates during invocation
calculate full chat totals during invocation
rewrite entire session history during invocation
```

بل:

```text
LLM completes
   ↓
small append transaction
   ↓
return
```

والتحليلات لاحقًا.

---

# 67. Async persistence

بسبب أن التطبيق async، أفضل flow:

```text
LLM response
      ↓
construct UsageRecord
      ↓
append ledger
      ↓
continue runtime
```

ويمكن مستقبلًا استخدام queue إذا أثبت القياس أن disk persistence أصبحت bottleneck.

لكن:

**لا نضيف queue معقدة في P1 من البداية.**

SQLite WAL مناسب لهذا الحجم من العمل المحلي، والكتابات الصغيرة جدًا مناسبة له.

---

# 68. لماذا لا نستخدم Langfuse / Helicone / Portkey؟

لأن متطلبات PELDRUN هنا مختلفة:

```text
Local-first
Privacy
Offline capability
No external dependency
No telemetry SaaS requirement
Full storage ownership
```

لكن تصميمنا يجب ألا يكون مغلقًا أمام:

```text
OTLP
Langfuse
OpenTelemetry Collector
custom exporter
```

في المستقبل.

أي:

```text
Internal canonical telemetry
            ↓
       optional exporters
```

وليس:

```text
PELDRUN
    ↓
Langfuse
```

---

# 69. أفضل البنية النهائية

```text
                         PELDRUN
                            │
                ┌───────────┴───────────┐
                │                       │
             WEB                     CORE
                │                       │
        ExecutionEngine            AgentRunner
                │                       │
                │                    LLM Client
                │                       │
                │                  Usage Extractor
                │                       │
                │                  Tokenizer Registry
                │                       │
                │                  Telemetry Record
                │                       │
                └───────────────┬───────┘
                                │
                         Public Telemetry
                                │
                    ┌───────────┴───────────┐
                    │                       │
               UsageStore              ArtifactStore
                    │
            ┌───────┴────────┐
            │                │
       token_ledger      model_pricing
            │
            ▼
       Cost Engine
            │
            ▼
      TelemetryService
            │
      ┌─────┼──────────┐
      ▼     ▼          ▼
   Chat   Dashboard   Reports
                      │
              ┌───────┼────────┐
              ▼       ▼        ▼
             JSON    CSV      HTML/PDF
```

---

# 70. خارطة P1 النهائية

```text
MILESTONE P1 — HARDENING & ACCOUNTING

├── P1-01 Engine Resolver Decoupling
│
├── P1-02 Web-Core Architectural Isolation
│
├── P1-03 Usage Accounting & Cost Metering
│   ├── P1-03A Usage Extraction
│   ├── P1-03B Token Ledger & Session Projection
│   ├── P1-03C Pricing & Cost Engine
│   ├── P1-03D Analytics API
│   ├── P1-03E Frontend Usage Dashboard
│   └── P1-03F Reports / Export / Print
│
├── P1-04 Artifact History & Persistence
│
├── P1-05 Structured Observability
│
├── P1-06 Verification & E2E Regression
│
└── P1-07 Operational Polish
    ├── frontend type hardening
    ├── empty/loading/error states
    ├── migration diagnostics
    └── storage health diagnostics
```

---

# 71. ترتيب التنفيذ الصحيح

لا أنصح:

```text
P1-01
P1-02
P1-03A
P1-03B
...
```

بشكل ميكانيكي فقط.

الترتيب الأفضل:

```text
PHASE 0
Architecture contracts
        ↓
P1-01
        ↓
P1-02
        ↓
P1-03A
        ↓
P1-03B
        ↓
P1-03C
        ↓
P1-05
        ↓
P1-03D
        ↓
P1-04
        ↓
P1-03E
        ↓
P1-03F
        ↓
P1-06
        ↓
P1-07
```

والسبب أن:

```text
Usage extraction
```

يحتاج:

```text
Core boundary
```

و:

```text
Reporting
```

يحتاج:

```text
Ledger
+
Pricing
```

و:

```text
Frontend
```

يجب ألا يُبنى قبل تثبيت backend DTOs.

---

# 72. ما الذي لا يجب فعله أثناء P1؟

ممنوع scope creep باتجاه:

```text
Multi-Agent
```

أو:

```text
AgentRunner rewrite
```

أو:

```text
ExecutionState redesign
```

أو:

```text
new event architecture
```

أو:

```text
new execution modes
```

أو:

```text
full OpenTelemetry collector infrastructure
```

أو:

```text
PostgreSQL
```

أو:

```text
distributed telemetry
```

في هذه المرحلة.

---

# 73. Definition of Done

P1 لا تعتبر مكتملة لأن dashboard يظهر أرقامًا.

بل عندما يستطيع الفريق إثبات:

```text
1. Run executes
2. LLM usage captured
3. Streaming usage captured
4. Missing provider usage estimated correctly
5. Unknown usage remains unknown
6. One invocation = one ledger identity
7. Ledger survives restart
8. Chat deletion does not destroy ledger history
9. Pricing is versioned
10. Historical cost is reproducible
11. Repricing does not mutate history
12. Reports match dashboard
13. CSV matches dashboard
14. Print report matches dashboard
15. Agent and Direct Chat use same accounting path
16. Web does not access Core internals
17. Artifact history survives restart
18. Latency/retry/failure metrics are available
19. Runtime V1 E2E tests remain green
20. No measurable regression in execution UX
```

---

# 74. التقييم النهائي

بعد إعادة التقييم، أعطي التصميم الجديد:

```text
Architecture              9/10
Accounting correctness    9.5/10
Local-first               10/10
Privacy                    9.5/10
Durability                 9/10
Extensibility              9.5/10
Analytics                  9/10
Frontend UX                9/10
Performance                9/10
Future provider support    9.5/10
```

لكن هذه الدرجة مشروطة بتنفيذ التصحيحات الأساسية أعلاه، خصوصًا:

```text
TokenUsage typed model
+
true streaming
+
unknown ≠ zero
+
model-native tokenizer fallback
+
immutable invocation ledger
+
versioned pricing
+
no destructive repricing
+
integer monetary precision
+
central LLM accounting boundary
+
Web/Core public telemetry boundary
```

## القرار الهندسي النهائي

الخطة الأصلية كانت جيدة.

لكن النسخة الأفضل لـ PELDRUN ليست:

> "نضيف token counter + SQLite + dashboard."

بل:

> **ننشئ طبقة Usage Accounting مستقلة، canonical، immutable، model-aware، provider-neutral، local-first، ومترابطة مع Runtime lifecycle، ثم نجعل cost وanalytics وreports مجرد projections فوق هذه الحقائق.**

هذا هو التصميم الذي أنصح الفريق ببنائه الآن، لأنه يسمح لكم بعد P1 بإضافة:

```text
quotas
budgets
alerts
cost limits
model comparison
provider comparison
per-project accounting
GPU/inference economics
OpenTelemetry export
billing
multi-agent accounting
```

دون تغيير الـ accounting core نفسه.

## المراجع التقنية الأساسية

* OpenTelemetry GenAI Semantic Conventions 2026: model/provider, conversation correlation, token usage, latency وTTFT.
* SQLite WAL/concurrency/durability behavior.
* Hugging Face model-native tokenizer loading and fast tokenizers.
* tiktoken model-specific encoding support.
* OpenAI current usage structures include input/output and, where applicable, cached/reasoning details.
