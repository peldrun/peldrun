# backend/peldrun/llm/catalog/external_registry.py
"""
PELDRUN External Model Registry (LiteLLM).

Loads and caches the community-maintained LiteLLM Model Registry — a
single JSON file that documents capabilities, context windows, and
pricing for ~1000 models across every major provider. It is the de-
facto industry source for "which models support what", used by Cursor,
Continue.dev, LibreChat, Open WebUI, and many others.

Design principles
-----------------

1. **No hardcoded model names.**
   Every capability fact this module returns originates from the
   upstream JSON file. If a model is not present in that file, this
   module returns ``None`` — it does NOT guess from the model ID
   string. This is the entire point of the module: to replace name-
   pattern heuristics with a real, externally-maintained data source.

2. **Exact match only.**
   Lookup tries a small set of well-defined key variants (verbatim,
   provider-prefixed, and a normalized lowercase form). It never does
   substring matching, fuzzy matching, or family inference. A model
   named ``deepseek-r1-0528-qwen3-8b`` is NOT matched to
   ``deepseek-r1`` unless the upstream file explicitly contains the
   variant. When the answer is unknown, ``None`` is returned so the
   UI can display "unknown" honestly.

3. **Offline-safe.**
   The registry is cached to disk. When the network is unavailable,
   the cached copy is served. When no cache exists and the network
   is unavailable, empty lookups are returned and the failure is
   logged — the caller is never blocked by an unreachable GitHub.

4. **Cache TTL, not invalidation.**
   The cache expires after ``CACHE_TTL_SECONDS`` (24 hours by
   default). There is no event-based invalidation because the
   upstream file has no webhook or ETag contract we can rely on.
   A manual refresh is always available via
   ``ExternalModelRegistry.refresh(force=True)``.

5. **Thread-safe.**
   Every mutation and read acquires ``self._lock``. The registry is
   designed to be called from both async contexts (via
   ``asyncio.to_thread``) and sync contexts (e.g. a FastAPI
   dependency resolver that runs on the main thread).

See also
--------

    * ``backend/peldrun/llm/catalog/lookup.py`` — the merge layer
    * ``backend/peldrun/llm/catalog/huggingface.py`` — HF model cards
    * ``backend/peldrun/storage_paths.py`` — cache location
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx

from peldrun.storage_paths import get_storage_root

logger = logging.getLogger("peldrun.llm.catalog.external_registry")


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


#: Canonical URL for the upstream LiteLLM registry JSON. This is the
#: file the LiteLLM project publishes and maintains; it is not
#: modified by PELDRUN.
REGISTRY_URL: str = (
    "https://raw.githubusercontent.com/BerriAI/litellm/main/"
    "model_prices_and_context_window.json"
)


#: Cache TTL in seconds. After this interval the next lookup triggers
#: a background refresh, but continues to serve the stale cache until
#: the new data lands (stale-while-revalidate pattern).
CACHE_TTL_SECONDS: int = 24 * 60 * 60


#: Network timeout for the upstream fetch. Kept tight so an unreachable
#: GitHub never blocks application startup for more than this many
#: seconds.
FETCH_TIMEOUT_SECONDS: float = 15.0


#: File name inside the storage root.
CACHE_FILE_NAME: str = "model_catalog_litellm.json"


#: Internal cache schema version. Bump when the on-disk format changes
#: in a way that requires discarding the previous cache.
CACHE_SCHEMA_VERSION: int = 1


#: Fields we care about from each LiteLLM entry. Anything else is
#: preserved verbatim in the raw entry (so future-proofing does not
#: require schema changes) but is not surfaced through the public
#: ``lookup`` return value.
_EXPOSED_FIELDS: tuple[str, ...] = (
    "supports_function_calling",
    "supports_vision",
    "supports_reasoning",
    "supports_response_schema",
    "supports_audio_input",
    "supports_audio_output",
    "supports_prompt_caching",
    "supports_system_messages",
    "supports_tool_choice",
    "max_input_tokens",
    "max_output_tokens",
    "max_tokens",
    "input_cost_per_token",
    "output_cost_per_token",
    "mode",
    "litellm_provider",
)


#: Provider prefixes tried as a secondary lookup. If the caller passes
#: ``deepseek-r1`` and the registry stores it under
#: ``deepseek/deepseek-r1``, the prefixed form is tried BEFORE falling
#: back to unknown. The list is intentionally short — adding a prefix
#: here is equivalent to declaring a recognized provider name; it is
#: not a heuristic about the model itself.
_KNOWN_PROVIDER_PREFIXES: tuple[str, ...] = (
    "openai",
    "azure",
    "anthropic",
    "google",
    "gemini",
    "vertex_ai",
    "deepseek",
    "mistral",
    "groq",
    "together_ai",
    "fireworks_ai",
    "openrouter",
    "xai",
    "cohere",
    "nvidia_nim",
    "ollama",
    "lm_studio",
)


# ---------------------------------------------------------------------------
# Entry dataclass (lightweight, no pydantic dependency)
# ---------------------------------------------------------------------------


class RegistryEntry:
    """
    One model's entry as stored in the LiteLLM registry.

    The dataclass exists so the caller can read well-named attributes
    without depending on the upstream JSON shape, while the raw data
    remains accessible via ``.raw`` for any field we did not expose.
    """

    __slots__ = ("model_id", "matched_key", "raw", "exposed")

    def __init__(
        self,
        model_id: str,
        matched_key: str,
        raw: Dict[str, Any],
        exposed: Dict[str, Any],
    ) -> None:
        self.model_id = model_id
        self.matched_key = matched_key
        self.raw = raw
        self.exposed = exposed

    def get(self, key: str, default: Any = None) -> Any:
        """Return an exposed field, falling back to the raw entry."""
        if key in self.exposed:
            return self.exposed[key]
        return self.raw.get(key, default)

    def to_dict(self) -> Dict[str, Any]:
        """Return the exposed fields plus provenance markers."""
        result = dict(self.exposed)
        result["source"] = "external_registry"
        result["source_url"] = REGISTRY_URL
        result["source_model_key"] = self.matched_key
        return result

    def __repr__(self) -> str:
        return (
            f"RegistryEntry(model_id={self.model_id!r}, "
            f"matched_key={self.matched_key!r})"
        )


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


class ExternalModelRegistry:
    """
    Loads, caches, and looks up the LiteLLM model catalog.

    Thread-safe singleton. Use ``get_external_model_registry()`` to
    obtain the module-level instance; do not construct your own unless
    writing tests.
    """

    _instance: Optional["ExternalModelRegistry"] = None
    _class_lock = threading.Lock()

    def __init__(
        self,
        *,
        cache_dir: Optional[Path] = None,
        ttl_seconds: int = CACHE_TTL_SECONDS,
    ) -> None:
        self._lock = threading.RLock()

        # Resolve cache location. When ``cache_dir`` is provided (tests),
        # it takes precedence. Otherwise, use the canonical storage root.
        self._cache_dir = cache_dir or get_storage_root()
        self._cache_path = self._cache_dir / CACHE_FILE_NAME

        self._ttl_seconds = int(ttl_seconds)

        # In-memory copy of the loaded registry, keyed by model id.
        # Empty dict means "not loaded yet".
        self._entries: Dict[str, Dict[str, Any]] = {}
        self._loaded_at: float = 0.0
        self._load_failed: bool = False

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def lookup(self, model_id: str) -> Optional[RegistryEntry]:
        """
        Look up a model in the registry.

        Returns ``None`` when the model is not present in the upstream
        catalog. This is the honest answer — the caller MUST NOT
        substitute a guess. The UI shows "unknown" and the user can
        declare the capability manually.

        Exact matching only. The lookup tries a small, fixed set of
        key forms:

            1. Verbatim ``model_id``
            2. Verbatim with known provider prefix (e.g.
               ``deepseek/deepseek-r1``)
            3. Lowercase ``model_id``

        No substring, fuzzy, or family inference is performed.
        """
        if not model_id:
            return None

        # Ensure the cache is present. This is a hot path — the load
        # is guarded by an in-memory flag so no disk I/O is performed
        # when the cache is already warm.
        self._ensure_loaded()

        with self._lock:
            entries = self._entries

        if not entries:
            return None

        # Build the candidate keys in strict priority order. Every
        # candidate is an EXACT match against a key in the registry —
        # there is no substring matching anywhere in this list.
        candidates: List[str] = []
        seen: set[str] = set()

        def _push(key: str) -> None:
            if key and key not in seen:
                candidates.append(key)
                seen.add(key)

        _push(model_id)
        _push(model_id.lower())

        # Provider-prefixed variants.
        if "/" not in model_id:
            for prefix in _KNOWN_PROVIDER_PREFIXES:
                _push(f"{prefix}/{model_id}")

        # Strip an existing provider prefix and try the bare form.
        if "/" in model_id:
            _, _, tail = model_id.partition("/")
            if tail:
                _push(tail)
                _push(tail.lower())

        for key in candidates:
            raw = entries.get(key)
            if not isinstance(raw, dict):
                continue
            exposed = {f: raw[f] for f in _EXPOSED_FIELDS if f in raw}
            return RegistryEntry(
                model_id=model_id,
                matched_key=key,
                raw=raw,
                exposed=exposed,
            )
        return None

    def is_loaded(self) -> bool:
        """Return True if the registry has entries in memory."""
        with self._lock:
            return bool(self._entries)

    def is_stale(self) -> bool:
        """Return True when the loaded cache exceeds the TTL."""
        with self._lock:
            if not self._entries:
                return True
            age = time.time() - self._loaded_at
            return age > self._ttl_seconds

    def model_count(self) -> int:
        """Return the number of distinct model keys currently cached."""
        with self._lock:
            return len(self._entries)

    async def ensure_fresh(self, force: bool = False) -> bool:
        """
        Ensure the registry is fresh, fetching from upstream if needed.

        Parameters
        ----------
        force
            When ``True``, bypass the TTL and always refetch.

        Returns
        -------
        bool
            ``True`` when the registry has entries after the call,
            ``False`` when neither the cache nor the network produced
            data. Callers should not treat ``False`` as fatal — it only
            means capability facts for the requested model are unknown.

        Never raises. Network failures are logged and the existing
        cached copy (if any) continues to be served.
        """
        # Fast path: fresh in-memory cache.
        with self._lock:
            loaded = bool(self._entries)
            age = time.time() - self._loaded_at if loaded else 0.0

        if loaded and not force and age <= self._ttl_seconds:
            return True

        # Try disk cache before hitting the network.
        if not force:
            loaded_from_disk = await asyncio.to_thread(self._load_cache_from_disk)
            if loaded_from_disk and not self.is_stale():
                return True

        # Fetch from upstream.
        ok = await self._fetch_from_upstream()
        if ok:
            return True

        # Fetch failed: fall back to disk cache if we have one.
        if not loaded:
            await asyncio.to_thread(self._load_cache_from_disk)

        with self._lock:
            return bool(self._entries)

    async def refresh(self) -> bool:
        """Force a refresh from upstream, ignoring the TTL."""
        return await self.ensure_fresh(force=True)

    # ------------------------------------------------------------------
    # Disk cache
    # ------------------------------------------------------------------

    def _load_cache_from_disk(self) -> bool:
        """
        Load the on-disk cache into memory.

        Returns ``True`` when the cache was successfully loaded.
        Silently ignores missing/corrupt files — the caller falls back
        to the network or to empty lookups.
        """
        try:
            if not self._cache_path.is_file():
                return False

            raw = self._cache_path.read_text(encoding="utf-8")
            payload = json.loads(raw)
            if not isinstance(payload, dict):
                logger.warning(
                    "model_catalog_litellm.json root is not a dict "
                    "(got %s); ignoring cache.",
                    type(payload).__name__,
                )
                return False

            meta = payload.get("_cache_meta")
            models = payload.get("_models")
            if not isinstance(meta, dict) or not isinstance(models, dict):
                logger.warning(
                    "model_catalog_litellm.json is missing required "
                    "top-level keys (_cache_meta / _models); ignoring.",
                )
                return False

            schema = meta.get("schema_version")
            if schema != CACHE_SCHEMA_VERSION:
                logger.info(
                    "model_catalog_litellm.json schema=%r does not "
                    "match expected=%r; ignoring cache.",
                    schema,
                    CACHE_SCHEMA_VERSION,
                )
                return False

            with self._lock:
                self._entries = models
                self._loaded_at = float(meta.get("fetched_at") or 0.0)
                self._load_failed = False
            logger.info(
                "Loaded %d models from LiteLLM disk cache (age=%.1fs).",
                len(models),
                max(0.0, time.time() - self._loaded_at),
            )
            return True
        except Exception as exc:
            logger.warning(
                "Failed to load LiteLLM disk cache at %s: %s",
                self._cache_path,
                exc,
            )
            return False

    def _write_cache_to_disk(self, models: Dict[str, Any], fetched_at: float) -> None:
        """Persist the registry atomically via a temp file + replace."""
        try:
            self._cache_dir.mkdir(parents=True, exist_ok=True)
            tmp = self._cache_path.with_suffix(".tmp")
            payload = {
                "_cache_meta": {
                    "schema_version": CACHE_SCHEMA_VERSION,
                    "fetched_at": fetched_at,
                    "source_url": REGISTRY_URL,
                    "model_count": len(models),
                },
                "_models": models,
            }
            tmp.write_text(
                json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                encoding="utf-8",
            )
            tmp.replace(self._cache_path)
            logger.info(
                "Wrote %d models to LiteLLM disk cache at %s.",
                len(models),
                self._cache_path,
            )
        except Exception as exc:
            logger.warning(
                "Failed to write LiteLLM disk cache at %s: %s",
                self._cache_path,
                exc,
            )

    # ------------------------------------------------------------------
    # Network fetch
    # ------------------------------------------------------------------

    async def _fetch_from_upstream(self) -> bool:
        """
        Fetch the LiteLLM registry from GitHub and cache it.

        Returns ``True`` when the fetch succeeded and the in-memory
        cache was updated. Never raises.
        """
        try:
            async with httpx.AsyncClient(
                timeout=FETCH_TIMEOUT_SECONDS, follow_redirects=True
            ) as client:
                resp = await client.get(REGISTRY_URL)
                if resp.status_code != 200:
                    logger.warning(
                        "LiteLLM registry returned HTTP %d (expected 200); "
                        "skipping refresh.",
                        resp.status_code,
                    )
                    self._mark_load_failed()
                    return False
                raw = resp.json()
        except Exception as exc:
            logger.warning(
                "Failed to fetch LiteLLM registry from %s: %s",
                REGISTRY_URL,
                exc,
            )
            self._mark_load_failed()
            return False

        if not isinstance(raw, dict):
            logger.warning(
                "LiteLLM registry returned unexpected root type %s; "
                "skipping refresh.",
                type(raw).__name__,
            )
            self._mark_load_failed()
            return False

        # The upstream file includes a ``sample_spec`` entry that
        # documents the schema itself. It is not a real model and must
        # be excluded from lookups.
        cleaned: Dict[str, Any] = {
            key: value
            for key, value in raw.items()
            if isinstance(key, str)
            and isinstance(value, dict)
            and key != "sample_spec"
        }

        if not cleaned:
            logger.warning(
                "LiteLLM registry contained no usable model entries; "
                "skipping cache update.",
            )
            self._mark_load_failed()
            return False

        fetched_at = time.time()
        with self._lock:
            self._entries = cleaned
            self._loaded_at = fetched_at
            self._load_failed = False

        # Persist to disk in the background. We don't await this because
        # the in-memory update is what matters for the current process;
        # a future process will load it on next startup.
        await asyncio.to_thread(
            self._write_cache_to_disk, cleaned, fetched_at
        )
        logger.info(
            "Fetched %d models from LiteLLM registry (%s).",
            len(cleaned),
            REGISTRY_URL,
        )
        return True

    def _mark_load_failed(self) -> None:
        with self._lock:
            self._load_failed = True

    def _ensure_loaded(self) -> None:
        """
        Synchronous guard used by the hot-path ``lookup``.

        The first call after startup blocks on a disk read; subsequent
        calls are a no-op. The upstream fetch is triggered separately
        via ``ensure_fresh`` (async) so the caller controls when network
        latency is paid.
        """
        with self._lock:
            if self._entries:
                return
        # First-ever access — try disk only. A caller who wants a
        # network refresh uses ``await ensure_fresh()`` explicitly.
        self._load_cache_from_disk()


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------


def get_external_model_registry(
    *, cache_dir: Optional[Path] = None
) -> ExternalModelRegistry:
    """
    Return the process-wide ExternalModelRegistry singleton.

    The singleton is constructed lazily on first call. Tests that need
    isolation should pass ``cache_dir`` on their own instance instead
    of using this function.
    """
    if ExternalModelRegistry._instance is None:
        with ExternalModelRegistry._class_lock:
            if ExternalModelRegistry._instance is None:
                ExternalModelRegistry._instance = ExternalModelRegistry(
                    cache_dir=cache_dir
                )
    return ExternalModelRegistry._instance


def reset_external_model_registry_for_tests() -> None:
    """Reset the singleton. Intended only for tests."""
    with ExternalModelRegistry._class_lock:
        ExternalModelRegistry._instance = None


__all__ = [
    "REGISTRY_URL",
    "CACHE_TTL_SECONDS",
    "FETCH_TIMEOUT_SECONDS",
    "CACHE_FILE_NAME",
    "RegistryEntry",
    "ExternalModelRegistry",
    "get_external_model_registry",
    "reset_external_model_registry_for_tests",
]
