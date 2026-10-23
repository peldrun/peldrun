# backend/omweb/resource_monitor/collectors/base.py
"""Abstract base collector specification for PELDRUN Resource Monitor.

Defines the contract and safety wrappers for telemetry data collectors,
ensuring failure-isolated, non-blocking hardware and process metric gathering.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from ..capabilities import ResourceMonitorCapabilities, probe_capabilities

logger = logging.getLogger(__name__)


class BaseCollector(ABC):
    """Abstract base class for all resource metric collectors."""

    def __init__(self, capabilities: Optional[ResourceMonitorCapabilities] = None) -> None:
        self._capabilities = capabilities or probe_capabilities()
        self._last_collected_at: Optional[datetime] = None
        self._is_enabled: bool = True

    @property
    @abstractmethod
    def collector_name(self) -> str:
        """Unique identifier name for this collector instance."""
        ...

    @abstractmethod
    def is_available(self) -> bool:
        """Determine whether prerequisites for this collector are met in the runtime."""
        ...

    @abstractmethod
    def _collect_internal(self) -> Dict[str, Any]:
        """Internal collector-specific collection logic to be implemented by subclasses."""
        ...

    def collect(self) -> Optional[Dict[str, Any]]:
        """Collect metrics with full exception isolation.

        Returns:
            Dictionary containing gathered metrics, or None if collector is unavailable
            or if an unrecoverable collection error occurred.
        """
        if not self._is_enabled or not self.is_available():
            return None

        try:
            metrics = self._collect_internal()
            self._last_collected_at = datetime.now(timezone.utc)
            return metrics
        except PermissionError as exc:
            logger.warning(
                "Permission denied in collector '%s': %s",
                self.collector_name,
                exc,
            )
            return None
        except Exception as exc:
            logger.error(
                "Unexpected failure in collector '%s': %s",
                self.collector_name,
                exc,
                exc_info=True,
            )
            return None

    @property
    def last_collected_at(self) -> Optional[datetime]:
        """Timestamp of the most recent successful collection."""
        return self._last_collected_at

    @property
    def is_enabled(self) -> bool:
        """Current operational enablement status."""
        return self._is_enabled

    def enable(self) -> None:
        """Enable collector execution."""
        self._is_enabled = True

    def disable(self) -> None:
        """Disable collector execution."""
        self._is_enabled = False
