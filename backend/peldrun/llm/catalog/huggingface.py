# backend/peldrun/llm/catalog/huggingface.py
"""
PELDRUN HuggingFace Model Config Loader.

Fetches the ``config.json`` file that ships with every HuggingFace
model repository and extracts the fields Transformers uses to
determine architecture, modality, and context limits.

Design principles
-----------------

1. **Only declared fields.**
   ``config.json`` is published by the model author. It is the
   authoritative source for a small, well-defined set of facts:
   architecture names, ``vision_config`` presence, ``audio_config``
   presence, ``model_type``, and ``max_position_embeddings``. This
   module extracts THOSE fields and only those fields.

2. **No reasoning / no tools inference.**
   ``config.json`` contains no field for reasoning capability or for
   function-calling support. Those capabilities CANNOT be derived
   from this file. This module returns ``None`` for both, and the
   caller (``lookup.py``) treats that as "unknown" — not as "false".

3. **Vision via ``vision_config`` only.**
   When ``config.json`` contains a ``vision_config`` block, the model
   unambiguously has a vision encoder (Transformers will only run if
   it is present). When the block is absent, we report "unknown" —
   we do NOT scan ``architectures`` strings for substrings like
   ``"VL"`` because that would reintroduce the heuristic this whole
   subsystem exists to eliminate.

4. **Repo-ID gating.**
   HuggingFace requires ``owner/repo`` form. If the requested model
   ID does not contain a ``/``, this module returns ``None``
   immediately. It does NOT attempt to guess an owner — that is
   inference, and inference is forbidden.

5. **90-day cache, per-model files.**
   HF configs change rarely. Each fetched model is stored in its own
   file under ``storage/hf_configs/``. This keeps cache invalidation
   trivial (per-model) and avoids a single giant JSON that would need
   full rewrites on every update.

6. **Offline-safe.**
   Network failure or a 404 returns ``None``. The caller is never
   blocked by an unreachable HuggingFace.

7. **Path-traversal safe.**
   The ``repo_id`` is validated before being placed in a file path.
   Malicious inputs like ``../../etc/passwd`` or ``foo/../../bar``
   are rejected.

See also
--------

    * ``backend/peldrun/llm/catalog/external_registry.py`` — LiteLLM
    * ``backend/peldrun/llm/catalog/lookup.py`` — merge layer
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional

import httpx

from peldrun.storage_paths import get_storage_root

logger = logging.getLogger("peldrun.llm.catalog.huggingface")


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


#: Base URL for raw file access. ``{repo_id}`` is substituted with the
#: validated ``owner/name`` form, and ``{filename}`` with the file we
#: want (``config.json`` in the common case).
RAW_URL_TEMPLATE: str = "https://huggingface.co/{repo_id}/raw/main/{filename}"


#: Default branch. Some repositories use ``main``, others ``master``.
#: The caller can try both via ``fetch_config(branches=...)``.
DEFAULT_BRANCH: str = "main"


#: Cache TTL in seconds (90 days). HuggingFace model configs are
#: extremely stable — the model author changes them only when they
#: retrain or reupload the model. 90 days is a safe upper bound.
CONFIG_TTL_SECONDS: int = 90 * 24 * 60 * 60


#: Network timeout for a single config.json fetch.
FETCH_TIMEOUT_SECONDS: float = 10.0


#: Directory name under the storage root.
CACHE_DIR_NAME: str = "hf_configs"


#: Validation regex for ``owner/name`` repo IDs.
#:
#: HuggingFace allows alphanumerics, hyphens, underscores, and dots in
#: both segments. Slashes appear exactly once. We deliberately keep
#: the pattern strict — a legitimate repo ID will never contain
#: whitespace, backslashes, control characters, or more than one slash.
_REPO_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*$")


#: Fields we extract from the raw config. Every other field is kept in
#: ``raw`` for diagnostics but is not exposed through the public
#: attribute surface.
_EXTRACTED_FIELDS: tuple[str, ...] = (
    "architectures",
    "model_type",
    "max_position_embeddings",
    "n_positions",
    "n_ctx",
    "hidden_size",
    "num_hidden_layers",
    "torch_dtype",
)


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------


class HFModelConfig:
    """
    Result of a HuggingFace config lookup.

    Attributes
    ----------
    repo_id
        The canonical ``owner/name`` that was looked up.
    branch
        The branch the config was fetched from (``main``, ``master``, ...).
    has_vision
        ``True`` when ``vision_config`` is present. ``False`` when the
        config was read and the block is absent. ``None`` when the
        config could not be read — the caller MUST distinguish these
        three states.
    has_audio
        Same semantics for ``audio_config``.
    vision_config
        The raw ``vision_config`` block (or ``None``).
    audio_config
        The raw ``audio_config`` block (or ``None``).
    architectures
        The ``architectures`` list (may be empty).
    model_type
        The ``model_type`` string (or ``None``).
    max_position_embeddings
        Context window hint, when present.
    raw_config
        The full parsed ``config.json``. Preserved so that future
        extraction rules do not require a cache invalidation.
    fetched_at
        Unix timestamp of the fetch.
    """

    __slots__ = (
        "repo_id",
        "branch",
        "has_vision",
        "has_audio",
        "vision_config",
        "audio_config",
        "architectures",
        "model_type",
        "max_position_embeddings",
        "raw_config",
        "fetched_at",
    )

    def __init__(
        self,
        *,
        repo_id: str,
        branch: str,
        raw_config: Dict[str, Any],
        fetched_at: float,
    ) -> None:
        self.repo_id = repo_id
        self.branch = branch
        self.raw_config = raw_config
        self.fetched_at = fetched_at

        # Modality: derived ONLY from the presence of the corresponding
        # config block. No string scanning. No architecture-name
        # inspection.
        self.vision_config = raw_config.get("vision_config")
        self.audio_config = raw_config.get("audio_config")
        self.has_vision = self.vision_config is not None
        self.has_audio = self.audio_config is not None

        # Declared architecture fields (verbatim from HF).
        archs = raw_config.get("architectures")
        self.architectures = archs if isinstance(archs, list) else []

        mt = raw_config.get("model_type")
        self.model_type = mt if isinstance(mt, str) else None

        # Context window — try the three common field names in order.
        # All three are published by HF config authors; we do not
        # derive the value from anything else.
        ctx: Optional[int] = None
        for key in ("max_position_embeddings", "n_positions", "n_ctx"):
            v = raw_config.get(key)
            if isinstance(v, int) and v > 0:
                ctx = v
                break
        self.max_position_embeddings = ctx

    # ------------------------------------------------------------------
    # Conversion helpers
    # ------------------------------------------------------------------

    def to_dict(self) -> Dict[str, Any]:
        """
        Return a normalized dict for the capability resolver.

        The shape mirrors what the LiteLLM extractor produces, so the
        merge layer (``lookup.py``) does not need to special-case HF.

        Only ``has_vision`` / ``has_audio`` are reported as definitive
        capability facts. Everything else (context, architecture,
        model_type) is descriptive metadata, not a capability claim.
        """
        result: Dict[str, Any] = {
            "source": "huggingface_config",
            "source_repo": self.repo_id,
            "source_branch": self.branch,
            "source_fetched_at": self.fetched_at,
            "model_type": self.model_type,
            "architectures": list(self.architectures),
        }
        if self.max_position_embeddings is not None:
            result["max_input_tokens"] = self.max_position_embeddings

        # Only emit capability facts when we have a definitive answer.
        # When ``has_vision`` is False (config read, no vision_config),
        # we still report ``supports_vision=False`` — this IS
        # definitive: a text-only config.json means no vision encoder.
        # When ``has_vision`` is None (config unreadable), we omit the
        # key entirely so the caller sees "no data".
        if self.has_vision is not None:
            result["supports_vision"] = bool(self.has_vision)
        if self.has_audio is not None:
            result["supports_audio_input"] = bool(self.has_audio)

        return result

    def __repr__(self) -> str:
        return (
            f"HFModelConfig(repo_id={self.repo_id!r}, "
            f"branch={self.branch!r}, "
            f"has_vision={self.has_vision!r}, "
            f"model_type={self.model_type!r})"
        )


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


class HuggingFaceConfigLoader:
    """
    Fetch, cache, and expose HF model configs.

    Thread-safe. The cache directory is created lazily on first write.
    """

    _instance: Optional["HuggingFaceConfigLoader"] = None
    _class_lock = threading.Lock()

    def __init__(
        self,
        *,
        cache_dir: Optional[Path] = None,
        ttl_seconds: int = CONFIG_TTL_SECONDS,
    ) -> None:
        self._lock = threading.RLock()
        self._cache_dir = (cache_dir or get_storage_root()) / CACHE_DIR_NAME
        self._ttl_seconds = int(ttl_seconds)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def fetch_config(
        self,
        repo_id: str,
        *,
        force: bool = False,
        branches: Optional[tuple[str, ...]] = None,
    ) -> Optional[HFModelConfig]:
        """
        Return the config for a HuggingFace repo, or ``None`` when it
        cannot be resolved.

        Parameters
        ----------
        repo_id
            ``owner/name`` form. Values without a slash, or containing
            traversal sequences, return ``None`` immediately.
        force
            Bypass the cache and refetch from HF.
        branches
            Ordered branch names to try. Defaults to ``("main", "master")``.

        Returns
        -------
        ``HFModelConfig`` on success, ``None`` otherwise.

        Never raises. A network failure, 404, 401, or malformed JSON
        all return ``None`` and are logged at ``debug``/``warning``.
        """
        if not self._validate_repo_id(repo_id):
            logger.debug(
                "Rejected HF repo id %r (fails validation).",
                repo_id,
            )
            return None

        # Cache hit (unless forced).
        if not force:
            cached = await asyncio.to_thread(self._read_cache, repo_id)
            if cached is not None and not self._is_stale(cached):
                return cached

        branches_to_try = branches or (DEFAULT_BRANCH, "master")
        for branch in branches_to_try:
            config = await self._fetch_branch(repo_id, branch)
            if config is not None:
                await asyncio.to_thread(self._write_cache, config)
                return config

        logger.debug(
            "HF config for %r not found on any branch (%s).",
            repo_id,
            ", ".join(branches_to_try),
        )
        return None

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------

    @staticmethod
    def _validate_repo_id(repo_id: str) -> bool:
        """
        Return ``True`` only for values that look like a valid
        ``owner/name`` HF repo ID.

        Rejects:
            * Empty / whitespace
            * Values without exactly one ``/``
            * Leading/trailing slashes
            * ``..`` or ``\\`` (path traversal)
            * Any character outside ``[A-Za-z0-9._-]``
        """
        if not repo_id or not isinstance(repo_id, str):
            return False
        s = repo_id.strip()
        if not s:
            return False
        if ".." in s or "\\" in s:
            return False
        return bool(_REPO_ID_RE.match(s))

    # ------------------------------------------------------------------
    # Network
    # ------------------------------------------------------------------

    async def _fetch_branch(
        self, repo_id: str, branch: str
    ) -> Optional[HFModelConfig]:
        """
        Fetch ``config.json`` from the given branch.

        Returns ``None`` on any error (404, 403, timeout, invalid
        JSON). ``404`` is expected for the ``master`` fallback — it is
        logged at ``debug`` level, not ``warning``.
        """
        url = RAW_URL_TEMPLATE.format(
            repo_id=repo_id, filename="config.json"
        )
        # The template above defaults to ``main``. If a caller asks for
        # ``master``, patch it in.
        if branch != DEFAULT_BRANCH:
            url = url.replace(
                f"/raw/{DEFAULT_BRANCH}/", f"/raw/{branch}/"
            )

        try:
            async with httpx.AsyncClient(
                timeout=FETCH_TIMEOUT_SECONDS, follow_redirects=True
            ) as client:
                resp = await client.get(url)
        except Exception as exc:
            logger.debug(
                "Network error fetching HF config %s: %s", url, exc
            )
            return None

        if resp.status_code == 404:
            # Expected for models that do not exist or use a branch we
            # did not try. Not a warning.
            return None
        if resp.status_code in (401, 403):
            logger.debug(
                "HF config for %r is gated (HTTP %d); skipping.",
                repo_id,
                resp.status_code,
            )
            return None
        if resp.status_code != 200:
            logger.warning(
                "HF config for %r returned HTTP %d; skipping.",
                repo_id,
                resp.status_code,
            )
            return None

        try:
            raw = resp.json()
        except Exception as exc:
            logger.warning(
                "HF config for %r is not valid JSON: %s", repo_id, exc
            )
            return None

        if not isinstance(raw, dict):
            logger.warning(
                "HF config for %r is not a JSON object (got %s).",
                repo_id,
                type(raw).__name__,
            )
            return None

        return HFModelConfig(
            repo_id=repo_id,
            branch=branch,
            raw_config=raw,
            fetched_at=time.time(),
        )

    # ------------------------------------------------------------------
    # Disk cache
    # ------------------------------------------------------------------

    def _cache_path_for(self, repo_id: str) -> Path:
        """
        Return the on-disk path for a repo's cached config.

        The ``owner`` and ``name`` segments are stored as separate
        subdirectories so the flat directory never holds a file with
        a slash in its name. Traversal is already blocked by
        ``_validate_repo_id``; this is defence in depth.
        """
        owner, _, name = repo_id.partition("/")
        safe_owner = self._safe_segment(owner)
        safe_name = self._safe_segment(name)
        return self._cache_dir / safe_owner / f"{safe_name}.json"

    @staticmethod
    def _safe_segment(segment: str) -> str:
        """
        Ensure a single path segment contains no traversal sequences.
        Already validated by ``_validate_repo_id``; kept as a final
        defence.
        """
        if not segment or ".." in segment or "/" in segment or "\\" in segment:
            # Fallback: hash the segment so the path stays deterministic
            # even for pathological inputs.
            return "_invalid_"
        return segment

    def _read_cache(self, repo_id: str) -> Optional[HFModelConfig]:
        """Read a cached config from disk, returning ``None`` if absent/corrupt."""
        try:
            path = self._cache_path_for(repo_id)
            if not path.is_file():
                return None
            payload = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                return None
            meta = payload.get("_meta")
            raw = payload.get("_config")
            if not isinstance(meta, dict) or not isinstance(raw, dict):
                return None
            fetched_at = float(meta.get("fetched_at") or 0.0)
            branch = str(meta.get("branch") or DEFAULT_BRANCH)
            return HFModelConfig(
                repo_id=repo_id,
                branch=branch,
                raw_config=raw,
                fetched_at=fetched_at,
            )
        except Exception as exc:
            logger.debug(
                "Failed reading HF cache for %r: %s", repo_id, exc
            )
            return None

    def _write_cache(self, config: HFModelConfig) -> None:
        """Persist a config to disk atomically."""
        try:
            path = self._cache_path_for(config.repo_id)
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".json.tmp")
            payload = {
                "_meta": {
                    "repo_id": config.repo_id,
                    "branch": config.branch,
                    "fetched_at": config.fetched_at,
                },
                "_config": config.raw_config,
            }
            tmp.write_text(
                json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                encoding="utf-8",
            )
            tmp.replace(path)
        except Exception as exc:
            logger.warning(
                "Failed writing HF cache for %r: %s", config.repo_id, exc
            )

    def _is_stale(self, config: HFModelConfig) -> bool:
        """Return ``True`` when the config is older than the TTL."""
        age = time.time() - config.fetched_at
        return age > self._ttl_seconds


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------


def get_huggingface_config_loader(
    *, cache_dir: Optional[Path] = None
) -> HuggingFaceConfigLoader:
    """
    Return the process-wide HuggingFaceConfigLoader singleton.

    Constructed lazily. Tests that need isolation should build their
    own loader with a temp ``cache_dir``.
    """
    if HuggingFaceConfigLoader._instance is None:
        with HuggingFaceConfigLoader._class_lock:
            if HuggingFaceConfigLoader._instance is None:
                HuggingFaceConfigLoader._instance = HuggingFaceConfigLoader(
                    cache_dir=cache_dir
                )
    return HuggingFaceConfigLoader._instance


def reset_huggingface_config_loader_for_tests() -> None:
    """Reset the singleton. Intended only for tests."""
    with HuggingFaceConfigLoader._class_lock:
        HuggingFaceConfigLoader._instance = None


__all__ = [
    "RAW_URL_TEMPLATE",
    "DEFAULT_BRANCH",
    "CONFIG_TTL_SECONDS",
    "FETCH_TIMEOUT_SECONDS",
    "CACHE_DIR_NAME",
    "HFModelConfig",
    "HuggingFaceConfigLoader",
    "get_huggingface_config_loader",
    "reset_huggingface_config_loader_for_tests",
]
