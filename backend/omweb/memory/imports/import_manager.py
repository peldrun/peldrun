# backend/omweb/memory/imports/import_manager.py
"""
PELDRUN Memory Platform - Import Orchestrator.

The single public entry point for the memory import subsystem.
Wraps the adapter registry and the two-phase
``preview`` → ``apply`` flow in an API-friendly façade.

Responsibilities (deliberately NARROW)
--------------------------------------

``ImportManager`` does EXACTLY five things:

    1. Validate the caller's arguments (structural programmer
       errors -> ``TypeError``, user errors -> ``MemoryValidationError``).
    2. Enforce ``MemoryMode`` (with the import-specific exemption
       described below).
    3. Select the appropriate adapter (auto-detection or
       caller-forced ``adapter_id``).
    4. Delegate to the adapter's ``preview()`` / ``apply()``.
    5. Log timing + counts for diagnostics.

It does NOT:

    * parse any format,
    * construct any ``MemoryEntry``,
    * apply any write policy,
    * touch SQLite or the filesystem directly.

All of those live in the adapters (and their shared base class).
``ImportManager`` is a THIN orchestrator on purpose -- the
less logic it owns, the less room there is for behavior to
diverge from the adapters' own tests.

Mode enforcement -- the READ_ONLY exemption
-------------------------------------------

The plan's ``MemoryMode.READ_ONLY`` docstring is explicit:

    *Reads allowed, writes rejected EXCEPT IMPORT... Imports
    through the preview-to-apply flow are still allowed because
    the operator has explicitly confirmed them.*

So ``ImportManager`` treats modes as follows:

    **DISABLED**
        Rejected for both ``preview`` and ``apply``. There is no
        memory subsystem to preview against or write into. The
        error is ``MemoryValidationError`` (user-facing) not
        ``MemoryUnavailableError`` (backend-facing), because the
        fix is "enable memory in settings", not "retry later".

    **READ_ONLY**
        BOTH operations allowed. Preview needs to query the port
        for dedupe analysis (a READ); apply needs to write
        candidates (an IMPORT). The first is obviously allowed
        under READ_ONLY; the second is allowed by the plan's
        explicit exemption -- a user who previews an import and
        clicks "Confirm" has CONFIRMED the write, so it is
        equivalent to an ``explicit_user`` save. Treating it as
        a forbidden write would defeat the plan's own import
        subsystem in READ_ONLY deployments.

    **READ_WRITE**
        Both operations allowed with no special-casing.

The asymmetry with ``MemorySaveTool`` (which REJECTS in
READ_ONLY) is intentional: that tool's default ``source_type``
is ``agent_tool`` -- the user granted the agent permission to
save, but did not confirm THIS specific write. An import via
preview → apply is different: the user explicitly reviewed
what was about to be written and confirmed it. That is a
higher level of consent, matching the ``explicit_user``
threshold. Different tool, different mode policy.

Preview -> apply is a caller responsibility
-------------------------------------------

``ImportManager`` does NOT enforce "preview must be called
before apply". It offers the two operations independently and
lets the caller (the API layer) orchestrate them. Rationale:

    * **Statelessness.** Enforcing "preview was called" would
      require either per-request state (session tokens,
      server-side preview registry) or a signature contract
      (the client passes a preview hash). Both add real
      complexity for a constraint the API layer already
      enforces at the UX level -- the UI's "Confirm Import"
      button only appears after a preview has been displayed.
    * **Scriptability.** A CLI import (e.g.
      ``peldrun-memory-restore backup.zip --yes``) genuinely
      wants to skip the preview. Its user reviewed the
      backup file before invoking the command. Forcing preview
      would break the scripted path with no security benefit.
    * **Testability.** Tests that exercise ``apply`` directly
      (verify it writes correctly) would have to mock the
      preview state as well -- noise in every test.

Acceptance test #13 ("preview does not write until apply")
is verified by the adapter tests, not by ImportManager. Those
tests prove that ``preview`` never calls ``port.create`` --
which is the actual security invariant. ``ImportManager``
inherits that guarantee by delegation.

Where the "user must confirm" invariant lives
---------------------------------------------

The plan says imports require "an explicit preview → apply
confirmation". Two layers enforce it:

    1. **The API layer.** ``POST /import/preview`` and
       ``POST /import/apply`` are SEPARATE endpoints. A
       client that hits ``apply`` without ``preview`` sees
       the resulting ``ImportApplyResult`` with counts it
       never reviewed -- on them, not on the server.
    2. **The UI layer.** The "Confirm Import" button that
       fires ``apply`` is only rendered after a preview has
       been received. The user's actual flow ALWAYS
       previews-then-applies.

If a future requirement is to make ``apply`` server-side
enforce the preview, the correct design is to add a
``preview_id`` parameter (the preview endpoint returns one;
apply validates it against an in-memory registry with TTL).
That is a P2 extension; P1 ships without it.

Argument validation taxonomy
----------------------------

Every ``ImportManager`` method can fail in three distinct
ways, and the taxonomy matters because the API layer maps
each to a different HTTP status:

    **TypeError**
        A programmer error: wrong argument type. The caller
        passed ``None`` where a ``MemoryScopeRef`` was expected,
        or a string where a ``MemorySettings`` was expected.
        API mapping: 500 (server bug), because callers should
        have validated their inputs before calling this layer.

    **MemoryValidationError**
        A user error: valid type, invalid value. The user
        supplied a malformed package, a database with no
        recognizable schema, or attempted import while memory
        is DISABLED. API mapping: 400 (client error), with the
        exception's ``message`` surfaced verbatim to the user
        (``client_facing=True``).

    **MemoryUnavailableError**
        A backend error: memory subsystem down (disk full,
        SQLite corrupted, provider crashed). API mapping: 503
        (retry later), with a generic message to the user (the
        exception's ``client_facing=False`` -- its message is
        for logs).

The three-tier taxonomy is inherited from
``peldrun.memory.domain.errors`` and used uniformly by every
consumer of the subsystem. ``ImportManager`` does not invent
its own exception types -- it propagates the domain's.
"""

from __future__ import annotations

import logging
import time
from typing import Any, List, Optional

from peldrun.memory.domain import (
    MemoryMode,
    MemoryPort,
    MemoryScopeRef,
    MemorySettings,
    MemoryUnavailableError,
    MemoryValidationError,
)

from omweb.memory.imports.adapters import (
    get_adapter_by_id,
    select_adapter,
)
from omweb.memory.imports.base_adapter import (
    BaseImportAdapter,
    ImportApplyResult,
    ImportPreview,
)


logger = logging.getLogger("omweb.memory.imports.manager")


# ---------------------------------------------------------------------------
# ImportManager
# ---------------------------------------------------------------------------


class ImportManager:
    """Orchestrates the memory import pipeline.

    See the module docstring for the full responsibility split,
    the mode policy (including the READ_ONLY exemption for
    imports), and the preview/apply contract.

    Lifecycle
    ---------
    Construct one instance per application (typically held by
    the ``MemoryService`` facade in P1.7). The manager is
    STATELESS between calls -- it holds only the injected
    ``port`` and ``settings`` references, both of which are
    re-read from the caller for every operation.

    Adapters are instantiated FRESH for every call (via the
    registry's ``select_adapter``) so that settings or port
    changes between imports are honored automatically. See the
    ``adapters/__init__`` module docstring for the rationale on
    not caching instances.
    """

    def __init__(
        self,
        port: MemoryPort,
        settings: MemorySettings,
    ) -> None:
        """Configure the manager.

        Parameters
        ----------
        port
            The ``MemoryPort`` the selected adapter will query
            for dedupe and write to during apply. MUST already
            be open.
        settings
            The current ``MemorySettings`` snapshot. Read for
            ``mode`` (enforced here) and forwarded to the
            adapter (read there for ``default_limit``).

        Raises
        ------
        TypeError
            When ``port`` is None or ``settings`` is not a
            ``MemorySettings``. Programmer errors surface
            immediately, before any I/O.
        """
        if port is None:
            raise TypeError("port must be a MemoryPort instance, got None")
        if not isinstance(settings, MemorySettings):
            raise TypeError(
                f"settings must be MemorySettings, got "
                f"{type(settings).__name__}"
            )
        self._port: MemoryPort = port
        self._settings: MemorySettings = settings

    # ------------------------------------------------------------------
    # Preview
    # ------------------------------------------------------------------

    async def preview(
        self,
        source: Any,
        scope_ref: MemoryScopeRef,
        *,
        adapter_id: Optional[str] = None,
    ) -> ImportPreview:
        """Parse ``source`` and classify each candidate. NO WRITES.

        The preview is what the UI shows the user before they
        confirm: "we will create N entries, skip M duplicates,
        and here are the warnings." Nothing in the store changes
        -- this method guarantees the "no write on preview"
        invariant by delegating to adapter code that has no
        write paths in ``preview``.

        Parameters
        ----------
        source
            Format-specific source. See the registry module
            docstring for the four supported source shapes
            (``dict`` / ``Path`` file / ``Path`` directory /
            ``bytes``).
        scope_ref
            Target scope reference. Required; user scope is
            rejected by adapters that cannot legally write there.
        adapter_id
            Optional explicit adapter id. When provided, the
            registry is bypassed and the named adapter is
            instantiated directly. Useful for debugging or for
            sources whose shape genuinely confuses the
            heuristic. When ``None`` (the default),
            ``select_adapter`` picks the best match
            automatically.

        Returns
        -------
        ImportPreview
            The adapter's protocol-shape result. See
            ``base_adapter.ImportPreview`` for field semantics.

        Raises
        ------
        TypeError
            Programmer errors (invalid ``scope_ref`` type,
            invalid ``adapter_id`` type).
        MemoryValidationError
            User errors: memory is DISABLED, no adapter can
            handle the source, or the source is malformed.
        MemoryUnavailableError
            Backend failure during dedupe analysis (port down,
            SQLite corrupted, etc.).
        """
        self._validate_scope_ref_arg(scope_ref)
        self._validate_adapter_id_arg(adapter_id)
        self._enforce_mode_preview()

        adapter = self._select_adapter(source, adapter_id)
        start = time.monotonic()

        logger.info(
            "Import preview starting (adapter=%s, scope=%s).",
            adapter.adapter_id,
            scope_ref,
        )

        try:
            preview = await adapter.preview(source, scope_ref)
        except MemoryValidationError:
            # Already a user-facing error -- log the counts and
            # re-raise unchanged so the API layer can map it to
            # HTTP 400 with the exception's message.
            elapsed_ms = round(
                (time.monotonic() - start) * 1000.0, 2
            )
            logger.warning(
                "Import preview rejected (adapter=%s, scope=%s, "
                "elapsed=%.2fms).",
                adapter.adapter_id,
                scope_ref,
                elapsed_ms,
            )
            raise
        except Exception:
            elapsed_ms = round(
                (time.monotonic() - start) * 1000.0, 2
            )
            logger.exception(
                "Import preview failed unexpectedly (adapter=%s, "
                "scope=%s, elapsed=%.2fms).",
                adapter.adapter_id,
                scope_ref,
                elapsed_ms,
            )
            raise

        elapsed_ms = round((time.monotonic() - start) * 1000.0, 2)
        logger.info(
            "Import preview complete (adapter=%s, scope=%s, "
            "candidates=%d, would_create=%d, would_skip=%d, "
            "warnings=%d, elapsed=%.2fms).",
            adapter.adapter_id,
            scope_ref,
            len(preview.candidates),
            preview.to_create,
            preview.to_skip,
            len(preview.warnings),
            elapsed_ms,
        )

        return preview

    # ------------------------------------------------------------------
    # Apply
    # ------------------------------------------------------------------

    async def apply(
        self,
        source: Any,
        scope_ref: MemoryScopeRef,
        *,
        adapter_id: Optional[str] = None,
    ) -> ImportApplyResult:
        """Parse ``source`` and write every viable candidate.

        The apply step is what makes the import REAL: entries
        are created (or superseded / reactivated) in the target
        scope. Best-effort semantics -- a failure on one
        candidate does not abort the batch. See
        ``base_adapter._apply_candidates`` for the exact
        per-candidate behavior.

        Parameters
        ----------
        source, scope_ref, adapter_id
            Same semantics as ``preview``. Note that ``source``
            is RE-PARSED here (defensive against caller bugs);
            the parse work is O(N) and cheap compared to the
            write path.

        Returns
        -------
        ImportApplyResult
            The adapter's protocol-shape result. See
            ``base_adapter.ImportApplyResult`` for field
            semantics. Note that ``errors`` is populated
            per-candidate -- the caller decides whether
            ``errors`` being non-empty means "retry the whole
            batch" or "accept partial success and report".

        Raises
        ------
        TypeError
            Programmer errors.
        MemoryValidationError
            User errors: memory DISABLED, no adapter matched,
            source malformed, or user-scope policy violation.
        MemoryUnavailableError
            Backend failure during the write path. When this
            fires, the batch is PARTIALLY APPLIED -- the caller
            should inform the user rather than retrying
            blindly, because the retry would duplicate the
            entries that succeeded.
        """
        self._validate_scope_ref_arg(scope_ref)
        self._validate_adapter_id_arg(adapter_id)
        self._enforce_mode_apply()

        adapter = self._select_adapter(source, adapter_id)
        start = time.monotonic()

        logger.info(
            "Import apply starting (adapter=%s, scope=%s).",
            adapter.adapter_id,
            scope_ref,
        )

        try:
            result = await adapter.apply(source, scope_ref)
        except MemoryValidationError:
            elapsed_ms = round(
                (time.monotonic() - start) * 1000.0, 2
            )
            logger.warning(
                "Import apply rejected (adapter=%s, scope=%s, "
                "elapsed=%.2fms).",
                adapter.adapter_id,
                scope_ref,
                elapsed_ms,
            )
            raise
        except MemoryUnavailableError:
            elapsed_ms = round(
                (time.monotonic() - start) * 1000.0, 2
            )
            logger.error(
                "Import apply hit backend failure (adapter=%s, "
                "scope=%s, elapsed=%.2fms). Batch may be "
                "partially applied.",
                adapter.adapter_id,
                scope_ref,
                elapsed_ms,
            )
            raise
        except Exception:
            elapsed_ms = round(
                (time.monotonic() - start) * 1000.0, 2
            )
            logger.exception(
                "Import apply failed unexpectedly (adapter=%s, "
                "scope=%s, elapsed=%.2fms).",
                adapter.adapter_id,
                scope_ref,
                elapsed_ms,
            )
            raise

        elapsed_ms = round((time.monotonic() - start) * 1000.0, 2)
        logger.info(
            "Import apply complete (adapter=%s, scope=%s, "
            "created=%d, skipped=%d, errors=%d, elapsed=%.2fms).",
            adapter.adapter_id,
            scope_ref,
            result.created,
            result.skipped,
            len(result.errors),
            elapsed_ms,
        )

        return result

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------

    def is_preview_available(self) -> bool:
        """Return True when preview calls will be accepted.

        Returns False in ``MemoryMode.DISABLED`` -- the API layer
        uses this to gray out the "Import" button rather than
        letting the user click it and get an error. Same mode
        rule as ``preview()`` (see ``_enforce_mode_preview``).
        """
        return self._settings.mode != MemoryMode.DISABLED

    def is_apply_available(self) -> bool:
        """Return True when apply calls will be accepted.

        Same mode rule as ``apply()``: ``DISABLED`` rejects;
        ``READ_ONLY`` and ``READ_WRITE`` allow. See
        ``_enforce_mode_apply`` for the READ_ONLY exemption.
        """
        return self._settings.mode != MemoryMode.DISABLED

    # ------------------------------------------------------------------
    # Internal: argument validation
    # ------------------------------------------------------------------

    @staticmethod
    def _validate_scope_ref_arg(scope_ref: MemoryScopeRef) -> None:
        """Programmer-error check on the ``scope_ref`` argument.

        TypeErrors are reserved for "the caller's code is wrong",
        not "the caller's data is wrong". A wrong type here means
        the API layer forgot to build a ``MemoryScopeRef``; that
        is a bug on our side, not a user-facing 400.
        """
        if not isinstance(scope_ref, MemoryScopeRef):
            raise TypeError(
                f"scope_ref must be MemoryScopeRef, got "
                f"{type(scope_ref).__name__}"
            )

    @staticmethod
    def _validate_adapter_id_arg(adapter_id: Optional[str]) -> None:
        """Programmer-error check on the ``adapter_id`` argument."""
        if adapter_id is not None and not isinstance(adapter_id, str):
            raise TypeError(
                f"adapter_id must be str or None, got "
                f"{type(adapter_id).__name__}"
            )

    # ------------------------------------------------------------------
    # Internal: mode enforcement
    # ------------------------------------------------------------------

    def _enforce_mode_preview(self) -> None:
        """Raise ``MemoryValidationError`` when preview is disabled.

        Preview reads from the port (dedupe analysis) and does
        not write. It requires at least ``READ_ONLY`` mode --
        ``DISABLED`` provides neither.
        """
        if self._settings.mode == MemoryMode.DISABLED:
            raise MemoryValidationError(
                "Memory is disabled; cannot preview imports. "
                "Enable memory (set mode to 'read_only' or "
                "'read_write') in Settings before importing.",
                field_name="mode",
                context={"mode": self._settings.mode.value},
            )

    def _enforce_mode_apply(self) -> None:
        """Raise ``MemoryValidationError`` when apply is disabled.

        Only ``DISABLED`` blocks apply. ``READ_ONLY`` ALLOWS
        imports -- see the module docstring for the plan's
        explicit READ_ONLY exemption for ``MemorySourceType.IMPORT``.
        """
        if self._settings.mode == MemoryMode.DISABLED:
            raise MemoryValidationError(
                "Memory is disabled; cannot apply imports. "
                "Enable memory (set mode to 'read_only' or "
                "'read_write') in Settings before importing.",
                field_name="mode",
                context={"mode": self._settings.mode.value},
            )

    # ------------------------------------------------------------------
    # Internal: adapter selection
    # ------------------------------------------------------------------

    def _select_adapter(
        self,
        source: Any,
        adapter_id: Optional[str],
    ) -> BaseImportAdapter:
        """Return the appropriate adapter for this call.

        Two paths:

          * Explicit ``adapter_id`` -> ``get_adapter_by_id``. The
            registry is BYPASSED; the named adapter is used even
            if its ``can_handle`` would return False. Useful for
            debugging and for callers who know the source shape
            better than the heuristic.

          * No ``adapter_id`` -> ``select_adapter`` runs the
            registry's auto-detection. Raises
            ``MemoryValidationError`` when no adapter matches.
        """
        if adapter_id is not None:
            return get_adapter_by_id(
                adapter_id, self._port, self._settings
            )
        return select_adapter(source, self._port, self._settings)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


__all__ = ["ImportManager"]
