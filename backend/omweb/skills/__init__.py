# backend/omweb/skills/__init__.py
"""
PELDRUN Skills subsystem.

Public entry points:
    * ``omweb.skills.manager.skill_manager``
        Lifecycle operations (install / delete / toggle) consumed by
        the HTTP router and the engine.
    * ``omweb.skills.registry.skill_registry``
        On-disk discovery and state persistence.
    * ``omweb.skills.assignments.resolve_skill_injection``
        Engine-time instruction injection (merges assigned + invoked
        skills into a single system-prompt block).
    * ``omweb.skills.manifest``
        Parsing, schema validation, and SVG sanitization.
    * ``omweb.skills.storage``
        Atomic install / delete of skill packages.

See ``docs/skills/manifest.schema.json`` for the manifest contract.
"""

__all__: list[str] = []
