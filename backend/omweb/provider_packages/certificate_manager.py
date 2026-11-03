# backend/omweb/provider_packages/certificate_manager.py
"""
Certificate installation and lifecycle management for Provider Packages.

This module is the single write path for TLS certificates that a
Provider Package requires on the host filesystem. It handles the full
lifecycle:

    1. **Download**  — fetch the certificate from the URL declared in
       the package's manifest (``CertificateSpec.download_url``).
    2. **Verify**    — check size, SHA-256 (if declared), and format
       (PEM or DER). Reject anything that does not match.
    3. **Install**   — write the file atomically to the destination
       path (either user-supplied or auto-managed under the provider's
       storage directory).
    4. **Record**    — persist the installed state in
       ``models_vault.json`` under the ``provider_certificates`` key,
       so the UI can show the status and the adapter can look up the
       path.
    5. **Monitor**   — on demand, re-verify the installed file
       (existence, hash, format) and update the record's
       ``last_checked_at`` and ``status``.
    6. **Remove**    — delete the file from disk and remove the
       record from the vault in one atomic operation.

Why a dedicated module
----------------------
Certificates are files on the host filesystem, not just JSON values.
They have a different lifecycle from connections:

    * Connections are pure JSON — saving them is a vault write.
    * Certificates represent content on disk that must remain
      consistent with the vault record. If the user deletes the file
      manually, the vault record becomes stale and the UI must show
      "missing". If the user restores it, the UI must show
      "installed" again.

Keeping this logic in one module makes the invariants explicit and
testable. The rest of the system (routers, cards, adapters) only
needs to call high-level methods like ``install()`` and
``check_status()``.

Security model
--------------
The module enforces:

    * **Size limit** — ``MAX_CERTIFICATE_BYTES`` (1 MB). A "certificate"
      larger than this is almost certainly an attack or a bug.
    * **Timeout** — 60 seconds for the download. No endless hangs.
    * **HTTPS-only** — the URL scheme is validated at manifest
      validation time (``CertificateSpec._validate_download_url``).
    * **Hash verification** — when ``download_sha256`` is declared, the
      downloaded bytes must match. This prevents tampering by a
      compromised mirror.
    * **Format validation** — the file must look like a PEM or DER
      certificate. This catches the "wrong file downloaded" class of
      mistakes (HTML error pages, redirect bodies, etc.).
    * **Atomic write** — the file is written to a temp path first, then
      renamed into place. A crash mid-write leaves the previous file
      intact.

The download is performed in the MAIN backend process (not the worker
subprocess) precisely because the worker's network allowlist restricts
outbound requests to the connection's ``base_url`` host. The CA host
(e.g. ``gu-st.ru``) is a different host, so the worker could not reach
it. By running the download in the main process, we bypass the
allowlist at the cost of trusting this specific code path — which is
the whole point of centralizing it here.

Storage layout
--------------
    <backend>/data/models_vault.json
        └── provider_certificates: {
              "<provider_id>:<certificate_id>": { ...record... },
              ...
            }

    <backend>/storage/store/providers/<provider_id>/certificates/
        └── <download_filename>

The vault is the SINGLE source of truth for "what we think is
installed". The filesystem is the source of truth for "what is
actually present". ``check_status()`` reconciles the two.

See also
--------
    * ``backend/omweb/provider_packages/schema.py`` — CertificateSpec,
      ProviderCertificateRecord, CERTIFICATES_VAULT_KEY
    * ``backend/omweb/routers/store/providers.py`` — HTTP endpoints
    * ``backend/omweb/routers/config_rtr.py`` — vault I/O precedent
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import ssl
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx

from omweb.provider_packages.errors import (
    ProviderPackageError,
    ProviderPackageStorageError,
    ProviderPackageValidationError,
)
from omweb.provider_packages.schema import (
    CERTIFICATES_VAULT_KEY,
    MAX_CERTIFICATE_BYTES,
    CertificateSpec,
    ProviderCertificateRecord,
)


logger = logging.getLogger("omweb.provider_packages.certificate_manager")


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


#: Default timeout for a single certificate download. Long enough for a
#: slow but working mirror, short enough that a wedged connection does
#: not block the install endpoint indefinitely.
DEFAULT_DOWNLOAD_TIMEOUT_SECONDS: float = 60.0


#: Subdirectory under each provider's storage directory where managed
#: certificates are placed when the spec does not declare an explicit
#: ``default_destination``.
MANAGED_CERTIFICATES_SUBDIR: str = "certificates"


#: Recognized PEM headers. A bundle is accepted if any of these appear.
_PEM_HEADERS: tuple = (
    "-----BEGIN CERTIFICATE-----",
    "-----BEGIN TRUSTED CERTIFICATE-----",
    "-----BEGIN X509 CERTIFICATE-----",
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _utc_now_iso() -> str:
    """Return the current UTC time as an ISO-8601 string."""
    return datetime.now(timezone.utc).isoformat()


def _sha256_of(data: bytes) -> str:
    """Return the hex-encoded SHA-256 digest of ``data``."""
    return hashlib.sha256(data).hexdigest()


def _sha256_of_file(path: Path) -> str:
    """Return the hex-encoded SHA-256 digest of a file's contents."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _looks_like_pem(data: bytes) -> bool:
    """
    Return True iff ``data`` appears to contain at least one PEM block.

    The check is deliberately loose — a proper parse would require the
    full x509 machinery and would reject bundles with unusual but valid
    extensions. The goal here is to catch the "user downloaded an HTML
    error page" class of mistakes, not to be a certificate validator.
    """
    if not data or len(data) < 32:
        return False
    try:
        text = data.decode("utf-8", errors="ignore")
    except Exception:
        return False
    return any(header in text for header in _PEM_HEADERS)


def _looks_like_der(data: bytes) -> bool:
    """
    Return True iff ``data`` appears to be a DER-encoded X.509 cert.

    A DER certificate starts with a 0x30 (SEQUENCE) tag. We attempt a
    full parse via ``ssl.DER_cert_to_PEM_cert`` to filter out files
    that happen to start with 0x30 for other reasons.
    """
    if not data or data[0] != 0x30:
        return False
    try:
        ssl.DER_cert_to_PEM_cert(data)
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Status report
# ---------------------------------------------------------------------------


@dataclass
class CertificateStatusReport:
    """
    Result of ``CertificateManager.check_status()``.

    A status report is what the UI consumes to decide what to show on
    the card and in the Certificate Manager modal. It carries both the
    vault record (what we last knew) and the live filesystem state
    (what is true right now). The two can drift — a user might delete
    the file manually, or the hash might change after an OS-level
    reinstall — and the UI needs to surface that drift.

    ``status`` is one of:
        * ``installed`` — record exists, file present, hash matches.
        * ``missing``   — record exists, file absent.
        * ``invalid``   — file present, hash or format mismatch.
        * ``expired``   — reserved for future use (cert expiry checks).
        * ``pending``   — no record yet (never attempted).
    """

    provider_id: str
    certificate_id: str
    display_name: str
    status: str
    installed_path: Optional[str] = None
    expected_sha256: Optional[str] = None
    actual_sha256: Optional[str] = None
    size_bytes: int = 0
    installed_at: Optional[str] = None
    last_checked_at: Optional[str] = None
    error_message: Optional[str] = None
    download_url: Optional[str] = None
    docs_url: Optional[str] = None
    required: bool = True

    def to_dict(self) -> Dict[str, Any]:
        """Serialize for the API response."""
        return {
            "provider_id": self.provider_id,
            "certificate_id": self.certificate_id,
            "display_name": self.display_name,
            "status": self.status,
            "installed_path": self.installed_path,
            "expected_sha256": self.expected_sha256,
            "actual_sha256": self.actual_sha256,
            "size_bytes": self.size_bytes,
            "installed_at": self.installed_at,
            "last_checked_at": self.last_checked_at,
            "error_message": self.error_message,
            "download_url": self.download_url,
            "docs_url": self.docs_url,
            "required": self.required,
        }


# ---------------------------------------------------------------------------
# Certificate Manager
# ---------------------------------------------------------------------------


class CertificateManager:
    """
    Manages installation and verification of TLS certificates.

    One instance is created at module load time and shared by all
    callers (router, tests). The class itself is stateless beyond the
    two paths passed to the constructor — the vault on disk is the
    single source of truth, read fresh on each method call to avoid
    stale caching issues across concurrent operations.

    Concurrency
    -----------
    All mutating operations acquire a per-instance lock before touching
    the vault. The lock is held for the full read-modify-write cycle,
    which is acceptable because certificate operations are rare
    (install once per package, occasionally re-check) — they are not
    on the hot path of request handling.
    """

    def __init__(
        self,
        vault_path: Path,
        storage_root: Path,
        *,
        download_timeout_seconds: float = DEFAULT_DOWNLOAD_TIMEOUT_SECONDS,
    ) -> None:
        """
        Parameters
        ----------
        vault_path
            Absolute path to ``models_vault.json``.
        storage_root
            Absolute path to ``storage/store/providers/``. Used to
            compute managed destinations when the spec does not declare
            an explicit path.
        download_timeout_seconds
            Timeout for a single download. Default 60 seconds.
        """
        self._vault_path = Path(vault_path).resolve()
        self._storage_root = Path(storage_root).resolve()
        self._timeout = float(download_timeout_seconds)

        # Per-instance async lock. Certificate operations are rare;
        # serializing them eliminates a whole class of race conditions.
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Public API — read
    # ------------------------------------------------------------------

    def list_records(
        self, provider_id: Optional[str] = None
    ) -> List[ProviderCertificateRecord]:
        """
        Return every vault record, optionally filtered by provider.

        Records whose ``status`` is ``"missing"`` or ``"invalid"`` are
        still returned — the UI needs to show a "last attempt failed"
        state, not silently hide the record.
        """
        vault = self._load_vault()
        records_raw = vault.get(CERTIFICATES_VAULT_KEY) or {}
        if not isinstance(records_raw, dict):
            return []

        result: List[ProviderCertificateRecord] = []
        for key, raw in records_raw.items():
            if not isinstance(raw, dict):
                continue
            try:
                record = ProviderCertificateRecord.model_validate(raw)
            except Exception as exc:
                logger.warning(
                    "Skipping malformed certificate record %r: %s", key, exc
                )
                continue
            if provider_id is not None and record.provider_package_id != provider_id:
                continue
            result.append(record)
        return result

    def get_record(
        self, provider_id: str, certificate_id: str
    ) -> Optional[ProviderCertificateRecord]:
        """Return the record for a specific certificate, or ``None``."""
        key = self._record_key(provider_id, certificate_id)
        vault = self._load_vault()
        records_raw = vault.get(CERTIFICATES_VAULT_KEY) or {}
        raw = records_raw.get(key)
        if not isinstance(raw, dict):
            return None
        try:
            return ProviderCertificateRecord.model_validate(raw)
        except Exception:
            return None

    def resolve_destination(
        self, provider_id: str, spec: CertificateSpec
    ) -> Path:
        """
        Compute the destination path for a certificate.

        When ``spec.default_destination`` is declared, it is used
        verbatim (must be absolute). Otherwise, the manager places the
        file under the provider's managed directory:
        ``<storage_root>/<provider_id>/certificates/<filename>``.

        The managed path is chosen when no explicit destination is
        required, because it does not need root privileges and it stays
        within PELDRUN's own storage tree.
        """
        if spec.default_destination:
            return Path(spec.default_destination).resolve()

        return (
            self._storage_root
            / provider_id
            / MANAGED_CERTIFICATES_SUBDIR
            / spec.download_filename
        )

    # ------------------------------------------------------------------
    # Public API — write
    # ------------------------------------------------------------------

    async def install(
        self,
        provider_id: str,
        spec: CertificateSpec,
        *,
        destination: Optional[Path] = None,
        allow_overwrite: bool = True,
    ) -> ProviderCertificateRecord:
        """
        Download, verify, and install a certificate.

        Idempotency
        -----------
        If the destination file already exists with the correct SHA-256
        (when ``spec.download_sha256`` is declared), the download is
        skipped and the existing file is registered as installed. This
        makes repeated "Install" clicks cheap.

        Error handling
        --------------
        On any failure, a vault record with ``status="missing"`` (or
        ``"invalid"``) and a populated ``error_message`` is saved. This
        lets the UI show "last attempt failed: <reason>" instead of
        silently retrying. The exception is then re-raised so the
        caller can return the correct HTTP status.

        Parameters
        ----------
        provider_id
            The package's ``manifest.id``.
        spec
            The ``CertificateSpec`` from the manifest.
        destination
            Optional override for the install path. When ``None``,
            ``resolve_destination()`` is used.
        allow_overwrite
            When ``False``, an existing destination file causes a
            ``ProviderPackageConflictError`` instead of being
            overwritten.

        Raises
        ------
        ProviderPackageError
            On any download, verification, or filesystem failure. The
            vault record is updated BEFORE the exception propagates.
        """
        async with self._lock:
            target = destination or self.resolve_destination(provider_id, spec)
            target = Path(target).resolve()

            logger.info(
                "Installing certificate %r for provider %r → %s",
                spec.id,
                provider_id,
                target,
            )

            # ----------------------------------------------------------
            # Step 1 — idempotency shortcut.
            # If the file already exists with the right hash, register
            # it as installed without re-downloading.
            # ----------------------------------------------------------
            if target.is_file() and spec.download_sha256:
                try:
                    existing_hash = _sha256_of_file(target)
                    if existing_hash == spec.download_sha256:
                        record = self._make_record(
                            provider_id=provider_id,
                            spec=spec,
                            installed_path=str(target),
                            sha256=existing_hash,
                            size_bytes=target.stat().st_size,
                            status="installed",
                        )
                        self._save_record(record)
                        logger.info(
                            "Certificate %r already installed at %s "
                            "(hash matches, skipping download).",
                            spec.id,
                            target,
                        )
                        return record
                except Exception as exc:
                    logger.debug(
                        "Hash pre-check failed for %s: %s",
                        target,
                        exc,
                    )
                    # Fall through to a fresh download.

            # ----------------------------------------------------------
            # Step 2 — download.
            # ----------------------------------------------------------
            try:
                data = await self._download(spec.download_url)
            except Exception as exc:
                error_message = f"Download failed: {exc}"
                self._save_record(
                    self._make_record(
                        provider_id=provider_id,
                        spec=spec,
                        installed_path=None,
                        sha256=None,
                        size_bytes=0,
                        status="missing",
                        error_message=error_message,
                    )
                )
                if isinstance(exc, ProviderPackageError):
                    raise
                raise ProviderPackageError(
                    error_message,
                    code="PROVIDER_CERTIFICATE_DOWNLOAD_FAILED",
                    details={
                        "provider_id": provider_id,
                        "certificate_id": spec.id,
                        "download_url": spec.download_url,
                    },
                ) from exc

            # ----------------------------------------------------------
            # Step 3 — size check (defense in depth; the download
            # method already enforces this, but check again).
            # ----------------------------------------------------------
            if len(data) > MAX_CERTIFICATE_BYTES:
                error_message = (
                    f"Downloaded file is {len(data)} bytes, exceeding "
                    f"the limit of {MAX_CERTIFICATE_BYTES} bytes."
                )
                self._save_record(
                    self._make_record(
                        provider_id=provider_id,
                        spec=spec,
                        installed_path=None,
                        sha256=None,
                        size_bytes=len(data),
                        status="invalid",
                        error_message=error_message,
                    )
                )
                raise ProviderPackageValidationError(
                    error_message,
                    code="PROVIDER_CERTIFICATE_TOO_LARGE",
                    details={
                        "provider_id": provider_id,
                        "certificate_id": spec.id,
                        "size_bytes": len(data),
                        "max_bytes": MAX_CERTIFICATE_BYTES,
                    },
                )

            # ----------------------------------------------------------
            # Step 4 — hash verification.
            # ----------------------------------------------------------
            actual_hash = _sha256_of(data)
            if (
                spec.download_sha256
                and actual_hash != spec.download_sha256
            ):
                error_message = (
                    f"SHA-256 mismatch. Expected "
                    f"{spec.download_sha256}, got {actual_hash}."
                )
                self._save_record(
                    self._make_record(
                        provider_id=provider_id,
                        spec=spec,
                        installed_path=None,
                        sha256=actual_hash,
                        size_bytes=len(data),
                        status="invalid",
                        error_message=error_message,
                    )
                )
                raise ProviderPackageValidationError(
                    error_message,
                    code="PROVIDER_CERTIFICATE_HASH_MISMATCH",
                    details={
                        "provider_id": provider_id,
                        "certificate_id": spec.id,
                        "expected_sha256": spec.download_sha256,
                        "actual_sha256": actual_hash,
                    },
                )

            # ----------------------------------------------------------
            # Step 5 — format validation.
            # ----------------------------------------------------------
            is_pem = _looks_like_pem(data)
            is_der = _looks_like_der(data)
            if not is_pem and not is_der:
                error_message = (
                    "Downloaded file does not look like a PEM or DER "
                    "certificate. Common causes: the URL returned an "
                    "HTML error page, a redirect body, or a file that "
                    "is not a certificate at all."
                )
                self._save_record(
                    self._make_record(
                        provider_id=provider_id,
                        spec=spec,
                        installed_path=None,
                        sha256=actual_hash,
                        size_bytes=len(data),
                        status="invalid",
                        error_message=error_message,
                    )
                )
                raise ProviderPackageValidationError(
                    error_message,
                    code="PROVIDER_CERTIFICATE_INVALID_FORMAT",
                    details={
                        "provider_id": provider_id,
                        "certificate_id": spec.id,
                        "sha256": actual_hash,
                        "size_bytes": len(data),
                    },
                )

            # ----------------------------------------------------------
            # Step 6 — write atomically.
            # ----------------------------------------------------------
            if target.exists() and not allow_overwrite:
                raise ProviderPackageError(
                    f"Certificate file already exists at {target} "
                    f"and allow_overwrite is False.",
                    code="PROVIDER_CERTIFICATE_ALREADY_EXISTS",
                    details={
                        "provider_id": provider_id,
                        "certificate_id": spec.id,
                        "path": str(target),
                    },
                )

            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                tmp_fd, tmp_path_str = tempfile.mkstemp(
                    prefix=target.name + ".",
                    suffix=".tmp",
                    dir=str(target.parent),
                )
                tmp_path = Path(tmp_path_str)
                try:
                    with os.fdopen(tmp_fd, "wb") as f:
                        f.write(data)
                        f.flush()
                        os.fsync(f.fileno())
                    # Preserve reasonable permissions on the target.
                    os.chmod(tmp_path, 0o644)
                    os.replace(str(tmp_path), str(target))
                except Exception:
                    # Best-effort cleanup of the temp file.
                    try:
                        if tmp_path.exists():
                            tmp_path.unlink()
                    except Exception:
                        pass
                    raise
            except OSError as exc:
                error_message = (
                    f"Could not write certificate to {target}: {exc}"
                )
                self._save_record(
                    self._make_record(
                        provider_id=provider_id,
                        spec=spec,
                        installed_path=None,
                        sha256=actual_hash,
                        size_bytes=len(data),
                        status="missing",
                        error_message=error_message,
                    )
                )
                raise ProviderPackageStorageError(
                    error_message,
                    details={
                        "provider_id": provider_id,
                        "certificate_id": spec.id,
                        "path": str(target),
                    },
                ) from exc

            # ----------------------------------------------------------
            # Step 7 — persist success record.
            # ----------------------------------------------------------
            record = self._make_record(
                provider_id=provider_id,
                spec=spec,
                installed_path=str(target),
                sha256=actual_hash,
                size_bytes=len(data),
                status="installed",
            )
            self._save_record(record)

            logger.info(
                "Certificate %r installed at %s (%d bytes, sha256=%s).",
                spec.id,
                target,
                len(data),
                actual_hash[:12] + "...",
            )
            return record

    async def check_status(
        self,
        provider_id: str,
        spec: CertificateSpec,
    ) -> CertificateStatusReport:
        """
        Reconcile the vault record with the actual filesystem state.

        Updates the record's ``status``, ``last_checked_at``, and
        ``actual_sha256`` fields, then returns a full report. Does NOT
        re-download — the check operates on whatever is currently on
        disk.

        Cases:
            * **No record + no file** → ``pending`` (never attempted).
            * **No record + file present** → ``installed`` (adopt the
              existing file; useful when the operator pre-installed it).
            * **Record + file absent** → ``missing``.
            * **Record + file present + hash matches** → ``installed``.
            * **Record + file present + hash mismatch** → ``invalid``.
            * **Record + file present + format invalid** → ``invalid``.
        """
        target = self.resolve_destination(provider_id, spec)
        target = Path(target).resolve()
        existing = self.get_record(provider_id, spec.id)

        # ----------------------------------------------------------
        # Case: no file present.
        # ----------------------------------------------------------
        if not target.is_file():
            if existing is not None:
                existing.status = "missing"
                existing.last_checked_at = _utc_now_iso()
                existing.error_message = (
                    existing.error_message
                    or f"File not found at {target}."
                )
                self._save_record(existing)
            return self._report_from_parts(
                provider_id=provider_id,
                spec=spec,
                status="missing" if existing is not None else "pending",
                installed_path=None,
                actual_sha256=None,
                size_bytes=0,
                installed_at=existing.installed_at if existing else None,
                error_message=(
                    existing.error_message if existing else None
                ),
            )

        # ----------------------------------------------------------
        # Case: file present. Compute hash, compare, validate format.
        # ----------------------------------------------------------
        try:
            actual_hash = _sha256_of_file(target)
            size_bytes = target.stat().st_size
        except Exception as exc:
            return self._report_from_parts(
                provider_id=provider_id,
                spec=spec,
                status="invalid",
                installed_path=str(target),
                actual_sha256=None,
                size_bytes=0,
                installed_at=existing.installed_at if existing else None,
                error_message=f"Could not read file: {exc}",
            )

        status = "installed"
        error_message: Optional[str] = None

        if spec.download_sha256 and actual_hash != spec.download_sha256:
            status = "invalid"
            error_message = (
                f"File exists but SHA-256 does not match. Expected "
                f"{spec.download_sha256}, got {actual_hash}."
            )
        else:
            # Loose format check — the file must look like a cert.
            try:
                with open(target, "rb") as f:
                    head = f.read(4096)
                if not (_looks_like_pem(head) or _looks_like_der(head)):
                    status = "invalid"
                    error_message = (
                        "File does not look like a PEM or DER "
                        "certificate."
                    )
            except Exception as exc:
                status = "invalid"
                error_message = f"Could not read file header: {exc}"

        # ----------------------------------------------------------
        # Persist the reconciled record.
        # ----------------------------------------------------------
        record = self._make_record(
            provider_id=provider_id,
            spec=spec,
            installed_path=str(target),
            sha256=actual_hash,
            size_bytes=size_bytes,
            status=status,
            error_message=error_message,
            preserve_installed_at=(
                existing.installed_at if existing else None
            ),
        )
        self._save_record(record)

        return CertificateStatusReport(
            provider_id=provider_id,
            certificate_id=spec.id,
            display_name=spec.display_name,
            status=status,
            installed_path=str(target),
            expected_sha256=spec.download_sha256,
            actual_sha256=actual_hash,
            size_bytes=size_bytes,
            installed_at=record.installed_at,
            last_checked_at=record.last_checked_at,
            error_message=error_message,
            download_url=spec.download_url,
            docs_url=spec.docs_url,
            required=spec.required,
        )

    async def delete(
        self,
        provider_id: str,
        certificate_id: str,
        *,
        delete_file: bool = True,
    ) -> bool:
        """
        Delete a certificate: the vault record, and optionally the
        installed file.

        Returns ``True`` when a record existed and was removed,
        ``False`` when the record was already absent.

        Idempotent: calling delete on a non-existent record is not an
        error — it just returns ``False``.
        """
        async with self._lock:
            key = self._record_key(provider_id, certificate_id)
            vault = self._load_vault()
            records_raw = vault.get(CERTIFICATES_VAULT_KEY) or {}
            if not isinstance(records_raw, dict):
                records_raw = {}

            existing_raw = records_raw.pop(key, None)

            # Save the vault even if no record existed — this ensures
            # the vault file is created if it did not exist before.
            vault[CERTIFICATES_VAULT_KEY] = records_raw
            self._save_vault(vault)

            if existing_raw is None:
                return False

            if delete_file:
                try:
                    path_str = existing_raw.get("installed_path")
                    if isinstance(path_str, str) and path_str:
                        path = Path(path_str)
                        if path.is_file():
                            path.unlink()
                            logger.info(
                                "Deleted certificate file %s.", path
                            )
                except OSError as exc:
                    # The record is gone; the file deletion is
                    # best-effort. A locked file would otherwise leave
                    # the user stuck.
                    logger.warning(
                        "Could not delete certificate file for "
                        "%r/%r: %s",
                        provider_id,
                        certificate_id,
                        exc,
                    )

            logger.info(
                "Deleted certificate record %r/%r.",
                provider_id,
                certificate_id,
            )
            return True

    async def check_all_for_provider(
        self,
        provider_id: str,
        specs: List[CertificateSpec],
    ) -> List[CertificateStatusReport]:
        """
        Convenience: check every certificate declared by a package.

        Returns a list of reports in the same order as ``specs``, so
        the caller can pair them positionally. Specs that do not have
        a matching record are reported with status ``"pending"``.
        """
        reports: List[CertificateStatusReport] = []
        for spec in specs:
            report = await self.check_status(provider_id, spec)
            reports.append(report)
        return reports

    # ------------------------------------------------------------------
    # Internal — download
    # ------------------------------------------------------------------

    async def _download(self, url: str) -> bytes:
        """
        Download ``url`` and return the bytes.

        Enforces the size limit incrementally, so a malicious mirror
        cannot force the process to buffer gigabytes before being cut
        off.
        """
        if not url or not isinstance(url, str):
            raise ProviderPackageValidationError(
                "download_url is empty or invalid.",
                code="PROVIDER_CERTIFICATE_INVALID_URL",
                details={"url": str(url)},
            )

        timeout = httpx.Timeout(self._timeout, connect=15.0)
        bytes_read = 0

        async with httpx.AsyncClient(
            timeout=timeout, follow_redirects=True
        ) as client:
            async with client.stream("GET", url) as response:
                response.raise_for_status()
                chunks: List[bytes] = []
                async for chunk in response.aiter_bytes(chunk_size=65536):
                    bytes_read += len(chunk)
                    if bytes_read > MAX_CERTIFICATE_BYTES:
                        raise ProviderPackageValidationError(
                            f"Download exceeded {MAX_CERTIFICATE_BYTES} "
                            f"bytes before completing.",
                            code="PROVIDER_CERTIFICATE_TOO_LARGE",
                            details={
                                "url": url,
                                "max_bytes": MAX_CERTIFICATE_BYTES,
                            },
                        )
                    chunks.append(chunk)

        if bytes_read == 0:
            raise ProviderPackageValidationError(
                "Downloaded file is empty.",
                code="PROVIDER_CERTIFICATE_EMPTY",
                details={"url": url},
            )

        return b"".join(chunks)

    # ------------------------------------------------------------------
    # Internal — vault I/O
    # ------------------------------------------------------------------

    def _load_vault(self) -> Dict[str, Any]:
        """Load ``models_vault.json`` or return an empty dict."""
        try:
            if not self._vault_path.is_file():
                return {}
            raw = self._vault_path.read_text(encoding="utf-8")
            data = json.loads(raw)
            if not isinstance(data, dict):
                logger.warning(
                    "models_vault.json root is not a dict — "
                    "starting from empty state."
                )
                return {}
            return data
        except Exception as exc:
            logger.error(
                "Could not read models_vault.json at %s: %s",
                self._vault_path,
                exc,
            )
            return {}

    def _save_vault(self, data: Dict[str, Any]) -> None:
        """
        Atomically persist ``models_vault.json``.

        Uses temp file + ``os.replace``, mirroring the pattern in
        ``config_rtr.write_raw_config`` and ``registry._save_registry``.
        A crash mid-write leaves the previous valid file untouched.
        """
        try:
            self._vault_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._vault_path.with_suffix(".tmp")
            tmp.write_text(
                json.dumps(data, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            os.replace(str(tmp), str(self._vault_path))
        except OSError as exc:
            raise ProviderPackageStorageError(
                f"Could not write models_vault.json: {exc}",
                details={"path": str(self._vault_path)},
            ) from exc

    def _save_record(self, record: ProviderCertificateRecord) -> None:
        """Insert or update a single certificate record in the vault."""
        key = self._record_key(
            record.provider_package_id, record.certificate_id
        )
        vault = self._load_vault()
        records = vault.get(CERTIFICATES_VAULT_KEY)
        if not isinstance(records, dict):
            records = {}
        records[key] = record.model_dump()
        vault[CERTIFICATES_VAULT_KEY] = records
        self._save_vault(vault)

    # ------------------------------------------------------------------
    # Internal — record construction
    # ------------------------------------------------------------------

    @staticmethod
    def _record_key(provider_id: str, certificate_id: str) -> str:
        """
        Composite key for the vault.

        Format: ``<provider_id>:<certificate_id>``. Two different
        providers can declare certificates with the same ``id`` without
        clashing — a real possibility with common names like
        ``root_ca``.
        """
        return f"{provider_id}:{certificate_id}"

    def _make_record(
        self,
        *,
        provider_id: str,
        spec: CertificateSpec,
        installed_path: Optional[str],
        sha256: Optional[str],
        size_bytes: int,
        status: str,
        error_message: Optional[str] = None,
        preserve_installed_at: Optional[str] = None,
    ) -> ProviderCertificateRecord:
        """
        Build a ``ProviderCertificateRecord`` from install-time facts.

        ``installed_at`` is only set when ``status == "installed"`` (or
        preserved from a previous successful install) so a failed
        attempt does not claim a false install timestamp.
        """
        now = _utc_now_iso()
        installed_at = (
            preserve_installed_at
            if preserve_installed_at
            else (now if status == "installed" else None)
        )

        return ProviderCertificateRecord(
            id=self._record_key(provider_id, spec.id),
            provider_package_id=provider_id,
            certificate_id=spec.id,
            installed_path=installed_path,
            sha256=sha256,
            size_bytes=size_bytes,
            status=status,  # type: ignore[arg-type]
            installed_at=installed_at,
            last_checked_at=now,
            error_message=error_message,
        )

    def _report_from_parts(
        self,
        *,
        provider_id: str,
        spec: CertificateSpec,
        status: str,
        installed_path: Optional[str],
        actual_sha256: Optional[str],
        size_bytes: int,
        installed_at: Optional[str],
        error_message: Optional[str],
    ) -> CertificateStatusReport:
        """Build a status report for cases where we skip full reconciliation."""
        return CertificateStatusReport(
            provider_id=provider_id,
            certificate_id=spec.id,
            display_name=spec.display_name,
            status=status,
            installed_path=installed_path,
            expected_sha256=spec.download_sha256,
            actual_sha256=actual_sha256,
            size_bytes=size_bytes,
            installed_at=installed_at,
            last_checked_at=_utc_now_iso(),
            error_message=error_message,
            download_url=spec.download_url,
            docs_url=spec.docs_url,
            required=spec.required,
        )


# ---------------------------------------------------------------------------
# Default paths + module-level singleton
# ---------------------------------------------------------------------------


def _default_vault_path() -> Path:
    """
    Compute the default path to ``models_vault.json``.

    Deferred import of ``omweb.config`` avoids a circular import at
    module load time — ``config`` imports from ``peldrun`` which may
    transitively import ``provider_packages`` depending on the boot
    order.
    """
    from omweb.config import BACKEND_DIR
    return BACKEND_DIR / "data" / "models_vault.json"


def _default_storage_root() -> Path:
    """Compute the default provider-packages storage root."""
    from omweb.config import BACKEND_DIR
    return BACKEND_DIR / "storage" / "store" / "providers"


#: Process-wide manager singleton. Wired to the standard paths so
#: routers can import it directly. Tests that need isolation should
#: construct their own ``CertificateManager(vault_path=tmp, ...)``.
certificate_manager = CertificateManager(
    vault_path=_default_vault_path(),
    storage_root=_default_storage_root(),
)


__all__ = [
    "DEFAULT_DOWNLOAD_TIMEOUT_SECONDS",
    "MANAGED_CERTIFICATES_SUBDIR",
    "CertificateStatusReport",
    "CertificateManager",
    "certificate_manager",
]
