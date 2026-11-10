
# PELDRUN Tool Output Streaming Contract

## 1. Overview

The Tool Output Streaming feature allows tools to publish live telemetry, standard output (stdout), standard error (stderr), and structured progress updates **while they are executing**, rather than waiting until the tool finishes to return a single `ToolResult`.

This provides a critical UX improvement for long-running tasks (e.g., running a shell command, downloading a file, or executing a complex Python script) by displaying real-time feedback in the UI.

### Design Principles
1. **Opt-in via Contract**: Tools that do not wish to stream output do not need to change a single line of code. Streaming is entirely additive.
2. **No Mocking**: The framework never fabricates progress. If a tool does not emit data, the UI will not show fake progress bars.
3. **Transport-Agnostic**: Tools are unaware of SSE, EventEmitters, or React. They simply write to a `ToolExecutionContext`. The Core Runtime handles the rest.
4. **Retry-Safe**: Data streams carry a `sequence` and `attempt` number, ensuring the frontend can gracefully discard partial output from failed attempts and deduplicate chunks after an SSE reconnect.

---

## 2. Architecture Pipeline

When a tool emits a chunk, it travels through the following pipeline:

```text
[ Your Tool ]
     │  1. await context.emit_output("...", stream="stdout")
     ▼
[ ToolExecutionContext ] (Queue)
     │  2. ToolCallAgent drains the queue
     ▼
[ ToolCallAgent.act() ]
     │  3. Emits EventType.TOOL_OUTPUT
     ▼
[ Core EventEmitter ]
     │  4. Pushes to PeldrunEngine via Queue
     ▼
[ PeldrunEngine ]
     │  5. Translates to SSEEventType.TOOL_OUTPUT
     ▼
[ SSE Stream ]
     │  6. HTTP `text/event-stream` chunk
     ▼
[ Frontend RunStreamManager ]
     │  7. Redux-style stream-reducer deduplicates
     ▼
[ UI Tool Card ]
        8. Displays in `Live Output` box
```

---

## 3. The Developer Contract (`ToolExecutionContext`)

The framework passes an optional `context` object to your tool. You can use it to stream output.

```python
from typing import Literal, Protocol, Optional

@runtime_checkable
class ToolExecutionContext(Protocol):
    async def emit_output(
        self,
        chunk: str,
        *,
        stream: Literal["stdout", "stderr", "output"] = "stdout",
    ) -> None:
        """Publish one raw output chunk."""
        ...

    async def emit_progress(
        self,
        *,
        current: Optional[int] = None,
        total: Optional[int] = None,
        phase: Optional[str] = None,
        message: Optional[str] = None,
    ) -> None:
        """Publish a structured progress update."""
        ...
```

### Method Details

#### `await context.emit_output(chunk, stream)`
- **`chunk`**: The text fragment to append (can be a single character, a line, or a block).
- **`stream`**: `"stdout"`, `"stderr"`, or `"output"`. The frontend uses this to apply colors (e.g., red for `stderr`).
- *Note*: Empty chunks are ignored.

#### `await context.emit_progress(current, total, phase, message)`
- **`current` / `total`**: If both are provided, the frontend calculates a `%` and renders a progress bar.
- **`phase`**: A machine-readable state string (e.g., `"downloading"`, `"extracting"`).
- **`message`**: A human-readable string displayed next to the progress bar.

### Synchronous Tools (Threading)
If your tool is synchronous (e.g., uses `requests` instead of `httpx`, or `subprocess.run`), it will be offloaded to a worker thread via `asyncio.to_thread`. You **cannot** use `await` inside a synchronous function.

For this reason, the concrete `ExecutionContext` also provides **thread-safe** synchronous variants:
- `context.emit_output_sync(chunk, stream)`
- `context.emit_progress_sync(current, total, phase, message)`

---

## 4. Feature Flag Guardrail

Streaming is globally disabled by default to preserve legacy behavior. You can enable it via the UI (`Runtime Settings -> Features -> Enable Tool Output Streaming`) or by setting the environment variable:

```bash
PELDRUN_ENABLE_TOOL_OUTPUT_STREAMING=1
```

### Important: How to check for `None`
If the feature flag is disabled, or if the tool is being invoked from a context that does not support streaming (e.g., an old CLI client), the `context` passed to your tool will be `None`.

**You MUST always check for `None` before emitting:**

```python
if context is not None:
    await context.emit_output("Starting...")
```

If you call `context.emit_output()` without checking, your tool will raise an `AttributeError` when the feature is disabled.

---

## 5. Complete Examples

### Example A: Asynchronous Tool (Reading Subprocess Output)
This is the canonical pattern for a tool that spawns a process and reads its output live. It uses `asyncio.create_subprocess_exec` and reads chunks via an async iterator.

```python
import asyncio
from peldrun.tools.sdk import BaseTool, ToolResult
from typing import Optional

class LivePingTool(BaseTool):
    name = "live_ping"
    description = "Ping a host and stream the live output."

    async def _arun(
        self,
        host: str,
        context: Optional[ToolExecutionContext] = None, # Opt-in!
    ) -> ToolResult:

        process = await asyncio.create_subprocess_exec(
            "ping", "-c", "4", host,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        stdout_chunks = []

        # Read output line by line
        async for line in process.stdout:
            text = line.decode("utf-8")
            stdout_chunks.append(text)

            # Stream to frontend
            if context is not None:
                await context.emit_output(text, stream="stdout")

        await process.wait()

        return ToolResult(
            output="".join(stdout_chunks),
            exit_code=process.returncode
        )
```

### Example B: Synchronous Tool (Simulating a Download)
This example runs inside a thread. It uses the `_sync` variants of the context.

```python
import time
import os
from peldrun.tools.sdk import BaseTool, ToolResult
from typing import Optional

class SlowDownloadTool(BaseTool):
    name = "slow_download"
    description = "Simulates a long download with progress."

    async def _arun(
        self,
        file_name: str,
        context: Optional[ToolExecutionContext] = None,
    ) -> ToolResult:

        # This is a sync function (no await), so it's safe to use _sync
        def _perform_download():
            if context is not None:
                context.emit_progress_sync(phase="prepare", message="Starting download...")

            total_steps = 5
            for i in range(total_steps):
                time.sleep(1) # Simulate work
                if context is not None:
                    # Emit progress bar
                    context.emit_progress_sync(
                        current=i + 1,
                        total=total_steps,
                        phase="download",
                        message=f"Downloaded chunk {i + 1}/{total_steps}"
                    )
                    # Emit raw text log
                    context.emit_output_sync(f"Saving {file_name} chunk {i+1}...\n")

        # Offload to thread so the event loop isn't blocked
        await asyncio.to_thread(_perform_download)

        return ToolResult(output=f"Successfully downloaded {file_name}")
```

---

## 6. Frontend Rendering Contract

The UI handles `TOOL_OUTPUT` and `TOOL_PROGRESS` events automatically.

- **`TOOL_OUTPUT`**: Appended to the `liveOutput` field of the tool card. The UI will display it in a scrollable `<pre>` block with a terminal icon. If `stream="stderr"`, the text is rendered in red.
- **`TOOL_PROGRESS`**: Rendered as a progress bar above the output. If `current` and `total` are present, a percentage bar is drawn. If only `phase` is provided, it displays as text.

### Event Payload Structure (Internal)
When you call `emit_output`, the frontend eventually receives an SSE event like this:
```json
{
  "type": "tool_output",
  "step": 1,
  "data": {
    "tool_call_id": "call_abc123",
    "tool_name": "live_ping",
    "stream": "stdout",
    "chunk": "64 bytes from 8.8.8.8: icmp_seq=1 ttl=119 time=14.5 ms\n",
    "sequence": 1,
    "attempt": 1
  }
}
```

### Retries and Sequences
If a tool fails and the framework retries it, the `attempt` number increments. The frontend uses this to **clear the previous attempt's output** from the screen.

You do not need to manage sequences or retries yourself—the framework's `ToolExecutionContext` automatically assigns these numbers.

---

## 7. Best Practices

1. **Always check `if context is not None`**: Never assume the feature is enabled.
2. **Flush carefully**: If using `subprocess`, ensure the child process flushes its stdout (e.g., `print(..., flush=True)` in Python, or `stdbuf -oL` in bash).
3. **Do not block the event loop**: If reading a subprocess in an async tool, use `await stream.read(4096)` rather than `stream.readline()` to avoid deadlocks on Windows.
4. **Do not fabricate progress**: Only emit data that the underlying library or subprocess actually produces. If a library does not report progress, simply omit `emit_progress`. The UI will gracefully display a simple loading spinner.
5. **Respect the Stream Type**: Pass `stream="stderr"` for errors and warnings so the UI can color them red for the user.
