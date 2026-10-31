# Path: backend/omweb/services/temp_artifacts_cleaner.py
"""
Periodic temp-artifact cleanup service.

Runs as an asyncio background task inside the FastAPI process, sweeping
``storage/artifacts/temp/`` on a configurable interval (default: every
6 hours). Files older than the configured max-age (default: 24 hours)
are deleted. The sweep also runs once at startup so a long-suspended
server that restarts does not carry yesterday's leftovers into today.

Design notes
------------
* The task is cancelled gracefully on shutdown — no dangling coroutines.
* Every sweep is wrapped in try/except; a failure in one iteration
  logs a warning but never terminates the loop.
* The cleanup logic itself lives in ``omweb.routers.files`` (imported
  lazily) so there is exactly one implementation of the deletion rules.
* Configuration comes from environment variables so operators can tune
  behaviour without touching code:

    PELDRUN_TEMP_CLEANUP_ENABLED         = "1" | "0"  (default "1")
    PELDRUN_TEMP_CLEANUP_INTERVAL_HOURS  = float      (default "6")
    PELDRUN_TEMP_CLEANUP_MAX_AGE_HOURS   = float      (default "24")

Wiring
------
In ``omweb/main.py``, inside the lifespan context::

    from omweb.services.temp_artifacts_cleaner import temp_artifacts_cleaner

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await temp_artifacts_cleaner.start()
        yield
        await temp_artifacts_cleaner.stop()

The cleaner is a module-level singleton so multiple imports share one
task. Starting it twice is a no-op.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Dict, Optional

logger = logging.getLogger("omweb.services.temp_artifacts_cleaner")


class TempArtifactsCleaner:
    """Singleton background cleaner for the temp artifact store."""

    def __init__(self) -> None:
        self._task: Optional[asyncio.Task] = None
        self._started = False

    # ------------------------------------------------------------------
    # Config
    # ------------------------------------------------------------------

    @staticmethod
    def _is_enabled() -> bool:
        raw = os.environ.get("PELDRUN_TEMP_CLEANUP_ENABLED", "1").strip().lower()
        return raw not in ("0", "false", "no", "off", "disabled")

    @staticmethod
    def _interval_seconds() -> float:
        try:
            hours = float(
                os.environ.get("PELDRUN_TEMP_CLEANUP_INTERVAL_HOURS", "6")
            )
        except (TypeError, ValueError):
            hours = 6.0
        # Clamp to a sane range: 1 minute → 7 days.
        hours = max(1.0 / 60.0, min(7.0 * 24.0, hours))
        return hours * 3600.0

    @staticmethod
    def _max_age_hours() -> float:
        try:
            hours = float(
                os.environ.get("PELDRUN_TEMP_CLEANUP_MAX_AGE_HOURS", "24")
            )
        except (TypeError, ValueError):
            hours = 24.0
        # Clamp: 6 minutes → 30 days.
        return max(0.1, min(30.0 * 24.0, hours))

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Start the background cleanup loop (idempotent)."""
        if self._started:
            logger.debug("TempArtifactsCleaner already started; skipping.")
            return
        if not self._is_enabled():
            logger.info(
                "TempArtifactsCleaner disabled via "
                "PELDRUN_TEMP_CLEANUP_ENABLED=0"
            )
            self._started = True
            return

        self._started = True
        self._task = asyncio.create_task(self._loop(), name="temp-artifacts-cleaner")
        logger.info(
            "TempArtifactsCleaner started (interval=%.2fh, max_age=%.2fh)",
            self._interval_seconds() / 3600.0,
            self._max_age_hours(),
        )

    async def stop(self) -> None:
        """Cancel the background loop and await graceful shutdown."""
        self._started = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                logger.debug("TempArtifactsCleaner stop notice: %s", exc)
            self._task = None
            logger.info("TempArtifactsCleaner stopped.")

    # ------------------------------------------------------------------
    # Loop
    # ------------------------------------------------------------------

    async def _run_once(self) -> Dict[str, Any]:
        """Invoke the canonical cleanup routine once, guarded."""
        try:
            from omweb.routers.files import cleanup_temp_artifacts
        except Exception as import_exc:
            logger.warning(
                "TempArtifactsCleaner: could not import cleanup routine: %s",
                import_exc,
            )
            return {"error": str(import_exc)}

        max_age = self._max_age_hours()
        try:
            report = await asyncio.to_thread(
                cleanup_temp_artifacts, older_than_hours=max_age
            )
        except Exception as exc:
            logger.warning(
                "TempArtifactsCleaner sweep failed: %s", exc, exc_info=True
            )
            return {"error": str(exc)}

        logger.info(
            "TempArtifactsCleaner sweep: scanned=%s deleted=%s kept=%s "
            "reclaimed=%s bytes (threshold=%.2fh)",
            report.get("scanned"),
            report.get("deleted_count"),
            report.get("kept_count"),
            report.get("deleted_bytes"),
            max_age,
        )
        return report

    async def _loop(self) -> None:
        interval = self._interval_seconds()
        try:
            # Startup sweep: reclaim yesterday's leftovers immediately.
            await self._run_once()

            while True:
                try:
                    await asyncio.sleep(interval)
                except asyncio.CancelledError:
                    raise

                await self._run_once()
        except asyncio.CancelledError:
            return
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "TempArtifactsCleaner loop terminated unexpectedly: %s",
                exc,
                exc_info=True,
            )


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

temp_artifacts_cleaner = TempArtifactsCleaner()


__all__ = ["TempArtifactsCleaner", "temp_artifacts_cleaner"]
