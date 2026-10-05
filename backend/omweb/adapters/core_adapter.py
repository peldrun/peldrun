"""
Adapter layer connecting PELDRUN Web Platform constructs to PELDRUN Core Runtime.
Provides real runtime tool bindings for shell, python, editor, resilient web search,
real browser inspection, and interactive human suspension loops.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import os
import re
import shutil
import sys
import urllib.parse
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

import httpx
from bs4 import BeautifulSoup

from peldrun.llm.client import AsyncLLMClient

try:
    from omweb.tools.registry import ToolRegistry, tool_registry
except ImportError:
    from backend.omweb.tools.registry import ToolRegistry, tool_registry

try:
    from omweb.agent_bridge import human_answers, human_data, current_active_job_id
except ImportError:
    human_answers: Dict[str, asyncio.Event] = {}
    human_data: Dict[str, str] = {}
    current_active_job_id: Dict[str, str] = {}


def find_safe_bash_executable() -> Optional[str]:
    """
    Finds a genuine Git Bash or MSYS2 executable, strictly avoiding
    the Windows WSL launcher stubs located in System32.
    """
    if sys.platform != "win32":
        return shutil.which("bash")

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

    git_exe = shutil.which("git")
    if git_exe:
        git_root = Path(git_exe).resolve().parent.parent
        for sub_path in (git_root / "bin" / "bash.exe", git_root / "usr" / "bin" / "bash.exe"):
            if sub_path.is_file():
                return str(sub_path)

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


# Standard schemas ensuring local and remote LLMs receive explicit parameters and never call empty functions
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
                "description": "The search query terms to look up on the live web."
            },
            "max_results": {
                "type": "integer",
                "default": 5,
                "description": "Maximum number of search results to retrieve (default: 5)."
            }
        },
        "required": ["query"]
    },
    "browser_use": {
        "type": "object",
        "properties": {
            "url": {
                "type": "string",
                "description": "The target website URL to visit, inspect, or extract content from."
            },
            "action": {
                "type": "string",
                "enum": ["navigate", "extract_content", "click", "scroll_down"],
                "default": "extract_content",
                "description": "The browser operation to perform on the target web page."
            },
            "selector": {
                "type": "string",
                "description": "Optional CSS selector or keyword to target specific elements on the page."
            }
        },
        "required": ["url"]
    },
    "ask_human": {
        "type": "object",
        "properties": {
            "prompt": {
                "type": "string",
                "description": "The question, confirmation, or clarification needed from the human user."
            },
            "input_type": {
                "type": "string",
                "enum": ["text", "confirm", "select", "multiple_choice"],
                "default": "text",
                "description": "Interaction type: 'text' (input field), 'confirm' (Yes/No buttons), 'select' (choice buttons)."
            },
            "options": {
                "type": "array",
                "items": {"type": "string"},
                "description": "List of choice buttons for the user to select from (e.g. ['Yes', 'No'] or custom options)."
            },
            "timeout_seconds": {
                "type": "integer",
                "default": 600,
                "description": "Maximum wait time in seconds for the user to respond."
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
    def create_web_search_executor(workspace_root: Optional[Path] = None) -> Callable[..., Any]:
        """
        Executes web search with direct citations and link attribution.
        First delegates to peldrun-core WebSearchTool, then falls back to resilient multi-engine scraping.
        """
        seen_queries: set = set()

        async def _execute_search(query: str = "", max_results: int = 5, **kwargs: Any) -> str:
            clean_q = (query or kwargs.get("q") or "").strip()
            if not clean_q:
                return "Error: Search query cannot be empty."

            limit = int(max_results) if max_results else 5
            norm_q = clean_q.lower()

            if norm_q in seen_queries:
                return (
                    f"Notice: You have already searched for '{clean_q}'. "
                    "Do not repeat identical searches. Please use the results already gathered, "
                    "use 'browser_use' to visit a specific URL, or formulate your final answer to the user."
                )
            seen_queries.add(norm_q)

            # 1. Attempt delegation to peldrun-core built-in tool
            try:
                from peldrun.tools.builtins.web_search import WebSearchTool
                core_search = WebSearchTool(workspace_root=str(workspace_root) if workspace_root else None)
                res = await core_search.aexecute(query=clean_q, max_results=limit)
                if res and res.output and "No concise web snippets" not in str(res.output):
                    return str(res.output)
            except Exception:
                pass

            # 2. Resilient DuckDuckGo Lite multi-engine fallback
            headers = {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/128.0.0.0 Safari/537.36"
                ),
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            }

            results: List[Dict[str, str]] = []
            try:
                async with httpx.AsyncClient(timeout=12.0, follow_redirects=True, headers=headers) as client:
                    resp = await client.post("https://lite.duckduckgo.com/lite/", data={"q": clean_q})
                    if resp.status_code == 200:
                        soup = BeautifulSoup(resp.text, "html.parser")
                        links = soup.find_all("a", class_="result-link")
                        snippets = soup.find_all("td", class_="result-snippet")
                        for idx in range(min(len(links), limit)):
                            a_tag = links[idx]
                            raw_href = a_tag.get("href", "")
                            target_url = raw_href
                            if "uddg=" in raw_href:
                                m = re.search(r"uddg=([^&]+)", raw_href)
                                if m:
                                    target_url = urllib.parse.unquote(m.group(1))

                            title = a_tag.get_text().strip()
                            snip = snippets[idx].get_text().strip() if idx < len(snippets) else ""
                            if target_url and not target_url.startswith("/"):
                                results.append({"title": title or target_url, "url": target_url, "snippet": snip})
            except Exception:
                pass

            # 3. Format structured results with full Markdown links
            if results:
                lines = [
                    f"### Verified Web Search Results for: '{clean_q}'",
                    "*(You MUST cite these sources and include their target URLs in your response)*\n"
                ]
                for i, r in enumerate(results, 1):
                    lines.append(f"**{i}. [{r['title']}]({r['url']})**\n   - **URL:** {r['url']}\n   - **Snippet:** {r['snippet']}\n")
                return "\n".join(lines).strip()

            return (
                f"Search completed for '{clean_q}'. No web snippets were returned by search engines at this moment. "
                "Notice: Do not repeat search queries. Synthesize your final answer with available knowledge, "
                "or inspect specific URLs directly via 'browser_use'."
            )

        return _execute_search

    @staticmethod
    def create_browser_executor(workspace_root: Optional[Path] = None) -> Callable[..., Any]:
        """
        Executes genuine web page inspection, extracting page titles, article headlines,
        direct URLs, and clean text content from the target website.
        """
        async def _execute_browser(
            url: str = "",
            action: str = "extract_content",
            selector: Optional[str] = None,
            **kwargs: Any
        ) -> str:
            target_url = url or kwargs.get("link") or kwargs.get("href") or ""
            if not target_url.strip():
                return "Error: 'url' parameter is required for browser_use."

            if not target_url.startswith("http://") and not target_url.startswith("https://"):
                target_url = f"https://{target_url}"

            headers = {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/128.0.0.0 Safari/537.36"
                ),
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
            }

            try:
                async with httpx.AsyncClient(timeout=18.0, follow_redirects=True, headers=headers) as client:
                    resp = await client.get(target_url)
                    if resp.status_code >= 400:
                        return f"Failed to load web page '{target_url}' (HTTP Status: {resp.status_code})."
                    html_text = resp.text

                soup = BeautifulSoup(html_text, "html.parser")

                # Remove non-content noisy tags
                for element in soup(["script", "style", "nav", "footer", "header", "noscript", "svg", "form"]):
                    element.decompose()

                page_title = soup.title.string.strip() if soup.title and soup.title.string else target_url

                # Extract prominent articles and outbound links
                extracted_links = []
                for a_tag in soup.find_all("a", href=True):
                    a_text = a_tag.get_text(separator=" ", strip=True)
                    a_href = a_tag["href"]
                    if a_href.startswith("/"):
                        a_href = urllib.parse.urljoin(target_url, a_href)
                    if len(a_text) > 15 and a_href.startswith("http"):
                        if not any(a_href == item[1] for item in extracted_links):
                            extracted_links.append((a_text, a_href))
                    if len(extracted_links) >= 12:
                        break

                # Extract cleaned body text
                body_text = soup.get_text(separator="\n", strip=True)
                body_text = re.sub(r"\n{3,}", "\n\n", body_text)
                truncated_text = body_text[:3000]

                output_parts = [
                    f"### Successfully Inspected Web Page: [{page_title}]({target_url})",
                    f"**URL:** {target_url}\n",
                ]

                if extracted_links:
                    output_parts.append("**Extracted Headlines & Direct URLs:**")
                    for idx, (headline, link) in enumerate(extracted_links[:8], start=1):
                        output_parts.append(f"{idx}. [{headline}]({link})")
                    output_parts.append("")

                output_parts.append("**Extracted Page Content:**")
                output_parts.append(truncated_text)

                return "\n".join(output_parts)
            except Exception as ex:
                return f"Error accessing URL '{target_url}' with browser: {str(ex)}"

        return _execute_browser

    @staticmethod
    def create_human_input_executor() -> Callable[..., Any]:
        """
        Executes interactive human suspension. Blocks autonomous agent execution until
        the human user answers via UI (input field or choice buttons) or API.
        """
        async def _execute_human_input(prompt: str = "", **kwargs: Any) -> str:
            question = prompt or kwargs.get("query") or kwargs.get("question") or ""
            input_type = kwargs.get("input_type", "text")
            options = kwargs.get("options", [])
            timeout_val = float(kwargs.get("timeout_seconds") or 600)

            active_job = current_active_job_id.get("current", "")
            if not active_job:
                return f"Human Input Received: {question}"

            # 1. Initialize job-scoped suspension event
            event = asyncio.Event()
            human_answers[active_job] = event

            # 2. Register future in peldrun-core HumanInputRegistry
            future = None
            try:
                from peldrun.tools.builtins.human_input import HumanInputRegistry
                future = HumanInputRegistry.register_request(
                    active_job,
                    {"prompt": question, "input_type": input_type, "options": options, "job_id": active_job}
                )
            except Exception:
                pass

            # 3. Wait asynchronously for user resolution
            try:
                async def wait_event() -> str:
                    await event.wait()
                    return human_data.get(active_job, "")

                waiters = [asyncio.create_task(wait_event())]
                if future is not None:
                    waiters.append(future)

                done, pending = await asyncio.wait(
                    waiters,
                    timeout=timeout_val,
                    return_when=asyncio.FIRST_COMPLETED
                )

                for p in pending:
                    p.cancel()

                if not done:
                    return f"Human response timed out after {int(timeout_val)} seconds."

                first_done = done.pop()
                user_reply = first_done.result()
                return f"Human Operator Response: {user_reply}"
            except Exception as ex:
                return f"Error awaiting human interaction: {str(ex)}"
            finally:
                human_answers.pop(active_job, None)
                human_data.pop(active_job, None)
                try:
                    from peldrun.tools.builtins.human_input import HumanInputRegistry
                    HumanInputRegistry.cancel_request(active_job)
                except Exception:
                    pass

        return _execute_human_input


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
        executor = RealToolExecutionFactory.create_web_search_executor(workspace_root)
    elif name in ("browser_use", "chrome_browser", "browser"):
        executor = RealToolExecutionFactory.create_browser_executor(workspace_root)
    elif name in ("ask_human", "human_input"):
        executor = RealToolExecutionFactory.create_human_input_executor()

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