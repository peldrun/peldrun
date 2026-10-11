# PELDRUN Web – Installation & Operation Guide

[![PELDRUN Core](https://img.shields.io/badge/PELDRUN-Core%200.2.0-blue?style=for-the-badge&logo=github)](https://github.com/peldrun/peldrun)
[![FastAPI](https://img.shields.io/badge/Backend-FastAPI-009688?style=for-the-badge&logo=fastapi)](https://fastapi.tiangolo.com)
[![Next.js](https://img.shields.io/badge/Frontend-Next.js%2016-000000?style=for-the-badge&logo=next.js)](https://nextjs.org)
[![License](https://img.shields.io/badge/License-MIT-yellow?style=for-the-badge)](LICENSE)

A complete web dashboard and runtime control plane for the **PELDRUN** AI agent. Visual execution, live streaming, and full workspace, deliverables, and settings management.

---

## ✨ Key Features

- **Split-View Interface** – Chat and live telemetry side by side.
- **Live Streaming (SSE)** – Real-time thoughts, tool calls, and observations.
- **Embedded PELDRUN Core** – Single authoritative execution engine running directly within `backend/peldrun/`.
- **Secure File Explorer** – File tree, code editor, downloads, and path-traversal protection.
- **Task History & Replay** – Persistent session storage with token usage and cost accounting.
- **Bilingual Support** – Arabic and English with full RTL/LTR.
- **Advanced Security** – Workspace boundary enforcement, safe file deletion, and secret isolation.
- **One-Click Launch** – PowerShell scripts to start both backend and frontend servers.

---

## 🏗️ Architecture

```text
Browser (localhost:3088)
      │ HTTP + SSE
      ▼
Frontend (Next.js 16) ── /api/* proxy ──► Backend Control Plane (FastAPI) on port 8088
                                                   │
                                                   ▼
                                        Embedded PELDRUN Core
                                          (backend/peldrun/)

```

---

## 📂 Project Structure

```text
peldrun/
├─ start-all.ps1
├─ start-backend.ps1
├─ start-frontend.ps1
├─ backend/
│  ├─ .venv/
│  ├─ requirements.txt
│  ├─ omweb/             # Web application & control plane layer
│  ├─ peldrun/           # Embedded PELDRUN Core runtime engine
│  └─ tests/
├─ frontend/
│  ├─ package.json
│  └─ src/
└─ storage/              # Persistent chats, projects, and runtime database

```

---

## 🛠️ Prerequisites

| Requirement | Recommended Version | Notes |
| --- | --- | --- |
| **Python** | 3.12+ | Backend Control Plane & Embedded PELDRUN Core |
| **Node.js** | 18+ | Next.js frontend |
| **Git** | Latest | Version control |
| **PowerShell** | 5.1+ | Windows launch scripts |

---

## 🚀 Installation Guide

### Step 1 – Clone the Repository

```bash
git clone [https://github.com/peldrun/peldrun.git](https://github.com/peldrun/peldrun.git)
cd peldrun

```

### Step 2 – Backend Setup

```bash
cd backend
python -m venv .venv

# Activate virtual environment
# Windows:
.venv\Scripts\activate
# macOS/Linux:
source .venv/bin/activate

pip install -r requirements.txt

```

### Step 3 – Frontend Setup

```bash
cd ../frontend
npm install

```

### Step 4 – Embedded Core Runtime Verification

PELDRUN Core is shipped directly embedded inside the repository under `backend/peldrun/`. No external engine cloning, downloading, or manual directory linking is needed.

---

## 🏃 Running the Application

### Option A – Automated Launch (Windows)

From the project root:

```powershell
.\start-all.ps1

```

This starts both the FastAPI backend and Next.js frontend in separate PowerShell windows.

### Option B – Manual Launch (Cross-Platform)

Open **two terminals** from the project root.

**Terminal 1 – Backend (FastAPI)**

```bash
cd backend
# Windows:
.venv\Scripts\activate
python -c "import sys, asyncio, uvicorn; asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy()) if sys.platform == 'win32' else None; uvicorn.run('omweb.main:app', host='127.0.0.1', port=8088)"

# macOS / Linux:
source .venv/bin/activate
uvicorn omweb.main:app --host 127.0.0.1 --port 8088

```

**Terminal 2 – Frontend (Next.js)**

```bash
cd frontend
npm run dev -- -p 3088

```

---

## 🌐 Endpoints

| Service | URL |
| --- | --- |
| PELDRUN UI | `http://localhost:3088` |
| Backend API | `http://localhost:8088/api/` |
| Health Check | `http://localhost:8088/api/status/health` |
| API Docs | `http://localhost:8088/docs` |

---

## ⚙️ Configuration

Configure your LLM provider (OpenAI, DeepSeek, LM Studio, Ollama, etc.) either via:

1. **Settings UI** at `http://localhost:3088/settings`.
2. **`config/config.toml`** in the project root:

```toml
[llm]
model = "gpt-4o"
base_url = "[https://api.openai.com/v1](https://api.openai.com/v1)"
api_key = "sk-your-api-key-here"
max_tokens = 4096
temperature = 0.7

```

Local endpoints (e.g., LM Studio: `http://127.0.0.1:1234/v1`, Ollama: `http://127.0.0.1:11434/v1`) are detected and supported directly.

---

## 🔒 Security

* **Workspace Boundary Guard** – All file operations are constrained to authorized storage directories.
* **Safe File Deletion** – Workspace files are securely quarantined to `.trash/`.
* **Secret Isolation** – API keys and runtime files are excluded via `.gitignore`.
* **Native Embedded Authority** – Execution runs strictly through embedded PELDRUN Core contracts.

---

## 🧪 Troubleshooting

| Issue | Solution |
| --- | --- |
| Backend not starting | Ensure `.venv` is activated and dependencies installed via `pip install -r requirements.txt`. |
| Frontend not loading | Run `npm install` inside `frontend/` and ensure port 3088 is available. |
| Core engine missing | Ensure `backend/peldrun/` contains `__init__.py`. |
| Invalid API key or model unreachable | Verify model settings in UI or check port (e.g. 1234 for LM Studio). |
| SSE streaming issues | Ensure port 8088 is not blocked by local security software. |

---
