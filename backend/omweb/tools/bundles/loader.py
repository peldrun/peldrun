"""
Python module loading for installed tool bundles.

Responsibilities:
  * Import a tool module from inside an immutable blob directory.
  * Instantiate the appropriate SovereignBaseTool subclass.
  * Bind the workspace root to the instantiated tool.
  * Manage sys.path cleanly so bundle-scoped helper imports work during
    load time, without leaking into the process afterwards.

Safety invariants:
  * No code is imported from outside the blob directory.
  * sys.path is always restored, even on failure.
  * A unique module name is generated per load to avoid sys.modules
    pollution when the same tool is loaded more than once.
  * Any exception from user code is wrapped in BundleLoadError with
    enough context to debug.
"""

from __future__ import annotations

import importlib.util
import logging
import sys
import uuid
from pathlib import Path
from types import ModuleType
from typing import Any, List, Optional

from omweb.tools.base import SovereignBaseTool
from omweb.tools.bundles.errors import BundleLoadError

logger = logging.getLogger("omweb.tools.bundles.loader")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def load_tool_from_bundle(
    blob_dir: Path,
    entry_point: str,
    *,
    class_name: Optional[str] = None,
    workspace_root: Optional[str] = None,
    bundle_id: Optional[str] = None,
) -> SovereignBaseTool:
    """
    Import the entry-point module from a blob directory and instantiate a
    tool from it.

    Args:
        blob_dir: Absolute path to the committed blob directory (contains
            manifest.json, the entry .py, and any helpers).
        entry_point: Relative path from blob_dir to the entry .py.
        class_name: Explicit class name to instantiate. If None, the first
            SovereignBaseTool subclass found in the module is used.
        workspace_root: Optional workspace to bind to the tool instance.
        bundle_id: Optional identifier used in error messages and module
            naming. If omitted, derived from blob_dir.name.

    Returns:
        An instantiated SovereignBaseTool ready for execution.

    Raises:
        BundleLoadError: on any failure (missing file, import error,
            no valid class, instantiation error).
    """
    blob_dir = Path(blob_dir).resolve()
    if not blob_dir.is_dir():
        raise BundleLoadError(
            f"Blob directory does not exist: {blob_dir}",
            details={"blob_dir": str(blob_dir)},
        )

    entry_file = (blob_dir / entry_point).resolve()
    try:
        entry_file.relative_to(blob_dir)
    except ValueError:
        raise BundleLoadError(
            f"Entry point {entry_point!r} escapes blob directory.",
            details={"entry_point": entry_point, "blob_dir": str(blob_dir)},
        )

    if not entry_file.is_file():
        raise BundleLoadError(
            f"Entry point not found: {entry_file}",
            details={"entry_point": entry_point, "blob_dir": str(blob_dir)},
        )

    # ── 1. Temporarily prepend blob_dir to sys.path so that bundle-internal
    #       imports (e.g. `from helpers import util`) resolve during load.
    added_path: Optional[str] = None
    blob_dir_str = str(blob_dir)
    if blob_dir_str not in sys.path:
        sys.path.insert(0, blob_dir_str)
        added_path = blob_dir_str

    # Unique module name — avoids sys.modules collisions on re-load.
    unique = uuid.uuid4().hex[:8]
    safe_id = (bundle_id or blob_dir.name).replace("-", "_").replace(".", "_")
    module_name = f"_peldrun_bundle_{safe_id}_{unique}"

    try:
        module = _import_module_from_path(module_name, entry_file)
    except Exception as exc:
        raise BundleLoadError(
            f"Failed to import bundle entry point {entry_point!r}: {exc}",
            details={
                "entry_point": entry_point,
                "bundle_id": bundle_id,
                "exception_type": type(exc).__name__,
            },
        ) from exc
    finally:
        # Restore sys.path immediately after import. The module's own
        # module-global namespace retains references to any helper modules
        # it imported, so removing blob_dir does not break runtime calls.
        if added_path is not None:
            try:
                sys.path.remove(added_path)
            except ValueError:
                pass

    # ── 2. Locate the tool class
    tool_class = _resolve_tool_class(module, class_name, bundle_id)

    # ── 3. Instantiate
    try:
        instance = tool_class()
    except Exception as exc:
        raise BundleLoadError(
            f"Failed to instantiate tool class {tool_class.__name__!r}: {exc}",
            details={
                "class_name": tool_class.__name__,
                "bundle_id": bundle_id,
                "exception_type": type(exc).__name__,
            },
        ) from exc

    if not isinstance(instance, SovereignBaseTool):
        raise BundleLoadError(
            f"Instantiated object is not a SovereignBaseTool: "
            f"{type(instance).__name__}",
            details={"bundle_id": bundle_id},
        )

    # ── 4. Bind workspace root (best-effort, both protocols supported)
    if workspace_root:
        _bind_workspace(instance, str(workspace_root))

    return instance


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------

def _import_module_from_path(module_name: str, file_path: Path) -> ModuleType:
    """Import a Python file as a module with a unique, non-cached name."""
    spec = importlib.util.spec_from_file_location(module_name, str(file_path))
    if spec is None or spec.loader is None:
        raise BundleLoadError(
            f"Could not create import spec for {file_path}",
            details={"module_name": module_name},
        )

    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        # Don't leave a half-initialized module in sys.modules
        sys.modules.pop(module_name, None)
        raise
    return module


def _resolve_tool_class(
    module: ModuleType,
    class_name: Optional[str],
    bundle_id: Optional[str],
) -> type:
    """
    Find a SovereignBaseTool subclass in the module.

    If class_name is given, only that exact name is considered. Otherwise,
    the first subclass found in source order is returned.
    """
    if class_name:
        attr = getattr(module, class_name, None)
        if attr is None:
            raise BundleLoadError(
                f"class_name {class_name!r} not found in bundle module.",
                details={"bundle_id": bundle_id, "class_name": class_name},
            )
        if not (isinstance(attr, type) and issubclass(attr, SovereignBaseTool)):
            raise BundleLoadError(
                f"{class_name!r} is not a SovereignBaseTool subclass.",
                details={"bundle_id": bundle_id, "class_name": class_name},
            )
        if attr is SovereignBaseTool:
            raise BundleLoadError(
                f"{class_name!r} refers to the abstract base class itself.",
                details={"bundle_id": bundle_id},
            )
        return attr

    # First subclass found
    for attr_name in dir(module):
        attr = getattr(module, attr_name)
        if (
            isinstance(attr, type)
            and issubclass(attr, SovereignBaseTool)
            and attr is not SovereignBaseTool
        ):
            return attr

    raise BundleLoadError(
        "No SovereignBaseTool subclass found in bundle entry point.",
        details={"bundle_id": bundle_id},
    )


def _bind_workspace(instance: SovereignBaseTool, workspace_root: str) -> None:
    """Best-effort workspace root injection across both supported protocols."""
    if hasattr(instance, "set_workspace_root") and callable(instance.set_workspace_root):
        try:
            instance.set_workspace_root(workspace_root)
            return
        except Exception as exc:
            logger.debug("set_workspace_root failed for %s: %s", instance.name, exc)

    if hasattr(instance, "workspace_root"):
        try:
            setattr(instance, "workspace_root", workspace_root)
        except Exception as exc:
            logger.debug("attribute injection failed for %s: %s", instance.name, exc)


__all__ = ["load_tool_from_bundle"]
