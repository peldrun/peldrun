"""
PELDRUN Core Flow Base Architecture.
Defines abstract execution flow interfaces coordinating multi-step planning,
agent delegation, and event distribution.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import Any, Dict, Optional
from uuid import UUID, uuid4

from peldrun.events.emitter import EventEmitter
from peldrun.memory import MemoryManager

logger = logging.getLogger("peldrun.flows.base")


class FlowResult:
    """Outcome returned upon completion of a structured flow execution."""

    def __init__(
        self,
        success: bool,
        output: str = "",
        error: str = "",
        metadata: Optional[Dict[str, Any]] = None,
    ):
        self.success = success
        self.output = output
        self.error = error
        self.metadata = metadata or {}


class BaseFlow(ABC):
    """Abstract base class for high-level agent execution flows."""

    def __init__(
        self,
        flow_id: Optional[UUID] = None,
        emitter: Optional[EventEmitter] = None,
        memory: Optional[MemoryManager] = None,
    ) -> None:
        self.flow_id = flow_id or uuid4()
        self.emitter = emitter or EventEmitter(run_id=self.flow_id)
        self.memory = memory

    @abstractmethod
    async def aexecute(self, task: str, **kwargs: Any) -> FlowResult:
        """Execute the flow asynchronously and return the final FlowResult."""
        ...