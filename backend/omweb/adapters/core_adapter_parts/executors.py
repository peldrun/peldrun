"""
Concrete executor factories for every PELDRUN built-in tool.

The single public class :class:`RealToolExecutionFactory` exposes one
static factory method per tool. Each factory returns an async callable
bound to a specific workspace directory without global process CWD mutations.
Hardened under Phase M2 & Bash Sanitization:
- Intercepts blocking foreground web servers (e.g. python -m http.server) with immediate notices.
- Cleanses accidental Windows absolute paths in shell commands to prevent backslash escape corruption.
"""

from __future__ import annotations

import asyncio
import inspect
import os
from pathlib import Path
import re
import sys
from typing import Any, Callable, Dict, List, Optional
import urllib.parse

import httpx

# Resilient optional dependency handling for HTML parsing
try:
    from bs4 import BeautifulSoup
except ImportError:
    BeautifulSoup = None  # type: ignore[assignment, misc]

from .terminal_utils import decode_terminal_bytes, find_safe_bash_executable

# Optional bridge to the web platform's human-in-the-loop machinery.
try:
    from omweb.agent_bridge import current_active_job_id, human_answers, human_data
except ImportError:
    human_answers: Dict[str, asyncio.Event] = {}
    human_data: Dict[str, str] = {}
    current_active_job_id: Dict[str, str] = {}


class RealToolExecutionFactory:
    """Factory creating concrete execution callables bound to the conversation directory."""

    @staticmethod
    def create_bash_executor(workspace_root: Path) -> Callable[..., Any]:
        """Build an async bash executor bound to workspace_root without CWD mutation."""
        safe_bash = find_safe_bash_executable()

        async def _execute_bash(command: str = "", **kwargs: Any) -> str:
            cmd = command or kwargs.get("cmd") or kwargs.get("script") or ""
            if not cmd.strip():
                return "Error: No command provided to bash terminal."

            # 1. Intercept blocking foreground servers to prevent 60-second timeouts
            if re.search(r"python(?:3)?\s+-m\s+http\.server", cmd, re.IGNORECASE):
                return (
                    "[Notice] Local web server command intercepted: In PELDRUN, web deliverables "
                    "(HTML/CSS/JS) are automatically served and rendered live in the UI Preview panel. "
                    "A blocking foreground server process is not required and has been safely simulated."
                )

            # 2. Sanitize accidental absolute Windows workspace paths to prevent backslash stripping
            ws_str_win = str(workspace_root)
            ws_str_posix = str(workspace_root).replace("\\", "/")
            ws_str_escaped = ws_str_win.replace("\\", "\\\\")

            if ws_str_escaped in cmd:
                cmd = cmd.replace(ws_str_escaped, ".")
            if ws_str_win in cmd:
                cmd = cmd.replace(ws_str_win, ".")
            if ws_str_posix in cmd:
                cmd = cmd.replace(ws_str_posix, ".")

            # Convert any remaining Windows drive letter backslashes (e.g. C:\ or D:\) to forward slashes
            if sys.platform == "win32":
                cmd = re.sub(r"([a-zA-Z]):\\", r"\1:/", cmd)
                cmd = cmd.replace(".\\", "./")

            try:
                if sys.platform == "win32":
                    if safe_bash:
                        proc = await asyncio.create_subprocess_exec(
                            safe_bash,
                            "-c",
                            cmd,
                            cwd=str(workspace_root),
                            stdout=asyncio.subprocess.PIPE,
                            stderr=asyncio.subprocess.PIPE,
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
                            stderr=asyncio.subprocess.PIPE,
                        )
                else:
                    proc = await asyncio.create_subprocess_shell(
                        cmd,
                        cwd=str(workspace_root),
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
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
        """Build an async Python executor running strictly within workspace_root."""
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
                    stderr=asyncio.subprocess.PIPE,
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
        """Build an async editor executor with path traversal protection."""
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
                **kwargs: Any,
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
        """Executes web search with direct citations and link attribution."""
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

            # Guard against missing beautifulsoup4 dependency
            if BeautifulSoup is None:
                return (
                    f"Search completed for '{clean_q}'. No web snippets were returned. "
                    "Notice: 'beautifulsoup4' is not installed in the environment. "
                    "Install it via 'pip install beautifulsoup4' to enable HTML scraping fallback."
                )

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
                    "*(You MUST cite these sources and include their target URLs in your response)*\n",
                ]
                for i, r in enumerate(results, 1):
                    lines.append(
                        f"**{i}. [{r['title']}]({r['url']})**\n   - **URL:** {r['url']}\n   - **Snippet:** {r['snippet']}\n"
                    )
                return "\n".join(lines).strip()

            return (
                f"Search completed for '{clean_q}'. No web snippets were returned by search engines at this moment. "
                "Notice: Do not repeat search queries. Synthesize your final answer with available knowledge, "
                "or inspect specific URLs directly via 'browser_use'."
            )

        return _execute_search

    @staticmethod
    def create_browser_executor(workspace_root: Optional[Path] = None) -> Callable[..., Any]:
        """Executes web page inspection, extracting titles, articles, and text content."""
        async def _execute_browser(
            url: str = "",
            action: str = "extract_content",
            selector: Optional[str] = None,
            **kwargs: Any,
        ) -> str:
            target_url = url or kwargs.get("link") or kwargs.get("href") or ""
            if not target_url.strip():
                return "Error: 'url' parameter is required for browser_use."

            if not target_url.startswith("http://") and not target_url.startswith("https://"):
                target_url = f"https://{target_url}"

            if BeautifulSoup is None:
                return (
                    f"Error accessing URL '{target_url}': 'beautifulsoup4' is not installed in the environment. "
                    "Install it via 'pip install beautifulsoup4' to enable web page content extraction."
                )

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

                for element in soup(["script", "style", "nav", "footer", "header", "noscript", "svg", "form"]):
                    element.decompose()

                page_title = soup.title.string.strip() if soup.title and soup.title.string else target_url

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
        """Executes interactive human suspension blocking until user resolution."""
        async def _execute_human_input(prompt: str = "", **kwargs: Any) -> str:
            question = prompt or kwargs.get("query") or kwargs.get("question") or ""
            input_type = kwargs.get("input_type", "text")
            options = kwargs.get("options", [])
            timeout_val = float(kwargs.get("timeout_seconds") or 600)

            active_job = current_active_job_id.get("current", "")
            if not active_job:
                return f"Human Input Received: {question}"

            event = asyncio.Event()
            human_answers[active_job] = event

            future = None
            try:
                from peldrun.tools.builtins.human_input import HumanInputRegistry

                future = HumanInputRegistry.register_request(
                    active_job,
                    {"prompt": question, "input_type": input_type, "options": options, "job_id": active_job},
                )
            except Exception:
                pass

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
                    return_when=asyncio.FIRST_COMPLETED,
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