# backend/omweb/provider_packages/catalog.py
"""
Provider Package catalog.

Exposes the list of Provider Packages that a user can install with one
click from the Store, WITHOUT uploading a ZIP manually. Two sources:

    1. **Builtin catalog** — ``builtin_catalog.json`` shipped next to
       this module. Contains the officially curated provider packages
       (OpenAI-compatible, Anthropic-compatible, community packages).

    2. **Custom catalog** — optional override via the environment
       variable ``PELDRUN_PROVIDER_CATALOG_PATH``. Points at a JSON
       file with the same shape. Allows operators to host their own
       curated catalog without modifying PELDRUN's source.

Both sources are merged at load time: custom items with the same ``id``
as a builtin item REPLACE the builtin entry. This lets an operator
override a builtin's download URL (e.g. to point at an internal mirror)
without forking the catalog file.

Catalog item lifecycle
----------------------

Every catalog item has two states:

    * **Installable** — ``download_url`` is set AND ``checksum_sha256``
      is set. The install-from-catalog flow downloads the ZIP, verifies
      the hash, and runs the standard install pipeline.

    * **Preview-only** — ``download_url`` is missing OR
      ``checksum_sha256`` is missing. The item is displayed in the UI
      as "Coming Soon" (or similar) and cannot be installed. This is
      deliberate — a download without a checksum is a supply-chain
      risk that PELDRUN will not accept.

Design invariants
-----------------

1. **Checksum is mandatory for install.** ``download_url`` without
   ``checksum_sha256`` is treated as preview-only, even if the URL
   looks valid. There is no "trust the server" mode.

2. **Size is capped at the download layer.** A catalog item cannot
   cause PELDRUN to download more than ``MAX_PACKAGE_SIZE_BYTES``.
   The cap is enforced by streaming the response and aborting when
   the accumulated size exceeds the limit — not by trusting the
   ``Content-Length`` header (which a malicious server can lie about).

3. **Download is bounded by a hard timeout.** A slow or hung server
   cannot block the install endpoint indefinitely. The timeout covers
   the entire download, not just the initial connection.

4. **The catalog file is validated at load time.** A malformed JSON,
   a missing required field, or an item with a bad provider ID causes
   that ITEM to be skipped (with a warning), not the whole catalog.
   One broken entry must not take down the entire Store.

5. **No network calls during listing.** Loading the catalog is a
   disk-only operation. The remote fetch (if added in the future)
   would be a separate explicit endpoint — never implicit on page
   load.

6. **Catalog items are immutable once loaded.** The catalog is a
   snapshot in memory. Refreshing the catalog requires a restart or
   an explicit ``reload()`` call (used by tests and by an admin
   endpoint if one is added later).

See also
--------

    * ``backend/omweb/provider_packages/schema.py`` — ``ProviderPackageCatalogItem``
    * ``backend/omweb/provider_packages/package_manager.py`` — install orchestration
    * ``backend/omweb/provider_packages/errors.py`` — exception types
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

from omweb.provider_packages.errors import (
    ProviderPackageCatalogError,
    ProviderPackageError,
)
from omweb.provider_packages.package_manager import (
    ProviderPackageManager,
    provider_package_manager as default_manager,
)
from omweb.provider_packages.schema import (
    MAX_PACKAGE_SIZE_BYTES,
    ProviderPackageCatalogItem,
    ProviderPackageInstallResult,
)

logger = logging.getLogger("omweb.provider_packages.catalog")


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


#: Filename of the bundled catalog, located next to this module.
BUILTIN_CATALOG_FILENAME: str = "builtin_catalog.json"


#: Environment variable for a custom catalog override (local file).
#: When set, items with the same ID as a builtin or remote item
#: REPLACE the corresponding entry. Used by operators who host their
#: own curated catalog on disk, or by tests that need to inject a
#: specific item set.
CUSTOM_CATALOG_ENV_VAR: str = "PELDRUN_PROVIDER_CATALOG_PATH"


#: Environment variable for the REMOTE catalog URL. When unset (the
#: default), no remote fetch is ever attempted — PELDRUN ships with a
#: fully self-contained builtin catalog and does not phone home.
#: Operators who want to centralize catalog management across many
#: PELDRUN instances can point this at a GitHub Raw URL (or any
#: HTTPS-served JSON file with the same shape as
#: ``builtin_catalog.json``).
#:
#: Opt-in by design: outbound network access on every store-page
#: load would be a privacy and availability liability. Users who do
#: not explicitly configure this URL see exactly the builtin catalog
#: and nothing else.
REMOTE_CATALOG_ENV_VAR: str = "PELDRUN_PROVIDER_CATALOG_URL"


#: Default remote catalog URL, shipped with every PELDRUN build.
#: This is the URL that the application uses out of the box when no
#: environment variable overrides it. Its presence here is what makes
#: the remote catalog work for every user without any manual setup.
#:
#: Override behavior:
#:   * ``PELDRUN_PROVIDER_CATALOG_URL=<url>``  → use <url> instead
#:   * ``PELDRUN_PROVIDER_CATALOG_URL=""``     → disable remote fetch
#:                                               (offline-only mode)
#:   * unset                                   → use this default
DEFAULT_REMOTE_CATALOG_URL: str = (
    "https://peldrunai.github.io/peldrun-provider-catalog/catalog.json"
)


#: Filename for the on-disk cache of the remote catalog. Placed in
#: ``<backend>/data/`` next to ``models_vault.json`` and
#: ``models_metadata.json`` — the same storage area the rest of the
#: system uses for cross-session state.
REMOTE_CATALOG_CACHE_FILENAME: str = "catalog_remote_cache.json"


#: Cache TTL for the remote catalog (24 hours, matching the LiteLLM
#: external registry pattern). The cache is served STALE during the
#: refresh window — see ``ensure_fresh()``. A user who installs a
#: package a minute after a new version lands will still see the
#: previous catalog until the TTL elapses; the operator can force a
#: refresh via the admin endpoint.
REMOTE_CATALOG_TTL_SECONDS: int = 24 * 60 * 60


#: Network timeout for a single remote catalog fetch. Kept tight so a
#: wedged GitHub never blocks the store UI for more than this many
#: seconds. The call is best-effort; a timeout leaves the previous
#: cache intact.
REMOTE_CATALOG_FETCH_TIMEOUT_SECONDS: float = 15.0


#: Maximum size of a remote catalog response. A real catalog is a few
#: kilobytes; anything over 2 MB is suspicious (or a misconfigured
#: URL pointing at something that is not a catalog).
REMOTE_CATALOG_MAX_BYTES: int = 2 * 1024 * 1024


#: Maximum number of redirects followed during a remote fetch. GitHub
#: Raw and Releases serve the content directly; any redirect chain
#: longer than 3 hops is almost certainly an error page.
REMOTE_CATALOG_MAX_REDIRECTS: int = 3


#: Internal schema version for the on-disk remote cache. Bump when the
#: cache format changes in a way that requires discarding the previous
#: file. Independent of the catalog's own ``schema_version`` (which
#: tracks the CATALOG format, not the CACHE format).
REMOTE_CATALOG_CACHE_SCHEMA_VERSION: int = 1


#: Hard timeout for the entire ZIP download, in seconds. Long enough
#: for a slow connection to fetch a 50 MB package; short enough that a
#: wedged server cannot freeze the install request for minutes.
DOWNLOAD_TIMEOUT_SECONDS: float = 120.0


#: Maximum number of redirects followed during download. A malicious
#: server could otherwise redirect forever.
MAX_DOWNLOAD_REDIRECTS: int = 3


#: Minimum valid checksum length (SHA-256 hex digest = 64 chars).
_SHA256_HEX_RE = re.compile(r"^[0-9a-f]{64}$")


# ---------------------------------------------------------------------------
# Catalog loading
# ---------------------------------------------------------------------------


def _resolve_remote_catalog_url() -> str:
    """
    Resolve the effective remote catalog URL.

    Resolution order
    ----------------
    1. ``PELDRUN_PROVIDER_CATALOG_URL`` is set to a NON-EMPTY string
       → return it verbatim. This is the operator-override path
         (self-hosted catalogs, internal mirrors, air-gapped deploys
         with a local proxy).

    2. ``PELDRUN_PROVIDER_CATALOG_URL`` is set to an EMPTY string
       → return empty. The operator explicitly disabled remote
         fetching. PELDRUN will operate entirely offline against its
         built-in catalog.

    3. ``PELDRUN_PROVIDER_CATALOG_URL`` is NOT set (the common case
       for a freshly-installed app on a user's machine)
       → return ``DEFAULT_REMOTE_CATALOG_URL``. Every PELDRUN install
         ships with the official catalog active out of the box.

    This is the function every caller should use. Never read
    ``os.environ`` for this variable directly — always go through
    here, so the default-vs-override-vs-disable logic stays in one
    place.
    """
    if REMOTE_CATALOG_ENV_VAR in os.environ:
        return os.environ[REMOTE_CATALOG_ENV_VAR].strip()
    return DEFAULT_REMOTE_CATALOG_URL


def _default_cache_dir() -> Path:
    """
    Compute the default cache directory for the remote catalog.

    Mirrors the location used by ``models_vault.json`` and
    ``models_metadata.json``:

        <backend>/data/catalog_remote_cache.json

    Deferred path calculation (does not import ``omweb.config``) so
    this module can be imported in tests without triggering the full
    config bootstrap.
    """
    # __file__ = backend/omweb/provider_packages/catalog.py
    # .parent            = backend/omweb/provider_packages/
    # .parent.parent     = backend/omweb/
    # .parent.parent.parent = backend/
    backend_root = Path(__file__).resolve().parent.parent.parent
    return backend_root / "data"


def _load_builtin_catalog() -> Dict[str, ProviderPackageCatalogItem]:
    """
    Load the builtin catalog shipped with PELDRUN.

    Always available — the offline baseline. If this file is missing
    or corrupt, the catalog starts empty (a warning is logged). This
    is the one source that MUST work without any network or filesystem
    configuration.
    """
    builtin_path = Path(__file__).resolve().parent / BUILTIN_CATALOG_FILENAME
    if not builtin_path.is_file():
        logger.info(
            "Builtin catalog file not found at %s; the catalog will "
            "start empty unless a custom or remote catalog is "
            "configured.",
            builtin_path,
        )
        return {}
    try:
        raw = builtin_path.read_text(encoding="utf-8")
        parsed = json.loads(raw)
        return _parse_catalog_payload(parsed, source_path=builtin_path)
    except Exception as exc:
        logger.warning(
            "Builtin catalog at %s is unreadable: %s",
            builtin_path,
            exc,
        )
        return {}


def _load_custom_catalog() -> Dict[str, ProviderPackageCatalogItem]:
    """
    Load the operator-configured custom catalog (if any).

    Read from the path in ``PELDRUN_PROVIDER_CATALOG_PATH``. Items
    with the same ID as a builtin or remote entry REPLACE that entry
    — the operator's local file is the highest local priority.
    """
    path_str = os.environ.get(CUSTOM_CATALOG_ENV_VAR, "").strip()
    if not path_str:
        return {}

    try:
        path = Path(path_str).expanduser().resolve()
    except Exception as exc:
        logger.warning(
            "Could not resolve custom catalog path %r: %s",
            path_str,
            exc,
        )
        return {}

    if not path.is_file():
        logger.warning(
            "Custom catalog path %s does not exist; ignoring.",
            path,
        )
        return {}

    try:
        raw = path.read_text(encoding="utf-8")
        parsed = json.loads(raw)
        return _parse_catalog_payload(parsed, source_path=path)
    except Exception as exc:
        logger.warning(
            "Custom catalog at %s is unreadable: %s",
            path,
            exc,
        )
        return {}


def _load_extra_paths(
    paths: Iterable[Path],
) -> Dict[str, ProviderPackageCatalogItem]:
    """
    Load catalog items from additional local paths (test injection).

    Items loaded here have the HIGHEST priority in the merge —
    callers who supply ``extra_paths`` explicitly want to override
    everything else.
    """
    merged: Dict[str, ProviderPackageCatalogItem] = {}
    for path in paths:
        try:
            if not path.is_file():
                logger.warning(
                    "Extra catalog path %s does not exist; skipping.",
                    path,
                )
                continue
            raw = path.read_text(encoding="utf-8")
            parsed = json.loads(raw)
            items = _parse_catalog_payload(parsed, source_path=path)
            merged.update(items)
        except Exception as exc:
            logger.warning(
                "Failed to load extra catalog from %s: %s",
                path,
                exc,
            )
            continue
    return merged


def _parse_catalog_payload(
    payload: Any,
    *,
    source_path: Path,
) -> Dict[str, ProviderPackageCatalogItem]:
    """
    Parse a JSON-decoded catalog payload into a dict keyed by item ID.

    Expected shape::

        {
          "schema_version": 1,
          "items": [
            {
              "id": "acme",
              "name": "Acme AI",
              "version": "1.0.0",
              "download_url": "https://...",
              "checksum_sha256": "abc123...",
              ...
            },
            ...
          ]
        }

    Items that fail Pydantic validation are logged and skipped — the
    rest of the catalog remains usable. ``download_url`` without
    ``checksum_sha256`` (or vice versa) is demoted to preview-only
    at lookup time, not at load time, so the UI can still show the
    item with a "not installable" badge.
    """
    if not isinstance(payload, dict):
        logger.warning(
            "Catalog file %s root is not an object (got %s); ignoring.",
            source_path,
            type(payload).__name__,
        )
        return {}

    # Accept two top-level shapes:
    #   * {"items": [...]}  — the canonical form.
    #   * [...]             — a bare list of items (convenience for
    #                         simple custom catalogs).
    raw_items: Any = None
    if isinstance(payload, dict):
        raw_items = payload.get("items")
    elif isinstance(payload, list):
        raw_items = payload

    if raw_items is None:
        logger.warning(
            "Catalog file %s has no 'items' array; ignoring.",
            source_path,
        )
        return {}

    if not isinstance(raw_items, list):
        logger.warning(
            "Catalog file %s 'items' field is not a list (got %s); "
            "ignoring.",
            source_path,
            type(raw_items).__name__,
        )
        return {}

    items: Dict[str, ProviderPackageCatalogItem] = {}
    for idx, raw in enumerate(raw_items):
        if not isinstance(raw, dict):
            logger.warning(
                "Catalog file %s: item at index %d is not an object; "
                "skipping.",
                source_path,
                idx,
            )
            continue
        try:
            item = ProviderPackageCatalogItem.model_validate(raw)
        except Exception as exc:
            logger.warning(
                "Catalog file %s: item at index %d failed validation "
                "(%s); skipping.",
                source_path,
                idx,
                exc,
            )
            continue

        if not item.id:
            logger.warning(
                "Catalog file %s: item at index %d has no 'id'; "
                "skipping.",
                source_path,
                idx,
            )
            continue

        items[item.id] = item

    return items


def load_catalog(
    *,
    extra_paths: Optional[Iterable[Path]] = None,
) -> Dict[str, ProviderPackageCatalogItem]:
    """
    Load catalog items from all LOCAL sources (no network).

    Priority (lowest first, each layer overrides the previous):

        1. Builtin catalog (``builtin_catalog.json``).
        2. Custom catalog (``PELDRUN_PROVIDER_CATALOG_PATH``).
        3. Extra paths (``extra_paths`` argument).

    This function is kept for backward compatibility and for callers
    that need explicit local-only loading (e.g. tests). It does NOT
    consult the remote catalog — use ``ProviderPackageCatalog`` and
    its ``ensure_fresh()`` method for remote support.

    .. note::
       This function fixes a prior ordering bug where the builtin
       catalog silently OVERRODE the custom catalog. The documented
       intent (in the module docstring and the previous
       ``_resolve_catalog_paths`` docstring) has always been that
       custom items override builtin entries with the same ID.
    """
    merged: Dict[str, ProviderPackageCatalogItem] = {}
    merged.update(_load_builtin_catalog())
    merged.update(_load_custom_catalog())
    if extra_paths:
        merged.update(
            _load_extra_paths(
                [Path(p).expanduser().resolve() for p in extra_paths]
            )
        )
    return merged


# ---------------------------------------------------------------------------
# Installability check
# ---------------------------------------------------------------------------


def is_installable(item: ProviderPackageCatalogItem) -> bool:
    """
    Return True iff ``item`` can be installed from the catalog.

    Rules
    -----
    * ``download_url`` must be present and non-empty.
    * ``checksum_sha256`` must be present, non-empty, and a valid
      SHA-256 hex digest (64 lowercase hex characters).

    An item failing either rule is treated as preview-only. The UI
    shows it as "Coming Soon" and the install endpoint returns a
    structured error if invoked anyway.
    """
    if not item.download_url:
        return False
    if not item.checksum_sha256:
        return False
    if not _SHA256_HEX_RE.match(item.checksum_sha256.strip().lower()):
        return False
    return True


def installability_reason(item: ProviderPackageCatalogItem) -> str:
    """
    Return a human-readable explanation of why ``item`` is or is not
    installable. Used by the UI to render the badge tooltip.
    """
    if not item.download_url:
        return "Preview only — no download URL is published yet."
    if not item.checksum_sha256:
        return (
            "Preview only — no checksum is published. PELDRUN requires "
            "a SHA-256 checksum before downloading any package."
        )
    if not _SHA256_HEX_RE.match(item.checksum_sha256.strip().lower()):
        return (
            "Preview only — the published checksum is not a valid "
            "SHA-256 hex digest."
        )
    return "Installable — click to download and install."


# ---------------------------------------------------------------------------
# Catalog class
# ---------------------------------------------------------------------------


class ProviderPackageCatalog:
    """
    Wraps the merged catalog with caching and download-install logic.

    The catalog is merged from FOUR sources (lowest priority first):

        1. **Builtin** — ``builtin_catalog.json`` shipped next to
           this module. Always available, offline baseline.

        2. **Remote** — fetched from ``PELDRUN_PROVIDER_CATALOG_URL``
           (when configured) with a 24h disk cache. Optional;
           disabled by default. Uses the LiteLLM-style pattern:
           cache-first, stale-while-revalidate, offline-safe.

        3. **Custom local** — set via
           ``PELDRUN_PROVIDER_CATALOG_PATH``. Overrides both builtin
           and remote. Used by operators who host their own curated
           catalog on disk.

        4. **Extra paths** — injected via the ``extra_paths``
           constructor argument. Used by tests. Highest priority.

    Reads are safe without a lock (dict reads are atomic in CPython).
    ``reload()`` and ``ensure_fresh()`` rebuild the merged view
    atomically by swapping ``self._items`` — concurrent readers see
    either the old state or the new one, never a partial state.

    ``reload()`` is disk-only and can be called from synchronous
    contexts. ``ensure_fresh()`` performs the network fetch and must
    be awaited — call it from async endpoints before serving the
    catalog to the UI.
    """

    def __init__(
        self,
        *,
        extra_paths: Optional[Iterable[Path]] = None,
        manager: Optional[ProviderPackageManager] = None,
        cache_dir: Optional[Path] = None,
        ttl_seconds: int = REMOTE_CATALOG_TTL_SECONDS,
    ) -> None:
        self._extra_paths: List[Path] = [
            Path(p).expanduser().resolve() for p in (extra_paths or [])
        ]
        self._manager = manager or default_manager

        # Remote catalog cache.
        self._cache_dir = cache_dir or _default_cache_dir()
        self._cache_path = self._cache_dir / REMOTE_CATALOG_CACHE_FILENAME
        self._ttl_seconds = int(ttl_seconds)

        # Per-source item stores. Each is rebuilt independently so
        # that a stale remote cannot poison the builtin baseline, and
        # a corrupted custom file cannot hide the remote overlay.
        self._builtin_items: Dict[str, ProviderPackageCatalogItem] = {}
        self._custom_items: Dict[str, ProviderPackageCatalogItem] = {}
        self._extra_items: Dict[str, ProviderPackageCatalogItem] = {}
        self._remote_items: Dict[str, ProviderPackageCatalogItem] = {}

        # Merged view — what every caller reads. Swapped atomically in
        # ``_rebuild_merged_items`` so concurrent readers never see a
        # partial state.
        self._items: Dict[str, ProviderPackageCatalogItem] = {}

        # Remote fetch state.
        self._remote_loaded_at: float = 0.0
        self._remote_last_error: Optional[str] = None

        self.reload()

    # ------------------------------------------------------------------
    # Load / reload
    # ------------------------------------------------------------------

    def reload(self) -> int:
        """
        Reload the catalog from disk. Returns the new item count.

        Reads:

            * Builtin catalog file (always — offline baseline).
            * Custom local catalog (if configured).
            * Extra paths (if any).
            * Remote disk cache (if ``PELDRUN_PROVIDER_CATALOG_URL`` is
              configured AND a cache file exists AND the cache is
              schema-compatible AND the cache's source URL matches the
              current one).

        Does NOT fetch from the network — that is ``ensure_fresh()``'s
        job, called by the async endpoints that serve the catalog. This
        separation guarantees ``reload()`` is fast (disk-only) and can
        be called from synchronous contexts (``__init__``, tests)
        without blocking on the network.
        """
        self._builtin_items = _load_builtin_catalog()
        self._custom_items = _load_custom_catalog()
        self._extra_items = _load_extra_paths(self._extra_paths)
        self._load_remote_cache()
        self._rebuild_merged_items()
        logger.info(
            "Provider package catalog reloaded: %d items "
            "(builtin=%d, custom=%d, extra=%d, remote=%d).",
            len(self._items),
            len(self._builtin_items),
            len(self._custom_items),
            len(self._extra_items),
            len(self._remote_items),
        )
        return len(self._items)

    def _rebuild_merged_items(self) -> None:
        """
        Rebuild ``self._items`` from the per-source stores.

        Priority (lowest first — later layers overwrite earlier ones):

            1. builtin
            2. remote
            3. custom
            4. extra

        The whole replacement is a single reference assignment. Any
        reader that is currently iterating an old dict keeps seeing the
        old state — there is no partial view.
        """
        merged: Dict[str, ProviderPackageCatalogItem] = {}
        merged.update(self._builtin_items)
        merged.update(self._remote_items)
        merged.update(self._custom_items)
        merged.update(self._extra_items)
        self._items = merged

    # ------------------------------------------------------------------
    # Remote catalog
    # ------------------------------------------------------------------

    async def ensure_fresh(self, force: bool = False) -> bool:
        """
        Ensure the remote catalog is fresh (fetch if stale or forced).

        The effective URL is resolved by
        ``_resolve_remote_catalog_url()``:

            * ``PELDRUN_PROVIDER_CATALOG_URL`` set to a non-empty
              string → use that URL (operator override).
            * ``PELDRUN_PROVIDER_CATALOG_URL`` set to an empty string
              → no-op (operator explicitly disabled remote fetching).
            * ``PELDRUN_PROVIDER_CATALOG_URL`` unset → use
              ``DEFAULT_REMOTE_CATALOG_URL``. This is the case for
              every freshly-installed PELDRUN: the official catalog is
              fetched automatically with no user configuration.

        Parameters
        ----------
        force
            When ``True``, bypass the TTL check and always refetch.

        Returns
        -------
        bool
            ``True`` when a valid remote catalog is loaded (either
            from a fresh cache or from the network), ``False`` when
            the remote fetch failed AND no cached copy exists. Never
            raises — the caller is never blocked by an unreachable
            GitHub.
        """
        url = _resolve_remote_catalog_url()
        if not url:
            # Operator explicitly disabled remote fetching
            # (PELDRUN_PROVIDER_CATALOG_URL=""). Intentional offline
            # deployment — not an error.
            return True

        # TTL check: serve from cache if still fresh (unless forced).
        if not force and self._remote_loaded_at > 0:
            age = time.time() - self._remote_loaded_at
            if age <= self._ttl_seconds:
                return True

        ok = await self._fetch_remote(url)
        if ok:
            self._rebuild_merged_items()
        return ok

    async def refresh(self) -> bool:
        """
        Force-fetch from remote, ignoring TTL.

        Used by the admin endpoint ``POST /api/store/providers/catalog/
        refresh`` so operators can immediately pull a newly-published
        catalog entry without waiting 24 hours.
        """
        return await self.ensure_fresh(force=True)

    async def _fetch_remote(self, url: str) -> bool:
        """
        Download and cache the remote catalog. Never raises.

        Enforces (in order):
            * HTTP(S) scheme only — no ``file://``, ``data:``, etc.
            * Streaming download with a hard byte cap — a malicious
              server cannot exhaust memory by streaming gigabytes.
            * Bounded redirect chain — max 3 hops.
            * JSON parse — a malformed response leaves the previous
              cache intact and reports the error via
              ``_remote_last_error``.
        """
        if not url.lower().startswith(("http://", "https://")):
            msg = (
                f"Remote catalog URL scheme is not HTTP(S): {url!r}. "
                f"Only http:// and https:// are allowed."
            )
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
                            f"Remote catalog returned HTTP "
                            f"{response.status_code} from {url}."
                        )
                        logger.warning(msg)
                        self._remote_last_error = msg
                        return False

                    content = bytearray()
                    async for chunk in response.aiter_bytes(
                        chunk_size=64 * 1024
                    ):
                        content.extend(chunk)
                        if len(content) > REMOTE_CATALOG_MAX_BYTES:
                            msg = (
                                f"Remote catalog exceeds "
                                f"{REMOTE_CATALOG_MAX_BYTES}-byte limit "
                                f"(aborted at {len(content)} bytes)."
                            )
                            logger.warning(msg)
                            self._remote_last_error = msg
                            return False

                    raw = json.loads(bytes(content).decode("utf-8"))
        except Exception as exc:
            msg = f"Failed to fetch remote catalog from {url}: {exc}"
            logger.warning(msg)
            self._remote_last_error = msg
            return False

        # Structural validation — the ONLY failure conditions.
        #
        # An empty ``items`` array is a legitimate, intentional state:
        # a freshly-bootstrapped catalog repository that has not yet
        # published any provider. Treating it as a fetch failure
        # would:
        #
        #   * log a spurious warning every TTL cycle;
        #   * skip writing the cache, so the fetch path itself would
        #     never be proven to work end-to-end;
        #   * surface a misleading "parsed to zero items" message that
        #     reads like a bug rather than an intentional empty state.
        #
        # Only malformed payloads (non-object root, missing `items`,
        # `items` not an array) count as fetch errors.
        if not isinstance(raw, dict):
            msg = (
                f"Remote catalog at {url} is not a JSON object "
                f"(got {type(raw).__name__})."
            )
            logger.warning(msg)
            self._remote_last_error = msg
            return False

        raw_items = raw.get("items")
        if not isinstance(raw_items, list):
            msg = (
                f"Remote catalog at {url} has no 'items' array "
                f"(got {type(raw_items).__name__})."
            )
            logger.warning(msg)
            self._remote_last_error = msg
            return False

        # From here on, the payload is structurally valid. An empty
        # `items` array parses to `{}` and is accepted as-is.
        items = _parse_catalog_payload(raw, source_path=Path(url))

        fetched_at = time.time()
        self._remote_items = items
        self._remote_loaded_at = fetched_at
        self._remote_last_error = None

        await asyncio.to_thread(
            self._write_remote_cache, items, url, fetched_at
        )
        logger.info(
            "Fetched %d items from remote catalog (%s).",
            len(items),
            url,
        )
        return True

    def _load_remote_cache(self) -> bool:
        """
        Load the remote catalog from the on-disk cache (sync).

        The cache is valid only when ALL of the following hold:

            * ``PELDRUN_PROVIDER_CATALOG_URL`` is currently set.
            * The cache file exists.
            * The cache's schema version matches
              ``REMOTE_CATALOG_CACHE_SCHEMA_VERSION``.
            * The cache's ``source_url`` matches the CURRENT env var
              value — this guards against a stale cache written when
              a different URL was configured. Without this check,
              switching from catalog A to catalog B would silently
              serve A's items until the first successful B fetch.

        Returns ``True`` when the cache was loaded into
        ``_remote_items``.
        """
        url = _resolve_remote_catalog_url()
        if not url:
            # Operator explicitly disabled remote fetching. Clear any
            # stale in-memory state — the caller will see the builtin
            # catalog alone.
            self._remote_items = {}
            self._remote_loaded_at = 0.0
            return False

        if not self._cache_path.is_file():
            return False

        try:
            payload = json.loads(
                self._cache_path.read_text(encoding="utf-8")
            )
        except Exception as exc:
            logger.warning(
                "Remote catalog cache at %s is unreadable: %s",
                self._cache_path,
                exc,
            )
            return False

        if not isinstance(payload, dict):
            return False

        meta = payload.get("_cache_meta")
        items_raw = payload.get("_items")
        if not isinstance(meta, dict) or not isinstance(items_raw, dict):
            logger.warning(
                "Remote catalog cache is missing required keys "
                "(_cache_meta / _items); ignoring."
            )
            return False

        schema = meta.get("schema_version")
        if schema != REMOTE_CATALOG_CACHE_SCHEMA_VERSION:
            logger.info(
                "Remote catalog cache schema=%r != expected=%r; "
                "ignoring.",
                schema,
                REMOTE_CATALOG_CACHE_SCHEMA_VERSION,
            )
            return False

        cached_url = meta.get("source_url")
        if cached_url != url:
            logger.info(
                "Remote catalog cache source_url=%r != current=%r; "
                "ignoring (the URL changed since this cache was "
                "written).",
                cached_url,
                url,
            )
            return False

        items: Dict[str, ProviderPackageCatalogItem] = {}
        for item_id, item_dict in items_raw.items():
            if not isinstance(item_dict, dict):
                continue
            try:
                items[item_id] = ProviderPackageCatalogItem.model_validate(
                    item_dict
                )
            except Exception as exc:
                logger.warning(
                    "Skipping malformed cached item %r: %s",
                    item_id,
                    exc,
                )
                continue

        self._remote_items = items
        self._remote_loaded_at = float(meta.get("fetched_at") or 0.0)
        logger.info(
            "Loaded %d items from remote catalog cache (age=%.1fs).",
            len(items),
            max(0.0, time.time() - self._remote_loaded_at),
        )
        return True

    def _write_remote_cache(
        self,
        items: Dict[str, ProviderPackageCatalogItem],
        url: str,
        fetched_at: float,
    ) -> None:
        """Persist the remote catalog to disk atomically. Never raises."""
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
            logger.info(
                "Wrote %d items to remote catalog cache at %s.",
                len(items),
                self._cache_path,
            )
        except Exception as exc:
            logger.warning(
                "Failed to write remote catalog cache at %s: %s",
                self._cache_path,
                exc,
            )

    def get_status(self) -> Dict[str, Any]:
        """
        Return a snapshot of the catalog state. Used by the admin
        endpoint and for debugging.

        The ``remote_url_source`` field reports how the effective URL
        was determined:

            * ``"default"``      — env var unset, using the built-in
                                   ``DEFAULT_REMOTE_CATALOG_URL``. This
                                   is the normal state for a
                                   freshly-installed PELDRUN.
            * ``"env_override"`` — env var set to a custom URL.
            * ``"disabled"``     — env var set to an empty string.
        """
        effective_url = _resolve_remote_catalog_url()
        explicit_override = (
            REMOTE_CATALOG_ENV_VAR in os.environ
            and bool(os.environ.get(REMOTE_CATALOG_ENV_VAR, "").strip())
        )
        explicit_disable = (
            REMOTE_CATALOG_ENV_VAR in os.environ
            and not os.environ.get(REMOTE_CATALOG_ENV_VAR, "").strip()
        )
        return {
            "item_count": len(self._items),
            "builtin_count": len(self._builtin_items),
            "custom_count": len(self._custom_items),
            "extra_count": len(self._extra_items),
            "remote_count": len(self._remote_items),
            "remote_url_configured": bool(effective_url),
            "remote_url_effective": effective_url or None,
            "remote_url_source": (
                "env_override"
                if explicit_override
                else "disabled"
                if explicit_disable
                else "default"
            ),
            "remote_cache_age_seconds": (
                max(0.0, time.time() - self._remote_loaded_at)
                if self._remote_loaded_at > 0
                else None
            ),
            "remote_ttl_seconds": self._ttl_seconds,
            "remote_last_error": self._remote_last_error,
            "cache_path": str(self._cache_path),
        }

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    def list_items(self) -> List[ProviderPackageCatalogItem]:
        """
        Return every catalog item, sorted by display name.

        Each item is returned as-is; use ``is_installable()`` to
        determine whether it can be installed right now.
        """
        items = list(self._items.values())
        items.sort(
            key=lambda i: (i.display_name or i.name or i.id).lower()
        )
        return items

    def get_item(
        self, item_id: str
    ) -> Optional[ProviderPackageCatalogItem]:
        """Return the catalog item with ``item_id``, or ``None``."""
        if not item_id:
            return None
        return self._items.get(item_id)

    def get_or_raise(self, item_id: str) -> ProviderPackageCatalogItem:
        """
        Return the catalog item, or raise ``ProviderPackageCatalogError``.
        """
        item = self.get_item(item_id)
        if item is None:
            raise ProviderPackageCatalogError(
                f"Catalog item '{item_id}' does not exist. Available "
                f"items: {sorted(self._items.keys())}.",
                details={"item_id": item_id},
            )
        return item

    # ------------------------------------------------------------------
    # Install from catalog
    # ------------------------------------------------------------------

    async def install_from_catalog(
        self, item_id: str
    ) -> ProviderPackageInstallResult:
        """
        Download, verify, and install a catalog item.

        Flow
        ----
        1. Lookup the item; 404-equivalent error if missing.
        2. Reject preview-only items with a clear explanation.
        3. Download the ZIP with a hard timeout and a size cap.
        4. Verify the SHA-256 against ``item.checksum_sha256``.
        5. Delegate to ``ProviderPackageManager.install_bundle``.

        Parameters
        ----------
        item_id
            The catalog item's ``id`` field.

        Returns
        -------
        ProviderPackageInstallResult
            The same shape returned by a manual upload.

        Raises
        ------
        ProviderPackageCatalogError
            When the item is missing, not installable, the download
            fails, the size cap is exceeded, or the checksum does not
            match.
        ProviderPackageError
            Any error from the install pipeline itself (validation,
            conflict, environment, storage) is propagated unchanged.
        """
        # Best-effort refresh so an item published to the remote
        # catalog a minute ago is installable without waiting for the
        # listing endpoint's TTL to expire. Failure is non-fatal —
        # we fall back to the merged view that may already contain
        # the item from cache or from a local source.
        try:
            await self.ensure_fresh()
        except Exception:
            # ensure_fresh() is documented never to raise, but this
            # is a hard user-facing operation — never let a refresh
            # bug block an install that could otherwise succeed.
            pass

        item = self.get_or_raise(item_id)

        if not is_installable(item):
            raise ProviderPackageCatalogError(
                f"Catalog item '{item_id}' cannot be installed: "
                f"{installability_reason(item)}",
                details={"item_id": item_id, "reason": "not_installable"},
            )

        # Defense-in-depth: re-assert the invariants after the
        # installability check, so static type checkers and future
        # refactors cannot accidentally pass ``None`` to the downloader.
        assert item.download_url is not None
        assert item.checksum_sha256 is not None

        zip_bytes = await self._download_zip(
            url=item.download_url,
            expected_sha256=item.checksum_sha256.strip().lower(),
            provider_hint=item.id,
        )

        # Delegate to the manager. Any install error propagates as-is;
        # the caller (the HTTP route) maps it to the appropriate status.
        return await self._manager.install_bundle(
            zip_bytes=zip_bytes,
            source_filename=f"{item.id}-{item.version}.zip",
            install_dependencies=True,
            source="catalog",
        )

    # ------------------------------------------------------------------
    # Download
    # ------------------------------------------------------------------

    async def _download_zip(
        self,
        *,
        url: str,
        expected_sha256: str,
        provider_hint: str,
    ) -> bytes:
        """
        Download ``url`` into memory, enforcing timeout, size cap, and
        SHA-256 verification.

        Parameters
        ----------
        url
            The download URL from the catalog item.
        expected_sha256
            The lowercase hex SHA-256 the downloaded bytes must match.
        provider_hint
            The catalog item's ID, used only in error messages.

        Returns
        -------
        bytes
            The verified ZIP content.

        Raises
        ------
        ProviderPackageCatalogError
            On any failure (HTTP error, timeout, size cap exceeded,
            checksum mismatch).
        """
        # --------------------------------------------------------------
        # Sanity check the URL scheme. Only http(s) is allowed. A
        # ``file://`` URL would let a malicious catalog entry read
        # arbitrary local files under the PELDRUN process's UID —
        # not acceptable for an end-user-facing feature.
        # --------------------------------------------------------------
        scheme_check = (url or "").strip().lower()
        if not (
            scheme_check.startswith("http://")
            or scheme_check.startswith("https://")
        ):
            raise ProviderPackageCatalogError(
                f"Catalog item '{provider_hint}' declares an unsafe "
                f"download URL scheme. Only http:// and https:// are "
                f"allowed.",
                details={
                    "item_id": provider_hint,
                    "reason": "unsafe_url_scheme",
                },
            )

        # --------------------------------------------------------------
        # Stream the download so we can enforce the size cap mid-stream
        # without trusting the Content-Length header.
        # --------------------------------------------------------------
        content = bytearray()
        hasher = hashlib.sha256()

        try:
            # follow_redirects=True is bounded by MAX_DOWNLOAD_REDIRECTS
            # via the ``max_redirects`` kwarg.
            async with httpx.AsyncClient(
                timeout=DOWNLOAD_TIMEOUT_SECONDS,
                follow_redirects=True,
                max_redirects=MAX_DOWNLOAD_REDIRECTS,
            ) as client:
                async with client.stream("GET", url) as response:
                    if response.status_code != 200:
                        raise ProviderPackageCatalogError(
                            f"Catalog item '{provider_hint}' download "
                            f"failed: HTTP {response.status_code} from "
                            f"{url}.",
                            details={
                                "item_id": provider_hint,
                                "status_code": response.status_code,
                                "url": url,
                            },
                        )

                    async for chunk in response.aiter_bytes(
                        chunk_size=64 * 1024
                    ):
                        if not chunk:
                            continue

                        # Enforce the cap BEFORE extending the buffer.
                        if (
                            len(content) + len(chunk)
                            > MAX_PACKAGE_SIZE_BYTES
                        ):
                            raise ProviderPackageCatalogError(
                                f"Catalog item '{provider_hint}' "
                                f"download exceeds the {MAX_PACKAGE_SIZE_BYTES}"
                                f"-byte limit. Aborting to protect "
                                f"the server.",
                                details={
                                    "item_id": provider_hint,
                                    "max_bytes": MAX_PACKAGE_SIZE_BYTES,
                                    "received_so_far": len(content),
                                },
                            )

                        content.extend(chunk)
                        hasher.update(chunk)

        except ProviderPackageCatalogError:
            raise
        except httpx.TimeoutException as exc:
            raise ProviderPackageCatalogError(
                f"Catalog item '{provider_hint}' download timed out "
                f"after {DOWNLOAD_TIMEOUT_SECONDS:.0f}s: {exc}",
                details={
                    "item_id": provider_hint,
                    "reason": "timeout",
                },
            ) from exc
        except httpx.HTTPError as exc:
            raise ProviderPackageCatalogError(
                f"Catalog item '{provider_hint}' download failed: {exc}",
                details={
                    "item_id": provider_hint,
                    "reason": "http_error",
                },
            ) from exc
        except Exception as exc:
            raise ProviderPackageCatalogError(
                f"Catalog item '{provider_hint}' download failed "
                f"unexpectedly: {exc}",
                details={
                    "item_id": provider_hint,
                    "reason": "unexpected_error",
                },
            ) from exc

        if not content:
            raise ProviderPackageCatalogError(
                f"Catalog item '{provider_hint}' download returned "
                f"empty content.",
                details={
                    "item_id": provider_hint,
                    "reason": "empty_body",
                },
            )

        # --------------------------------------------------------------
        # Verify the checksum.
        # --------------------------------------------------------------
        actual_sha256 = hasher.hexdigest()
        if actual_sha256 != expected_sha256:
            raise ProviderPackageCatalogError(
                f"Catalog item '{provider_hint}' failed checksum "
                f"verification. Expected: {expected_sha256[:16]}..., "
                f"got: {actual_sha256[:16]}.... The download may have "
                f"been tampered with, or the catalog may be out of "
                f"date.",
                details={
                    "item_id": provider_hint,
                    "reason": "checksum_mismatch",
                    "expected": expected_sha256,
                    "actual": actual_sha256,
                },
            )

        logger.info(
            "Downloaded and verified catalog item '%s' (%d bytes, "
            "sha256=%s...).",
            provider_hint,
            len(content),
            actual_sha256[:16],
        )

        return bytes(content)


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------


#: Shared catalog instance. Constructed lazily so a test that only
#: imports this module does not pay the disk-read cost.
_catalog_instance: Optional[ProviderPackageCatalog] = None


def get_provider_package_catalog() -> ProviderPackageCatalog:
    """
    Return the process-wide catalog singleton.

    Constructed on first call. Tests that need isolation should build
    their own ``ProviderPackageCatalog(extra_paths=[...], manager=...)``
    instead of using this function.
    """
    global _catalog_instance
    if _catalog_instance is None:
        _catalog_instance = ProviderPackageCatalog()
    return _catalog_instance


def reset_provider_package_catalog_for_tests() -> None:
    """Reset the catalog singleton. Intended only for tests."""
    global _catalog_instance
    _catalog_instance = None


__all__ = [
    "BUILTIN_CATALOG_FILENAME",
    "CUSTOM_CATALOG_ENV_VAR",
    "REMOTE_CATALOG_ENV_VAR",
    "REMOTE_CATALOG_CACHE_FILENAME",
    "REMOTE_CATALOG_TTL_SECONDS",
    "REMOTE_CATALOG_FETCH_TIMEOUT_SECONDS",
    "REMOTE_CATALOG_MAX_BYTES",
    "REMOTE_CATALOG_MAX_REDIRECTS",
    "REMOTE_CATALOG_CACHE_SCHEMA_VERSION",
    "DOWNLOAD_TIMEOUT_SECONDS",
    "MAX_DOWNLOAD_REDIRECTS",
    "load_catalog",
    "is_installable",
    "installability_reason",
    "ProviderPackageCatalog",
    "get_provider_package_catalog",
    "reset_provider_package_catalog_for_tests",
]
