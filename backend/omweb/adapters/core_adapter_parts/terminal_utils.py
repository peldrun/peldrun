"""
Terminal / shell utility helpers used by the PELDRUN core adapter layer.

This module contains the two low-level helpers required to spawn bash on
Windows without hitting the WSL stub, and to decode raw bytes returned by
subprocesses without the classic code-page corruption.
"""

from __future__ import annotations

import os
import shutil
import re as _re

import sys
from pathlib import Path
from typing import Optional


def find_safe_bash_executable() -> Optional[str]:
    """
    Finds a genuine Git Bash or MSYS2 executable, strictly avoiding
    the Windows WSL launcher stubs located in System32.

    The search order is:
      1. Well-known Git installation paths.
      2. The directory that owns the `git` binary on PATH.
      3. `shutil.which("bash")` -- but only if it does NOT resolve to a
         System32 / SysWOW64 / Windows stub.

    Returns:
        Optional[str]: Absolute path to a real bash binary, or None.
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

    Strategy:
      1. If the buffer contains NUL bytes, try UTF-16 variants first
         (Windows stubs often emit UTF-16-LE).
      2. Otherwise try a cascade of single-byte encodings: utf-8, cp1252,
         cp1256, latin1.
      3. As a last resort, decode UTF-8 with `errors="replace"`.

    Args:
        raw: Raw bytes returned by a subprocess pipe.

    Returns:
        str: Clean, stripped, NUL-free decoded text.
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


_ANSI_ESCAPE_RE = _re.compile(
    r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])"
)
_CONTROL_CHARS_RE = _re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def sanitize_terminal_output(raw_output: Optional[str]) -> str:
    """
    Clean terminal output for safe display / model consumption:
    - Strips ANSI escape sequences (colors, cursor movement).
    - Removes control characters that break downstream formatting.
    - Collapses runs of blank lines.
    - Bounds maximum length to avoid runaway output.

    This is a display-layer sanitizer, not a security boundary.
    """
    if not raw_output:
        return ""

    text = str(raw_output)
    text = _ANSI_ESCAPE_RE.sub("", text)
    text = _CONTROL_CHARS_RE.sub("", text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _re.sub(r"\n{4,}", "\n\n\n", text)

    max_chars = 200_000
    if len(text) > max_chars:
        text = text[:max_chars] + f"\n\n... [truncated, original length: {len(raw_output)} chars]"

    return text.rstrip()
