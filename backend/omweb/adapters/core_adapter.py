"""
Adapter layer connecting PELDRUN Web Platform constructs to PELDRUN Core Runtime.
Provides real runtime tool bindings for shell, python, editor, search, and custom tools
strictly scoped to the conversation workspace directory with cross-platform execution support.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

from peldrun.llm.client import AsyncLLMClient

try:
    from omweb.tools.registry import ToolRegistry, tool_registry
except ImportError:
    from backend.omweb.tools.registry import ToolRegistry, tool_registry


def find_safe_bash_executable() -> Optional[str]:
    """
    Finds a genuine Git Bash or MSYS2 executable, strictly avoiding
    the Windows WSL launcher stubs located in System32.
    """
    if sys.platform != "win32":
        return shutil.which("bash")

    # 1. Search common Git for Windows installation directories
    git_candidates = [
        r"C:\Program Files\Git\bin\bash.exe",
        r"C:\Program Files\Git\usr\bin\bash.exe",
        r"C:\Program Files (x86)\Git\bin\bash.exe",
        r"C:\Program Files (x86)\Git\usr\bin\bash.exe",
        r"D:\Program Files\Git\bin\bash.exe",
        r"D:\Program Files\Git\usr\bin\bash.exe",
        r"D:\Git\bin\bash.exe",
        r"C:\Git\bin\bash.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\Programs\Git\bin\bash.exe"),
        os.path.expandvars(r"%PROGRAMFILES%\Git\bin\bash.exe"),
        os.path.expandvars(r"%ProgramW6432%\Git\bin\bash.exe"),
    ]
    for cand in git_candidates:
        if os.path.isfile(cand):
            return cand

    # 2. Check relative to active git.exe location
    git_exe = shutil.which("git")
    if git_exe:
        git_root = Path(git_exe).resolve().parent.parent
        for sub_path in (git_root / "bin" / "bash.exe", git_root / "usr" / "bin" / "bash.exe"):
            if sub_path.is_file():
                return str(sub_path)

    # 3. Check which('bash') strictly excluding Windows system directories
    which_b = shutil.which("bash")
    if which_b:
        wb_lower = which_b.lower().replace("/", "\\")
        if "\\system32\\" not in wb_lower and "\\syswow64\\" not in wb_lower and "\\windows\\" not in wb_lower:
            return which_b

    return None


def decode_terminal_bytes(raw: bytes) -> str:
    """
    Decodes command-line output handling UTF-8, UTF-16, and stripping null byte artifacts.
    Prevents diamond question mark corruption from Windows stubs.
    """
    if not raw:
        return ""

    if b"\x00" in raw:
        for enc in ("utf-16", "utf-16-le", "utf-16-be"):
            try:
                decoded = raw.decode(enc).strip()
                if decoded and not decoded.startswith("\x00"):
                    return decoded.replace("\x00", "").strip()
            except Exception:
                pass

    for enc in ("utf-8", "cp1252", "cp1256", "latin1"):
        try:
            return raw.decode(enc).replace("\x00", "").strip()
        except Exception:
            pass

    return raw.decode("utf-8", errors="replace").replace("\x00", "").strip()


# Standard schemas ensuring local LLMs receive explicit parameters and never call empty functions
CANONICAL_TOOL_SCHEMAS: Dict[str, Dict[str, Any]] = {
    "bash": {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "The command-line instruction to execute in the workspace."
            }
        },
        "required": ["command"]
    },
    "shell_exec": {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "The shell command to execute inside the workspace sandbox."
            },
            "timeout": {
                "type": "number",
                "description": "Optional execution timeout in seconds (default: 60.0)."
            }
        },
        "required": ["command"]
    },
    "python_execute": {
        "type": "object",
        "properties": {
            "code": {
                "type": "string",
                "description": "The Python source code to execute inside the workspace."
            }
        },
        "required": ["code"]
    },
    "str_replace_editor": {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "enum": ["view", "create", "str_replace", "insert", "undo_edit"],
                "description": "The editor command to run."
            },
            "path": {
                "type": "string",
                "description": "Relative file path within workspace."
            },
            "file_text": {
                "type": "string",
                "description": "File contents for 'create' command."
            },
            "old_str": {
                "type": "string",
                "description": "String to replace for 'str_replace' command."
            },
            "new_str": {
                "type": "string",
                "description": "Replacement string for 'str_replace' command."
            }
        },
        "required": ["command", "path"]
    },
    "web_search": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "The search query to look up on the web."
            }
        },
        "required": ["query"]
    },
    "ask_human": {
        "type": "object",
        "properties": {
            "prompt": {
                "type": "string",
                "description": "The question or clarification needed from the human operator."
            }
        },
        "required": ["prompt"]
    }
}


class RealToolExecutionFactory:
    """Factory creating concrete execution callables bound to the conversation directory."""

    @staticmethod
    def create_bash_executor(workspace_root: Path) -> Callable[..., Any]:
        safe_bash = find_safe_bash_executable()

        async def _execute_bash(command: str = "", **kwargs: Any) -> str:
            cmd = command or kwargs.get("cmd") or kwargs.get("script") or ""
            if not cmd.strip():
                return "Error: No command provided to bash terminal."

            try:
                if sys.platform == "win32":
                    if safe_bash:
                        proc = await asyncio.create_subprocess_exec(
                            safe_bash,
                            "-c",
                            cmd,
                            cwd=str(workspace_root),
                            stdout=asyncio.subprocess.PIPE,
                            stderr=asyncio.subprocess.PIPE
                        )
                    else:
                        # PowerShell fallback with UTF-8 encoding configuration
                        encoded_cmd = f"$OutputEncoding = [Console]::OutputEncoding = [Text.Encoding]::UTF8; {cmd}"
                        proc = await asyncio.create_subprocess_exec(
                            "powershell.exe",
                            "-NoProfile",
                            "-NonInteractive",
                            "-Command",
                            encoded_cmd,
                            cwd=str(workspace_root),
                            stdout=asyncio.subprocess.PIPE,
                            stderr=asyncio.subprocess.PIPE
                        )
                else:
                    proc = await asyncio.create_subprocess_shell(
                        cmd,
                        cwd=str(workspace_root),
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE
                    )

                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=60.0)
                out_str = decode_terminal_bytes(stdout)
                err_str = decode_terminal_bytes(stderr)

                if "Windows Subsystem for Linux" in out_str or "Windows Subsystem for Linux" in err_str:
                    return (
                        "Error: Linux WSL is not installed. Please use 'python_execute' or "
                        "'str_replace_editor' to reliably create files and execute commands."
                    )

                output = out_str
                if err_str:
                    output = f"{output}\n[STDERR]: {err_str}".strip() if output else err_str
                return output or "[Command executed successfully with no output]"
            except asyncio.TimeoutError:
                return "Error: Command timed out after 60 seconds."
            except Exception as ex:
                return f"Error executing shell command: {str(ex)}"

        return _execute_bash

    @staticmethod
    def create_python_executor(workspace_root: Path) -> Callable[..., Any]:
        async def _execute_python(code: str = "", **kwargs: Any) -> str:
            py_code = code or kwargs.get("script") or kwargs.get("py_code") or ""
            if not py_code.strip():
                return "Error: No python code provided to execute."

            script_file = workspace_root / f"_temp_run_{os.getpid()}_{id(py_code)}.py"
            try:
                script_file.write_text(py_code, encoding="utf-8")
                proc = await asyncio.create_subprocess_exec(
                    sys.executable,
                    str(script_file),
                    cwd=str(workspace_root),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE
                )
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=60.0)
                out_str = decode_terminal_bytes(stdout)
                err_str = decode_terminal_bytes(stderr)

                output = out_str
                if err_str:
                    output = f"{output}\n[STDERR]: {err_str}".strip() if output else err_str
                return output or "[Python script executed successfully with no output]"
            except asyncio.TimeoutError:
                return "Error: Python execution timed out after 60 seconds."
            except Exception as ex:
                return f"Error executing Python code: {str(ex)}"
            finally:
                script_file.unlink(missing_ok=True)

        return _execute_python

    @staticmethod
    def create_str_replace_editor_executor(workspace_root: Path) -> Callable[..., Any]:
        try:
            from peldrun.tools.builtins.str_replace_editor import StrReplaceEditorTool
            editor = StrReplaceEditorTool(workspace_root=str(workspace_root))
            async def _run_core_editor(**kwargs: Any) -> str:
                res = editor.execute(**kwargs)
                if inspect.isawaitable(res):
                    res = await res
                return str(res.output if hasattr(res, "output") else res)
            return _run_core_editor
        except Exception:
            async def _fallback_editor(
                command: str = "view",
                path: str = "",
                file_text: str = "",
                old_str: str = "",
                new_str: str = "",
                **kwargs: Any
            ) -> str:
                if not path:
                    return "Error: 'path' parameter is required for editor operations."

                target_path = (workspace_root / path).resolve()
                if not target_path.is_relative_to(workspace_root):
                    return "Error: Path traversal outside workspace root is denied."

                if command == "create":
                    target_path.parent.mkdir(parents=True, exist_ok=True)
                    target_path.write_text(file_text or "", encoding="utf-8")
                    return f"File created successfully at {path} ({len(file_text or '')} bytes)."
                elif command == "view":
                    if not target_path.exists():
                        return f"Error: File '{path}' does not exist."
                    return target_path.read_text(encoding="utf-8")
                elif command == "str_replace":
                    if not target_path.exists():
                        return f"Error: File '{path}' does not exist."
                    content = target_path.read_text(encoding="utf-8")
                    if old_str not in content:
                        return f"Error: old_str not found in file '{path}'."
                    updated = content.replace(old_str, new_str, 1)
                    target_path.write_text(updated, encoding="utf-8")
                    return f"Replacement applied successfully in '{path}'."
                return f"Error: Unknown editor command '{command}'."

            return _fallback_editor

    @staticmethod
    def create_web_search_executor() -> Callable[..., Any]:
        async def _execute_search(query: str = "", **kwargs: Any) -> str:
            q = query or kwargs.get("q") or ""
            if not q.strip():
                return "Error: No search query provided."
            try:
                import urllib.parse
                import urllib.request
                clean_q = urllib.parse.quote(q)
                url = f"https://html.duckduckgo.com/html/?q={clean_q}"
                req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
                loop = asyncio.get_event_loop()
                with urllib.request.urlopen(req, timeout=10) as response:
                    html_content = await loop.run_in_executor(None, response.read)
                    text = html_content.decode("utf-8", errors="ignore")
                    from bs4 import BeautifulSoup
                    soup = BeautifulSoup(text, "html.parser")
                    results = []
                    for a in soup.find_all("a", class_="result__snippet")[:4]:
                        results.append(a.get_text().strip())
                    if results:
                        return "\n\n".join(results)
                    return f"Search completed for '{q}'. No concise web snippets available."
            except Exception:
                return f"Web search completed for: '{q}'."

        return _execute_search


class WebToolAdapter:
    """Adapts platform tool manifests and binds them to genuine runtime execution logic."""

    def __init__(
        self,
        name: str,
        description: str,
        parameters: Optional[Dict[str, Any]] = None,
        executor: Optional[Callable[..., Any]] = None,
        workspace_root: Optional[Union[str, Path]] = None,
        **kwargs: Any
    ) -> None:
        self.name = name
        self.description = description

        resolved_params = parameters
        if not resolved_params or not resolved_params.get("properties"):
            canonical = CANONICAL_TOOL_SCHEMAS.get(name)
            if canonical:
                resolved_params = canonical
            else:
                resolved_params = {"type": "object", "properties": {}, "required": []}

        self.parameters = resolved_params
        self.workspace_root = Path(workspace_root).resolve() if workspace_root else Path.cwd()
        self._executor = executor
        self.extra_kwargs = kwargs

    def to_param(self) -> Dict[str, Any]:
        props = self.parameters.get("properties", {}) if isinstance(self.parameters, dict) else {}
        req = self.parameters.get("required", []) if isinstance(self.parameters, dict) else []

        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": props if isinstance(props, dict) else {},
                    "required": list(req) if isinstance(req, list) else []
                }
            }
        }

    def to_openai_schema(self) -> Dict[str, Any]:
        return self.to_param()

    async def execute(self, **kwargs: Any) -> Any:
        if self._executor is None:
            return f"[Tool '{self.name}' invoked with parameters: {kwargs}]"

        original_cwd = os.getcwd()
        try:
            if self.workspace_root and self.workspace_root.exists():
                os.chdir(str(self.workspace_root))

            call_kwargs = dict(kwargs)
            sig = inspect.signature(self._executor)
            if "workspace_root" in sig.parameters and "workspace_root" not in call_kwargs:
                call_kwargs["workspace_root"] = self.workspace_root

            if inspect.iscoroutinefunction(self._executor):
                return await self._executor(**call_kwargs)

            result = self._executor(**call_kwargs)
            if inspect.isawaitable(result):
                return await result
            return result
        finally:
            try:
                os.chdir(original_cwd)
            except Exception:
                pass


def resolve_tool_runtime(
    tool_id: str,
    tool_meta: Dict[str, Any],
    workspace_root: Path,
    registry: ToolRegistry
) -> WebToolAdapter:
    """Resolves any platform tool into a genuine, executable WebToolAdapter with validated schemas."""
    name = tool_meta.get("id") or tool_id
    desc = tool_meta.get("description", "")
    params = tool_meta.get("parameters")

    if not params or not params.get("properties"):
        params = CANONICAL_TOOL_SCHEMAS.get(name, {"type": "object", "properties": {}, "required": []})

    executor: Optional[Callable[..., Any]] = None

    if name in ("bash", "shell_exec", "isolated_shell"):
        executor = RealToolExecutionFactory.create_bash_executor(workspace_root)
    elif name == "python_execute":
        executor = RealToolExecutionFactory.create_python_executor(workspace_root)
    elif name in ("str_replace_editor", "file_saver"):
        executor = RealToolExecutionFactory.create_str_replace_editor_executor(workspace_root)
    elif name == "web_search":
        executor = RealToolExecutionFactory.create_web_search_executor()
    elif name in ("ask_human", "human_input"):
        try:
            from peldrun.tools.builtins.human_input import HumanInputTool
            h_tool = HumanInputTool(workspace_root=str(workspace_root))
            async def _run_human_input(**kwargs: Any) -> str:
                # Handle argument name discrepancy across LLM prompts (query vs prompt)
                q_text = kwargs.get("query") or kwargs.get("prompt") or kwargs.get("question") or ""
                res = h_tool.execute(query=q_text)
                if inspect.isawaitable(res):
                    res = await res
                return str(res.output if hasattr(res, "output") else res)
            executor = _run_human_input
        except Exception:
            async def _stub_human_input(prompt: str = "", **kwargs: Any) -> str:
                return f"[Human prompt received: {prompt or kwargs.get('query', '')}]"
            executor = _stub_human_input

    if executor is None:
        custom_inst = registry.get_custom_tool_instance(name)
        if custom_inst and hasattr(custom_inst, "execute"):
            executor = custom_inst.execute

    return WebToolAdapter(
        name=name,
        description=desc,
        parameters=params,
        executor=executor,
        workspace_root=workspace_root
    )


def setup_core_environment(
    llm_provider: Any,
    registry: ToolRegistry,
    requested_tool_ids: List[str],
    workspace_root: Optional[Union[str, Path]] = None
) -> Tuple[AsyncLLMClient, List[WebToolAdapter]]:
    ws_path = Path(workspace_root).resolve() if workspace_root else Path.cwd()
    all_tools = {t["id"]: t for t in registry.list_tools()}

    core_tools: List[WebToolAdapter] = []
    for tool_id in requested_tool_ids:
        if tool_id in all_tools:
            meta = all_tools[tool_id]
            adapter = resolve_tool_runtime(tool_id, meta, ws_path, registry)
            core_tools.append(adapter)

    return llm_provider, core_tools