```markdown
# PELDRUN Resource Monitor - Technical Documentation

## 1. Overview
The **PELDRUN Resource Monitor** is an internal observability and telemetry subsystem designed to track hardware utilization (CPU, RAM, Disk I/O), process trees, and execution attribution across AI agent lifecycles. It provides a dedicated dashboard for users to inspect historical run metrics, detect memory leaks (creep), identify orphaned subprocesses, and analyze per-tool resource consumption.

### Core Principles
- **Zero-Impact / Low-Overhead:** Adaptive sampling and asynchronous batch writes prevent the monitor from slowing down the main application (target: `<1%` CPU overhead).
- **Separation of Concerns:** The monitor runs independently of the execution engine. It relies on event hooks (spans) and does not interfere with the core `asyncio` event loop.
- **High-Fidelity Attribution:** Differentiates between `DIRECT` (dedicated subprocess) and `PROPORTIONAL` (shared process space) resource consumption.

---

## 2. Architecture & Directory Structure

### Backend
Located in `backend/omweb/resource_monitor/` and `backend/peldrun/runtime/`.
```text
backend/
├── omweb/
│   ├── resource_monitor/       # Core monitoring subsystem
│   │   ├── collectors/         # Host, Process, Disk, Network collectors (psutil)
│   │   ├── providers/          # OS-specific probes (Windows, Linux, macOS, Docker, WSL)
│   │   ├── storage/            # SQLite DB, WAL config, Repositories, Rollups
│   │   ├── attribution.py      # Process-to-Run mapping & concurrency partitioning
│   │   ├── diagnostics.py      # Leak detection & orphan heuristics
│   │   ├── lifecycle.py        # Session & Run state coordinator
│   │   ├── process_tracker.py  # PID tracking & grace period evaluation
│   │   └── sampling_scheduler.py # Adaptive async polling loop
│   ├── services/
│   │   └── resource_monitor_service.py # Facade bridging FastAPI and the subsystem
│   └── routers/
│       └── resource_monitor.py # REST API endpoints
└── peldrun/
    └── runtime/
        └── executor.py         # Injects tool spans into the monitor via event interceptor
```

### Frontend
Located in `frontend/src/features/resource-monitor/` and `frontend/src/app/resource-monitor/`.
```text
frontend/src/
├── app/
│   └── resource-monitor/       # Next.js App Router (Page & Badge)
│       ├── page.tsx            # Main dashboard page
│       ├── layout.tsx          # Page metadata
│       ├── ResourceMonitorBadge.tsx # Top-bar status indicator
│       └── useResourceAlerts.ts     # Global alert polling hook
└── features/
    └── resource-monitor/       # Feature-specific logic
        ├── api/                # Typed fetch client & Zod schemas
        ├── components/         # UI (Dashboard, Cards, Tables, Drawer, Charts)
        ├── hooks/              # Polling hooks (Current, History, ProcessList)
        ├── types/              # TypeScript interfaces matching backend
        └── utils/              # Formatters (Bytes, Duration)
```

---

## 3. Backend Deep Dive

### 3.1. Telemetry Pipeline
1. **SamplingScheduler:** Runs an asynchronous loop. In *Idle* mode, it samples every 5s. In *Active* mode (runs > 0), it samples every 1s. If host CPU > 85%, it throttles to 3s.
2. **Collectors:** Uses `psutil` to gather host metrics and the PELDRUN process tree. Buffer is flushed to SQLite via `asyncio.to_thread` to avoid blocking.
3. **Attribution Engine:** Correlates samples with active runs. If 3 runs share the main process, their memory is divided proportionally, and `measurement_confidence` is set to `PROPORTIONAL`.
4. **Diagnostics Engine:** Runs in an independent loop. Checks for orphans (processes surviving >30s after run completion) and memory creep (steady RSS increase over 5 idle cycles).

### 3.2. Database Schema (SQLite)
Uses **WAL mode** (`peldrun_monitor.db`) for concurrent reads/writes.
- `monitor_sessions`: App startup/shutdown.
- `execution_runs`: Run metadata (duration, peak RSS, confidence).
- `execution_spans`: Granular tool calls (start/end time, target name, CPU/RSS).
- `resource_samples`: High-frequency timeseries data.
- `diagnostic_events`: Alerts (orphans, leaks).

### 3.3. API Endpoints (`/api/resource-monitor`)
- `GET /environment`: Hardware and OS profile.
- `GET /current`: Real-time stateless snapshot (Host + PELDRUN).
- `GET /processes`: Active subprocesses and orphan indicators.
- `GET /runs`: Paginated execution history (supports `include_all_sessions`).
- `GET /runs/{run_id}/timeline`: Run details, spans, and bounded timeseries for drill-down.
- `GET /timeseries`: Historical chart data.
- `GET /diagnostics`: List of anomaly events.
- `POST /actions/terminate-orphan`: Kills a rogue process.

### 3.4. Execution Hooks (Integration)
- **Dispatcher:** `_safe_monitor_start_run()`, `_safe_monitor_complete_run()` are called at the bridge level.
- **Executor:** `_telemetry_event_interceptor` listens to `TOOL_CALL` and `OBSERVATION` events from `peldrun-core` and maps them to `start_span` / `end_span` calls.

---

## 4. Frontend Deep Dive

### 4.1. Data Fetching & State
- **Polling:** Custom hooks (`useCurrentResources`, `useProcessList`, `useResourceHistory`) use `setInterval` with backoff logic. Polling pauses when the tab is hidden (`document.visibilityState`).
- **Validation:** Every API response is validated using **Zod** (`api/schemas.ts`) before being passed to the UI. This prevents crashes from backend contract drift.
- **Drawer Data:** The `RunDetailDrawer` fetches `/runs/{id}/timeline` on-demand when opened.

### 4.2. Components
- **`ResourceDashboard`**: Main container. Manages tab state and settings modal.
- **`HostOverviewCards`**: Displays Host RAM, CPU, PELDRUN RSS, and Disk. Uses SVG circular gauges and animated numbers (`motion`).
- **`TimeseriesCharts`**: Uses `recharts` (AreaChart) to plot CPU and Memory. Supports 1H, 6H, 24H, 7D, 30D ranges.
- **`RunConsumptionTable`**: Lists runs. Clicking a row opens the `RunDetailDrawer`.
- **`RunDetailDrawer`**: Renders via `createPortal` (crucial for avoiding CSS transform issues) and displays:
    1.  **Timeline:** A visual block representation of tool spans over time.
    2.  **Tools:** A BarChart and table grouping spans by `target_name`.
    3.  **Memory:** A dual-axis AreaChart mapping CPU and RSS during the run's exact timeframe.
- **`DiagnosticsAlertBanner`**: Shows critical warnings (e.g., orphan processes) at the top of the page.
- **`ResourceMonitorBadge`**: Top-bar pill. Shows "Live" (pulsing green) when healthy, "Offline" on API failure, and a red/amber count on alerts.

---

## 5. Issues Resolved in This Release
The following critical bugs and architectural flaws were fixed:
1.  **Missing `force` parameter:** Added to `TerminateOrphanRequest` to prevent 500 errors.
2.  **REST Idempotency:** Separated `GET /current` (stateless) from the internal tracking loop to prevent data pollution when multiple tabs are open.
3.  **JSON Export Corruption:** Fixed `str(dict)` serialization in `/export` by using `json.dumps()`.
4.  **Async Blocking:** Wrapped `sample_now` in `asyncio.to_thread` to prevent the UI from freezing during psutil calls.
5.  **Lifecycle Data Loss:** Moved `resolve_final_metrics` *before* `on_run_ended` in `lifecycle.py` to ensure accurate peak recording.
6.  **Session Filtering UX:** Defaulted `list_runs` to include all sessions so user data doesn't "disappear" on backend restart.
7.  **Tool Span Ingestion:** Implemented event interceptor in `executor.py` to correctly capture tool execution spans.
8.  **Drawer CSS Clipping:** Wrapped `RunDetailDrawer` in `createPortal` to escape the `transform` context of the parent table.
9.  **CPU First-Read:** Added `interval=0.1` to `HostCollector.__init__` to prevent initial `0.0%` readings.

---

## 6. Known Issues & Pending Tasks (Roadmap for Future Devs)

### 🔴 High Priority (Backend)
- **Error Reporting in Collectors:** When a collector fails (e.g., permission denied), it currently fails silently (returns `None`). Future devs should inject a `MEASUREMENT_GAP` `DiagnosticEvent` into the DB.
- **Memory Creep False Positives:** The current heuristic (strictly growing baseline over 5 cycles) might trigger on normal Python allocator fragmentation. Consider factoring in `gc.collect()` success rates.
- **Restart Gap on Spans:** If the server crashes mid-run, open spans remain in DB without an `ended_at`. The recovery logic in `lifecycle.py` should mark these as `TIMED_OUT`.

### 🟡 Medium Priority (Frontend & Performance)
- **React Query Migration:** Replace custom `setInterval` hooks with `@tanstack/react-query`. This will provide automatic deduplication, caching, and background refetching (reducing API load by ~60%).
- **SSE for Live Telemetry:** Replace the 2-second `GET /current` polling with a Server-Sent Events (SSE) endpoint. This is more efficient and provides true real-time updates.
- **Health Score Widget:** Add a centralized `0-100` score card based on RAM/CPU thresholds, orphan count, and active diagnostics.
- **Compare Mode:** Allow users to compare a run's memory chart against the same run from a previous week.

### 🟢 Low Priority (UX)
- **Empty State:** A welcoming screen when `runs.length === 0` and `processes.length === 0` (delayed by user request).
- **Toasts for Refresh:** Replace the spinner on the Refresh button with a `sonner` or `react-hot-toast` notification.
- **Alert Thresholds UI:** Expose `alert_ram_threshold_percent` and `alert_cpu_threshold_percent` in the Settings modal (currently hardcoded in `config.py`).

---

## 7. Developer Guide

### Running Locally
**Backend:**
```powershell
cd  peldrun\backend
.\.venv\Scripts\Activate.ps1
python -m uvicorn omweb.main:app --reload --port 8088
```
*Note: Ensure `psutil` is installed in the active virtual environment.*

**Frontend:**
```powershell
cd  peldrun\frontend
npm run dev
```
Navigate to `http://localhost:3088/resource-monitor`.

### Testing the Orphan Detector
1. Start a run that spawns a subprocess (e.g., a bash command).
2. Kill the main run abruptly (via cancel).
3. Wait 30 seconds (the `orphan_grace_period_sec`).
4. The process will appear in the `Processes` tab with a red `Orphan` badge, and a `CRITICAL` alert will appear in the top banner.
```
