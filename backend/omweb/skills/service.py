# backend/omweb/skills/service.py
"""
DEPRECATED / MERGED.

The unified skill service interface originally planned for this module
is implemented in :class:`omweb.skills.manager.SkillManager`, which is
the single public entry point for:

    * HTTP routes        — ``omweb.routers.skills``
    * Agent assignments  — ``omweb.routers.store_rtr`` (via assignments)
    * Engine injection   — ``omweb.engines.peldrun_engine``
                          (via assignments.resolve_skill_injection)

The manager wraps ``SkillStorage`` and ``SkillRegistry``, and is
deliberately the ONLY layer that both writes to disk and refreshes
the in-memory registry. Splitting this into a separate "service"
would introduce a redundant layer with no added value.

This file is retained as a documentation stub. Do NOT add logic here —
extend ``manager.py`` instead.
"""

__all__: list[str] = []
