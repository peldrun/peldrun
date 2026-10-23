# backend/omweb/resource_monitor/collectors/network.py
"""Network throughput and packet rate collector for PELDRUN Resource Monitor.

Measures host-level network utilization, including bytes sent/received per second,
packet transfer rates, socket error frequencies, and dropped packet metrics.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, Optional

from ..capabilities import ResourceMonitorCapabilities, probe_capabilities
from .base import BaseCollector

logger = logging.getLogger(__name__)

# Graceful optional import of psutil
try:
    import psutil
    _PSUTIL_AVAILABLE = True
except ImportError:
    psutil = None  # type: ignore
    _PSUTIL_AVAILABLE = False


class NetworkCollector(BaseCollector):
    """Collector responsible for measuring host network bandwidth and packet counters."""

    def __init__(self, capabilities: Optional[ResourceMonitorCapabilities] = None) -> None:
        super().__init__(capabilities=capabilities)
        self._last_net_counters: Optional[Any] = None
        self._last_net_timestamp: Optional[float] = None

        # Seed baseline network counters if available
        if _PSUTIL_AVAILABLE and psutil is not None and self._capabilities.can_measure_net_counters:
            try:
                self._last_net_counters = psutil.net_io_counters()
                self._last_net_timestamp = time.monotonic()
            except Exception as exc:
                logger.debug("Failed to initialize baseline network counters: %s", exc)

    @property
    def collector_name(self) -> str:
        """Unique identifier name for this collector instance."""
        return "network"

    def is_available(self) -> bool:
        """Verify network telemetry availability."""
        return _PSUTIL_AVAILABLE and psutil is not None and self._capabilities.can_measure_net_counters

    def _collect_internal(self) -> Dict[str, Any]:
        """Collect host network throughput and error rates.

        Returns:
            Dictionary containing instantaneous throughput rates and cumulative counters.
        """
        assert psutil is not None

        rates = {
            "bytes_sent_per_sec": 0.0,
            "bytes_recv_per_sec": 0.0,
            "packets_sent_per_sec": 0.0,
            "packets_recv_per_sec": 0.0,
            "errin_count_per_sec": 0.0,
            "errout_count_per_sec": 0.0,
            "dropin_count_per_sec": 0.0,
            "dropout_count_per_sec": 0.0,
            "total_bytes_sent": 0,
            "total_bytes_recv": 0,
        }

        try:
            current_counters = psutil.net_io_counters()
            if current_counters is None:
                return rates

            now = time.monotonic()
            rates["total_bytes_sent"] = int(current_counters.bytes_sent)
            rates["total_bytes_recv"] = int(current_counters.bytes_recv)

            if self._last_net_counters is not None and self._last_net_timestamp is not None:
                delta_t = now - self._last_net_timestamp
                if delta_t > 0.05:
                    delta_sent = current_counters.bytes_sent - self._last_net_counters.bytes_sent
                    delta_recv = current_counters.bytes_recv - self._last_net_counters.bytes_recv
                    delta_pkt_sent = current_counters.packets_sent - self._last_net_counters.packets_sent
                    delta_pkt_recv = current_counters.packets_recv - self._last_net_counters.packets_recv
                    delta_errin = current_counters.errin - self._last_net_counters.errin
                    delta_errout = current_counters.errout - self._last_net_counters.errout
                    delta_dropin = current_counters.dropin - self._last_net_counters.dropin
                    delta_dropout = current_counters.dropout - self._last_net_counters.dropout

                    # Ensure rates are non-negative in event of interface reboots/counter overflows
                    rates["bytes_sent_per_sec"] = max(0.0, float(delta_sent / delta_t))
                    rates["bytes_recv_per_sec"] = max(0.0, float(delta_recv / delta_t))
                    rates["packets_sent_per_sec"] = max(0.0, float(delta_pkt_sent / delta_t))
                    rates["packets_recv_per_sec"] = max(0.0, float(delta_pkt_recv / delta_t))
                    rates["errin_count_per_sec"] = max(0.0, float(delta_errin / delta_t))
                    rates["errout_count_per_sec"] = max(0.0, float(delta_errout / delta_t))
                    rates["dropin_count_per_sec"] = max(0.0, float(delta_dropin / delta_t))
                    rates["dropout_count_per_sec"] = max(0.0, float(delta_dropout / delta_t))

            self._last_net_counters = current_counters
            self._last_net_timestamp = now
        except (AttributeError, NotImplementedError, OSError, PermissionError) as exc:
            logger.debug("Failed to read network I/O counters: %s", exc)

        return rates
