"""
PELDRUN Core Planning Flow Engine.
Formulates structured execution plans and drives agents sequentially through plan steps
with continuous progress broadcasts and plan persistence.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

from peldrun.agents.tool_call_agent import ToolCallAgent
from peldrun.events.emitter import EventEmitter
from peldrun.events.schema import EventType
from peldrun.flows.base import BaseFlow, FlowResult
from peldrun.flows.planning import PlanState, PlanStep, StepStatus
from peldrun.llm.client import AsyncLLMClient, LLMResponse
from peldrun.memory import MemoryManager

logger = logging.getLogger("peldrun.flows.planning_flow")


class PlanningFlow(BaseFlow):
    """
    Two-phase execution flow:
    1. Formulation: Synthesize a structured sequential plan via LLM.
    2. Execution: Execute each plan step via ToolCallAgent, updating plan status.
    """

    def __init__(
        self,
        llm: Any,
        agent: ToolCallAgent,
        emitter: Optional[EventEmitter] = None,
        memory: Optional[MemoryManager] = None,
    ) -> None:
        super().__init__(emitter=emitter, memory=memory)
        self.llm = llm
        self.agent = agent

    async def _formulate_plan(self, task: str) -> PlanState:
        """Request structured plan steps from the LLM."""
        plan_prompt = (
            f"You are an expert project planner. Break down the following task into 2 to 5 clear, "
            f"sequential, actionable steps.\n"
            f"Return ONLY valid JSON matching this schema:\n"
            f'{{"steps": [{{"index": 1, "title": "Step title", "description": "Details"}}]}}\n\n'
            f"Task: {task}"
        )

        if hasattr(self.llm, "chat_completion"):
            response = await self.llm.chat_completion(
                messages=[{"role": "user", "content": plan_prompt}],
                temperature=0.2,
            )
            content = response.content or "" if hasattr(response, "content") else str(response)
        elif hasattr(self.llm, "generate"):
            response = await self.llm.generate(
                messages=[{"role": "user", "content": plan_prompt}],
                temperature=0.2,
            )
            content = response.content or "" if hasattr(response, "content") else str(response)
        else:
            raise TypeError(f"Unsupported LLM type in PlanningFlow: {type(self.llm).__name__}")

        content = content.strip()
        if "```json" in content:
            content = content.split("```json")[1].split("```")[0].strip()
        elif "```" in content:
            content = content.split("```")[1].split("```")[0].strip()

        try:
            parsed = json.loads(content)
            raw_steps = parsed.get("steps", [])
            steps = [
                PlanStep(
                    index=int(s.get("index", idx)),
                    title=str(s.get("title", f"Step {idx}")),
                    description=str(s.get("description", "")),
                )
                for idx, s in enumerate(raw_steps, start=1)
            ]
        except Exception as exc:
            logger.warning("Failed to parse LLM plan JSON: %s. Using default single-step plan.", exc)
            steps = [PlanStep(index=1, title="Execute Task", description=task)]

        return PlanState(task=task, steps=steps)

    async def aexecute(self, task: str, **kwargs: Any) -> FlowResult:
        """Execute the planning flow across all defined steps."""
        await self.emitter.emit_agent_activity(
            phase="planning",
            message=f"Formulating execution plan for: {task}",
            step=1,
        )

        plan = await self._formulate_plan(task)

        # Broadcast initial plan
        await self.emitter.emit(
            self.emitter.create_event(
                event_type=EventType.SNAPSHOT,
                payload={"plan": plan.model_dump(), "plan_markdown": plan.to_markdown()},
                step=1,
            )
        )

        collected_outputs: List[str] = []

        for step in plan.steps:
            plan.mark_step_in_progress(step.index)
            await self.emitter.emit_agent_activity(
                phase="executing",
                message=f"Step {step.index}/{len(plan.steps)}: {step.title}",
                step=step.index,
            )

            step_prompt = (
                f"Overall Task: {task}\n"
                f"Current Step {step.index} of {len(plan.steps)}: {step.title}\n"
                f"Step Instructions: {step.description}\n"
                f"Execute this step completely."
            )

            try:
                step_result = await self.agent.run_task(prompt=step_prompt, max_steps=10)
                plan.mark_step_completed(step.index, output=step_result)
                collected_outputs.append(f"### {step.title}\n{step_result}")

                await self.emitter.emit(
                    self.emitter.create_event(
                        event_type=EventType.STEP_END,
                        payload={"step_index": step.index, "output": step_result},
                        step=step.index,
                    )
                )

            except Exception as step_exc:
                plan.mark_step_failed(step.index, error=str(step_exc))
                await self.emitter.emit_error(
                    error=f"Plan step {step.index} failed: {step_exc}",
                    step=step.index,
                )
                return FlowResult(
                    success=False,
                    output="\n\n".join(collected_outputs),
                    error=f"Execution halted at step {step.index}: {step_exc}",
                    metadata={"plan": plan.model_dump()},
                )

        final_summary = "\n\n".join(collected_outputs)
        await self.emitter.emit_final(output=final_summary, step=len(plan.steps))

        return FlowResult(
            success=True,
            output=final_summary,
            metadata={"plan": plan.model_dump()},
        )