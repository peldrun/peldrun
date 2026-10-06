"""
Agent Registry - Persists, scopes, and manages lifecycle statuses of custom and builtin agents.

Enforces authoritative agent lookup and strict fail-fast policies, eliminating
silent fallback masking across the execution plane.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional


class AgentNotFoundError(Exception):
    """Raised when an requested agent identifier is not found in the registry."""

    def __init__(self, agent_id: str):
        super().__init__(f"Agent '{agent_id}' is not registered in the Agent Store.")
        self.agent_id = agent_id


class AgentRegistry:
    """Registry managing the lifecycle, configuration, and retrieval of agents."""

    def __init__(self) -> None:
        backend_dir = Path(__file__).resolve().parent.parent.parent
        self.custom_dir = backend_dir / "storage" / "store" / "agents"
        self.custom_dir.mkdir(parents=True, exist_ok=True)
        self.state_file = self.custom_dir / "agents_state.json"
        self._builtin_agents: Dict[str, Dict[str, Any]] = self._init_builtin_agents()

    def _init_builtin_agents(self) -> Dict[str, Dict[str, Any]]:
        """Initialize default system built-in agent specifications."""
        return {
            "peldrun": {
                "id": "peldrun",
                "name": "peldrun Generalist",
                "role": "General Autonomous Specialist",
                "icon": "Bot",
                "description": "Full-capability autonomous specialist capable of browsing, data analysis, terminal coding, and delivering complete project solutions.",
                "system_prompt": "You are peldrun, an all-around autonomous specialist agent.",
                "tools": [
                    "python_execute",
                    "bash",
                    "str_replace_editor",
                    "web_search",
                    "browser_use",
                    "ask_human",
                    "mcp",
                ],
                "max_steps": 30,
                "is_builtin": True,
                "status": "active",
            },
            "coder": {
                "id": "coder",
                "name": "Software Engineer",
                "role": "Full-Stack Developer",
                "icon": "Code2",
                "description": "Dedicated developer specializing in software architecture, bug fixing, test writing, and clean backend/frontend implementations.",
                "system_prompt": "You are an expert software developer. Write clean, modular, and production-ready code.",
                "tools": ["python_execute", "bash", "str_replace_editor"],
                "max_steps": 30,
                "is_builtin": True,
                "status": "active",
            },
            "researcher": {
                "id": "researcher",
                "name": "Deep Researcher",
                "role": "Web Intelligence & Synthesis",
                "icon": "Search",
                "description": "Gathers multi-source intelligence across the web, cross-references factual data, and creates comprehensive structured briefs.",
                "system_prompt": "You are a thorough research analyst. Prioritize verified sources and synthesize structured reports.",
                "tools": ["web_search", "browser_use", "str_replace_editor"],
                "max_steps": 25,
                "is_builtin": True,
                "status": "active",
            },
            "analyst": {
                "id": "analyst",
                "name": "Data Analyst",
                "role": "Quantitative & Visual Analytics",
                "icon": "BarChart3",
                "description": "Processes data tables, calculates statistics, and builds headless visual charts and graphs using Python.",
                "system_prompt": "You are a skilled data analyst. Process numerical deliverables and generate publication-quality figures.",
                "tools": ["python_execute", "str_replace_editor"],
                "max_steps": 25,
                "is_builtin": True,
                "status": "active",
            },
        }

    def _load_states(self) -> Dict[str, str]:
        """Read agent activation states from persistent storage."""
        if self.state_file.exists():
            try:
                return json.loads(self.state_file.read_text(encoding="utf-8"))
            except Exception:
                return {}
        return {}

    def _save_states(self, states: Dict[str, str]) -> None:
        """Persist agent activation states to storage."""
        try:
            self.state_file.write_text(
                json.dumps(states, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
        except Exception as e:
            print(f"[AGENT REGISTRY ERROR] Could not save agent states: {e}")

    def list_agents(self) -> List[Dict[str, Any]]:
        """List all registered agents merging built-ins with custom definitions."""
        states = self._load_states()
        results: Dict[str, Dict[str, Any]] = {}

        # 1. Built-in agents
        for aid, ameta in self._builtin_agents.items():
            copied = dict(ameta)
            copied["status"] = states.get(aid, "active")
            results[aid] = copied

        # 2. Custom saved agents
        if self.custom_dir.exists():
            for p in sorted(self.custom_dir.glob("*.json")):
                if p.name == "agents_state.json":
                    continue
                try:
                    data = json.loads(p.read_text(encoding="utf-8"))
                    aid = data.get("id") or p.stem
                    data["id"] = aid
                    data["is_builtin"] = False
                    data["status"] = states.get(aid, data.get("status", "active"))
                    results[aid] = data
                except Exception:
                    continue

        return list(results.values())

    def get_agent(self, agent_id: str) -> Dict[str, Any]:
        """Retrieve an agent manifest by ID.

        Enforces strict lookup without silent fallback masking.

        Args:
            agent_id: The unique identifier of the agent.

        Returns:
            The agent manifest dictionary.

        Raises:
            AgentNotFoundError: If the requested agent ID does not exist.
        """
        agents = {a["id"]: a for a in self.list_agents()}
        if agent_id not in agents:
            raise AgentNotFoundError(agent_id)
        return agents[agent_id]

    def toggle_agent_status(self, agent_id: str) -> Dict[str, Any]:
        """Toggle an agent between active and disabled states."""
        if agent_id == "peldrun":
            return {"error": "Default primary agent 'peldrun' cannot be disabled."}

        try:
            agent = self.get_agent(agent_id)
        except AgentNotFoundError:
            return {"error": f"Agent '{agent_id}' not found"}

        states = self._load_states()
        current = states.get(agent_id, agent.get("status", "active"))
        new_status = "disabled" if current == "active" else "active"
        states[agent_id] = new_status
        self._save_states(states)

        agent["status"] = new_status
        custom_file = self.custom_dir / f"{agent_id}.json"
        if custom_file.exists():
            try:
                data = json.loads(custom_file.read_text(encoding="utf-8"))
                data["status"] = new_status
                custom_file.write_text(
                    json.dumps(data, indent=2, ensure_ascii=False),
                    encoding="utf-8",
                )
            except Exception:
                pass

        return agent

    def create_custom_agent(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """Create and persist a new custom agent manifest."""
        name = data.get("name", "Custom Agent").strip()
        aid = f"custom_{name.lower().replace(' ', '_')}"
        data["id"] = aid
        data["is_builtin"] = False
        data["status"] = "active"
        data.setdefault("tools", ["python_execute", "bash", "str_replace_editor"])

        target = self.custom_dir / f"{aid}.json"
        target.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        return data

    def update_custom_agent(self, agent_id: str, data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Update an existing custom agent manifest."""
        target = self.custom_dir / f"{agent_id}.json"
        if not target.exists():
            return None

        data["id"] = agent_id
        data["is_builtin"] = False
        data.setdefault("status", "active")
        target.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        return data

    def delete_custom_agent(self, agent_id: str) -> bool:
        """Delete a custom agent from disk."""
        target = self.custom_dir / f"{agent_id}.json"
        if target.exists():
            target.unlink()
            states = self._load_states()
            states.pop(agent_id, None)
            self._save_states(states)
            return True
        return False


agent_registry = AgentRegistry()

__all__ = ["AgentRegistry", "AgentNotFoundError", "agent_registry"]