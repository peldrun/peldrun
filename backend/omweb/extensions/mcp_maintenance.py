# backend/omweb/extensions/mcp_maintenance.py
"""
MCP Subsystem Maintenance Operations.

Three recovery actions for the two operational issues that recur in
practice:

  1. **npm cache repair** — after ``npm cache clean --force`` (or a
     network outage) the local npx cache loses its downloaded packages
     and every ``npx -y ...`` call must re-download, delaying startup
     by 30-120s. ``warm_npm_cache()`` re-populates the cache in one
     shot, in parallel.

  2. **Orphan cleanup** — crashed or interrupted MCP sessions can
     leave Node subprocesses alive on Windows (no automatic
     parent-death signal). ``cleanup_orphans()`` closes every tracked
     session and forces the pool to release its clients.

  3. **Backend restart** — after editing a Python module, the running
     backend still executes the old bytecode. ``restart_backend()``
     re-executes the current process via ``os.execv`` so the new code
     is loaded without requiring the user to Ctrl+C manually.

All operations are idempotent and safe to run repeatedly. None of them
touch persistent storage or existing chat sessions beyond the documented
cleanup scope.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shlex
import shutil
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("omweb.extensions.mcp_maintenance")


# ─────────────────────────────────────────────────────────────────
# npm cache repair
# ─────────────────────────────────────────────────────────────────

def _extract_npx_package(command: str) -> Optional[str]:
    """
    Extract the package name from a command like ``npx -y @scope/pkg arg1``.

    Returns the package spec (e.g. ``@modelcontextprotocol/server-github``)
    or None when no package can be parsed.
    """
    if not command:
        return None

    try:
        if os.name == "nt":
            parts = shlex.split(command, posix=False)
        else:
            parts = shlex.split(command)
    except ValueError:
        return None

    if not parts:
        return None

    # Skip the npx executable itself.
    for i, raw in enumerate(parts):
        if i == 0:
            continue
        token = raw.strip('"').strip("'")
        if not token or token.startswith("-"):
            continue
        return token

    return None


def _get_npx_servers() -> List[Tuple[str, str, str]]:
    """
    Return ``[(server_id, display_name, package), ...]`` for every
    registered MCP server whose command invokes ``npx``.
    """
    from omweb.extensions.registry import extension_registry

    out: List[Tuple[str, str, str]] = []
    for srv in extension_registry.list_mcp_servers():
        command = str(srv.get("command") or "")
        if "npx" not in command.lower():
            continue
        package = _extract_npx_package(command)
        if not package:
            continue
        sid = str(srv.get("id") or "")
        if not sid:
            continue
        out.append((sid, str(srv.get("name") or sid), package))
    return out


async def _warm_one_package(
    npm_path: str,
    server_id: str,
    package: str,
    timeout_seconds: float = 120.0,
) -> Dict[str, Any]:
    """Run ``npm cache add <package>`` and return a per-server report."""
    started = time.perf_counter()
    try:
        proc = await asyncio.create_subprocess_exec(
            npm_path,
            "cache",
            "add",
            package,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            shell=False,
        )
        try:
            stdout_b, stderr_b = await asyncio.wait_for(
                proc.communicate(), timeout=timeout_seconds
            )
        except asyncio.TimeoutError:
            proc.kill()
            try:
                await proc.wait()
            except Exception:
                pass
            return {
                "server_id": server_id,
                "package": package,
                "ok": False,
                "error": f"timed out after {timeout_seconds:.0f}s",
                "duration_ms": int((time.perf_counter() - started) * 1000),
            }

        duration_ms = int((time.perf_counter() - started) * 1000)
        ok = proc.returncode == 0
        detail = ""
        if not ok:
            err_text = (stderr_b or stdout_b or b"").decode(
                errors="replace"
            ).strip()
            detail = err_text[-500:]
        logger.info(
            "Warm cache for %s (%s): %s (%dms)",
            server_id,
            package,
            "ok" if ok else "failed",
            duration_ms,
        )
        return {
            "server_id": server_id,
            "package": package,
            "ok": ok,
            "exit_code": proc.returncode,
            "error": detail or None,
            "duration_ms": duration_ms,
        }
    except Exception as exc:  # noqa: BLE001
        logger.warning("Warm cache failed for %s: %s", server_id, exc)
        return {
            "server_id": server_id,
            "package": package,
            "ok": False,
            "error": str(exc),
            "duration_ms": int((time.perf_counter() - started) * 1000),
        }


async def warm_npm_cache() -> Dict[str, Any]:
    """
    Re-populate the npm cache for every npx-based MCP server.

    Runs packages in parallel (bounded concurrency = 2) so a 5-server
    setup completes in ~30-60s instead of 5× individually.

    Never raises. A missing npm executable returns a structured error.
    """
    npm_path = shutil.which("npm")
    if not npm_path:
        return {
            "ok": False,
            "error": "npm executable not found on PATH.",
            "total": 0,
            "succeeded": 0,
            "failed": 0,
            "results": [],
        }

    servers = _get_npx_servers()
    if not servers:
        return {
            "ok": True,
            "total": 0,
            "succeeded": 0,
            "failed": 0,
            "results": [],
            "note": "No npx-based MCP servers registered.",
        }

    sem = asyncio.Semaphore(2)

    async def _run(sid: str, name: str, pkg: str) -> Dict[str, Any]:
        async with sem:
            report = await _warm_one_package(npm_path, sid, pkg)
            report["server_name"] = name
            return report

    results = await asyncio.gather(
        *[_run(sid, name, pkg) for sid, name, pkg in servers]
    )

    return {
        "ok": all(r["ok"] for r in results),
        "total": len(results),
        "succeeded": sum(1 for r in results if r["ok"]),
        "failed": sum(1 for r in results if not r["ok"]),
        "results": results,
    }


# ─────────────────────────────────────────────────────────────────
# Orphan cleanup
# ─────────────────────────────────────────────────────────────────

async def cleanup_orphans() -> Dict[str, Any]:
    """
    Close every tracked MCP session.

    This is the safe recovery action for "Test Connection hangs" or
    "Tools Refresh stalls" — scenarios caused by a leaked Node
    subprocess that is still bound to a previous server version.

    Also invalidates all persistent tool caches so the next discovery
    starts from a clean slate.
    """
    from omweb.extensions.mcp_pool import mcp_session_pool
    from omweb.extensions.registry import extension_registry
    from omweb.extensions.mcp_tool_cache import invalidate as _invalidate_cache

    before = len(mcp_session_pool._clients)

    # Close every active session (across all workspaces).
    try:
        await mcp_session_pool.close_all()
    except Exception as exc:  # noqa: BLE001
        logger.warning("close_all() failed during cleanup: %s", exc)
        return {
            "ok": False,
            "error": str(exc),
            "sessions_closed": 0,
        }

    # Invalidate persistent tool caches for all registered servers.
    invalidated = 0
    try:
        for srv in extension_registry.list_mcp_servers():
            sid = srv.get("id")
            if sid and _invalidate_cache(str(sid)):
                invalidated += 1
    except Exception as exc:  # noqa: BLE001
        logger.debug("Cache invalidation during cleanup failed: %s", exc)

    logger.info(
        "Cleanup: closed %d session(s), invalidated %d cache(s).",
        before,
        invalidated,
    )
    return {
        "ok": True,
        "sessions_closed": before,
        "caches_invalidated": invalidated,
    }


# ─────────────────────────────────────────────────────────────────
# Backend restart
# ─────────────────────────────────────────────────────────────────

def _get_restart_argv() -> Optional[List[str]]:
    """
    Reconstruct the command line for os.execv, defensively.

    Windows' os.execv does NOT quote argv[0] reliably — if the path
    contains spaces, CreateProcess splits it into two tokens and the
    first one (e.g. "C:\\Program") is tried as the executable, which
    fails with a confusing "can't open file" error.

    Additionally, venv layouts on Windows work as follows:
      * ``.venv\\Scripts\\python.exe`` is a launcher stub that reads
        ``pyvenv.cfg``, sets PYTHONHOME/PYTHONPATH, then re-execs the
        BASE Python (``C:\\Program Files\\Python312\\python.exe``) with
        the same argv.
      * Inside the resulting process, ``sys.executable`` correctly
        reports the venv path (Python detects it), but
        ``psutil.cmdline()[0]`` reports the BASE python (the actual
        running executable).

    Therefore the BASE python is what psutil sees, and it is the one
    with a space in its path — exactly the value we must NOT pass to
    os.execv.

    The fix: take ``argv[0]`` from ``sys.executable`` (venv python,
    no space), and take the remaining args from ``psutil.cmdline()``
    (which correctly reflects ``-c "..."`` and any additional args).

    Returns:
      * A valid ``argv`` list whose ``argv[0]`` is ``sys.executable``,
        or
      * ``None`` if no safe reconstruction was possible.
    """
    python_exe = sys.executable or ""
    if not python_exe or not os.path.isfile(python_exe):
        logger.warning(
            "sys.executable is missing or invalid (%r); cannot restart.",
            python_exe,
        )
        return None

    # On Windows, a path with spaces in argv[0] breaks os.execv.
    # On Unix, argv is passed as an array — spaces are perfectly fine.
    if os.name == "nt" and " " in python_exe:
        logger.warning(
            "sys.executable contains a space (%r). This venv may be "
            "in a path with spaces; automatic restart is disabled.",
            python_exe,
        )
        return None

    # ── 1. Preferred: sys.executable + psutil.cmdline()[1:] ──────
    # psutil gives us the full original args (e.g. "-c", "<code>").
    # We only replace argv[0] with sys.executable to avoid the
    # space-in-path problem.
    try:
        import psutil  # type: ignore

        raw = psutil.Process().cmdline()
        if raw and len(raw) >= 1:
            # raw[0] may be the BASE python with spaces; skip it.
            # raw[1:] is the actual args.
            if len(raw) == 1:
                # Only the exec name — no args. Launch python with
                # default REPL (won't run our app, so reject).
                logger.warning(
                    "psutil cmdline has no args besides exec: %r", raw
                )
                return None
            return [python_exe] + list(raw[1:])
    except Exception as exc:  # noqa: BLE001
        logger.debug("psutil cmdline read failed: %s", exc)

    # ── 1b. Unix fallback: read /proc/self/cmdline directly ──────
    # Works without psutil. NUL-separated args.
    if os.name != "nt":
        try:
            with open("/proc/self/cmdline", "rb") as f:
                raw = f.read()
            if raw:
                proc_argv = [
                    a.decode("utf-8", errors="replace")
                    for a in raw.split(b"\x00")
                    if a
                ]
                if proc_argv and len(proc_argv) >= 2:
                    return [python_exe] + proc_argv[1:]
        except (FileNotFoundError, OSError):
            pass  # macOS has no /proc; fall through to sys.argv

    # ── 2. Fallback: sys.argv (works when launched via -m) ───────
    argv = list(sys.argv)
    if argv and argv[0].endswith(("__main__.py", "__main__.pyc")):
        package = Path(argv[0]).parent.name
        if package:
            candidate = [python_exe, "-m", package] + argv[1:]
            logger.debug("Reconstructed argv via __main__: %r", candidate)
            return candidate

    # ── 3. Refuse to guess ───────────────────────────────────────
    logger.warning(
        "Could not reconstruct a safe restart argv. "
        "sys.executable=%r, sys.argv=%r",
        python_exe, argv,
    )
    return None


def _validate_restart_argv(argv: Optional[List[str]]) -> Optional[str]:
    """
    Return an error message if ``argv`` is unsafe to execute, else None.
    """
    if not argv:
        return "Restart argv is empty or could not be determined."
    exe = argv[0]
    if not exe:
        return "Restart argv[0] is empty."
    if not os.path.isfile(exe):
        return (
            f"Restart executable does not exist: {exe!r}. "
            f"This usually means the path was split at a space. "
            f"Please restart the backend manually (Ctrl+C, then "
            f"run '.\\start-backend' again)."
        )
    return None


def _write_restart_bat(argv: List[str], delay_seconds: float) -> Path:
    """
    Write a .bat file that runs ``argv`` after ``delay_seconds``.

    The bat file is the only reliable way to restart on Windows:
      * The parent must exit to release port 8088.
      * The child must start AFTER the parent has released the port.
      * A small delay in the bat file bridges the two.

    The bat file:
      1. Waits ``delay_seconds`` (default 2.0s).
      2. cd's to the parent's CWD.
      3. Runs ``argv`` with output redirected to a log file.
      4. On exit, appends the exit code to the log.
    """
    import tempfile

    def _quote_for_batch(arg: str) -> str:
        # Batch quoting: only quote if the arg contains whitespace or
        # shell metacharacters. Double any internal double quotes.
        if any(c in arg for c in (' ', '\t', '"', '&', '|', '<', '>', '(', ')', '^')):
            escaped = arg.replace('"', '""')
            return f'"{escaped}"'
        return arg

    quoted_args = [_quote_for_batch(a) for a in argv]
    cmd_line = " ".join(quoted_args)

    log_path = Path(tempfile.gettempdir()) / "peldrun_restart.log"
    bat_path = Path(tempfile.gettempdir()) / "peldrun_restart.bat"

    cwd_str = str(Path.cwd())
    bat_content = (
        "@echo off\r\n"
        f"timeout /t {max(1, int(delay_seconds))} /nobreak >nul\r\n"
        f'cd /d "{cwd_str}"\r\n'
        f'{cmd_line} > "{log_path}" 2>&1\r\n'
        f'echo [Restart exited with code %ERRORLEVEL%] >> "{log_path}"\r\n'
    )
    bat_path.write_text(bat_content, encoding="utf-8")
    logger.info("Wrote restart bat to %s (log: %s)", bat_path, log_path)
    return bat_path


async def _restart_async(delay_seconds: float, argv: List[str]) -> None:
    """
    Perform a graceful in-place restart.

    Cross-platform strategy:
      * **Unix (Linux / macOS):** ``os.execv`` replaces the current
        process image atomically. argv is passed as an array, so paths
        with spaces work without quoting gymnastics. Uvicorn re-binds
        port 8088 cleanly because the old process is replaced, not
        coexisted.
      * **Windows:** ``os.execv`` is unreliable when ``argv[0]``
        contains spaces (CPython does not quote the executable) and
        spawns a child that may race with the parent for port 8088.
        We therefore write a temporary ``.bat`` file, spawn it with
        ``DETACHED_PROCESS``, and let the parent exit.

    The delay before the platform-specific action lets the HTTP
    response that triggered the restart flush to the client first.
    """
    await asyncio.sleep(delay_seconds)

    # Best-effort cleanup so no orphaned Node subprocess survives.
    try:
        from omweb.extensions.mcp_pool import mcp_session_pool

        await mcp_session_pool.close_all()
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not close MCP sessions before restart: %s", exc)

    try:
        from omweb.extensions.auth_manager import oauth_manager

        await oauth_manager.close()
    except Exception:  # noqa: BLE001
        pass

    # ── Unix path: execv replaces this process in place ─────────
    if os.name != "nt":
        if not argv or not argv[0] or not os.path.isfile(argv[0]):
            logger.error(
                "Refusing to restart: argv[0] is not a valid file: %r",
                argv[0] if argv else None,
            )
            os._exit(1)

        logger.info(
            "Re-executing backend (Unix execv): %s", " ".join(argv)
        )
        try:
            os.execv(argv[0], argv)
        except Exception as exc:  # noqa: BLE001
            logger.error("os.execv failed on Unix: %s", exc)
            os._exit(1)
        # execv never returns.
        return

    # ── Windows path: bat + DETACHED_PROCESS ─────────────────────
    try:
        bat_path = _write_restart_bat(argv, delay_seconds=2.0)
    except Exception as exc:  # noqa: BLE001
        logger.error("Could not write restart bat; aborting restart: %s", exc)
        return  # Server stays up — safer than exit with no replacement.

    try:
        import subprocess

        DETACHED_PROCESS = 0x00000008
        CREATE_NEW_PROCESS_GROUP = 0x00000200
        subprocess.Popen(
            ["cmd.exe", "/c", str(bat_path)],
            cwd=str(Path.cwd()),
            creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP,
            close_fds=True,
        )
        logger.info("Restart bat spawned; parent will exit now.")
    except Exception as exc:  # noqa: BLE001
        logger.error("Could not spawn restart bat; aborting restart: %s", exc)
        return  # Server stays up.

    await asyncio.sleep(0.3)
    os._exit(0)


def schedule_restart(delay_seconds: float = 1.5) -> List[str]:
    """
    Schedule an in-process backend restart.

    Raises RuntimeError if the restart argv cannot be reconstructed
    safely — the caller must surface this to the user rather than
    stopping the server without a replacement.
    """
    argv = _get_restart_argv()
    validation_error = _validate_restart_argv(argv)
    if validation_error:
        raise RuntimeError(validation_error)
    assert argv is not None  # for type checkers
    logger.info(
        "Scheduling backend restart in %.1fs: %s",
        delay_seconds,
        " ".join(argv),
    )
    asyncio.create_task(_restart_async(delay_seconds, argv))
    return argv


__all__ = [
    "warm_npm_cache",
    "cleanup_orphans",
    "schedule_restart",
    "_get_restart_argv",
]
