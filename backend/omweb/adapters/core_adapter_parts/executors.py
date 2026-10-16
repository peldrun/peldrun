# backend/omweb/adapters/core_adapter_parts/executors.py
"""
Real Tool Execution Factory for the Web-to-Core Adapter Layer.

Constructs workspace-bound async tool executors bridging PELDRUN Web runtime
requests to canonical peldrun-core tool implementations.

Key guarantees for web_search (Phase 4 — SR-401..SR-405):
- Preserves the structured Core ToolResult (is_error, exit_code, metadata, artifacts)
  instead of flattening output into a plain string.
- Never injects a hard-coded default of 5 when max_results is omitted (passes None).
- Eliminates the legacy direct DuckDuckGo Lite scraping fallback and obsolete
  "No concise web snippets" check so SearchProviderFactory is the single policy owner.
- Propagates run_id, job_id, session_id, chat_id, and project_id to WebSearchTool
  for run-scoped circuit breaker isolation and usage accounting.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, Optional, Union

from peldrun.tools.builtins.file_ops import FileOpsTool
from peldrun.tools.builtins.isolated_shell import IsolatedShellTool as IsolatedPythonTool
from peldrun.tools.builtins.shell_exec import ShellExecTool
from peldrun.tools.builtins.str_replace_editor import StrReplaceEditor as StrReplaceEditorTool
from peldrun.tools.builtins.web_search import WebSearchTool
from peldrun.tools.contract import ToolExecutionPolicy, ToolResult

from .terminal_utils import sanitize_terminal_output

logger = logging.getLogger("omweb.adapters.executors")

WorkspacePathType = Optional[Union[Path, str]]


def _resolve_workspace_path(workspace_dir: WorkspacePathType) -> Path:
    """Normalize workspace_dir to an existing resolved Path."""
    if workspace_dir is None:
        resolved = Path.cwd().resolve()
    elif isinstance(workspace_dir, Path):
        resolved = workspace_dir.resolve()
    else:
        resolved = Path(str(workspace_dir)).resolve()

    resolved.mkdir(parents=True, exist_ok=True)
    return resolved


def _ensure_tool_result(raw: Any, *, default_error_code: Optional[str] = None) -> ToolResult:
    """
    Ensure the returned object is a canonical Core ToolResult while preserving
    any existing ToolResult fields (is_error, exit_code, metadata, artifacts).

    [FIX-PY-EXEC] Also surfaces stderr (stored by IsolatedShellTool in the
    pydantic 'extra' field named `error`) as the `output` when the process
    failed and stdout is empty. This makes the actual failure reason visible
    to the caller instead of an empty string.
    """
    if isinstance(raw, ToolResult):
        # Surface captured stderr into output when the process failed.
        try:
            extra = getattr(raw, "__pydantic_extra__", None) or {}
            err_text = extra.get("error") if isinstance(extra, dict) else None
        except Exception:
            err_text = None

        if err_text and not str(raw.output or "").strip():
            raw.output = str(err_text)

        # Align is_error with exit_code: if the process exited non-zero
        # but the result was not explicitly flagged, flag it now.
        if raw.exit_code != 0 and not raw.is_error:
            raw.is_error = True

        # Attach the default error code when one was requested and none
        # is already present.
        if raw.is_error and default_error_code and "error_code" not in raw.metadata:
            raw.metadata["error_code"] = default_error_code

        return raw


    if isinstance(raw, dict):
        is_err = bool(raw.get("is_error") or raw.get("error"))
        exit_code = int(raw.get("exit_code", 1 if is_err else 0))
        output_val = raw.get("output")
        if output_val is None:
            output_val = raw.get("error") or ""
        meta = dict(raw.get("metadata") or {})
        if is_err and default_error_code and "error_code" not in meta:
            meta["error_code"] = default_error_code
        return ToolResult(
            output=str(output_val),
            exit_code=exit_code,
            is_error=is_err,
            metadata=meta,
            artifacts=list(raw.get("artifacts") or []),
        )

    return ToolResult(
        output="" if raw is None else str(raw),
        exit_code=0,
        is_error=False,
    )


async def _invoke_file_ops(
    tool: FileOpsTool,
    *,
    operation: str,
    file_path: str,
    content: str = "",
    mode: str = "w",
    **extra: Any,
) -> Any:
    """
    Defensive invocation of FileOpsTool across possible interface shapes:
    - dedicated methods: read()/write(), read_file()/write_file(), load()/save()
    - unified aexecute(action=..., path=..., content=...)
    - unified aexecute(command=..., path=..., content=...)
    """
    if operation not in ("read", "write"):
        raise ValueError(f"Unsupported file op: {operation!r}")

    candidate_methods = {
        "read": ("read", "read_file", "load", "get"),
        "write": ("write", "write_file", "save", "put"),
    }[operation]

    # Path 1: dedicated methods on the tool instance
    for name in candidate_methods:
        method = getattr(tool, name, None)
        if callable(method):
            sig = inspect.signature(method)
            kwargs: Dict[str, Any] = {}
            for p in sig.parameters:
                if p == "self":
                    continue
                if p in ("file_path", "path", "filename", "name"):
                    kwargs[p] = file_path
                elif p in ("content", "text", "data", "value"):
                    kwargs[p] = content
                elif p == "mode":
                    kwargs[p] = mode
            kwargs.update(extra)
            res = method(**kwargs)
            if inspect.isawaitable(res):
                res = await res
            return res

    # Path 2: unified aexecute/execute with detected parameters
    executor = getattr(tool, "aexecute", None) or getattr(tool, "execute", None)
    if callable(executor):
        sig = inspect.signature(executor)
        kwargs = {}
        for p in sig.parameters:
            if p == "self":
                continue
            if p in ("file_path", "path", "filename", "name"):
                kwargs[p] = file_path
            elif p in ("content", "text", "data"):
                kwargs[p] = content
            elif p == "mode":
                kwargs[p] = mode
            elif p in ("action", "operation", "command", "op"):
                kwargs[p] = operation
        kwargs.update(extra)
        res = executor(**kwargs)
        if inspect.isawaitable(res):
            res = await res
        return res

    exposed = [a for a in dir(tool) if not a.startswith("_") and callable(getattr(tool, a, None))]
    raise AttributeError(
        f"FileOpsTool exposes no usable read/write interface. Candidates: {exposed}"
    )


class RealToolExecutionFactory:
    """Factory creating real workspace-bound async executors for Core tools."""

    @staticmethod
    def create_python_executor(
        workspace_dir: WorkspacePathType = None,
        timeout: float = 60.0,
        **kwargs: Any,
    ) -> Callable[..., Awaitable[ToolResult]]:
        """
        Create an async executor for `python_execute` bound to workspace_dir.

        [FIX-PY-EXEC] The web manifest `python_execute.json` declares `code`
        as its input field, while the underlying IsolatedShellTool expects a
        `command` string. This adapter bridges the mismatch by writing the
        Python source to a hidden temporary file inside the workspace and
        invoking it via the current Python interpreter.

        Design notes:
        - A temp file is used instead of `python -c "..."` because inline
          code is subject to cross-platform shell escaping hazards
          (Windows cmd.exe and POSIX sh have incompatible quoting rules).
        - The temp file is named with a leading dot (`.peldrun_py_*.py`)
          so it is automatically excluded from artifact detection, which
          skips any path component starting with a dot.
        - Using `sys.executable` guarantees the same interpreter that runs
          the backend, so all installed packages are available.
        - On Windows, the entire shell command is wrapped in an extra pair
          of quotes to correctly handle executable paths that contain
          spaces (e.g. `C:\\Program Files\\Python312\\python.exe`).
        """
        import sys
        import uuid

        ws_path = _resolve_workspace_path(workspace_dir)
        py_tool = IsolatedPythonTool(workspace_root=str(ws_path))
        python_exe = sys.executable or "python"

        async def _execute_python(
            code: str = "",
            timeout_seconds: Optional[float] = None,
            **call_kwargs: Any,
        ) -> ToolResult:
            if not code and "script" in call_kwargs:
                code = str(call_kwargs.pop("script") or "")

            if not code.strip():
                return ToolResult(
                    output="Python Execution Error: empty code payload.",
                    exit_code=1,
                    is_error=True,
                    metadata={"error_code": "PYTHON_EMPTY_CODE"},
                )

            eff_timeout = float(
                timeout_seconds if timeout_seconds is not None else timeout
            )

            # Give the shell tool a slightly larger budget than the requested
            # timeout so its own outer wait_for does not fire first.
            try:
                py_tool.set_execution_policy(
                    ToolExecutionPolicy(
                        max_attempts=1,
                        timeout_seconds=eff_timeout + 5.0,
                        retryable=False,
                        idempotent=False,
                        side_effects=True,
                    )
                )
            except Exception:
                # Non-fatal: if policy mutation fails, fall back to defaults.
                pass

            # Hidden filename → skipped by artifact detection (leading dot).
            script_name = f".peldrun_py_exec_{uuid.uuid4().hex[:8]}.py"
            script_path = ws_path / script_name

            try:
                script_path.write_text(code, encoding="utf-8")
            except Exception as write_err:
                return ToolResult(
                    output=f"Python Execution Error: failed to write script: {write_err}",
                    exit_code=1,
                    is_error=True,
                    metadata={"error_code": "PYTHON_WRITE_FAILED"},
                )

            try:
                # [FIX-PY-EXEC] Both cmd.exe and POSIX sh handle the form
                #   "path/to/python" "script.py"
                # natively. An additional outer pair of quotes (as was
                # previously added on Windows) makes cmd.exe misparse the
                # executable name and return exit_code=1 without running
                # anything.
                command = f'"{python_exe}" "{script_name}"'

                res = await py_tool.aexecute(
                    command=command,
                    timeout=eff_timeout,
                    **call_kwargs,
                )
                tool_res = _ensure_tool_result(
                    res, default_error_code="PYTHON_EXECUTION_ERROR"
                )
                if tool_res.output:
                    tool_res.output = sanitize_terminal_output(str(tool_res.output))
                return tool_res
            except Exception as exc:
                logger.warning(
                    "Python tool execution exception in %s: %s", ws_path, exc
                )
                return ToolResult(
                    output=f"Python Execution Error: {exc}",
                    exit_code=1,
                    is_error=True,
                    metadata={"error_code": "PYTHON_EXECUTION_EXCEPTION"},
                )
            finally:
                # Best-effort cleanup — never let this mask the real result.
                try:
                    script_path.unlink(missing_ok=True)
                except Exception:
                    pass

        return _execute_python

    @staticmethod
    def create_bash_executor(
        workspace_dir: WorkspacePathType = None,
        timeout: float = 60.0,
        **kwargs: Any,
    ) -> Callable[..., Awaitable[ToolResult]]:
        """Create an async executor for bash / shell commands bound to workspace_dir."""
        ws_path = _resolve_workspace_path(workspace_dir)
        shell_tool = ShellExecTool(workspace_root=str(ws_path))

        async def _execute_bash(
            command: str = "",
            timeout_seconds: Optional[float] = None,
            **call_kwargs: Any,
        ) -> ToolResult:
            if not command and "cmd" in call_kwargs:
                command = str(call_kwargs.pop("cmd") or "")
            eff_timeout = float(timeout_seconds if timeout_seconds is not None else timeout)
            try:
                res = await shell_tool.aexecute(
                    command=command,
                    timeout=eff_timeout,
                    **call_kwargs,
                )
                tool_res = _ensure_tool_result(res, default_error_code="BASH_EXECUTION_ERROR")
                if tool_res.output:
                    tool_res.output = sanitize_terminal_output(str(tool_res.output))
                return tool_res
            except Exception as exc:
                logger.warning("Bash tool execution exception in %s: %s", ws_path, exc)
                return ToolResult(
                    output=f"Shell Execution Error: {exc}",
                    exit_code=1,
                    is_error=True,
                    metadata={"error_code": "BASH_EXECUTION_EXCEPTION"},
                )

        return _execute_bash

    @staticmethod
    def create_file_saver_executor(
        workspace_dir: WorkspacePathType = None,
        **kwargs: Any,
    ) -> Callable[..., Awaitable[ToolResult]]:
        """Create an async executor for file_saver bound to workspace_dir."""
        ws_path = _resolve_workspace_path(workspace_dir)
        file_ops = FileOpsTool(workspace_root=str(ws_path))

        async def _execute_file_saver(
            file_path: Optional[str] = None,
            content: str = "",
            mode: str = "w",
            **call_kwargs: Any,
        ) -> ToolResult:
            target_path = (
                file_path
                or call_kwargs.pop("path", None)
                or call_kwargs.pop("filename", None)
                or ""
            )
            if not str(target_path).strip():
                return ToolResult(
                    output="File Saver Error: file_path is required.",
                    exit_code=1,
                    is_error=True,
                    metadata={"error_code": "FILE_SAVER_MISSING_PATH"},
                )
            try:
                res = await _invoke_file_ops(
                    file_ops,
                    operation="write",
                    file_path=str(target_path),
                    content=content,
                    mode=mode,
                    **call_kwargs,
                )
                return _ensure_tool_result(res, default_error_code="FILE_SAVER_ERROR")
            except Exception as exc:
                logger.warning("File saver exception in %s: %s", ws_path, exc)
                return ToolResult(
                    output=f"File Saver Error: {exc}",
                    exit_code=1,
                    is_error=True,
                    metadata={"error_code": "FILE_SAVER_EXCEPTION"},
                )

        return _execute_file_saver

    @staticmethod
    def create_file_reader_executor(
        workspace_dir: WorkspacePathType = None,
        **kwargs: Any,
    ) -> Callable[..., Awaitable[ToolResult]]:
        """Create an async executor for reading workspace files bound to workspace_dir."""
        ws_path = _resolve_workspace_path(workspace_dir)
        file_ops = FileOpsTool(workspace_root=str(ws_path))

        async def _execute_file_reader(
            file_path: Optional[str] = None,
            **call_kwargs: Any,
        ) -> ToolResult:
            target_path = (
                file_path
                or call_kwargs.pop("path", None)
                or call_kwargs.pop("filename", None)
                or ""
            )
            if not str(target_path).strip():
                return ToolResult(
                    output="File Read Error: file_path is required.",
                    exit_code=1,
                    is_error=True,
                    metadata={"error_code": "FILE_READER_MISSING_PATH"},
                )
            try:
                res = await _invoke_file_ops(
                    file_ops,
                    operation="read",
                    file_path=str(target_path),
                    **call_kwargs,
                )
                return _ensure_tool_result(res, default_error_code="FILE_READER_ERROR")
            except Exception as exc:
                return ToolResult(
                    output=f"File Read Error: {exc}",
                    exit_code=1,
                    is_error=True,
                    metadata={"error_code": "FILE_READER_EXCEPTION"},
                )

        return _execute_file_reader

    @staticmethod
    def create_str_replace_editor_executor(
        workspace_dir: WorkspacePathType = None,
        **kwargs: Any,
    ) -> Callable[..., Awaitable[ToolResult]]:
        """Create an async executor for str_replace_editor bound to workspace_dir."""
        ws_path = _resolve_workspace_path(workspace_dir)
        editor_tool = StrReplaceEditorTool(workspace_root=str(ws_path))

        async def _execute_str_replace_editor(
            command: str = "view",
            path: str = "",
            file_text: Optional[str] = None,
            old_str: Optional[str] = None,
            new_str: Optional[str] = None,
            insert_line: Optional[int] = None,
            view_range: Optional[list[int]] = None,
            **call_kwargs: Any,
        ) -> ToolResult:
            if not path and "file_path" in call_kwargs:
                path = str(call_kwargs.pop("file_path") or "")
            try:
                res = await editor_tool.aexecute(
                    command=command,
                    path=path,
                    file_text=file_text,
                    old_str=old_str,
                    new_str=new_str,
                    insert_line=insert_line,
                    view_range=view_range,
                    **call_kwargs,
                )
                return _ensure_tool_result(res, default_error_code="STR_REPLACE_EDITOR_ERROR")
            except Exception as exc:
                logger.warning("str_replace_editor exception in %s: %s", ws_path, exc)
                return ToolResult(
                    output=f"Editor Error: {exc}",
                    exit_code=1,
                    is_error=True,
                    metadata={"error_code": "STR_REPLACE_EDITOR_EXCEPTION"},
                )

        return _execute_str_replace_editor

    @staticmethod
    def create_web_search_executor(
        workspace_dir: WorkspacePathType = None,
        *,
        search_config: Optional[Dict[str, Any]] = None,
        run_id: Optional[str] = None,
        job_id: Optional[str] = None,
        session_id: Optional[str] = None,
        chat_id: Optional[str] = None,
        project_id: Optional[str] = None,
        **factory_kwargs: Any,
    ) -> Callable[..., Awaitable[ToolResult]]:
        """
        Create an async executor for web_search backed exclusively by Core WebSearchTool
        and SearchProviderFactory.

        Engineering guarantees:
        1. Returns the original Core ToolResult (preserving is_error, exit_code, metadata).
        2. Preserves max_results=None when omitted by the tool call so user settings
           (search.default_max_results and search.<provider>.max_results) govern execution.
        3. Never performs a hidden direct DuckDuckGo Lite HTTP request outside
           SearchProviderFactory policy.
        4. Propagates execution context IDs (run_id, job_id, session_id, chat_id, project_id)
           for run-scoped circuit breaker isolation.
        """
        ws_root_str = str(_resolve_workspace_path(workspace_dir)) if workspace_dir is not None else None
        core_search = WebSearchTool(
            workspace_root=ws_root_str,
            search_config=search_config,
            run_id=run_id,
            job_id=job_id,
            session_id=session_id,
            chat_id=chat_id,
            project_id=project_id,
        )

        async def _execute_web_search(
            query: Optional[str] = None,
            max_results: Optional[int] = None,
            **call_kwargs: Any,
        ) -> ToolResult:
            raw_query = query
            if raw_query is None:
                for alias in ("q", "search_query", "input"):
                    if alias in call_kwargs:
                        raw_query = call_kwargs.pop(alias)
                        break

            clean_q = str(raw_query or "").strip()
            if not clean_q:
                return ToolResult(
                    output="Search Error: Query cannot be empty.",
                    exit_code=1,
                    is_error=True,
                    metadata={
                        "error_code": "EMPTY_SEARCH_QUERY",
                        "requested_max_results": max_results,
                    },
                )

            # Forward per-call context overrides if supplied, otherwise use factory-bound context
            eff_run_id = call_kwargs.pop("run_id", None) or run_id
            eff_job_id = call_kwargs.pop("job_id", None) or job_id
            eff_session_id = call_kwargs.pop("session_id", None) or session_id
            eff_chat_id = call_kwargs.pop("chat_id", None) or chat_id
            eff_project_id = call_kwargs.pop("project_id", None) or project_id
            eff_search_cfg = call_kwargs.pop("search_config", None) or search_config

            try:
                res = await core_search.aexecute(
                    query=clean_q,
                    max_results=max_results,
                    search_config=eff_search_cfg,
                    run_id=eff_run_id,
                    job_id=eff_job_id,
                    session_id=eff_session_id,
                    chat_id=eff_chat_id,
                    project_id=eff_project_id,
                    **call_kwargs,
                )
                return _ensure_tool_result(res, default_error_code="SEARCH_EXECUTION_ERROR")
            except Exception as exc:
                logger.warning("Core WebSearchTool execution error for query %r: %s", clean_q, exc)
                return ToolResult(
                    output=f"Search Error: {exc}",
                    exit_code=1,
                    is_error=True,
                    metadata={
                        "error_code": "WEB_SEARCH_EXECUTOR_EXCEPTION",
                        "requested_max_results": max_results,
                    },
                )

        return _execute_web_search

    @staticmethod
    def create_browser_use_executor(
        workspace_dir: WorkspacePathType = None,
        **kwargs: Any,
    ) -> Callable[..., Awaitable[ToolResult]]:
        """Create an async executor for browser_use bound to workspace_dir."""
        ws_path = _resolve_workspace_path(workspace_dir)

        async def _execute_browser_use(
            action: str = "navigate",
            url: Optional[str] = None,
            **call_kwargs: Any,
        ) -> ToolResult:
            target_url = (url or call_kwargs.get("target_url") or "").strip()
            if action in ("navigate", "open", "extract", "fetch") and target_url:
                try:
                    import httpx
                    from bs4 import BeautifulSoup

                    async with httpx.AsyncClient(
                        timeout=20.0,
                        follow_redirects=True,
                        headers={"User-Agent": "PELDRUN-BrowserAgent/1.0"},
                    ) as client:
                        resp = await client.get(target_url)
                        resp.raise_for_status()
                        soup = BeautifulSoup(resp.text, "html.parser")
                        for tag in soup(["script", "style", "noscript"]):
                            tag.decompose()
                        title = soup.title.string.strip() if soup.title and soup.title.string else target_url
                        text_body = " ".join(soup.get_text(separator=" ").split())[:8000]
                        return ToolResult(
                            output=f"Page Title: {title}\nURL: {target_url}\n\nContent:\n{text_body}",
                            exit_code=0,
                            is_error=False,
                            metadata={"url": target_url, "action": action, "workspace": str(ws_path)},
                        )
                except Exception as exc:
                    return ToolResult(
                        output=f"Browser action '{action}' failed for {target_url!r}: {exc}",
                        exit_code=1,
                        is_error=True,
                        metadata={"url": target_url, "action": action, "error_code": "BROWSER_ACTION_FAILED"},
                    )

            return ToolResult(
                output=f"Browser action '{action}' requires a valid target URL.",
                exit_code=1,
                is_error=True,
                metadata={"action": action, "error_code": "INVALID_BROWSER_ARGS"},
            )

        return _execute_browser_use

    @staticmethod
    def create_ask_human_executor(
        workspace_dir: WorkspacePathType = None,
        human_input_callback: Optional[Callable[..., Awaitable[str]]] = None,
        **kwargs: Any,
    ) -> Callable[..., Awaitable[ToolResult]]:
        """Create an async executor for ask_human bridged to human_input_callback or Core HumanInputTool."""
        ws_path = _resolve_workspace_path(workspace_dir)

        async def _execute_ask_human(
            question: str = "",
            **call_kwargs: Any,
        ) -> ToolResult:
            prompt_q = (
                question
                or call_kwargs.pop("prompt", None)
                or call_kwargs.pop("query", None)
                or ""
            ).strip()
            if not prompt_q:
                return ToolResult(
                    output="Ask Human Error: Question cannot be empty.",
                    exit_code=1,
                    is_error=True,
                    metadata={"error_code": "EMPTY_HUMAN_QUESTION"},
                )

            if callable(human_input_callback):
                try:
                    ans = await human_input_callback(prompt_q, **call_kwargs)
                    return ToolResult(
                        output=str(ans),
                        exit_code=0,
                        is_error=False,
                    )
                except Exception as exc:
                    return ToolResult(
                        output=f"Ask Human Error: {exc}",
                        exit_code=1,
                        is_error=True,
                        metadata={"error_code": "HUMAN_INPUT_CALLBACK_FAILED"},
                    )

            try:
                from peldrun.tools.builtins.human_input import HumanInputTool

                tool = HumanInputTool(workspace_root=str(ws_path))
                res = await tool.aexecute(question=prompt_q, **call_kwargs)
                return _ensure_tool_result(res, default_error_code="HUMAN_INPUT_ERROR")
            except Exception as exc:
                return ToolResult(
                    output=f"Ask Human Error: {exc}",
                    exit_code=1,
                    is_error=True,
                    metadata={"error_code": "HUMAN_INPUT_TOOL_FAILED"},
                )

        return _execute_ask_human


__all__ = ["RealToolExecutionFactory"]
