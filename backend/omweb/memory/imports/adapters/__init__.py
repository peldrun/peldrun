# backend/omweb/memory/imports/adapters/__init__.py
"""
PELDRUN Memory Platform - Import Adapter Registry.

Single import surface for the three built-in adapters PLUS the
machinery the ``ImportManager`` uses to select one based on the
source shape.

Selection order -- why it matters
---------------------------------

The three adapters are tried in this fixed order:

    1. ``JsonAdapter``     -- narrow detection
                              (``manifest`` key, ``.zip`` extension,
                              ``PK`` magic bytes, or a directory
                              containing ``manifest.json``).
    2. ``MarkdownAdapter`` -- accepts ANY directory plus
                              ``.md``/``.markdown`` files and dicts
                              with a ``content`` key.
    3. ``SqliteAdapter``   -- ``.db``/``.sqlite`` extensions,
                              SQLite magic bytes, or dicts with
                              a ``path``/``db_path`` key.

The order is LOAD-BEARING for two cases:

    * **Directory dispatch.** ``JsonAdapter`` claims a directory
      only when ``manifest.json`` exists at the root. Without
      that check ``MarkdownAdapter`` would claim EVERY directory
      (it accepts any dir as a potential markdown tree). The
      order ensures a peldrun-memory-package directory is
      correctly identified as JSON, not mistakenly parsed as
      markdown.

    * **Dict dispatch with overlapping keys.** A dict could
      theoretically carry BOTH ``manifest`` and ``content``
      keys (e.g. a wrapper produced by a future caller).
      ``JsonAdapter``'s requirement of ``manifest`` is the
      MORE SPECIFIC signal -- it explicitly declares "this is
      a peldrun-memory-package". ``Markdown``'s ``content``
      key is more generic. Trying Json first is therefore
      correct.

For ``Path`` (file) and ``bytes`` sources, the extensions and
magic bytes are disjoint across the three adapters, so the order
has no effect. The rule "Json first, Markdown second, Sqlite
third" is enforced uniformly -- no conditional logic tries a
different order for a different source type. Predictability
beats micro-optimization.

Third-party adapters
--------------------

``register_adapter(adapter_class)`` appends a new adapter to the
END of the priority list. It cannot displace a built-in; a
plugin that needs to override, say, markdown handling would
have to be a deliberate P2 feature (per-id override).

The mechanism is intentionally narrow: this module is not a
plugin discovery system, it is a well-ordered list of known
adapters plus a hook for future additions. Discovery of installed
packages is the job of a future ``provider_packages``-style
subsystem, not of this module.

Instance lifecycle
------------------

``select_adapter(source, port, settings)`` INSTANTIATES the
chosen adapter on every call. Rationale:

    * The adapter holds ``port`` and ``settings`` references.
      Both can change between imports (settings reload, provider
      rebuild). A cached instance would bind to stale references.
    * Adapter construction is trivial (assign two attributes).
      No I/O, no schema inspection, no state to warm up.
    * The cost is ~2 microseconds per candidate -- invisible
      next to the parsing work that follows.

The alternative -- caching instances per (adapter_id, port,
settings) triple -- adds cache-invalidation complexity for zero
measurable benefit. Rejected.
"""

from __future__ import annotations

import logging
from typing import Any, List, Optional, Tuple, Type

from peldrun.memory.domain import (
    MemoryPort,
    MemorySettings,
    MemoryValidationError,
)

from omweb.memory.imports.base_adapter import BaseImportAdapter
from omweb.memory.imports.adapters.json_adapter import JsonAdapter
from omweb.memory.imports.adapters.markdown_adapter import MarkdownAdapter
from omweb.memory.imports.adapters.sqlite_adapter import SqliteAdapter


logger = logging.getLogger("omweb.memory.imports.adapters")


# ---------------------------------------------------------------------------
# Built-in adapter priority list
# ---------------------------------------------------------------------------
#
# ORDER IS LOAD-BEARING. See the module docstring for the exact
# cases where it matters (directory dispatch, dict with
# overlapping keys).
#
# To add a new BUILT-IN adapter:
#   1. Implement it in its own module.
#   2. Add one entry to this tuple.
#   3. Consider whether its `can_handle` overlaps an existing
#      adapter's -- if so, place it accordingly.
#
# To add a THIRD-PARTY adapter:
#   Use ``register_adapter(YourAdapterClass)``. It appends to the
#   END of the priority list, which is correct for any adapter
#   that does not overlap the built-ins.

_BUILTIN_ADAPTERS: Tuple[Type[BaseImportAdapter], ...] = (
    JsonAdapter,
    MarkdownAdapter,
    SqliteAdapter,
)


#: Mutable copy that ``register_adapter`` extends. Kept separate
#: from ``_BUILTIN_ADAPTERS`` so a test can register a fake
#: adapter without permanently modifying the module's notion of
#: "the built-ins".
_RUNTIME_ADAPTERS: List[Type[BaseImportAdapter]] = list(
    _BUILTIN_ADAPTERS
)


# ---------------------------------------------------------------------------
# Short source representation for logs / errors
# ---------------------------------------------------------------------------


def _short_source_repr(source: Any) -> str:
    """Return a short, safe string representation of ``source``.

    Truncates long byte strings and dict reprs so that a diagnostic
    message does not flood the UI or log. Never raises -- any
    failure to stringify falls back to the type name.
    """
    try:
        if isinstance(source, bytes):
            return f"<bytes len={len(source)}>"
        if isinstance(source, dict):
            keys = list(source.keys())[:5]
            return f"<dict keys={keys}>"
        if hasattr(source, "__class__"):
            class_name = type(source).__name__
            if class_name == "PosixPath" or class_name == "WindowsPath":
                return str(source)
            return f"<{class_name}>"
        return str(source)[:80]
    except Exception:
        return f"<{type(source).__name__}>"


# ---------------------------------------------------------------------------
# Selection
# ---------------------------------------------------------------------------


def select_adapter(
    source: Any,
    port: MemoryPort,
    settings: MemorySettings,
) -> BaseImportAdapter:
    """Return the first adapter that can handle ``source``.

    Iterates ``_RUNTIME_ADAPTERS`` in priority order and returns
    a FRESHLY INSTANTIATED adapter -- see the module docstring
    for why instances are not cached.

    Parameters
    ----------
    source
        The caller's raw input. The adapter's ``can_handle`` is
        the ONLY thing that runs during selection -- no parsing,
        no I/O, no database inspection.
    port
        The ``MemoryPort`` the selected adapter will query for
        dedupe analysis and write to during apply.
    settings
        The current ``MemorySettings`` snapshot.

    Returns
    -------
    BaseImportAdapter
        A ready-to-use adapter instance. The caller invokes
        ``.preview()`` or ``.apply()`` as needed.

    Raises
    ------
    MemoryValidationError
        When NO registered adapter matches ``source``. The
        message names every adapter that was tried (with its
        description) so the operator can investigate.

    Notes on failure handling
    -------------------------
    Two distinct failure modes are handled per-adapter WITHOUT
    aborting the whole selection:

      * **Construction failure.** An adapter whose ``__init__``
        raises (misconfigured port, wrong settings type) is
        logged at DEBUG and skipped. The NEXT adapter gets its
        turn. This is important because a broken third-party
        adapter must not block the built-ins.

      * **``can_handle`` failure.** An adapter whose
        ``can_handle`` raises (a bug in a subclass) is logged
        at WARNING and skipped for the same reason.

    In both cases the skip is silent-ish (DEBUG for
    construction, WARNING for can_handle) but never fatal --
    only "no adapter matched at all" raises.
    """
    for adapter_cls in _RUNTIME_ADAPTERS:
        # ----- Construct the adapter ------------------------------
        try:
            instance = adapter_cls(port=port, settings=settings)
        except Exception as exc:
            logger.debug(
                "Adapter %s could not be constructed during "
                "selection; skipping. error=%s",
                getattr(adapter_cls, "adapter_id", adapter_cls.__name__),
                exc,
            )
            continue

        # ----- Ask it about the source ----------------------------
        try:
            matched = instance.can_handle(source)
        except Exception as exc:
            logger.warning(
                "Adapter %s raised during can_handle; skipping. "
                "source=%s error=%s",
                instance.adapter_id,
                _short_source_repr(source),
                exc,
            )
            continue

        if matched:
            logger.info(
                "Adapter selected for source=%s: %s",
                _short_source_repr(source),
                instance.adapter_id,
            )
            return instance

    # ----- No match -- raise with a diagnostic listing ------------
    adapter_lines = []
    for idx, cls in enumerate(_RUNTIME_ADAPTERS, start=1):
        aid = getattr(cls, "adapter_id", cls.__name__)
        desc = getattr(cls, "description", "(no description)")
        adapter_lines.append(f"  {idx}. {aid} -- {desc}")
    adapter_list = "\n".join(adapter_lines)

    raise MemoryValidationError(
        f"No import adapter can handle this source.\n"
        f"  source type: {type(source).__name__}\n"
        f"  source:      {_short_source_repr(source)}\n"
        f"  registered adapters (in priority order):\n"
        f"{adapter_list}",
        field_name="source",
        context={
            "source_type": type(source).__name__,
            "registered_adapters": [
                getattr(cls, "adapter_id", cls.__name__)
                for cls in _RUNTIME_ADAPTERS
            ],
        },
    )


def get_adapter_by_id(
    adapter_id: str,
    port: MemoryPort,
    settings: MemorySettings,
) -> BaseImportAdapter:
    """Return a specific adapter instance by its ``adapter_id``.

    Used when the caller (typically the UI's "Force adapter"
    dropdown) wants to bypass automatic selection. Useful for
    debugging or when the caller knows the source shape better
    than the auto-detection heuristic.

    Parameters
    ----------
    adapter_id
        One of the registered ``adapter_id`` values. Match is
        case-insensitive and whitespace-trimmed.
    port, settings
        Same as ``select_adapter``.

    Raises
    ------
    MemoryValidationError
        When no adapter has that ``adapter_id``.
    """
    if not isinstance(adapter_id, str):
        raise MemoryValidationError(
            f"adapter_id must be a string, got "
            f"{type(adapter_id).__name__}",
            field_name="adapter_id",
        )
    clean = adapter_id.strip().lower()
    if not clean:
        raise MemoryValidationError(
            "adapter_id must not be empty",
            field_name="adapter_id",
        )

    for cls in _RUNTIME_ADAPTERS:
        cls_id = getattr(cls, "adapter_id", "").lower()
        if cls_id == clean:
            return cls(port=port, settings=settings)

    available = sorted(
        getattr(cls, "adapter_id", cls.__name__)
        for cls in _RUNTIME_ADAPTERS
    )
    raise MemoryValidationError(
        f"Unknown adapter_id {adapter_id!r}. Available: {available}",
        field_name="adapter_id",
        context={"available": available},
    )


# ---------------------------------------------------------------------------
# Introspection
# ---------------------------------------------------------------------------


def list_adapters() -> List[dict]:
    """Return metadata for every registered adapter.

    Each entry is a dict:

        {
          "adapter_id": str,      # canonical id
          "description": str,     # human-readable
          "is_builtin": bool,     # True if registered at import time
        }

    Ordered by priority (the order ``select_adapter`` tries them
    in). Suitable for direct serialization to the UI's adapter
    dropdown.
    """
    builtin_ids = {
        getattr(cls, "adapter_id", cls.__name__)
        for cls in _BUILTIN_ADAPTERS
    }
    result: List[dict] = []
    for cls in _RUNTIME_ADAPTERS:
        aid = getattr(cls, "adapter_id", cls.__name__)
        result.append(
            {
                "adapter_id": aid,
                "description": getattr(cls, "description", ""),
                "is_builtin": aid in builtin_ids,
            }
        )
    return result


def is_registered(adapter_id: str) -> bool:
    """Return True when ``adapter_id`` resolves to a registered adapter."""
    if not isinstance(adapter_id, str):
        return False
    clean = adapter_id.strip().lower()
    for cls in _RUNTIME_ADAPTERS:
        if getattr(cls, "adapter_id", "").lower() == clean:
            return True
    return False


# ---------------------------------------------------------------------------
# Extension hooks
# ---------------------------------------------------------------------------


def register_adapter(
    adapter_class: Type[BaseImportAdapter],
) -> None:
    """Register a THIRD-PARTY adapter at the end of the priority list.

    The adapter is appended, not prepended. Built-ins always take
    precedence over third-party adapters for a given source, so
    that a misbehaving plugin cannot accidentally intercept
    well-known source shapes.

    Parameters
    ----------
    adapter_class
        A concrete ``BaseImportAdapter`` subclass with a
        non-empty ``adapter_id`` attribute.

    Raises
    ------
    TypeError
        When ``adapter_class`` is not a ``BaseImportAdapter``
        subclass, or has no usable ``adapter_id``.

    Notes
    -----
    * Re-registering an existing ``adapter_id`` OVERWRITES the
      previous class silently (DEBUG log). Tests need this for
      fake adapters; production SHOULD unregister first.
    * There is no priority parameter. Append-only is deliberate
      -- see the module docstring for why.
    """
    if (
        not isinstance(adapter_class, type)
        or not issubclass(adapter_class, BaseImportAdapter)
    ):
        raise TypeError(
            f"adapter_class must be a BaseImportAdapter subclass, "
            f"got {adapter_class!r}"
        )
    aid = getattr(adapter_class, "adapter_id", None)
    if not isinstance(aid, str) or not aid.strip():
        raise TypeError(
            f"adapter_class must define a non-empty 'adapter_id' "
            f"attribute; got {aid!r}"
        )

    # Replace any existing registration with the same id (keeps
    # the list from growing with duplicates across tests).
    clean = aid.strip().lower()
    for idx, cls in enumerate(_RUNTIME_ADAPTERS):
        if getattr(cls, "adapter_id", "").lower() == clean:
            logger.debug(
                "Replacing registered adapter %r "
                "(was %s, now %s).",
                clean,
                cls.__name__,
                adapter_class.__name__,
            )
            _RUNTIME_ADAPTERS[idx] = adapter_class
            return

    _RUNTIME_ADAPTERS.append(adapter_class)
    logger.info(
        "Registered import adapter %r -> %s.",
        clean,
        adapter_class.__name__,
    )


def unregister_adapter(adapter_id: str) -> bool:
    """Remove an adapter registration by id.

    Returns True when a registration existed and was removed,
    False otherwise. Idempotent.

    Primarily for tests (teardown after ``register_adapter``).
    Can also remove BUILT-IN adapters -- used by tests that want
    to verify "no adapter matched" paths without modifying the
    built-in list permanently (a companion ``register_adapter``
    restores it).
    """
    if not isinstance(adapter_id, str):
        return False
    clean = adapter_id.strip().lower()
    for idx, cls in enumerate(_RUNTIME_ADAPTERS):
        if getattr(cls, "adapter_id", "").lower() == clean:
            del _RUNTIME_ADAPTERS[idx]
            logger.info("Unregistered import adapter %r.", clean)
            return True
    return False


def _reset_adapters_to_builtins() -> None:
    """Restore the runtime adapter list to the pristine built-ins.

    Test helper. Not exported via ``__all__`` -- application code
    has no reason to reset the registry mid-process. Tests use
    this in teardown to guarantee isolation from any
    ``register_adapter`` calls made by a previous test.
    """
    _RUNTIME_ADAPTERS.clear()
    _RUNTIME_ADAPTERS.extend(_BUILTIN_ADAPTERS)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


__all__ = [
    # Selection
    "select_adapter",
    "get_adapter_by_id",
    # Introspection
    "list_adapters",
    "is_registered",
    # Extension
    "register_adapter",
    "unregister_adapter",
    # Adapter classes (re-exported for direct use in tests and
    # in callers that want to bypass the registry)
    "JsonAdapter",
    "MarkdownAdapter",
    "SqliteAdapter",
    "BaseImportAdapter",
]
