
---

# PELDRUN-Web: Sovereign Multi-Agent & Tooling Architecture

## Technical Reference & Developer Onboarding Manual

---

## 1. Executive Summary & Core Philosophy

**PELDRUN-Web** is an enterprise-grade autonomous agent workspace combining a reactive **Next.js (App Router)** client with an asynchronous **FastAPI** backend orchestrating autonomous task execution.

### The Monolithic Challenge vs. Sovereign Personas

In native upstream implementations, autonomous agents are often constructed as monolithic generalists (`app.agent.peldrun.peldrun`). A single generalist agent:

* Operates under a generic system prompt ("I am an all-capable assistant").
* Loads all environment tools indiscriminately (`bash`, `python_execute`, `file_saver`, `http_fetch`, `web_search`, and custom MCP plugins).
* Suffixes excessive token overhead to LLM reasoning cycles.
* Suffers from prompt distraction, tool hallucination, and unpredictable step counts.

**The Sovereign Agent Paradigm** solves this through **Dynamic Persona & Capability Scoping**. Rather than maintaining fragile, divergent agent subclasses, PELDRUN-Web dynamically projects a specialized persona manifest onto the hardened execution engine at runtime.

```
┌────────────────────────────────────────────────────────────────────────┐
│                        CLIENT LAYER (UI / SDK)                         │
│  - Engine Selector (LM Studio GPU / Cloud / Ollama / Custom Endpoints) │
│  - Mode Switcher (Autonomous Agent Mode vs. Direct Chat Mode)          │
│  - Sovereign Agent Picker (Code Architect, Deep Researcher, etc.)     │
└───────────────────────────────────┬────────────────────────────────────┘
                                    │ HTTP POST /api/run
                                    ▼
┌────────────────────────────────────────────────────────────────────────┐
│                   DISPATCH ROUTER (omweb/routers/run.py)               │
│  - Request validation & payload normalization                          │
│  - Chat session persistence (recording active agent_id)                │
│  - Background task scheduling with explicit keyword arguments          │
└───────────────────────────────────┬────────────────────────────────────┘
                                    │ background_tasks.add_task(...)
                                    ▼
┌────────────────────────────────────────────────────────────────────────┐
│              EXECUTION BRIDGE (omweb/agent_bridge.py)                  │
│  - Universal Parameter Resolver (Defensive against positional drift)   │
│  - Manifest retrieval from AgentRegistry                               │
│  - Dynamic Tool Scoping (Least Privilege Filtering)                    │
│  - Persona System Prompt Injection into engine context                 │
│  - Runtime AsyncOpenAI client injection (Base URL / API Key override)   │
└───────────────────────────────────┬────────────────────────────────────┘
                                    │
                                    ▼
┌────────────────────────────────────────────────────────────────────────┐
│                  AUTONOMOUS RUNTIME STEP LOOP                          │
│  - Step-by-step reasoning, tool execution, observation handling        │
│  - Real-time Server-Sent Events (SSE) telemetry                        │
│  - Mandatory core tool safety enforcement (terminate, ask_human)       │
└────────────────────────────────────────────────────────────────────────┘

```

---

## 2. Directory & Component Architecture

Understanding where key layers reside is critical for every developer joining the project:

```text
PELDRUN-Web/
├── backend/
│   ├── omweb/
│   │   ├── agents/
│   │   │   ├── __init__.py
│   │   │   └── registry.py          <-- AgentRegistry & Built-in Personas
│   │   ├── tools/
│   │   │   ├── __init__.py
│   │   │   └── registry.py          <-- ToolRegistry & Capability Metadata
│   │   ├── routers/
│   │   │   ├── run.py               <-- /api/run task dispatching & stream endpoint
│   │   │   ├── chats.py             <-- Workspace chat sessions & project history
│   │   │   └── store_rtr.py         <-- /api/store endpoints (Agents, Tools, MCP)
│   │   ├── agent_bridge.py          <-- Universal Parameter Resolver & Scoping Logic
│   │   └── project_manager.py       <-- File workspaces & session serialization
│   └── app/                         <-- Core peldrun execution engine
└── frontend/
    └── src/
        ├── app/
        │   ├── chat/page.tsx        <-- Main workspace chat interface
        │   ├── chat/[id]/page.tsx   <-- Direct session replay wrapper
        │   └── stores/page.tsx      <-- Sovereign Stores Hub (Agents & Tools store)
        ├── components/
        │   ├── chat/
        │   │   ├── composer.tsx      <-- Omnibar, Model Switcher & Conditional Agent
        │   │   ├── agent-selector.tsx<-- Dropdown agent persona picker
        │   │   ├── chat-landing.tsx  <-- Hero landing card with agent affinity
        │   │   ├── run-header.tsx    <-- Active agent transparency badging
        │   │   └── chat-container.tsx<-- Stream orchestration & payload bridge
        │   └── layout/
        │       ├── app-shell.tsx     <-- 64px Primary navigation rail (Stores link)
        │       └── sidebar.tsx       <-- 260px Collapsible session & project drawer
        └── stores/
            └── chat-store.ts        <-- Zustand state holding selectedAgentId

```

---

## 3. The Sovereign Agent Manifest Specification

Every agent in the system is governed by a declarative manifest schema. Manifests are registered in memory and serialized to disk.

### 3.1 Data Schema

```typescript
interface AgentManifest {
  id: string;            // Unique alphanumeric identifier (e.g., "code_architect")
  name: string;          // Human-readable display title (e.g., "Code Architect")
  role: string;          // Short professional subtitle
  description: string;   // Full operational scope and guidelines
  icon: string;          // Lucide icon key ("Code2" | "Search" | "BarChart3" | "Bot" | "Sparkles")
  system_prompt: string; // Specialized instructions prepended to the runtime prompt
  tools: string[];       // Permitted tool names (excluding implicit core tools)
  max_steps: number;     // Step safety limit (e.g., 20 - 35)
  builtin: boolean;      // True if immutable system agent; False if user-created
  created_at: string;    // ISO-8601 creation timestamp
}

```

### 3.2 Built-in Agents Reference Matrix

| Agent ID | Display Name | Core Purpose | Active Tools | Max Steps |
| --- | --- | --- | --- | --- |
| `peldrun` | **peldrun Generalist** | Unrestricted, all-purpose autonomous task runner. | All tools in environment | 30 |
| `code_architect` | **Code Architect** | Production software engineering, refactoring, unit testing, and modular architecture. | `bash`, `python_execute`, `file_saver` | 30 |
| `deep_researcher` | **Deep Researcher** | Web intelligence, source synthesis, multi-page data extraction, and report compiling. | `web_search`, `http_fetch`, `file_saver` | 35 |
| `data_scientist` | **Data Scientist** | Statistical exploration, Pandas/NumPy data processing, automated Matplotlib/Seaborn visualization. | `python_execute`, `file_saver` | 25 |

---

## 4. Execution Bridge: Universal Parameter Resolver

### 4.1 The Positional Argument Shift Problem

When FastAPI dispatches asynchronous tasks via `background_tasks.add_task(func, *args, **kwargs)`, positional arguments can easily shift across versions. A major failure mode occurred when calling:

```python
background_tasks.add_task(run_instrumented, actual_job_id, agent_prompt, llm_override)

```

Because `agent_id` was introduced as the third parameter in the function signature, the third argument (`llm_override`, a Python `dict`) was received as `agent_id`. Passing a dictionary into an agent registry expecting a string caused immediate fallback to `peldrun Generalist`.

### 4.2 The Universal Resolver Solution

In `backend/omweb/agent_bridge.py`, `run_instrumented` utilizes a defensive argument unpacking and unshifting pattern:

```python
async def run_instrumented(
    job_id: str,
    prompt: str,
    *args: Any,
    agent_id: Any = None,
    llm_override: Optional[Dict[str, Any]] = None,
    **kwargs: Any
) -> None:
    # 1. Unpack variable positional arguments
    for arg in args:
        if isinstance(arg, dict) and llm_override is None:
            llm_override = arg
        elif isinstance(arg, str) and agent_id is None:
            agent_id = arg

    # 2. Extract agent_id if embedded inside llm_override dictionary
    if not agent_id and isinstance(llm_override, dict):
        agent_id = llm_override.get("agent_id")

    # 3. Fallback resolution to guarantee a valid string
    if not agent_id or not isinstance(agent_id, str):
        agent_id = kwargs.get("agent_id") or "peldrun"

```

This ensures that whether a caller passes:

1. `(job_id, prompt, agent_id, llm_override)` (Ideal positional)
2. `(job_id, prompt, llm_override)` (Legacy positional shift)
3. `(job_id, prompt, agent_id="code_architect", llm_override={...})` (Named kwargs)

The bridge will always correctly extract the target agent persona and the LLM override configuration.

---

## 5. Tool Scoping & Safety Primitives

### 5.1 Least Privilege Scoping

To maintain security, minimize token usage, and prevent tool hallucination, `peldrun.tools` is filtered down dynamically to match the agent manifest:

```python
from omweb.agents.registry import agent_registry

manifest = agent_registry.get_agent(agent_id)
allowed_tools = [t.lower() for t in manifest.get("tools", [])]

# Mandatory primitives that can NEVER be removed
allowed_tools.extend(["terminate", "ask_human"])

if hasattr(agent, "tools"):
    if isinstance(agent.tools, list):
        agent.tools = [
            t for t in agent.tools
            if getattr(t, "name", t.__class__.__name__).lower() in allowed_tools
        ]
    elif hasattr(agent.tools, "tools") and isinstance(agent.tools.tools, list):
        agent.tools.tools = [
            t for t in agent.tools.tools
            if getattr(t, "name", t.__class__.__name__).lower() in allowed_tools
        ]

```

### 5.2 Mandatory Safety Tools

* `terminate`: Allows the model to signal task completion (`{"status": "success"}`). If omitted, the agent will loop endlessly until hitting `max_steps`.
* `ask_human`: Allows the model to pause execution and request human input in case of ambiguous directives.

---

## 6. Frontend UI/UX Architecture

### 6.1 Dual-Selector Pattern

The interface explicitly separates **where the model runs** from **who the agent is**:

1. **Model / Provider Picker (`Composer` top-bar):** Controls hardware targets (Local LM Studio GPU, Cloud endpoints, Ollama, custom URLs). Populates `omweb_active_llm_override` in `localStorage`.
2. **Sovereign Agent Selector (`AgentSelector`):** Controls the persona, system prompt, and scoped tools. Populates `selectedAgentId` in Zustand `chat-store`.

### 6.2 Conditional Agent Affinity

To reduce visual clutter and cognitive load:

* **Agent Mode (`execMode === 'agent'`):** Renders the `<AgentSelector/>` inside the Omnibar toolbar and the landing page hero card.
* **Chat Mode (`execMode === 'chat'`):** Automatically unmounts `<AgentSelector/>`, directing queries to direct conversation without autonomous tools.

### 6.3 Next.js Byte-Level Hygiene (Zero UTF-8 BOM)

Next.js Turbopack and SWC enforce strict compliance: `"use client";` must reside at byte offset 0.

* Standard PowerShell commands (`Set-Content -Encoding UTF8`) inject a 3-byte Byte Order Mark (`0xEF, 0xBB, 0xBF`).
* This causes Next.js compilation failures: `Error: The "use client" directive must be placed before other expressions`.
* All automated file modification operations must write clean binary streams (`Path.write_bytes(content.encode("utf-8"))`) without BOM.

---

## 7. Developer Onboarding & Extension Guides

### 7.1 How to Create a New Sovereign Agent Programmatically

To register a new custom agent, add a manifest definition via `omweb.agents.registry`:

```python
from omweb.agents.registry import agent_registry

agent_registry.register_agent({
    "id": "security_auditor",
    "name": "Security Auditor",
    "role": "Static Code & Vulnerability Scanner",
    "description": "Reviews source trees, checks CVE databases, and reports security risks.",
    "icon": "Sparkles",
    "system_prompt": (
        "You are an expert Application Security Engineer. Your job is to analyze repositories, "
        "identify OWASP Top 10 vulnerabilities, inspect dependencies, and output structured markdown audits."
    ),
    "tools": ["bash", "file_saver"],
    "max_steps": 25,
    "builtin": False
})

```

Once registered:

1. It immediately appears in `GET /api/store/agents`.
2. The UI dropdown (`AgentSelector`) automatically displays the new agent with its icon and title.
3. Tasks dispatched with `"agent_id": "security_auditor"` will have tools restricted to `['bash', 'file_saver', 'terminate', 'ask_human']`.

### 7.2 How to Create a New Autonomous Tool

1. Create a tool class implementing peldrun `BaseTool`:

```python
# backend/app/tool/git_operator.py
from app.tool.base import BaseTool

class GitOperator(BaseTool):
    name: str = "git_operator"
    description: str = "Executes safe, verified Git operations like status, diff, and commit."
    parameters: dict = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["status", "diff", "log"],
                "description": "The git action to perform"
            }
        },
        "required": ["action"]
    }

    async def execute(self, action: str) -> str:
        # Implementation logic here
        return f"Git action '{action}' executed successfully."

```

2. Register the tool in `backend/omweb/tools/registry.py`:

```python
tool_registry.register_tool({
    "id": "git_operator",
    "name": "Git Operator",
    "description": "Enables safe, non-destructive Git inspection.",
    "category": "developer"
})

```

3. Add `"git_operator"` to the `tools` array of any Sovereign Agent manifest.

---

## 8. API Reference

### 8.1 Dispatch Task (`POST /api/run`)

Dispatches a new autonomous task or direct chat session.

**Request Payload:**

```json
{
  "prompt": "Inspect the src/ directory and refactor common utility functions.",
  "max_steps": 30,
  "agent_id": "code_architect",
  "mode": "agent",
  "chat_id": "chat_custom_session_01",
  "model": "qwen3-vl-8b-instruct",
  "provider": "lmstudio",
  "base_url": "http://127.0.0.1:1234/v1"
}

```

**Response (200 OK):**

```json
{
  "job_id": "job_dced4341cc58",
  "status": "running",
  "chat_id": "chat_custom_session_01",
  "mode": "agent",
  "agent_id": "code_architect",
  "model": "qwen3-vl-8b-instruct",
  "provider": "lmstudio"
}

```

### 8.2 List Sovereign Agents (`GET /api/store/agents`)

Returns all active system and custom agents.

**Response (200 OK):**

```json
[
  {
    "id": "peldrun",
    "name": "peldrun Generalist",
    "role": "General Autonomous Specialist",
    "tools": ["*"],
    "max_steps": 30,
    "builtin": true
  },
  {
    "id": "code_architect",
    "name": "Code Architect",
    "role": "Senior Software Architect",
    "tools": ["bash", "python_execute", "file_saver"],
    "max_steps": 30,
    "builtin": true
  }
]

```

### 8.3 Real-Time SSE Stream (`GET /api/run/jobs/{job_id}/stream`)

Subscribes to live step-by-step agent telemetry.

**Stream Event Types:**

* `thought`: Internal reasoning step.
* `tool_call`: Tool invocation with parameters.
* `observation`: Tool execution output.
* `final`: Final response payload.
* `done`: Terminal lifecycle event.

---

## 9. Verification & Diagnostics Runbook

When verifying autonomous behavior in terminal logs (`.\start-backend`):

### Expected Initialization Output

```text
[BRIDGE] Initializing agent for job: job_dced4341cc58
[BRIDGE] Target LLM: [LM Studio (Local)] Model: 'qwen3-vl-8b-instruct' | URL: 'http://127.0.0.1:1234/v1'
[BRIDGE] Activating agent 'Code Architect' (ID: code_architect)
[BRIDGE] Tools scoped to: ['bash', 'python_execute', 'file_saver', 'terminate', 'ask_human']
[BRIDGE] Successfully injected runtime AsyncOpenAI client (model=qwen3-vl-8b-instruct, base_url=http://127.0.0.1:1234/v1)

```

### Expected Step Execution Output

```text
2026-09-30 01:54:29.269 | INFO | app.agent.base:run:140 - Executing step 1/30
2026-09-30 01:54:43.806 | INFO | app.agent.toolcall:think:81 - ✨ peldrun's thoughts: I am a Senior Full-Stack Engineer and Architect specializing in writing clean, modular, robust code...
2026-09-30 01:54:43.815 | INFO | app.agent.toolcall:execute_tool:188 - 🔧 Activating tool: 'terminate'...
[BRIDGE] Execution completed successfully for job: job_dced4341cc58

```

### Common Diagnostic Checks

1. **Agent shows generalist prompt:** Verify `agent_id` is passed as a string in the payload, not masked inside a nested dictionary without resolver handling.
2. **Infinite step loop:** Check whether `terminate` was accidentally stripped during tool scoping.
3. **Next.js compilation errors:** Check if any `.tsx` or `.ts` file begins with a UTF-8 BOM (`\xef\xbb\xbf`) and ensure `"use client";` starts exactly at byte 0.

---

## 10. Summary of Architectural Achievements

1. **Full Persona Specialization:** Achieved dynamic system prompt injection without maintaining detached agent classes.
2. **Defensive Parameter Architecture:** Zero fragility against positional and keyword parameter shifts in FastAPI background tasks.
3. **Least Privilege Security:** Strict dynamic tool scoping with guaranteed preservation of core termination safety primitives.
4. **Complete Dual-Selector UX:** Unified model switching and persona dispatching with context-sensitive visibility rules.
5. **Session Traceability:** Permanent agent affinity tracking across workspace history, chat drawers, and replay tabs.

---

