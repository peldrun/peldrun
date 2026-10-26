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
                    "http_fetch",
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

    # ─────────────────────────────────────────────────────────────
    # User-saved settings (per-agent overrides)
    # ─────────────────────────────────────────────────────────────
    #
    # Design contract:
    #   - builtins/*.json and custom agents/*.json remain the developer-
    #     authored baseline and are NEVER mutated by user actions.
    #   - User overrides live in a separate ``agent_settings.json`` file
    #     under the custom agents directory, keyed by agent id.
    #   - Only keys declared in ``settings.schema.properties`` are
    #     eligible for override, so this path cannot touch identity,
    #     system_prompt, tools, or any other manifest-owned field.
    #   - ``get_effective_agent()`` is the runtime source of truth: the
    #     execution engine MUST call it (not ``get_agent``) so user
    #     changes propagate to behaviour, not just storage.

    def _settings_file(self) -> Path:
        """Path to the shared agent-settings storage file."""
        return self.custom_dir / "agent_settings.json"

    def _load_settings(self) -> Dict[str, Dict[str, Any]]:
        """Load the full agent-id -> user-saved-values map from disk."""
        f = self._settings_file()
        if not f.exists():
            return {}
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _save_settings(self, data: Dict[str, Dict[str, Any]]) -> None:
        """Atomically persist the full agent-id -> user-saved-values map.

        A tmp file plus ``os.replace`` guarantees that a crash mid-write
        cannot leave a truncated JSON on disk. This matters because a
        corrupted settings file would silently fall back to an empty
        dict via ``_load_settings`` and discard every override.
        """
        f = self._settings_file()
        tmp = f.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        tmp.replace(f)

    @staticmethod
    def _extract_settings_schema(
        manifest: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:
        """Return settings.schema only when it declares at least one property."""
        block = manifest.get("settings")
        if not isinstance(block, dict):
            return None
        schema = block.get("schema")
        if not isinstance(schema, dict):
            return None
        props = schema.get("properties")
        if not isinstance(props, dict) or not props:
            return None
        return schema

    def _merge_settings(
        self,
        agent_id: str,
        manifest: Dict[str, Any],
        schema: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Merge manifest values, schema defaults, and user-saved values.

        Precedence (lowest → highest):
          1. Manifest top-level value (developer baseline, e.g. ``max_steps: 30``).
          2. ``settings.defaults`` block (developer-recommended defaults).
          3. Per-property ``default`` in the schema (fallback only).
          4. User-saved value from ``agent_settings.json``.

        Only keys present in ``schema.properties`` are eligible at every
        stage, so a stray key in the user file cannot leak into the
        effective manifest.
        """
        merged: Dict[str, Any] = {}
        props = schema.get("properties") or {}

        # 1. Manifest top-level values: these are the developer's
        # authoritative runtime defaults (e.g. ``max_steps: 30``).
        for k in props.keys():
            if k in manifest:
                merged[k] = manifest[k]

        # 2. Per-property schema defaults: fallback for keys the
        # manifest does not set explicitly. ``setdefault`` guarantees
        # we only fill gaps, never override #1.
        for k, prop in props.items():
            if k not in merged and isinstance(prop, dict) and "default" in prop:
                merged[k] = prop["default"]

        # 3. User-saved values: final override from the Store UI.
        user_values = self._load_settings().get(agent_id, {})
        if isinstance(user_values, dict):
            for k, v in user_values.items():
                if k in props:
                    merged[k] = v

        return merged

    def get_agent_settings(self, agent_id: str) -> Dict[str, Any]:
        """Return effective schema-declared settings values for an agent.

        Mirrors what ``GET /agents/{id}/settings`` returns, so the UI
        and runtime agree on what "current value" means. Returns an
        empty dict for unknown agents or agents without a schema.
        """
        try:
            manifest = self.get_agent(agent_id)
        except AgentNotFoundError:
            return {}
        schema = self._extract_settings_schema(manifest)
        if schema is None:
            return {}
        return self._merge_settings(agent_id, manifest, schema)

    def save_agent_settings(
        self, agent_id: str, payload: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Persist user-saved settings and return the effective values.

        The existing per-agent record is preserved and only the keys
        present in ``payload`` are updated. This guarantees that tool
        overrides (``disabled_tools`` / ``extra_tools``) stored in the
        same file are not wiped when the user saves a max_steps change,
        and vice versa.

        Validation (JSON Schema) is performed at the route layer, not
        here, so this module does not depend on ``jsonschema`` at boot
        time. Invalid payloads reaching this method are the caller's
        responsibility.
        """
        manifest = self.get_agent(agent_id)
        schema = self._extract_settings_schema(manifest)
        if schema is None:
            raise ValueError(
                f"Agent '{agent_id}' has no configurable settings."
            )
        data = self._load_settings()
        existing = dict(data.get(agent_id, {}))
        existing.update(dict(payload))
        data[agent_id] = existing
        self._save_settings(data)
        return self._merge_settings(agent_id, manifest, schema)

    def get_effective_agent(self, agent_id: str) -> Dict[str, Any]:
        """Return the manifest merged with user-saved settings overrides.

        This is the **runtime source of truth**. ``PeldrunEngine.run``
        calls this instead of ``get_agent`` when building the execution
        context, which is what makes user-saved settings actually affect
        behaviour.

        Two override channels are honoured independently:

          1. **Schema-declared settings** — any key listed in
             ``settings.schema.properties`` is merged via
             :meth:`_merge_settings` (manifest → schema default → user).

          2. **Tool overrides** — ``disabled_tools`` and ``extra_tools``
             are injected only when the manifest sets
             ``settings.tools_editable = true``. Locked agents therefore
             cannot be altered through the tools endpoint even if a
             stray entry exists in ``agent_settings.json``.

        Agents with neither a schema nor ``tools_editable`` are returned
        unchanged (the same behaviour as before).
        """
        manifest = self.get_agent(agent_id)
        effective = dict(manifest)

        # ── Channel 1: schema-declared settings ──────────────────────
        schema = self._extract_settings_schema(manifest)
        if schema is not None:
            merged = self._merge_settings(agent_id, manifest, schema)
            effective.update(merged)

        # ── Channel 2: tool overrides (only when editable) ───────────
        settings_block = manifest.get("settings")
        if isinstance(settings_block, dict) and settings_block.get(
            "tools_editable", False
        ):
            saved = self._load_settings().get(agent_id, {})
            if isinstance(saved, dict):
                if "disabled_tools" in saved:
                    effective["disabled_tools"] = list(saved["disabled_tools"])
                if "extra_tools" in saved:
                    effective["extra_tools"] = list(saved["extra_tools"])

        return effective

    # ─────────────────────────────────────────────────────────────
    # Tool overrides — user-controlled additions/removals
    # ─────────────────────────────────────────────────────────────
    #
    # Storage shape (inside agent_settings.json):
    #
    #   "<agent_id>": {
    #       "disabled_tools": ["browser_interactive"],
    #       "extra_tools":    ["source_control"]
    #   }
    #
    # Effective tools = (manifest.tools − disabled) ∪ extra
    #
    # The tools UI and the runtime both consume ``get_effective_agent``
    # / ``get_agent_tools_state`` so their views stay consistent.

    def get_agent_tools_state(self, agent_id: str) -> Dict[str, Any]:
        """Return the current tool selection for an agent.

        Return shape::

            {
              "manifest_tools":  [...],   # baseline declared in the manifest
              "disabled_tools":  [...],   # subset of manifest_tools to exclude
              "extra_tools":     [...],   # additions from the Store
              "effective_tools": [...],   # manifest − disabled ∪ extra (deduped)
              "tools_editable":  bool,    # whether overrides are honoured
            }

        ``disabled_tools`` is always a subset of ``manifest_tools`` (a
        defensive filter catches stale entries if the manifest is later
        edited to remove a tool). ``extra_tools`` never duplicates a
        manifest tool for the same reason.

        When ``settings.tools_editable`` is false, saved overrides are
        ignored and both ``disabled_tools`` and ``extra_tools`` come back
        empty so this method stays consistent with
        :meth:`get_effective_agent` (which gates the same way).
        """
        manifest = self.get_agent(agent_id)
        manifest_tools = [str(t) for t in (manifest.get("tools") or [])]

        settings_block = manifest.get("settings")
        editable = isinstance(settings_block, dict) and settings_block.get(
            "tools_editable", False
        )

        if not editable:
            return {
                "manifest_tools": manifest_tools,
                "disabled_tools": [],
                "extra_tools": [],
                "effective_tools": list(manifest_tools),
                "tools_editable": False,
            }

        saved = self._load_settings().get(agent_id, {})
        disabled_raw = [str(t) for t in (saved.get("disabled_tools") or [])]
        extra_raw = [str(t) for t in (saved.get("extra_tools") or [])]

        manifest_set = set(manifest_tools)
        disabled_set = set(disabled_raw) & manifest_set
        extra_set = set(extra_raw) - manifest_set

        effective: List[str] = [
            t for t in manifest_tools if t not in disabled_set
        ]
        for t in extra_set:
            if t not in effective:
                effective.append(t)

        return {
            "manifest_tools": manifest_tools,
            "disabled_tools": sorted(disabled_set),
            "extra_tools": sorted(extra_set),
            "effective_tools": effective,
            "tools_editable": True,
        }

    def save_agent_tools_state(
        self,
        agent_id: str,
        disabled: List[str],
        extra: List[str],
    ) -> Dict[str, Any]:
        """Persist tool overrides for an agent and return the new state.

        Preserves any existing schema-declared settings (max_steps, ...)
        stored alongside the tool state in ``agent_settings.json``.
        """
        manifest = self.get_agent(agent_id)
        settings_block = manifest.get("settings")
        if not isinstance(settings_block, dict) or not settings_block.get(
            "tools_editable", False
        ):
            raise ValueError(
                f"Agent '{agent_id}' does not allow tool editing."
            )

        data = self._load_settings()
        existing = dict(data.get(agent_id, {}))
        existing["disabled_tools"] = list(disabled)
        existing["extra_tools"] = list(extra)
        data[agent_id] = existing
        self._save_settings(data)

        return self.get_agent_tools_state(agent_id)

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
        """Delete a custom agent from disk and purge its saved settings."""
        target = self.custom_dir / f"{agent_id}.json"
        if target.exists():
            target.unlink()
            states = self._load_states()
            states.pop(agent_id, None)
            self._save_states(states)

            # Purge any user-saved overrides so a re-created agent with
            # the same id starts from the manifest's authored defaults,
            # not from the previous owner's configuration.
            settings = self._load_settings()
            if agent_id in settings:
                settings.pop(agent_id, None)
                self._save_settings(settings)

            return True
        return False


agent_registry = AgentRegistry()

__all__ = ["AgentRegistry", "AgentNotFoundError", "agent_registry"]
