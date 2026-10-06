"""
backend/omweb/agents/registry.py

Agent Registry - Persists, scopes, and manages lifecycle statuses of custom and builtin agents.

Hardened under Phase M1:
- Treats `builtins/*.json` as the authoritative Single Source of Truth for system agents.
- Maintains backwards-compatible canonical aliases for legacy shorthand identifiers.
- Eliminates divergence between hardcoded Python definitions and JSON manifests.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class AgentNotFoundError(Exception):
    """Raised when a requested agent identifier is not found in the registry."""

    def __init__(self, agent_id: str):
        super().__init__(f"Agent '{agent_id}' is not registered in the Agent Store.")
        self.agent_id = agent_id


class AgentRegistry:
    """Authoritative registry managing built-in and user-defined agents."""

    def __init__(self) -> None:
        self.agents_dir = Path(__file__).resolve().parent
        self.builtins_dir = self.agents_dir / "builtins"
        backend_dir = self.agents_dir.parent.parent

        self.custom_dir = backend_dir / "storage" / "store" / "agents"
        self.custom_dir.mkdir(parents=True, exist_ok=True)
        self.state_file = self.custom_dir / "agents_state.json"

        # Canonical aliases mapping shorthand names to sovereign persona manifests
        self._aliases: Dict[str, str] = {
            "coder": "code_architect",
            "researcher": "deep_researcher",
            "analyst": "data_scientist",
        }

        self._builtin_agents: Dict[str, Dict[str, Any]] = self._init_builtin_agents()

    def _init_builtin_agents(self) -> Dict[str, Dict[str, Any]]:
        """
        Load system built-in agent manifests dynamically from `builtins/*.json`.
        
        Falls back safely to canonical baseline definitions if manifest files are absent.
        """
        agents: Dict[str, Dict[str, Any]] = {}

        if self.builtins_dir.exists() and self.builtins_dir.is_dir():
            for manifest_file in sorted(self.builtins_dir.glob("*.json")):
                try:
                    data = json.loads(manifest_file.read_text(encoding="utf-8"))
                    aid = data.get("id") or manifest_file.stem
                    data["id"] = aid
                    data["is_builtin"] = True
                    data.setdefault("status", "active")
                    agents[aid] = data
                except Exception as exc:
                    logger.error(
                        f"[AGENT REGISTRY] Failed loading builtin manifest {manifest_file.name}: {exc}"
                    )

        # Ensure primary default agent exists even if directory was empty
        if "peldrun" not in agents:
            agents["peldrun"] = {
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
            }

        return agents

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

        # 1. Built-in agents loaded from manifests
        for aid, ameta in self._builtin_agents.items():
            copied = dict(ameta)
            copied["status"] = states.get(aid, copied.get("status", "active"))
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
        """
        Retrieve an agent manifest by ID or registered alias.

        Enforces strict lookup without silent fallback masking.
        """
        normalized_id = (agent_id or "").strip().lower()
        resolved_id = self._aliases.get(normalized_id, normalized_id)

        agents = {a["id"].lower(): a for a in self.list_agents()}
        if resolved_id not in agents:
            raise AgentNotFoundError(agent_id)

        manifest = dict(agents[resolved_id])
        # If looked up by alias, expose requested ID for downstream runtime consistency
        if resolved_id != normalized_id and normalized_id in self._aliases:
            manifest["alias_id"] = normalized_id
        return manifest

    def toggle_agent_status(self, agent_id: str) -> Dict[str, Any]:
        """Toggle an agent between active and disabled states."""
        normalized_id = (agent_id or "").strip().lower()
        resolved_id = self._aliases.get(normalized_id, normalized_id)

        if resolved_id == "peldrun":
            return {"error": "Default primary agent 'peldrun' cannot be disabled."}

        try:
            agent = self.get_agent(resolved_id)
        except AgentNotFoundError:
            return {"error": f"Agent '{agent_id}' not found"}

        states = self._load_states()
        current = states.get(resolved_id, agent.get("status", "active"))
        new_status = "disabled" if current == "active" else "active"
        states[resolved_id] = new_status
        self._save_states(states)

        agent["status"] = new_status
        custom_file = self.custom_dir / f"{resolved_id}.json"
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