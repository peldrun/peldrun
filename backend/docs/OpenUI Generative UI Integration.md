تم تثبيت وتسجيل الـ Commit المرجعي للمستودع في الذاكرة بدقة:
`36cc46e78c60671c38bdf6799f99cb6b52e45782`.

---

تحليلك دقيق وفي صلب الحقيقة المعمارية؛ ما حدث خلال الخطوات السابقة كان **انحرافاً هندسياً عن الخطة المتفق عليها**. بدلاً من تنفيذ التحويل الجذري إلى محرك **OpenUI** الرسمي (`[https://www.openui.com/](https://www.openui.com/)`)، وقعنا في فخ **"الترقيع الهجين" (Hybrid Patching)**: قمنا بتثبيت الحزم الرسمية (`@openuidev/*`)، لكننا واصلنا ترقيع محلل التعبيرات النمطية القديم الموروث في `OpenUIRenderer.tsx`، وأبقينا موجه الباك إند يطلب صيغة نصية عشوائية لا علاقة لها بلغة **OpenUI Lang** الرسمية.

النتيجة كانت متوقعة: **فشل مزدوج**؛ لم نستفد من قوة محرك OpenUI الحقيقي، وكسرنا في الوقت نفسه المحرك القديم.

إليك **الخطة الهندسية الكاملة والصريحة للتحويل الجذري والصحيح** إلى محرك OpenUI الرسمي بدون وسيط سحابي (Zero-Gateway / Local-First)، متبوعة بـ **قائمة المهام التنفيذية الشاملة (Master Task List)**.

---

# 🏛️ وثيقة التحويل المعماري لمحرك OpenUI الرسمي في منظومة Peldrun

---

## 1. التشخيص المعماري: الفجوة بين الوضع الحالي والمعيار الرسمي

```mermaid
flowchart TD
    subgraph Current_Broken_Hybrid [الوضع المشوه الحالي]
        A1[LLM Prompt] -->|يطلب لغة مخصصة غير قياسية| B1[Text: 'Chart line: ...']
        B1 -->|700 سطر Regex هش في OpenUIRenderer| C1[Regex Matching Fails]
        C1 -->|سقوط في Prose / كتل مظلمة| D1[واجهة مشوهة وجداول مكسورة]
    end

    subgraph Official_OpenUI_Pipeline [المعمارية المستهدفة الرسمية]
        A2[Component Registry / Zod] -->|توليد آلي لموجه النظام| B2[LLM System Prompt]
        B2 -->|ينتج لغة OpenUI Lang v0.5 القياسية| C2["root = Stack([c1, ch1])"]
        C2 -->|@openuidev/lang-core AST Parser| D2[Strict Incremental AST]
        D2 -->|@openuidev/react-lang <Renderer/>| E2[Native Interactive React 19 UI]
    end

```

### الفروق الجوهرية التي يجب حسمها:

1. **لغة التوليد (OpenUI Lang):**
* **المعيار الرسمي:** لغة إعلانية وظيفية صارمة؛ تبدأ دائماً بـ `root = ...` مع إسنادات متغيرات موضعية `id = Component(args...)`.
* **الوضع القديم:** نصوص مسبوقة بكلمات مفتاحية ونقطتين (`Card: ...`, `Metric: ...`) بلا شجرة هرمية.


2. **مصدر الحقيقة وتوليد الموجه (Single Source of Truth):**
* **المعيار الرسمي:** تُعرّف المكونات مرة واحدة عبر **Zod Schemas**، ويقوم محرك `@openuidev/lang-core` باشتقاق موجه النظام (System Prompt) تلقائياً منها، مما يضمن تطابقاً بنسبة 100% بين ما يطلبه الموجه وما يفهمه المحلل.
* **الوضع القديم:** الموجه مكتوب يدوياً في Python بمعزل عن كود الفرونت إند، مما تسبب في ارتباك النموذج وخروجه عن النص.


3. **محرك التصيير (Runtime Parser & Renderer):**
* **المعيار الرسمي:** استخدام مكون `<Renderer/>` الرسمي ومحلل شجرة الـ AST التدريجي التابع لـ `@openuidev/react-lang` مع حل المراجع الأمامية (Forward References) بدون وميض (Zero Flickering).
* **الوضع القديم:** 700 سطر من تعابير Regex التخمينية (Best-effort parsing) التي تبتلع النصوص وتفشل مع أي تغيير طفيف في المسافات أو الأقواس.



---

## 2. المعمارية المستهدفة للتحويل النظيف (Target Architecture)

1. **الكتالوج الموحد للمكونات (`peldrun-library.ts`):**
* إعادة صياغة كافة المكونات الـ 14 باستخدام `defineComponent` من `@openuidev/react-lang`.
* دمج مكونات Peldrun المحصنة (`PeldrunDiagram` مع تعقيم SVG، و `PeldrunMathPlot` مع Whitelist الرياضي الصارم، و `PeldrunChart` المبني على Recharts).


2. **توليد مواصفات المكتبة (`LibrarySpec`):**
* تصدير العقد بصيغة JSON رسمية تقرؤها أداة الباك إند لإنتاج System Prompt متوافق كلياً مع OpenUI Lang v0.5.


3. **موجه الباك إند القياسي (`backend/peldrun/context/generative_ui.py`):**
* إلغاء صياغات `COMPONENT_SPECIFICATIONS` القديمة.
* إلزام النموذج بإنتاج كود OpenUI Lang الصريح الذي يبدأ بـ `root = ...` محصوراً فقط داخل وسم `<openui>...</openui>`.


4. **استبدال الـ Renderer القديم كلياً:**
* حذف ملف الـ Regex القديم أو إحالته للتقاعد بالكامل.
* استخدام مكون تصيير يعتمد على `createStreamingParser` و `<Renderer library="{peldrunLibrary}"/>` الرسميين.


5. **معالجة التقارير الهندسية والـ JSON المتعدد:**
* دعم كتل كود `json` و `recharts` في `code-block.tsx` لتصيير المخططات الهندسية (Bode plots, RLC steps) حتى عندما يكتب النموذج تقريراً علمياً عادياً خارج وسم `<openui>`.



---

## 3. قائمة المهام التنفيذية الشاملة (Master Task List)

---

### 🎯 المرحلة 1: بناء عقد مكتبة OpenUI الرسمية (P0 - Library Contract)

* [ ] **المهمة 1.1: تهيئة مكتبة المكونات الرسمية عبر `defineComponent**`
* **الملف المستهدف:** `frontend/src/components/generative-ui/peldrun-library.tsx`
* **الإجراء:**
1. استيراد `defineComponent` و `createLibrary` من `@openuidev/react-lang`.
2. إعادة صياغة المكونات الـ 14 مع Zod Schemas محددة الترتيب الموضعي (Positional Args) المتوافق مع OpenUI Lang:
* `Card(title, description)`
* `Metric(label, value, change, trend)`
* `Chart(type, title, unit, data)`
* `Table(headers, rows)`
* `Diagram(title, code)`
* `MathPlot(title, formula, equation, rangeX)`
* `Alert(type, message)`
* `Badge(label, variant)`
* `Progress(label, percentage)`
* `Carousel(title, slides)`
* `Accordion(items)`
* `TestResults(passed, failed, skipped)`
* `ActionButton(label, actionId)`
* `Form(title, fields, submitLabel)`


3. تجميع المكونات ومكونات التخطيط الهيكلي (`Stack`, `Grid`) عبر `createLibrary`.


* **بوابة التحقق:** `npm run typecheck` يمر بنجاح بدون أخطاء تضارب أنواع.


* [ ] **المهمة 1.2: استخراج وتصدير مواصفات المكتبة (`LibrarySpec`) للباك إند**
* **الملف المستهدف:** `frontend/src/components/generative-ui/peldrun-library.spec.json`
* **الإجراء:** تصدير مواصفات المكونات ومخططاتها بصيغة JSON المتوافقة مع معيار OpenUI v0.5 لتكون مرجعاً موحداً للباك إند.



---

### ⚙️ المرحلة 2: إعادة هندسة موجه الباك إند وفق OpenUI Lang (P0 - Backend Prompt Engine)

* [ ] **المهمة 2.1: إعادة كتابة `generative_ui.py` لقواعد OpenUI Lang الصريحة**
* **الملف المستهدف:** `backend/peldrun/context/generative_ui.py`
* **الإجراء:**
1. إزالة صيغ البادئات القديمة (`Card: [Title]`, `Metric: ...`).
2. اعتماد صيغة OpenUI Lang الرسمية:
```text
root = Stack([comp1, comp2])
comp1 = ComponentName(arg1, arg2, ...)

```


3. إلزام النموذج بقاعدة الانطلاق: "كل واجهة تبدأ حتماً بـ `root = ...`".
4. تحديث دالة `_build_dynamic_example` لإنتاج أمثلة OpenUI Lang صحيحة 100% تتضمن `Chart` بأرقام حقيقية و `Table` و `Card`.
5. فرض قيود سلبية حازمة تمنع الأقواس المربعة حول المكونات وتمنع الإحالات النصية الكسولة مثل `(values from table)`.


* **بوابة التحقق:** فحص موجه النظام واختبار المخرجات عبر اختبار وحدة في الباك إند.



---

### 🔄 المرحلة 3: استبدال محرك التحليل بالـ Runtime الرسمي (P0 - Frontend Runtime Replacement)

* [ ] **المهمة 3.1: بناء الـ Renderer الرسمي في الفرونت إند**
* **الملف المستهدف:** `frontend/src/components/generative-ui/OpenUIRenderer.tsx`
* **الإجراء:**
1. إحالة كود الـ 700 سطر القديم المعتمد على Regex إلى التقاعد التام.
2. استخدام مكون `<Renderer/>` ومحلل التدفق اللحظي `createStreamingParser` من حزمة `@openuidev/react-lang` و `@openuidev/lang-core`.
3. تمرير `peldrunLibrary` إلى المحرك لتصيير عناصر AST مباشرة إلى مكونات React 19 الأصلية.
4. تفعيل آلية صيد الأخطاء والـ Fallback النظيف إلى Markdown عند وصول كود غير مكتمل أو مشوه أثناء التدفق.


* **بوابة التحقق:** التحقق من قدرة المكون على تفسير كود `root = Stack(...)` وتصييره بسلاسة.


* [ ] **المهمة 3.2: تحديث `GenerativeUIRouter.tsx` لتدفق OpenUI Lang النقي**
* **الملف المستهدف:** `frontend/src/components/generative-ui/GenerativeUIRouter.tsx`
* **الإجراء:**
1. عزل كتل `<openui>...</openui>` النظيفة وتمرير كود OpenUI Lang الداخلي مباشرة إلى `OpenUIRenderer`.
2. التأكد من معالجة التدفق (Streaming) دون تقطيع نصوص الـ Markdown السابقة أو اللاحقة للواجهة.
3. تطهير الوسوم عند تعطيل الميزة دون ترك أي أثر نصي خام للمستخدم.


* **بوابة التحقق:** فحص استقرار التدفق والتأكد من انعدام ظاهرة الوميض (Zero Flickering).



---

### 📊 المرحلة 4: تكامل التقارير الهندسية والمخططات التفاعلية (P1 - Engineering Reports & Recharts Integration)

* [ ] **المهمة 4.1: ترقية `code-block.tsx` للاكتشاف التلقائي لبيانات Recharts**
* **الملف المستهدف:** `frontend/src/components/chat/code-block.tsx`
* **الإجراء:**
1. فحص كتل كود `json` أو `recharts` في الردود.
2. إذا احتوت الكتلة على بيانات سلاسل زمنية أو محاور هندسية (مثل استجابة RLC أو منحنيات الإجهاد)، يتم تصييرها تلقائياً كرسم بياني تفاعلي حي عبر `PeldrunChart`.
3. إضافة زر تبديل أنيق بين "الرسم البياني التفاعلي (Interactive Chart)" و "الكود البرمجي (Raw JSON)".


* **بوابة التحقق:** اختبار الصور المرفقة والتأكد من تحول صناديق كود JSON تلقائياً إلى رسوم بيانية حية.



---

### 🧪 المرحلة 5: بوابات الجودة والتحقق النهائي (P2 - Quality Gates & End-to-End Testing)

* [ ] **المهمة 5.1: فحص الأنواع واكتمال البناء (Typecheck & Build Gate)**
* **الإجراء:**
```powershell
cd frontend
npm run typecheck
npm run build

```


* **الشرط:** اجتياز فحص TypeScript واكتمال بناء Next.js 16 الإنتاجي بـ 0 أخطاء.


* [ ] **المهمة 5.2: اختبار استدلال حقيقي مع النماذج المحلية (Live Inference Gate)**
* **الإجراء:** تشغيل تجربة حية عبر LM Studio باستخدام نموذج `nvidia/nvidia-nemotron-3-nano-4b` ونموذج `qwen2.5-coder` للتأكد من إنتاج كود OpenUI Lang صحيح وتصييره في المتصفح بنجاح 100%.



---

