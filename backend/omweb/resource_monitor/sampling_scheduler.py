# backend/omweb/resource_monitor/sampling_scheduler.py
"""Adaptive asynchronous sampling scheduler for PELDRUN Resource Monitor.

Manages background periodic telemetry loops, dynamically switches sampling rates
between idle and active execution runs, throttles sampling frequency under heavy CPU load,
and coordinates in-memory sample buffering with batch database persistence.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, List, Optional

from .config import ResourceMonitorConfig, get_default_config
from .models import ResourceSampleModel
from .storage.repositories import MonitorRepository

logger = logging.getLogger(__name__)


class SamplingScheduler:
    """Asynchronous scheduler running adaptive resource telemetry collection."""

    def __init__(
        self,
        service: Any,
        config: Optional[ResourceMonitorConfig] = None,
        repository: Optional[MonitorRepository] = None,
    ) -> None:
        self._service = service
        self._config = config or get_default_config()
        self._repo = repository or getattr(service, "_repo", None)

        # In-memory buffer for high-frequency samples
        self._sample_buffer: List[ResourceSampleModel] = []
        self._last_flush_mono = time.monotonic()

        # Task and synchronization primitives
        self._loop_task: Optional[asyncio.Task[None]] = None
        self._diagnostics_task: Optional[asyncio.Task[None]] = None
        self._stop_event = asyncio.Event()
        self._is_running = False

    @property
    def is_running(self) -> bool:
        """Check whether the sampling scheduler background loop is active."""
        return self._is_running

    async def start(self) -> None:
        """Launch the background telemetry sampling task loop."""
        if self._is_running:
            return

        self._stop_event.clear()
        self._is_running = True
        self._last_flush_mono = time.monotonic()
        self._loop_task = asyncio.create_task(self._run_loop(), name="resource_monitor_sampling_loop")
        self._diagnostics_task = asyncio.create_task(self._run_diagnostics_loop(), name="resource_monitor_diagnostics_loop")
        logger.info("Adaptive sampling scheduler started.")

    async def stop(self) -> None:
        """Signal background loop termination and drain buffered samples to disk."""
        if not self._is_running:
            return

        logger.info("Stopping adaptive sampling scheduler...")
        self._stop_event.set()
        self._is_running = False

        if self._loop_task:
            try:
                await asyncio.wait_for(self._loop_task, timeout=2.5)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._loop_task.cancel()
            finally:
                self._loop_task = None

        if self._diagnostics_task:
            try:
                await asyncio.wait_for(self._diagnostics_task, timeout=2.5)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._diagnostics_task.cancel()
            finally:
                self._diagnostics_task = None

        # Final drain of in-memory buffered samples
        await self.async_flush_buffer()
        logger.info("Adaptive sampling scheduler stopped cleanly.")

    async def _run_loop(self) -> None:
        """Core execution loop with adaptive sleep periods and fault isolation."""
        while not self._stop_event.is_set():
            try:
                # 1. Sample telemetry through the service facade without blocking the event loop
                sample, _ = await asyncio.to_thread(self._service.sample_now)
                self._sample_buffer.append(sample)

                # 2. Check if batch flush conditions are met
                now_mono = time.monotonic()
                buffer_size = len(self._sample_buffer)
                flush_due_by_time = (now_mono - self._last_flush_mono) >= self._config.batch_flush_interval_sec
                flush_due_by_size = buffer_size >= self._config.batch_flush_max_samples

                if flush_due_by_time or flush_due_by_size:
                    await self.async_flush_buffer()

                # 3. Calculate next adaptive sleep duration
                interval = self._calculate_adaptive_interval(sample)

                # 4. Wait for the interval or early stop signal
                try:
                    await asyncio.wait_for(self._stop_event.wait(), timeout=interval)
                except asyncio.TimeoutError:
                    pass

            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error("Error in sampling scheduler loop: %s", exc, exc_info=True)
                # Brief safety backoff on unexpected error
                await asyncio.sleep(2.0)

    async def _run_diagnostics_loop(self) -> None:
        """Independent loop for orphan detection and diagnostic heuristics."""
        while not self._stop_event.is_set():
            try:
                diagnostics = getattr(self._service, "_diagnostics", None)
                if diagnostics is not None:
                    # Collect raw stateless telemetry to avoid interfering with sampling
                    sample, _ = self._service._collect_raw_telemetry()

                    # Only evaluate memory creep when idle
                    idle_rss = sample.peldrun_rss_bytes if sample.active_runs_count == 0 else None

                    await diagnostics.async_evaluate_all(
                        host_cpu_percent=sample.host_cpu_percent,
                        idle_peldrun_rss_bytes=idle_rss
                    )
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error("Error in diagnostics loop: %s", exc, exc_info=True)

            # Run diagnostics every 15 seconds
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=15.0)
            except asyncio.TimeoutError:
                pass

    def _calculate_adaptive_interval(self, sample: ResourceSampleModel) -> float:
        """Determine next interval based on CPU pressure and active runs count."""
        # 1. If host CPU breached saturation threshold, relax sampling to protect stability
        if sample.host_cpu_percent >= self._config.cpu_throttle_threshold_percent:
            return self._config.sampling_throttled_interval_sec

        # 2. If runs are actively executing, use high-resolution interval
        if sample.active_runs_count > 0:
            return self._config.sampling_active_interval_sec

        # 3. Otherwise, use relaxed idle interval
        return self._config.sampling_idle_interval_sec

    def flush_buffer(self) -> int:
        """Synchronously persist buffered samples to SQLite repository.

        Returns:
            The number of flushed sample records.
        """
        if not self._sample_buffer:
            self._last_flush_mono = time.monotonic()
            return 0

        # Swap buffer reference atomically
        samples_to_flush = self._sample_buffer
        self._sample_buffer = []
        self._last_flush_mono = time.monotonic()

        repo = self._repo or getattr(self._service, "_repo", None)
        if not repo:
            logger.warning("No repository available to flush %d resource samples.", len(samples_to_flush))
            return 0

        try:
            persisted = repo.insert_samples_batch(samples_to_flush)
            logger.debug("Flushed %d resource samples to database.", persisted)
            return persisted
        except Exception as exc:
            logger.error("Failed flushing %d resource samples: %s", len(samples_to_flush), exc, exc_info=True)
            # Re-queue on failure to avoid data loss
            self._sample_buffer = samples_to_flush + self._sample_buffer
            return 0

    async def async_flush_buffer(self) -> int:
        """Asynchronously flush sample buffer without blocking the asyncio loop."""
        return await asyncio.to_thread(self.flush_buffer)
