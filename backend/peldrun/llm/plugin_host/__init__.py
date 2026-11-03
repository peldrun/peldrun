# backend/peldrun/llm/plugin_host/__init__.py
"""
PELDRUN Plugin Host (M9 phase 2).

Executes externally-installed Provider Packages in isolated worker
subprocesses. The host mediates every interaction between the core
runtime and a package:

    Supervisor (main process)
        │  spawns + manages
        ▼
    Worker (subprocess, per-package venv)
        │  loads
        ▼
    Adapter (installed Provider Package)

Why a separate process per package
----------------------------------

1. **Dependency isolation at runtime.** A package installed with its
   own venv (see ``omweb/provider_packages/dependency_installer.py``)
   can only be safely imported by an interpreter whose ``sys.path``
   points at that venv. Spawning the worker with the venv's python
   gives full isolation without polluting the main process.

2. **Fault isolation.** A crashing adapter (segfault, OOM, unhandled
   exception loop) kills only the worker. The main backend process
   and every other provider remain unaffected.

3. **Permission enforcement.** The worker communicates with the
   Supervisor over a narrow, versioned IPC protocol. It never has
   direct access to the main process's memory, connections registry,
   or filesystem beyond its own package directory and the venv.

4. **Cancellation without state leakage.** Cancelling a streaming
   response terminates the worker cleanly. The next request gets a
   fresh worker — no thread pool, no shared asyncio loop, no
   lingering HTTP connections.

Threat model
------------

This module is NOT a sandbox. A package that obtains a Python
interpreter can execute arbitrary code with the user's privileges.
That is a fundamental property of pip-installed Python code.

What this module DOES guarantee:

    * The worker cannot read other packages' secrets via the shared
      PELDRUN registry — it only receives its own connection context.
    * The worker cannot corrupt the registry or the metadata vault —
      those files are not opened by the worker process.
    * A misbehaving worker is killed and reaped; it cannot orphan
      subprocesses or hold the main backend's event loop.
    * A package installed via ``/api/store/providers/*`` is subject to
      the M9 administrative validation (manifest, permissions, ZIP
      safety) BEFORE its worker is ever spawned.

OS-level sandboxing (seccomp, AppContainer, entitlements) is a future
enhancement. It is orthogonal to this module's design — the protocol
and supervisor API do not need to change to add it.

Sub-modules
-----------

    * ``protocol``    — versioned message schema and constants.
    * ``transport``   — the JSON-lines channel over stdin/stdout.
    * ``worker``      — the process that loads the adapter and serves.
    * ``supervisor``  — the main-process manager: spawn, call, cancel.
    * ``permissions`` — runtime enforcer for the manifest's declared
                        permissions.

See also
--------

    * ``backend/omweb/provider_packages/`` — the administrative layer
    * ``backend/peldrun/llm/providers/custom_plugin.py`` — the
      ``ProviderAdapter`` wrapper that the factory returns
"""

__all__: list = []
