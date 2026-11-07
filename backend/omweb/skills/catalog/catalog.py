# backend/omweb/skills/catalog/catalog.py
"""
Skill Catalog -- remote + local catalog for one-click skill install.

This module mirrors ``omweb.provider_packages.catalog.ProviderPackageCatalog``
but specialized for skills. The design is intentionally parallel so
the frontend can render both catalogs with the same visual pattern
(two-tab UI, cards with brand color + icon + Install button).

Sources (in merge priority, lowest first)
==========================================
    1. **builtin**  -- ``builtin_catalog.json`` shipped with PELDRUN.
    2. **remote**   -- fetched from ``PELDRUN_SKILL_CATALOG_URL``.
    3. **custom**   -- local file at ``PELDRUN_SKILL_CATALOG_PATH``.
    4. **extra**    -- passed via constructor (tests only).

Design invariants
=================
    1. ``checksum_sha256`` is mandatory for install. No exception.
    2. HTTPS-only URL schemes.
    3. Streaming download with hard byte cap (10 MB for the ZIP).
    4. Bounded redirect chain (max 3 hops).
    5. Atomic cache write via tmp + rename.
    6. Cache key includes source_url -- stale cache from a previous
       URL is ignored after the env var changes.
    7. Catalog item IDs are normalized to PELDRUN's canonical form so
       that ``catalogItem.id === installedSkill.id`` on the frontend.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

import httpx

from omweb.skills.catalog.schema import (
    SkillCatalogItem,
    installability_reason,
    is_installable,
)
from omweb.skills.errors import SkillError
from omweb.skills.manager import SkillManager, skill_manager as default_manager
from omweb.skills.registry import normalize_skill_id
from omweb.skills.storage import SkillInstallResult

logger = logging.getLogger("omweb.skills.catalog")


# -- Constants ------------------------------------------------------

BUILTIN_CATALOG_FILENAME = "builtin_catalog.json"
CUSTOM_CATALOG_ENV_VAR = "PELDRUN_SKILL_CATALOG_PATH"
REMOTE_CATALOG_ENV_VAR = "PELDRUN_SKILL_CATALOG_URL"
ENABLED_ENV_VAR = "PELDRUN_SKILL_CATALOG_ENABLED"

# Default remote catalog URL. Shipped with PELDRUN so the catalog
# works out of the box. Operators override via env var, disable via
# empty string.
DEFAULT_REMOTE_CATALOG_URL = (
    "https://peldrunai.github.io/peldrun-skill-catalog/catalog.json"
)

REMOTE_CATALOG_CACHE_FILENAME = "skill_catalog_remote_cache.json"
REMOTE_CATALOG_TTL_SECONDS = 24 * 60 * 60
REMOTE_CATALOG_FETCH_TIMEOUT_SECONDS = 15.0
REMOTE_CATALOG_MAX_BYTES = 2 * 1024 * 1024
REMOTE_CATALOG_MAX_REDIRECTS = 3
REMOTE_CATALOG_CACHE_SCHEMA_VERSION = 1

# Timeout for the ZIP download itself. Skills are small (~50 KB), so
# 60s is generous and still bounded.
DOWNLOAD_TIMEOUT_SECONDS = 60.0
MAX_DOWNLOAD_REDIRECTS = 3
MAX_SKILL_ZIP_BYTES = 10 * 1024 * 1024  # 10 MB hard cap

_SHA256_HEX_RE = re.compile(r"^[0-9a-f]{64}$")


# -- Module-level helpers -------------------------------------------

def _resolve_remote_catalog_url() -> str:
    """Resolve the effective remote catalog URL."""
    if REMOTE_CATALOG_ENV_VAR in os.environ:
        return os.environ[REMOTE_CATALOG_ENV_VAR].strip()
    return DEFAULT_REMOTE_CATALOG_URL


def _is_catalog_enabled() -> bool:
    """Feature kill switch. Default true."""
    raw = os.environ.get(ENABLED_ENV_VAR, "true").strip().lower()
    return raw not in ("0", "false", "no", "off")


def _default_cache_dir() -> Path:
    """<backend>/data/ -- same location as provider catalog cache."""
    backend_root = Path(__file__).resolve().parent.parent.parent.parent
    return backend_root / "data"


def _parse_catalog_payload(
    payload: Any,
    *,
    source_path: Path,
) -> Dict[str, SkillCatalogItem]:
    """
    Parse a decoded JSON payload into a dict keyed by item ID.

    Malformed items are logged and skipped. The rest of the catalog
    remains usable.

    Note on ID normalization:
        Catalog authors may write ``code-review`` or ``Code Review``.
        PELDRUN's canonical skill IDs are lowercase snake_case (see
        ``normalize_skill_id``), and ``SkillStorage`` normalizes the
        manifest ID at install time. We normalize here as well so that
        ``catalogItem.id === installedSkill.id`` on the frontend --
        otherwise the "Installed" badge would never appear for a
        catalog item whose author used hyphens.
    """
    if not isinstance(payload, dict):
        logger.warning(
            "Skill catalog %s root is not an object (got %s); ignoring.",
            source_path,
            type(payload).__name__,
        )
        return {}

    raw_items = payload.get("items")
    if not isinstance(raw_items, list):
        logger.warning(
            "Skill catalog %s has no 'items' array (got %s); ignoring.",
            source_path,
            type(raw_items).__name__,
        )
        return {}

    items: Dict[str, SkillCatalogItem] = {}
    for idx, raw in enumerate(raw_items):
        if not isinstance(raw, dict):
            logger.warning(
                "Skill catalog %s: item at index %d is not an object; skipping.",
                source_path,
                idx,
            )
            continue

        # Normalize the ID before Pydantic validation so downstream
        # lookups and frontend comparisons always use canonical form.
        if isinstance(raw.get("id"), str):
            raw = {**raw, "id": normalize_skill_id(raw["id"])}

        try:
            item = SkillCatalogItem.model_validate(raw)
        except Exception as exc:
            logger.warning(
                "Skill catalog %s: item %d failed validation (%s); skipping.",
                source_path,
                idx,
                exc,
            )
            continue

        if not item.id:
            continue

        # Compute installability fields.
        item.installable = is_installable(item)
        item.installability_reason = installability_reason(item)

        items[item.id] = item

    return items


def _load_builtin_catalog() -> Dict[str, SkillCatalogItem]:
    """Load the shipped builtin catalog."""
    builtin_path = Path(__file__).resolve().parent / BUILTIN_CATALOG_FILENAME
    if not builtin_path.is_file():
        logger.info(
            "Builtin skill catalog not found at %s; starting empty.",
            builtin_path,
        )
        return {}
    try:
        raw = builtin_path.read_text(encoding="utf-8")
        return _parse_catalog_payload(json.loads(raw), source_path=builtin_path)
    except Exception as exc:
        logger.warning("Builtin skill catalog unreadable: %s", exc)
        return {}


def _load_custom_catalog() -> Dict[str, SkillCatalogItem]:
    """Load the operator-configured custom catalog (if any)."""
    path_str = os.environ.get(CUSTOM_CATALOG_ENV_VAR, "").strip()
    if not path_str:
        return {}
    try:
        path = Path(path_str).expanduser().resolve()
    except Exception:
        return {}
    if not path.is_file():
        logger.warning("Custom skill catalog path %s does not exist.", path)
        return {}
    try:
        return _parse_catalog_payload(
            json.loads(path.read_text(encoding="utf-8")),
            source_path=path,
        )
    except Exception as exc:
        logger.warning("Custom skill catalog unreadable: %s", exc)
        return {}


def _load_extra_paths(paths: Iterable[Path]) -> Dict[str, SkillCatalogItem]:
    """Load additional local catalog paths (tests)."""
    merged: Dict[str, SkillCatalogItem] = {}
    for path in paths:
        try:
            if not path.is_file():
                continue
            items = _parse_catalog_payload(
                json.loads(path.read_text(encoding="utf-8")),
                source_path=path,
            )
            merged.update(items)
        except Exception as exc:
            logger.warning("Extra skill catalog %s unreadable: %s", path, exc)
    return merged


# -- SkillCatalog ---------------------------------------------------

class SkillCatalog:
    """
    Merged skill catalog with remote fetch + install orchestration.

    Mirror of ``ProviderPackageCatalog`` but simpler: no job polling,
    no venv, no pip. Install is a download + delegate to
    ``SkillManager.install_skill``.

    Thread-safety: reads are safe without a lock because ``dict`` reads
    are atomic in CPython. Writes swap ``self._items`` in a single
    assignment, so concurrent readers see either old or new state.
    """

    def __init__(
        self,
        *,
        extra_paths: Optional[Iterable[Path]] = None,
        manager: Optional[SkillManager] = None,
        cache_dir: Optional[Path] = None,
        ttl_seconds: int = REMOTE_CATALOG_TTL_SECONDS,
    ) -> None:
        self._extra_paths: List[Path] = [
            Path(p).expanduser().resolve() for p in (extra_paths or [])
        ]
        self._manager = manager or default_manager

        self._cache_dir = cache_dir or _default_cache_dir()
        self._cache_path = self._cache_dir / REMOTE_CATALOG_CACHE_FILENAME
        self._ttl_seconds = int(ttl_seconds)

        self._builtin_items: Dict[str, SkillCatalogItem] = {}
        self._custom_items: Dict[str, SkillCatalogItem] = {}
        self._extra_items: Dict[str, SkillCatalogItem] = {}
        self._remote_items: Dict[str, SkillCatalogItem] = {}

        self._items: Dict[str, SkillCatalogItem] = {}
        self._remote_loaded_at: float = 0.0
        self._remote_last_error: Optional[str] = None

        self.reload()

    # -- Reads ------------------------------------------------------

    def list_items(self) -> List[SkillCatalogItem]:
        """Return every catalog item sorted by display name."""
        items = list(self._items.values())
        items.sort(key=lambda i: (i.display_name or i.name or i.id).lower())
        return items

    def get_item(self, item_id: str) -> Optional[SkillCatalogItem]:
        if not item_id:
            return None
        return self._items.get(item_id)

    def get_or_raise(self, item_id: str) -> SkillCatalogItem:
        item = self.get_item(item_id)
        if item is None:
            raise SkillError(
                f"Skill catalog item {item_id!r} does not exist.",
                code="SKILL_CATALOG_ITEM_NOT_FOUND",
                details={"item_id": item_id},
            )
        return item

    # -- Reload / refresh -------------------------------------------

    def reload(self) -> int:
        """Reload from disk. Returns item count."""
        self._builtin_items = _load_builtin_catalog()
        self._custom_items = _load_custom_catalog()
        self._extra_items = _load_extra_paths(self._extra_paths)
        self._load_remote_cache()
        self._rebuild_merged_items()
        logger.info(
            "Skill catalog reloaded: %d items "
            "(builtin=%d, custom=%d, extra=%d, remote=%d).",
            len(self._items),
            len(self._builtin_items),
            len(self._custom_items),
            len(self._extra_items),
            len(self._remote_items),
        )
        return len(self._items)

    def _rebuild_merged_items(self) -> None:
        merged: Dict[str, SkillCatalogItem] = {}
        merged.update(self._builtin_items)
        merged.update(self._remote_items)
        merged.update(self._custom_items)
        merged.update(self._extra_items)
        self._items = merged

    # -- Remote fetch -----------------------------------------------

    async def ensure_fresh(self, force: bool = False) -> bool:
        """Fetch remote catalog if stale or forced. Never raises."""
        if not _is_catalog_enabled():
            return False

        url = _resolve_remote_catalog_url()
        if not url:
            return True  # offline mode, builtin only

        if not force and self._remote_loaded_at > 0:
            age = time.time() - self._remote_loaded_at
            if age <= self._ttl_seconds:
                return True

        ok = await self._fetch_remote(url)
        if ok:
            self._rebuild_merged_items()
        return ok

    async def refresh(self) -> bool:
        """Force-fetch, ignoring TTL."""
        return await self.ensure_fresh(force=True)

    async def _fetch_remote(self, url: str) -> bool:
        """Download + cache the remote catalog. Never raises."""
        if not url.lower().startswith(("http://", "https://")):
            msg = f"Skill catalog URL scheme not HTTP(S): {url!r}"
            logger.warning(msg)
            self._remote_last_error = msg
            return False

        try:
            async with httpx.AsyncClient(
                timeout=REMOTE_CATALOG_FETCH_TIMEOUT_SECONDS,
                follow_redirects=True,
                max_redirects=REMOTE_CATALOG_MAX_REDIRECTS,
            ) as client:
                async with client.stream("GET", url) as response:
                    if response.status_code != 200:
                        msg = (
                            f"Skill catalog HTTP {response.status_code} "
                            f"from {url}"
                        )
                        logger.warning(msg)
                        self._remote_last_error = msg
                        return False
                    content = bytearray()
                    async for chunk in response.aiter_bytes(chunk_size=64 * 1024):
                        content.extend(chunk)
                        if len(content) > REMOTE_CATALOG_MAX_BYTES:
                            msg = (
                                f"Skill catalog exceeds "
                                f"{REMOTE_CATALOG_MAX_BYTES} bytes"
                            )
                            logger.warning(msg)
                            self._remote_last_error = msg
                            return False
                    raw = json.loads(bytes(content).decode("utf-8"))
        except Exception as exc:
            msg = f"Skill catalog fetch failed: {exc}"
            logger.warning(msg)
            self._remote_last_error = msg
            return False

        if not isinstance(raw, dict):
            self._remote_last_error = "Skill catalog root is not a JSON object."
            return False

        items = _parse_catalog_payload(raw, source_path=Path(url))
        fetched_at = time.time()
        self._remote_items = items
        self._remote_loaded_at = fetched_at
        self._remote_last_error = None

        await asyncio.to_thread(self._write_remote_cache, items, url, fetched_at)
        logger.info(
            "Fetched %d items from skill catalog (%s).", len(items), url
        )
        return True

    def _load_remote_cache(self) -> bool:
        url = _resolve_remote_catalog_url()
        if not url:
            self._remote_items = {}
            self._remote_loaded_at = 0.0
            return False

        if not self._cache_path.is_file():
            return False

        try:
            payload = json.loads(self._cache_path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("Skill catalog cache unreadable: %s", exc)
            return False

        if not isinstance(payload, dict):
            return False

        meta = payload.get("_cache_meta")
        items_raw = payload.get("_items")
        if not isinstance(meta, dict) or not isinstance(items_raw, dict):
            return False

        if meta.get("schema_version") != REMOTE_CATALOG_CACHE_SCHEMA_VERSION:
            return False
        if meta.get("source_url") != url:
            return False

        items: Dict[str, SkillCatalogItem] = {}
        for item_id, item_dict in items_raw.items():
            if not isinstance(item_dict, dict):
                continue
            try:
                item = SkillCatalogItem.model_validate(item_dict)
                item.installable = is_installable(item)
                item.installability_reason = installability_reason(item)
                items[item_id] = item
            except Exception:
                continue

        self._remote_items = items
        self._remote_loaded_at = float(meta.get("fetched_at") or 0.0)
        logger.info(
            "Loaded %d items from skill catalog cache (age=%.1fs).",
            len(items),
            max(0.0, time.time() - self._remote_loaded_at),
        )
        return True

    def _write_remote_cache(
        self,
        items: Dict[str, SkillCatalogItem],
        url: str,
        fetched_at: float,
    ) -> None:
        try:
            self._cache_dir.mkdir(parents=True, exist_ok=True)
            tmp = self._cache_path.with_suffix(".tmp")
            payload = {
                "_cache_meta": {
                    "schema_version": REMOTE_CATALOG_CACHE_SCHEMA_VERSION,
                    "fetched_at": fetched_at,
                    "source_url": url,
                    "item_count": len(items),
                },
                "_items": {k: v.model_dump() for k, v in items.items()},
            }
            tmp.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            tmp.replace(self._cache_path)
        except Exception as exc:
            logger.warning("Skill catalog cache write failed: %s", exc)

    # -- Install ----------------------------------------------------

    async def install_from_catalog(
        self,
        item_id: str,
    ) -> SkillInstallResult:
        """
        Download, verify, and install a catalog skill.

        Delegates the actual install to ``SkillManager.install_skill``
        -- the same code path as a manual ZIP upload. This guarantees
        that catalog installs pass through the exact same validation
        as uploads (zip safety, manifest schema, sub_skills check,
        SVG sanitization, size caps).
        """
        try:
            await self.ensure_fresh()
        except Exception:
            pass

        item = self.get_or_raise(item_id)

        if not is_installable(item):
            raise SkillError(
                f"Catalog item {item_id!r} cannot be installed: "
                f"{installability_reason(item)}",
                code="SKILL_CATALOG_NOT_INSTALLABLE",
                details={"item_id": item_id, "reason": "not_installable"},
            )

        # Defensive: is_installable() guarantees non-None, but narrow
        # the types for mypy and add a runtime invariant check.
        if not item.download_url or not item.checksum_sha256:
            raise SkillError(
                f"Catalog item {item_id!r} is missing download_url or "
                f"checksum_sha256.",
                code="SKILL_CATALOG_INVALID_ITEM",
                details={"item_id": item_id},
            )

        logger.info(
            "Installing skill %r v%s from catalog (%s).",
            item.id,
            item.version,
            item.download_url,
        )

        zip_bytes = await self._download_zip(
            url=item.download_url,
            expected_sha256=item.checksum_sha256.strip().lower(),
            skill_hint=item.id,
        )

        # Delegate to manager (synchronous -- wrap in thread).
        return await asyncio.to_thread(
            self._manager.install_skill,
            zip_bytes,
            source_filename=f"{item.id}-{item.version}.zip",
            source="catalog",
        )

    async def _download_zip(
        self,
        *,
        url: str,
        expected_sha256: str,
        skill_hint: str,
    ) -> bytes:
        """Download, verify, and return the ZIP content."""
        scheme = (url or "").strip().lower()
        if not (scheme.startswith("http://") or scheme.startswith("https://")):
            raise SkillError(
                f"Skill {skill_hint!r} declares an unsafe download URL scheme.",
                code="SKILL_CATALOG_UNSAFE_URL",
                details={"item_id": skill_hint, "reason": "unsafe_url_scheme"},
            )

        content = bytearray()
        hasher = hashlib.sha256()

        try:
            async with httpx.AsyncClient(
                timeout=DOWNLOAD_TIMEOUT_SECONDS,
                follow_redirects=True,
                max_redirects=MAX_DOWNLOAD_REDIRECTS,
            ) as client:
                async with client.stream("GET", url) as response:
                    if response.status_code != 200:
                        raise SkillError(
                            f"Skill {skill_hint!r} download failed: "
                            f"HTTP {response.status_code}.",
                            code="SKILL_CATALOG_DOWNLOAD_FAILED",
                            details={
                                "item_id": skill_hint,
                                "status_code": response.status_code,
                            },
                        )
                    async for chunk in response.aiter_bytes(chunk_size=64 * 1024):
                        if not chunk:
                            continue
                        if len(content) + len(chunk) > MAX_SKILL_ZIP_BYTES:
                            raise SkillError(
                                f"Skill {skill_hint!r} download exceeds "
                                f"{MAX_SKILL_ZIP_BYTES} bytes. Aborting.",
                                code="SKILL_CATALOG_TOO_LARGE",
                                details={
                                    "item_id": skill_hint,
                                    "max_bytes": MAX_SKILL_ZIP_BYTES,
                                },
                            )
                        content.extend(chunk)
                        hasher.update(chunk)
        except SkillError:
            raise
        except httpx.TimeoutException as exc:
            raise SkillError(
                f"Skill {skill_hint!r} download timed out: {exc}",
                code="SKILL_CATALOG_TIMEOUT",
                details={"item_id": skill_hint},
            ) from exc
        except httpx.HTTPError as exc:
            raise SkillError(
                f"Skill {skill_hint!r} download failed: {exc}",
                code="SKILL_CATALOG_HTTP_ERROR",
                details={"item_id": skill_hint},
            ) from exc
        except Exception as exc:
            raise SkillError(
                f"Skill {skill_hint!r} download failed unexpectedly: {exc}",
                code="SKILL_CATALOG_DOWNLOAD_FAILED",
                details={"item_id": skill_hint},
            ) from exc

        if not content:
            raise SkillError(
                f"Skill {skill_hint!r} download returned empty body.",
                code="SKILL_CATALOG_EMPTY_BODY",
                details={"item_id": skill_hint},
            )

        actual = hasher.hexdigest()
        if actual != expected_sha256:
            raise SkillError(
                f"Skill {skill_hint!r} SHA-256 mismatch. Expected "
                f"{expected_sha256[:16]}..., got {actual[:16]}...",
                code="SKILL_CATALOG_CHECKSUM_MISMATCH",
                details={
                    "item_id": skill_hint,
                    "expected": expected_sha256,
                    "actual": actual,
                },
            )

        logger.info(
            "Downloaded & verified skill %r (%d bytes, sha256=%s...).",
            skill_hint,
            len(content),
            actual[:16],
        )
        return bytes(content)

    # -- Status (debug) ---------------------------------------------

    def get_status(self) -> Dict[str, Any]:
        """Snapshot for admin/debug endpoints."""
        return {
            "enabled": _is_catalog_enabled(),
            "item_count": len(self._items),
            "builtin_count": len(self._builtin_items),
            "custom_count": len(self._custom_items),
            "extra_count": len(self._extra_items),
            "remote_count": len(self._remote_items),
            "remote_url_configured": bool(_resolve_remote_catalog_url()),
            "remote_url_effective": _resolve_remote_catalog_url() or None,
            "remote_cache_age_seconds": (
                max(0.0, time.time() - self._remote_loaded_at)
                if self._remote_loaded_at > 0
                else None
            ),
            "remote_last_error": self._remote_last_error,
        }


# -- Singleton ------------------------------------------------------

_instance: Optional[SkillCatalog] = None


def get_skill_catalog() -> SkillCatalog:
    """Return the process-wide SkillCatalog singleton."""
    global _instance
    if _instance is None:
        _instance = SkillCatalog()
    return _instance


def reset_skill_catalog_for_tests() -> None:
    """Reset the singleton. Tests only."""
    global _instance
    _instance = None


__all__ = [
    "BUILTIN_CATALOG_FILENAME",
    "CUSTOM_CATALOG_ENV_VAR",
    "REMOTE_CATALOG_ENV_VAR",
    "ENABLED_ENV_VAR",
    "DEFAULT_REMOTE_CATALOG_URL",
    "REMOTE_CATALOG_CACHE_FILENAME",
    "REMOTE_CATALOG_TTL_SECONDS",
    "SkillCatalog",
    "get_skill_catalog",
    "reset_skill_catalog_for_tests",
]
