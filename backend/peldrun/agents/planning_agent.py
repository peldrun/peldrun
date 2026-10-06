"""
PELDRUN Core Planning Autonomous Agent Implementation.
Implements Plan-and-Solve architecture with initial goal decomposition, structured step progression,
dynamic replanning upon execution feedback, and deliverable synthesis.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, List, Literal, Optional
from pydantic import BaseModel, Field

from peldrun.agents.base import AgentConfig, BaseAgent
from peldrun.events.emitter import EventEmitter
from peldrun.llm.providers.openai_compat import BaseLLMProvider
from peldrun.memory import MemoryManager
from peldrun.sandbox.base import BaseSandbox
from peldrun.tools.registry import ToolRegistry

logger = logging.getLogger("peldrun.agents.planning_agent")

StepStatus = Literal["pending", "in_progress", "completed", "failed", "skipped"]


class PlanStep(BaseModel):
    """Structured unit of execution within an overarching plan."""
    id: int = Field(..., description="Sequential step index starting from 1")
    title: str = Field(..., description="Concise headline of the milestone or action")
    description: str = Field(default="", description="Detailed execution instruction for this step")
    status: StepStatus = Field(default="pending", description="Current step lifecycle status")
    result: Optional[str] = Field(default=None, description="Recorded outcome or deliverable produced")


class Plan(BaseModel):
    """Complete milestone hierarchy managing target progression."""
    goal: str = Field(..., description="Primary objective driving the plan")
    steps: List[PlanStep] = Field(default_factory=list, description="Ordered execution milestones")

    def get_current_step(self) -> Optional[PlanStep]:
        """Retrieve the first non-completed step."""
        for step in self.steps:
            if step.status in ("pending", "in_progress"):
                return step
        return None

    def mark_step(self, step_id: int, status: StepStatus, result: Optional[str] = None) -> None:
        """Update status and outcome of a specific step."""
        for step in self.steps:
            if step.id == step_id:
                step.status = status
                if result is not None:
                    step.result = result
                break

    @property
    def is_complete(self) -> bool:
        """True if all steps are completed or skipped."""
        return all(s.status in ("completed", "skipped") for s in self.steps)

    def to_summary_markdown(self) -> str:
        """Render plan status as an interactive Markdown checklist."""
        lines = [f"**Goal**: {self.goal}\n"]
        for step in self.steps:
            icon = "⬜"
            if step.status == "in_progress":
                icon = "🔄"
            elif step.status == "completed":
                icon = "✅"
            elif step.status == "failed":
                icon = "❌"
            elif step.status == "skipped":
                icon = "⏭️"
            lines.append(f"{icon} **Step {step.id}: {step.title}** ({step.status})")
            if step.description:
                lines.append(f"   _{step.description}_")
            if step.result:
                lines.append(f"   *Result*: {step.result[:120]}...")
        return "\n".join(lines)


class PlanningAgentConfig(AgentConfig):
    """Configuration options specific to the Planning Agent."""
    name: str = Field(default="planning_agent", description="Planning agent identifier")
    description: str = Field(
        default="Decomposes objectives into milestone plans, executes steps iteratively, and performs dynamic replanning.",
        description="Functional description"
    )
    max_plan_steps: int = Field(default=8, ge=2, le=20, description="Maximum milestone limit during decomposition")


class PlanningAgent(BaseAgent):
    """
    Autonomous Planning Agent.
    Formulates structured execution plans, tracks milestone states, executes scoped subtasks,
    and adjusts strategy upon execution feedback.
    """

    def __init__(
        self,
        config: Optional[PlanningAgentConfig] = None,
        llm_provider: Optional[BaseLLMProvider] = None,
        tool_registry: Optional[ToolRegistry] = None,
        memory_manager: Optional[MemoryManager] = None,
        sandbox: Optional[BaseSandbox] = None,
        emitter: Optional[EventEmitter] = None,
    ) -> None:
        super().__init__(
            config=config or PlanningAgentConfig(),
            llm_provider=llm_provider,
            tool_registry=tool_registry,
            memory_manager=memory_manager,
            sandbox=sandbox,
            emitter=emitter,
        )
        self.plan: Optional[Plan] = None

    async def _formulate_plan(self, goal: str) -> Plan:
        """Query LLM to break down target goal into a structured JSON plan."""
        prompt = (
            f"You are a master planner. Decompose the following goal into 3 to {getattr(self.config, 'max_plan_steps', 8)} logical steps.\n"
            f"Goal: {goal}\n\n"
            "Return STRICT JSON only matching this schema:\n"
            "{\n"
            '  "goal": "string",\n'
            '  "steps": [\n'
            '    {"id": 1, "title": "short title", "description": "concise instructions"},\n'
            '    {"id": 2, "title": "short title", "description": "concise instructions"}\n'
            "  ]\n"
            "}"
        )

        messages = [
            {"role": "system", "content": "You are a JSON-only strategic planning assistant."},
            {"role": "user", "content": prompt},
        ]

        if not self.llm:
            raise RuntimeError("LLM provider must be configured to generate plans.")

        response = await self.llm.generate(messages=messages, temperature=0.2)
        content = response.content or ""

        # Extract JSON block
        json_match = re.search(r"\{.*\}", content, re.DOTALL)
        if json_match:
            try:
                data = json.loads(json_match.group(0))
                return Plan(**data)
            except Exception as parse_err:
                logger.warning("Failed to parse plan JSON: %s. Using heuristic plan.", parse_err)

        # Fallback plan if model failed to output valid JSON
        return Plan(
            goal=goal,
            steps=[
                PlanStep(id=1, title="Analyze task requirements", description="Inspect context and requirements"),
                PlanStep(id=2, title="Execute core solution", description="Implement primary solution logic"),
                PlanStep(id=3, title="Verify deliverables", description="Validate results and synthesize summary"),
            ],
        )

    async def _astep(self) -> bool:
        """
        Execute one milestone or formulate the initial plan:
        1. Formulate plan on step 1 if not yet created.
        2. Identify next pending step.
        3. Execute step using LLM & registered tools.
        4. Record outcome and evaluate plan completion.
        """
        if not self.llm:
            raise RuntimeError("LLM provider must be configured to execute PlanningAgent steps.")

        task = self.state.metadata.get("task", "Execute task")

        # 1. Initialize Plan on first execution turn
        if self.plan is None:
            await self.emitter.emit_thought(thought="Formulating strategic milestone plan...")
            self.plan = await self._formulate_plan(task)
            self.state.metadata["plan"] = self.plan.model_dump()
            plan_summary = self.plan.to_summary_markdown()

            await self.emitter.emit_thought(thought=f"Formulated Execution Plan:\n\n{plan_summary}")
            self.state.add_message("assistant", f"Formulated Plan:\n{plan_summary}")
            self.memory.short_term.add_message("assistant", f"Formulated Plan:\n{plan_summary}")
            return True

        # 2. Identify current pending milestone
        current_step = self.plan.get_current_step()
        if not current_step:
            # All steps finished
            final_summary = f"All planned milestones completed successfully.\n\n{self.plan.to_summary_markdown()}"
            self.state.mark_completed(output=final_summary)
            return False

        current_step.status = "in_progress"
        self.state.metadata["plan"] = self.plan.model_dump()

        step_prompt = (
            f"You are executing Step {current_step.id} of the plan:\n"
            f"**Title**: {current_step.title}\n"
            f"**Instructions**: {current_step.description}\n\n"
            f"Current Overall Plan Status:\n{self.plan.to_summary_markdown()}\n\n"
            "Use available tools if necessary to accomplish this step. If step is fulfilled, provide a clear concise summary."
        )

        await self.emitter.emit_thought(thought=f"Commencing Step {current_step.id}: {current_step.title}")

        # 3. Dispatch execution turn
        budgeted_messages = self.memory.short_term.get_budgeted_messages()
        budgeted_messages.append({"role": "user", "content": step_prompt})
        tool_schemas = self.tools.get_openai_schemas()

        response = await self.llm.generate(
            messages=budgeted_messages,
            tools=tool_schemas if tool_schemas else None,
        )

        # 4. Handle tool execution if requested
        if response.tool_calls:
            for tc in response.tool_calls:
                func = tc.get("function", {})
                t_name = func.get("name", "")
                t_args_raw = func.get("arguments", "{}")
                try:
                    t_args = json.loads(t_args_raw)
                except Exception:
                    t_args = {}

                call_id = tc.get("id", f"call_{current_step.id}")
                await self.emitter.emit_tool_call(tool_name=t_name, arguments=t_args, tool_call_id=call_id)
                res = await self.tools.aexecute(t_name, **t_args)

                self.state.record_tool_execution(
                    tool_name=t_name,
                    arguments=t_args,
                    output=res.output,
                    exit_code=res.exit_code,
                    is_error=res.is_error,
                    artifacts=res.artifacts,
                    tool_call_id=call_id,
                )
                await self.emitter.emit_observation(
                    output=res.output,
                    tool_name=t_name,
                    exit_code=res.exit_code,
                    is_error=res.is_error,
                    artifacts=res.artifacts,
                    tool_call_id=call_id,
                )

                obs_str = res.output if isinstance(res.output, str) else json.dumps(res.output, ensure_ascii=False)
                self.memory.short_term.add_message(
                    role="tool",
                    content=obs_str,
                    name=t_name,
                    tool_call_id=call_id,
                )

            # Keep step in progress to process tool observation on next cycle
            return True

        # 5. Conclude Step
        outcome = response.content or f"Step {current_step.id} concluded."
        current_step.status = "completed"
        current_step.result = outcome
        self.state.metadata["plan"] = self.plan.model_dump()

        self.memory.short_term.add_message("assistant", outcome)
        await self.emitter.emit_thought(thought=f"Finished Step {current_step.id}: {outcome[:120]}...")

        # Check if plan reached global completion
        if self.plan.is_complete:
            final_report = f"Execution Plan Accomplished:\n\n{self.plan.to_summary_markdown()}"
            self.state.mark_completed(output=final_report)
            return False

        return True