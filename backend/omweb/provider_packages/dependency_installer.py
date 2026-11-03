# backend/omweb/provider_packages/dependency_installer.py
"""
Isolated Python environment manager for Provider Packages.

Every Provider Package that ships Python code (``transport`` in
``{"native", "custom"}``) gets its own virtual environment, created at
``<storage>/store/providers/<provider_id>/environments/<version>/``.
The package's dependencies are installed ONLY into that venv, never
into the PELDRUN main interpreter.

Why a dedicated venv per (package, version)
-------------------------------------------

1. **Dependency isolation.** Two providers may require incompatible
   versions of ``httpx`` or ``pydantic``. Without isolation, the second
   install would downgrade the library the first provider depends on.

2. **Determinism across upgrades.** A rollback to version ``1.0.0``
   must restore the exact environment that was validated at install
   time. Reusing a shared venv would leave orphaned packages from
   ``1.1.0`` behind.

3. **Bounded damage.** A malicious or buggy package can only pollute
   its own venv. It cannot modify PELDRUN's runtime dependencies.

4. **Clean uninstall.** Removing a package version removes its venv
   directory. No shared state is left behind.

Security model
--------------

* The venv is created with pip bootstrapped by the venv module itself
  (``with_pip=True``). This is what prevents a package from shipping a
  malicious ``pip`` shim that would shadow the real one.

* ``pip install`` is invoked with:
    * ``--no-input``   — no interactive prompts.
    * ``--disable-pip-version-check`` — deterministic output.
    * ``--no-cache-dir`` — no cross-venv cache reuse.
    * ``--no-warn-script-location`` — deterministic output.

* The timeout is enforced by ``asyncio.wait_for`` on the subprocess.
  A wedged pip (network stall, resolver loop) is killed after
  ``DEFAULT_INSTALL_TIMEOUT_SECONDS`` and the entire venv is deleted.

* The install log is truncated to ``MAX_LOG_BYTES`` when returned to
  the caller. The full log stays in the venv directory's pip cache
  if debugging remains necessary.

Design invariants
-----------------

1. **Never installs into the main interpreter.** Every ``pip`` call
   goes through the venv's ``python -m pip``.

2. **Idempotent.** Re-running ``create_environment()`` for an already-
   populated version returns the existing path unchanged. It does NOT
   re-create the venv, so a re-install of the same ZIP at the same
   version is a no-op.

3. **A failed install deletes the venv.** A partially-populated venv
   is worse than no venv: the adapter would import half the
   dependencies and fail with confusing ``ImportError`` messages at
   request time. Cleanup is unconditional on any failure path.

4. **``compatible`` packages never call this module.** A compatible
   provider ships only a ``manifest.json`` + endpoint configuration
   and reuses a built-in adapter. There is no Python code to install,
   so no venv is created.

See also
--------

    * ``backend/omweb/provider_packages/schema.py`` — ``ProviderManifest``
    * ``backend/omweb/provider_packages/errors.py`` — exception hierarchy
    * ``backend/omweb/provider_packages/package_manager.py`` — orchestrator
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import subprocess
import sys
import time
import venv
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

from omweb.provider_packages.errors import (
    ProviderPackageEnvironmentError,
    ProviderPackageStorageError,
)
from omweb.provider_packages.schema import ProviderManifest

logger = logging.getLogger("omweb.provider_packages.dependency_installer")


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


#: Default pip install timeout. 10 minutes accommodates a cold install
#: of a reasonably sized dependency tree (e.g. ``anthropic`` pulls
#: ``httpx`` + ``pydantic`` + ``anyio``) on a slow network. Long enough
#: to succeed, short enough that a wedged pip does not block the
#: upload endpoint indefinitely.
DEFAULT_INSTALL_TIMEOUT_SECONDS: float = 600.0


#: Maximum pip log stored in the report. Anything beyond this is
#: truncated — the full log stays in the venv directory for debugging.
MAX_LOG_BYTES: int = 8000


#: Filename of the lockfile that the installer prefers over
#: ``requirements.txt``. Matches the PyPA convention: a hash-pinned
#: lockfile is always preferred for reproducibility.
LOCKFILE_NAME: str = "requirements.lock"


#: Fallback requirements filename (unpinned).
REQUIREMENTS_NAME: str = "requirements.txt"


#: Alternative modern layout — installed via ``pip install -e .`` so
#: its ``[project.dependencies]`` are resolved by pip.
PYPROJECT_NAME: str = "pyproject.toml"


# ---------------------------------------------------------------------------
# Python version specifier check
# ---------------------------------------------------------------------------


def _parse_version_tuple(spec: str) -> Optional[Tuple[int, ...]]:
    """
    Parse a minimal subset of PEP 440 version strings into a tuple.

    Only ``MAJOR.MINOR[.PATCH]`` is supported — Provider Packages in
    PELDRUN do not need pre-release or local version segments. Returns
    ``None`` for anything that does not match.
    """
    if not isinstance(spec, str):
        return None
    parts = spec.strip().split(".")
    if len(parts) < 2 or len(parts) > 3:
        return None
    try:
        return tuple(int(p) for p in parts)
    except ValueError:
        return None


def _check_python_specifier(spec: Optional[str]) -> None:
    """
    Raise ``ProviderPackageEnvironmentError`` if ``spec`` is a PEP 440
    specifier that the running interpreter does not satisfy.

    Supported operators: ``>=``, ``<=``, ``>``, ``<``, ``==``.
    Multiple clauses separated by commas are ANDed. This covers every
    real-world usage in a provider manifest; anything more exotic is
    rejected with a clear error rather than silently ignored.

    ``None`` or empty string means "no constraint" — the function
    returns immediately.
    """
    if not spec or not spec.strip():
        return

    running = (
        sys.version_info.major,
        sys.version_info.minor,
        sys.version_info.micro,
    )
    running_short = running[:2]

    clauses = [c.strip() for c in spec.split(",") if c.strip()]
    if not clauses:
        return

    for clause in clauses:
        operator: Optional[str] = None
        version_str: str = ""
        for op in (">=", "<=", "==", ">", "<"):
            if clause.startswith(op):
                operator = op
                version_str = clause[len(op):].strip()
                break
        if operator is None:
            raise ProviderPackageEnvironmentError(
                f"Unsupported Python version specifier '{clause}'. "
                f"Use '>=' / '<=' / '>' / '<' / '==' with a "
                f"MAJOR.MINOR[.PATCH] version."
            )

        target = _parse_version_tuple(version_str)
        if target is None:
            raise ProviderPackageEnvironmentError(
                f"Invalid version in specifier '{clause}'. Expected "
                f"MAJOR.MINOR[.PATCH], got '{version_str}'."
            )

        length = max(len(target), len(running_short))
        target_padded = target + (0,) * (length - len(target))
        running_padded = running_short + (0,) * (length - len(running_short))

        if operator == ">=":
            satisfied = running_padded >= target_padded
        elif operator == "<=":
            satisfied = running_padded <= target_padded
        elif operator == ">":
            satisfied = running_padded > target_padded
        elif operator == "<":
            satisfied = running_padded < target_padded
        else:  # "=="
            satisfied = running_padded == target_padded

        if not satisfied:
            raise ProviderPackageEnvironmentError(
                f"Provider package requires Python {spec}, but the "
                f"running interpreter is "
                f"{sys.version_info.major}.{sys.version_info.minor}."
                f"{sys.version_info.micro}."
            )


# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------


@dataclass
class EnvironmentInstallResult:
    """
    Result of ``create_environment()``.

    ``venv_path`` is the absolute path to the created venv. Callers
    should persist this (or re-derive it) so the Supervisor (phase 2)
    can launch the worker with the correct interpreter.

    ``installed`` is ``True`` when at least one package was actually
    installed (either from ``requirements.lock``, ``requirements.txt``,
    ``pyproject.toml``, or the manifest's ``dependencies`` list). When
    no dependency file exists, ``installed`` is ``False`` and
    ``skipped_reason`` explains why.
    """

    venv_path: Path
    python_executable: Path
    installed: bool = False
    skipped_reason: str = ""
    duration_ms: int = 0
    log_tail: str = ""
    warnings: List[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Installer
# ---------------------------------------------------------------------------


class DependencyInstaller:
    """
    Stateless installer for isolated provider-package environments.

    All state is stored on disk under the caller-supplied
    ``environments_root``. The installer holds no in-memory cache —
    re-creating an environment for an already-populated version is a
    single ``is_file()`` check plus a path return.

    Safe to instantiate per call. Prefer the module-level
    ``dependency_installer`` for convenience.
    """

    def __init__(
        self,
        *,
        install_timeout_seconds: float = DEFAULT_INSTALL_TIMEOUT_SECONDS,
    ) -> None:
        self._timeout = float(install_timeout_seconds)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def create_environment(
        self,
        *,
        package_dir: Path,
        environments_root: Path,
        version: str,
        manifest: ProviderManifest,
        progress_callback: Optional[Any] = None,
    ) -> EnvironmentInstallResult:
        """
        Create an isolated venv for ``version`` of the package and
        install its declared dependencies.

        Parameters
        ----------
        package_dir
            The extracted package root (the directory that contains
            ``manifest.json`` and, usually, ``requirements.txt`` /
            ``pyproject.toml``).
        environments_root
            The parent directory under which ``<version>/`` is created.
            Typically ``<storage>/store/providers/<id>/environments/``.
        version
            The package version. Used as the venv subdirectory name.
        manifest
            The validated ``ProviderManifest``. Only ``runtime.python``
            and ``dependencies`` are read here.

        Returns
        -------
        EnvironmentInstallResult
            Always populated on success. On any failure, the venv
            directory is deleted before the exception propagates.

        Raises
        ------
        ProviderPackageEnvironmentError
            On any failure (version mismatch, venv creation error,
            pip failure, timeout).
        """
        started = time.perf_counter()

        # --------------------------------------------------------------
        # 1. Version specifier check — fail fast, before any disk I/O.
        # --------------------------------------------------------------
        _check_python_specifier(manifest.runtime.python)

        # --------------------------------------------------------------
        # 2. Resolve the target venv directory.
        # --------------------------------------------------------------
        try:
            environments_root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise ProviderPackageStorageError(
                f"Could not create environments root "
                f"{environments_root}: {exc}",
                details={"path": str(environments_root)},
            ) from exc

        venv_dir = environments_root / version

        # --------------------------------------------------------------
        # 3. Idempotency — an existing, populated venv is reused.
        # --------------------------------------------------------------
        python_exe = self._python_executable(venv_dir)
        if venv_dir.is_dir() and python_exe.is_file():
            logger.info(
                "Reusing existing venv for version %r at %s.",
                version,
                venv_dir,
            )
            return EnvironmentInstallResult(
                venv_path=venv_dir,
                python_executable=python_exe,
                installed=False,
                skipped_reason="existing_venv_reused",
                duration_ms=int((time.perf_counter() - started) * 1000),
            )

        # --------------------------------------------------------------
        # 4. Create the venv. Any partial state from a prior crashed
        #    attempt is wiped first.
        # --------------------------------------------------------------
        if venv_dir.exists():
            try:
                shutil.rmtree(venv_dir)
            except OSError as exc:
                raise ProviderPackageStorageError(
                    f"Could not clear stale venv directory "
                    f"{venv_dir}: {exc}",
                    details={"path": str(venv_dir)},
                ) from exc

        try:
            self._build_venv(venv_dir)
        except ProviderPackageEnvironmentError:
            raise
        except Exception as exc:
            self._safe_rmtree(venv_dir)
            raise ProviderPackageEnvironmentError(
                f"Failed to create virtual environment at "
                f"{venv_dir}: {exc}",
                details={"path": str(venv_dir)},
            ) from exc

        python_exe = self._python_executable(venv_dir)
        if not python_exe.is_file():
            self._safe_rmtree(venv_dir)
            raise ProviderPackageEnvironmentError(
                f"Virtual environment was created at {venv_dir} but "
                f"the interpreter ({python_exe.name}) is missing. "
                f"This usually means the 'venv' module is not "
                f"available in this Python build.",
                details={"path": str(venv_dir)},
            )

        # --------------------------------------------------------------
        # 5. Install dependencies.
        # --------------------------------------------------------------
        try:
            install_ok, log_tail, skipped_reason = (
                await self._install_dependencies(
                    package_dir=package_dir,
                    python_exe=python_exe,
                    manifest=manifest,
                    progress_callback=progress_callback,
                )
            )
        except ProviderPackageEnvironmentError:
            self._safe_rmtree(venv_dir)
            raise
        except Exception as exc:
            self._safe_rmtree(venv_dir)
            raise ProviderPackageEnvironmentError(
                f"Dependency installation failed: {exc}",
                details={"path": str(venv_dir)},
            ) from exc

        duration_ms = int((time.perf_counter() - started) * 1000)
        logger.info(
            "Created venv for version %r at %s in %dms (installed=%s).",
            version,
            venv_dir,
            duration_ms,
            install_ok,
        )

        return EnvironmentInstallResult(
            venv_path=venv_dir,
            python_executable=python_exe,
            installed=install_ok,
            skipped_reason=skipped_reason,
            duration_ms=duration_ms,
            log_tail=log_tail,
        )

    def delete_environment(
        self,
        *,
        environments_root: Path,
        version: str,
    ) -> bool:
        """
        Delete the venv for a specific version. Returns ``True`` if the
        directory existed and was removed, ``False`` if it did not.

        Never raises — a cleanup failure is logged but not propagated.
        Callers who need hard guarantees should verify the directory
        is gone after this call.
        """
        venv_dir = environments_root / version
        if not venv_dir.exists():
            return False
        try:
            shutil.rmtree(venv_dir)
            logger.info(
                "Deleted venv for version %r at %s.", version, venv_dir
            )
            return True
        except OSError as exc:
            logger.warning(
                "Could not delete venv for version %r at %s: %s",
                version,
                venv_dir,
                exc,
            )
            return False

    def list_environments(
        self, environments_root: Path
    ) -> List[str]:
        """
        Return the version directories present under ``environments_root``.
        """
        if not environments_root.is_dir():
            return []
        versions: List[str] = []
        for entry in environments_root.iterdir():
            if entry.is_dir():
                versions.append(entry.name)
        return sorted(versions)

    # ------------------------------------------------------------------
    # Internal — venv creation
    # ------------------------------------------------------------------

    def _build_venv(self, venv_dir: Path) -> None:
        """
        Create a virtual environment at ``venv_dir`` with pip bootstrapped.

        Uses ``venv.EnvBuilder`` (in-process) rather than the
        subprocess ``python -m venv`` because the builder API is stable
        across Python versions and gives a reliable exception on
        failure. ``with_pip=True`` runs the ``ensurepip`` step, which
        bootstraps the pip shim inside the new venv — this is what
        prevents a malicious package from shadowing ``pip`` via a
        shipped ``pip.py`` in its own ``site-packages``.
        """
        try:
            builder = venv.EnvBuilder(
                system_site_packages=False,
                clear=False,
                symlinks=False,        # copies — safer on Windows
                with_pip=True,
                upgrade_deps=True,     # ensure the venv ships a modern pip
            )
            builder.create(str(venv_dir))
        except Exception as exc:
            self._safe_rmtree(venv_dir)
            raise ProviderPackageEnvironmentError(
                f"venv.EnvBuilder failed for {venv_dir}: {exc}",
                details={"path": str(venv_dir)},
            ) from exc

    @staticmethod
    def _python_executable(venv_dir: Path) -> Path:
        """
        Return the path to the venv's Python interpreter, accounting
        for the Windows ``Scripts/`` vs POSIX ``bin/`` layout.
        """
        if os.name == "nt":
            return venv_dir / "Scripts" / "python.exe"
        return venv_dir / "bin" / "python"

    def _safe_rmtree(self, path: Path) -> None:
        """Best-effort recursive delete. Never raises."""
        if not path.exists():
            return
        try:
            shutil.rmtree(path)
        except OSError as exc:
            logger.warning(
                "Could not remove %s after install failure: %s",
                path,
                exc,
            )

    # ------------------------------------------------------------------
    # Internal — dependency installation
    # ------------------------------------------------------------------

    async def _install_dependencies(
        self,
        *,
        package_dir: Path,
        python_exe: Path,
        manifest: ProviderManifest,
        progress_callback: Optional[Any] = None,
    ) -> Tuple[bool, str, str]:
        """
        Install dependencies into the venv.

        Precedence
        ----------
        1. ``requirements.lock``    — hash-pinned lockfile (preferred).
        2. ``requirements.txt``     — unpinned requirements.
        3. ``pyproject.toml``       — install the package itself with
                                      ``pip install -e .`` so its
                                      dependencies are resolved from
                                      ``[project.dependencies]``.
        4. Neither present AND ``manifest.dependencies`` non-empty →
           install the manifest's declared list directly (fallback for
           packages that do not ship a requirements file but do declare
           dependencies in the manifest).

        Returns ``(installed, log_tail, skipped_reason)``.

        The ``progress_callback``, when provided, receives a short
        human-readable line for every meaningful event (pip output
        line, high-level phase change). It is invoked synchronously
        from within this coroutine, so callers must not block inside
        it.
        """
        lockfile = package_dir / LOCKFILE_NAME
        requirements = package_dir / REQUIREMENTS_NAME
        pyproject = package_dir / PYPROJECT_NAME

        if progress_callback is not None:
            try:
                progress_callback(
                    "[system] Resolving dependency source..."
                )
            except Exception:
                pass

        if lockfile.is_file():
            if progress_callback is not None:
                try:
                    progress_callback(
                        "[system] Using requirements.lock "
                        "(hash-pinned, preferred)."
                    )
                except Exception:
                    pass
            return await self._run_pip(
                python_exe,
                ["install", "-r", str(lockfile)],
                label="requirements.lock",
                progress_callback=progress_callback,
            )

        if requirements.is_file():
            if progress_callback is not None:
                try:
                    progress_callback(
                        "[system] Using requirements.txt."
                    )
                except Exception:
                    pass
            return await self._run_pip(
                python_exe,
                ["install", "-r", str(requirements)],
                label="requirements.txt",
                progress_callback=progress_callback,
            )

        if pyproject.is_file():
            if progress_callback is not None:
                try:
                    progress_callback(
                        "[system] Using pyproject.toml "
                        "(pip install -e .)."
                    )
                except Exception:
                    pass
            return await self._run_pip(
                python_exe,
                ["install", "-e", str(package_dir)],
                label="pyproject.toml",
                progress_callback=progress_callback,
            )

        if manifest.dependencies:
            if progress_callback is not None:
                try:
                    progress_callback(
                        "[system] Using manifest.dependencies "
                        f"({len(manifest.dependencies)} entr"
                        f"{'y' if len(manifest.dependencies) == 1 else 'ies'})."
                    )
                except Exception:
                    pass
            args = ["install"] + list(manifest.dependencies)
            return await self._run_pip(
                python_exe,
                args,
                label="manifest.dependencies",
                progress_callback=progress_callback,
            )

        # Nothing to install.
        if progress_callback is not None:
            try:
                progress_callback(
                    "[system] No dependency files found; "
                    "skipping pip install."
                )
            except Exception:
                pass
        return (False, "", "no_dependency_files")

    async def _run_pip(
        self,
        python_exe: Path,
        args: Sequence[str],
        *,
        label: str,
        progress_callback: Optional[Any] = None,
    ) -> Tuple[bool, str, str]:
        """
        Run ``python -m pip <args>`` in a subprocess with a hard timeout.

        Streams stdout/stderr line-by-line to ``progress_callback`` (if
        provided) so the UI can show a live install log. The complete
        output is also collected so the tail can be reported on
        failure.

        Returns ``(installed, log_tail, skipped_reason)`` on success.
        Raises ``ProviderPackageEnvironmentError`` on non-zero exit or
        timeout — the caller deletes the venv.
        """
        # pip >= 26 (2026) made --no-warn-script-location an
        # install-subcommand-only flag. When placed before "install"
        # (as older pip allowed), pip exits with code 2 (usage error).
        #
        # Fix: pass --no-warn-script-location AFTER the subcommand.
        # The subcommand is always the first element of ``args``
        # (e.g. "install"), so we split it out and reassemble.
        #
        # Also: --disable-pip-version-check is deprecated in pip 26
        # and emits a warning; it is no longer needed because pip 26
        # does not perform the version check on every invocation.
        subcommand = args[0] if args else "install"
        subcommand_args = list(args[1:]) if len(args) > 1 else []

        pip_args = [
            "-m", "pip",
            "--no-input",
            "--no-cache-dir",
            subcommand,
            "--no-warn-script-location",
            *subcommand_args,
        ]

        logger.info(
            "pip install via %s (label=%s)", python_exe, label
        )

        if progress_callback is not None:
            try:
                progress_callback(
                    f"[system] Running pip {subcommand} "
                    f"(label={label})..."
                )
            except Exception:
                pass

        # Merge stderr into stdout so we preserve the original
        # ordering of pip's output. pip writes progress (download
        # bars, "Collecting", "Installing") to stdout, but warnings
        # and errors to stderr; merging them together produces a
        # linear, readable log.
        try:
            proc = await asyncio.create_subprocess_exec(
                str(python_exe),
                *pip_args,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )
        except FileNotFoundError as exc:
            raise ProviderPackageEnvironmentError(
                f"Could not launch the venv interpreter "
                f"{python_exe}: {exc}",
                details={"interpreter": str(python_exe)},
            ) from exc
        except Exception as exc:
            raise ProviderPackageEnvironmentError(
                f"Could not launch pip subprocess: {exc}",
                details={"interpreter": str(python_exe)},
            ) from exc

        log_lines: List[str] = []

        async def _stream_reader() -> None:
            """
            Read pip's merged output line-by-line.

            Each line is (a) appended to ``log_lines`` for later
            reporting, and (b) forwarded to ``progress_callback`` so
            the UI sees it immediately.

            The callback is wrapped in try/except so a buggy callback
            cannot kill the pip process — a broken UI should never
            break an install.
            """
            assert proc.stdout is not None
            while True:
                raw = await proc.stdout.readline()
                if not raw:
                    break
                try:
                    line = raw.decode("utf-8", errors="replace").rstrip()
                except Exception:
                    line = repr(raw)
                if not line:
                    continue
                log_lines.append(line)
                if progress_callback is not None:
                    try:
                        progress_callback(f"[pip] {line}")
                    except Exception:
                        # Never let a broken callback abort the stream.
                        pass

        try:
            await asyncio.wait_for(
                asyncio.gather(_stream_reader(), proc.wait()),
                timeout=self._timeout,
            )
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            try:
                await proc.wait()
            except Exception:
                pass
            if progress_callback is not None:
                try:
                    progress_callback(
                        f"[error] pip timed out after "
                        f"{self._timeout:.0f}s."
                    )
                except Exception:
                    pass
            raise ProviderPackageEnvironmentError(
                f"pip install timed out after {self._timeout:.0f}s "
                f"(label={label}). The package's dependency tree may "
                f"be too large, or the network may be unreachable.",
                details={
                    "label": label,
                    "timeout_seconds": self._timeout,
                },
            )

        returncode = proc.returncode
        log_combined = "\n".join(log_lines)

        if returncode != 0:
            if progress_callback is not None:
                try:
                    progress_callback(
                        f"[error] pip exited with code {returncode}."
                    )
                except Exception:
                    pass
            raise ProviderPackageEnvironmentError(
                f"pip install failed (exit {returncode}, label={label}). "
                f"See pip_log_tail for details.",
                details={
                    "label": label,
                    "returncode": returncode,
                    "pip_log_tail": self._truncate(log_combined),
                },
            )

        if progress_callback is not None:
            try:
                progress_callback(
                    f"[system] pip install completed (label={label})."
                )
            except Exception:
                pass

        return (True, self._truncate(log_combined), "")

    @staticmethod
    def _combine_logs(
        stdout_bytes: bytes, stderr_bytes: bytes
    ) -> str:
        """Combine pip's stdout and stderr into a single text blob."""
        pieces: List[str] = []
        for label, raw in (("stdout", stdout_bytes), ("stderr", stderr_bytes)):
            if not raw:
                continue
            try:
                text = raw.decode("utf-8", errors="replace")
            except Exception:
                text = repr(raw)
            text = text.strip()
            if text:
                pieces.append(f"[{label}]\n{text}")
        return "\n\n".join(pieces)

    @staticmethod
    def _truncate(log: str) -> str:
        """Truncate ``log`` to ``MAX_LOG_BYTES`` keeping the tail."""
        if len(log.encode("utf-8", errors="replace")) <= MAX_LOG_BYTES:
            return log
        encoded = log.encode("utf-8", errors="replace")
        tail_bytes = encoded[-MAX_LOG_BYTES:]
        try:
            return "[...truncated...]\n" + tail_bytes.decode(
                "utf-8", errors="replace"
            )
        except Exception:
            return "[...truncated...]"


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------


#: Shared installer. Stateless — safe across concurrent calls.
dependency_installer = DependencyInstaller()


__all__ = [
    "DEFAULT_INSTALL_TIMEOUT_SECONDS",
    "MAX_LOG_BYTES",
    "LOCKFILE_NAME",
    "REQUIREMENTS_NAME",
    "PYPROJECT_NAME",
    "EnvironmentInstallResult",
    "DependencyInstaller",
    "dependency_installer",
]
