"""
PELDRUN Core Directed Execution Graph Engine.
Provides asynchronous node execution, conditional edge routing, and state machine compilation.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from typing import Any, Awaitable, Callable, Dict, List, Optional, Set, Tuple, Union

from peldrun.engine.state import ExecutionState, ExecutionStatus
from peldrun.events.emitter import EventEmitter

logger = logging.getLogger("peldrun.engine.graph")

# Graph termination and origin constants
START = "__start__"
END = "__end__"

# Type aliases for nodes and routing logic
NodeAction = Callable[[ExecutionState], Union[Optional[Dict[str, Any]], Awaitable[Optional[Dict[str, Any]]]]]
ConditionRouter = Callable[[ExecutionState], Union[str, Awaitable[str]]]


class GraphNode:
    """Represents an atomic execution unit within the state graph."""

    def __init__(self, name: str, action: NodeAction) -> None:
        self.name = name
        self.action = action

    async def execute(self, state: ExecutionState) -> Optional[Dict[str, Any]]:
        """Execute node logic safely supporting both sync and async actions."""
        try:
            if inspect.iscoroutinefunction(self.action):
                result = await self.action(state)
            else:
                result = self.action(state)
            return result
        except Exception as ex:
            logger.exception("Exception occurred during execution of node '%s': %s", self.name, ex)
            raise


class CompiledGraph:
    """Compiled, ready-to-run state machine orchestrating nodes and conditional edges."""

    def __init__(
        self,
        nodes: Dict[str, GraphNode],
        edges: Dict[str, str],
        conditional_edges: Dict[str, Tuple[ConditionRouter, Dict[str, str]]],
        entry_point: str,
        finish_points: Set[str],
    ) -> None:
        self.nodes = nodes
        self.edges = edges
        self.conditional_edges = conditional_edges
        self.entry_point = entry_point
        self.finish_points = finish_points

    async def step(self, current_node: str, state: ExecutionState) -> Tuple[Optional[str], ExecutionState]:
        """
        Execute an individual node and resolve the successor node.
        Returns a tuple of (next_node_name, updated_state).
        """
        if current_node not in self.nodes:
            raise ValueError(f"Node '{current_node}' is not registered in the compiled graph.")

        node = self.nodes[current_node]
        update_dict = await node.execute(state)

        # Apply state updates if dictionary returned
        if isinstance(update_dict, dict):
            for key, val in update_dict.items():
                if hasattr(state, key):
                    setattr(state, key, val)

        # Check explicit finish points
        if current_node in self.finish_points:
            return None, state

        # Check conditional edges first
        if current_node in self.conditional_edges:
            router, path_map = self.conditional_edges[current_node]
            if inspect.iscoroutinefunction(router):
                route_key = await router(state)
            else:
                route_key = router(state)

            next_node = path_map.get(route_key, route_key)
            if next_node == END:
                return None, state
            return next_node, state

        # Check standard fixed edges
        if current_node in self.edges:
            next_node = self.edges[current_node]
            if next_node == END:
                return None, state
            return next_node, state

        # Dead end reached
        return None, state

    async def run(
        self,
        state: ExecutionState,
        emitter: Optional[EventEmitter] = None,
        max_iterations: int = 100,
    ) -> ExecutionState:
        """
        Execute the full graph traversal until reaching END, a finish point, or hitting max_iterations.
        """
        current_node: Optional[str] = self.entry_point
        iterations = 0

        state.status = ExecutionStatus.RUNNING

        while current_node is not None and iterations < max_iterations:
            iterations += 1
            logger.debug("Executing graph node: %s (iteration %d)", current_node, iterations)

            try:
                next_node, state = await self.step(current_node, state)
                current_node = next_node
            except Exception as ex:
                state.status = ExecutionStatus.FAILED
                if emitter:
                    await emitter.emit_error(
                        message=f"Graph execution failed at node '{current_node}': {str(ex)}",
                        error_type=type(ex).__name__,
                        step=state.current_step,
                    )
                raise

        if iterations >= max_iterations and current_node is not None:
            logger.warning("Graph execution exceeded max iterations (%d). Halting.", max_iterations)
            state.status = ExecutionStatus.FAILED
            if emitter:
                await emitter.emit_error(
                    message=f"Graph execution exceeded maximum iteration ceiling ({max_iterations}).",
                    error_type="IterationLimitExceeded",
                    step=state.current_step,
                )
        elif state.status == ExecutionStatus.RUNNING:
            state.status = ExecutionStatus.COMPLETED

        return state


class ExecutionGraph:
    """Builder class for defining nodes, edges, and conditional transitions."""

    def __init__(self) -> None:
        self.nodes: Dict[str, GraphNode] = {}
        self.edges: Dict[str, str] = {}
        self.conditional_edges: Dict[str, Tuple[ConditionRouter, Dict[str, str]]] = {}
        self.entry_point: Optional[str] = None
        self.finish_points: Set[str] = set()

    def add_node(self, name: str, action: NodeAction) -> ExecutionGraph:
        """Register a processing node within the execution graph."""
        if name in self.nodes:
            raise ValueError(f"Node '{name}' already exists in graph.")
        self.nodes[name] = GraphNode(name=name, action=action)
        return self

    def set_entry_point(self, name: str) -> ExecutionGraph:
        """Declare the initial node where execution begins."""
        self.entry_point = name
        return self

    def add_edge(self, source: str, target: str) -> ExecutionGraph:
        """Define a fixed transition edge between two nodes."""
        self.edges[source] = target
        return self

    def add_conditional_edges(
        self,
        source: str,
        router: ConditionRouter,
        path_map: Optional[Dict[str, str]] = None,
    ) -> ExecutionGraph:
        """
        Define a conditional routing edge evaluated dynamically at runtime.
        path_map maps router output strings to concrete target node names.
        """
        self.conditional_edges[source] = (router, path_map or {})
        return self

    def add_finish_point(self, name: str) -> ExecutionGraph:
        """Mark a node as a terminal execution state."""
        self.finish_points.add(name)
        return self

    def compile(self) -> CompiledGraph:
        """Validate structure and generate an immutable CompiledGraph instance."""
        if not self.entry_point:
            raise ValueError("Cannot compile graph: Entry point has not been defined.")

        if self.entry_point not in self.nodes:
            raise ValueError(f"Entry point '{self.entry_point}' does not exist among registered nodes.")

        # Validate that referenced targets exist or are valid constants
        for src, target in self.edges.items():
            if src not in self.nodes:
                raise ValueError(f"Edge source '{src}' does not exist.")
            if target != END and target not in self.nodes:
                raise ValueError(f"Edge target '{target}' does not exist.")

        for src, (router, path_map) in self.conditional_edges.items():
            if src not in self.nodes:
                raise ValueError(f"Conditional edge source '{src}' does not exist.")
            for outcome, target in path_map.items():
                if target != END and target not in self.nodes:
                    raise ValueError(f"Conditional outcome target '{target}' for node '{src}' does not exist.")

        return CompiledGraph(
            nodes=dict(self.nodes),
            edges=dict(self.edges),
            conditional_edges=dict(self.conditional_edges),
            entry_point=self.entry_point,
            finish_points=set(self.finish_points),
        )