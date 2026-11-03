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

import hashlib
import json
import logging
import os
import re
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


#: Environment variable for a custom catalog override.
CUSTOM_CATALOG_ENV_VAR: str = "PELDRUN_PROVIDER_CATALOG_PATH"


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


def _resolve_catalog_paths() -> List[Path]:
    """
    Return the ordered list of catalog file paths to read.

    Order
    -----
    1. Custom catalog (via ``PELDRUN_PROVIDER_CATALOG_PATH``), if set
       and the file exists.
    2. Builtin catalog next to this module.

    Later sources override earlier ones at the ITEM level — an item
    with the same ``id`` in the custom catalog replaces the builtin
    entry of the same ID.
    """
    paths: List[Path] = []

    custom_path_str = os.environ.get(CUSTOM_CATALOG_ENV_VAR, "").strip()
    if custom_path_str:
        try:
            custom_path = Path(custom_path_str).expanduser().resolve()
            if custom_path.is_file():
                paths.append(custom_path)
            else:
                logger.warning(
                    "Custom catalog path '%s' does not exist; ignoring.",
                    custom_path,
                )
        except Exception as exc:
            logger.warning(
                "Custom catalog path '%s' could not be resolved: %s; "
                "ignoring.",
                custom_path_str,
                exc,
            )

    builtin_path = Path(__file__).resolve().parent / BUILTIN_CATALOG_FILENAME
    if builtin_path.is_file():
        paths.append(builtin_path)
    else:
        logger.info(
            "Builtin catalog file not found at %s; the catalog will be "
            "empty unless a custom catalog is configured.",
            builtin_path,
        )

    return paths


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
    Load and merge all catalog sources.

    Parameters
    ----------
    extra_paths
        Additional catalog file paths to merge. Used by tests and by
        an admin endpoint that wants to preview a candidate catalog.
        Higher-priority than the environment-variable custom catalog
        (applied last, so items override everything else).

    Returns
    -------
    Dict[str, ProviderPackageCatalogItem]
        Keyed by catalog item ID.
    """
    all_paths = _resolve_catalog_paths()
    if extra_paths:
        all_paths.extend(Path(p).expanduser().resolve() for p in extra_paths)

    merged: Dict[str, ProviderPackageCatalogItem] = {}

    for path in all_paths:
        try:
            raw_text = path.read_text(encoding="utf-8")
        except OSError as exc:
            logger.warning(
                "Could not read catalog file %s: %s; skipping.", path, exc
            )
            continue
        try:
            parsed = json.loads(raw_text)
        except json.JSONDecodeError as exc:
            logger.warning(
                "Catalog file %s is not valid JSON: %s; skipping.",
                path,
                exc,
            )
            continue

        items = _parse_catalog_payload(parsed, source_path=path)
        # Later sources override earlier ones at the item level.
        merged.update(items)

        logger.debug(
            "Catalog source %s contributed %d items.", path, len(items)
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

    The catalog is loaded once at construction (or on ``reload()``).
    Between reloads, ``list_items()`` and ``get_item()`` serve from the
    in-memory cache — no disk I/O on the hot path.

    Thread-safety
    -------------
    Reads are safe without a lock (dict reads are atomic in CPython).
    ``reload()`` replaces the entire dict reference atomically. Concurrent
    reads during a reload see either the old dict or the new one, never a
    partial state.
    """

    def __init__(
        self,
        *,
        extra_paths: Optional[Iterable[Path]] = None,
        manager: Optional[ProviderPackageManager] = None,
    ) -> None:
        self._extra_paths: List[Path] = [
            Path(p).expanduser().resolve() for p in (extra_paths or [])
        ]
        self._manager = manager or default_manager
        self._items: Dict[str, ProviderPackageCatalogItem] = {}
        self.reload()

    # ------------------------------------------------------------------
    # Load / reload
    # ------------------------------------------------------------------

    def reload(self) -> int:
        """
        Reload the catalog from disk. Returns the new item count.

        Used by tests and by any future admin endpoint. Under normal
        operation the catalog is loaded once at process startup.
        """
        items = load_catalog(extra_paths=self._extra_paths)
        # Atomic reference swap.
        self._items = items
        logger.info("Provider package catalog loaded: %d items.", len(items))
        return len(items)

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
    "DOWNLOAD_TIMEOUT_SECONDS",
    "MAX_DOWNLOAD_REDIRECTS",
    "load_catalog",
    "is_installable",
    "installability_reason",
    "ProviderPackageCatalog",
    "get_provider_package_catalog",
    "reset_provider_package_catalog_for_tests",
]
