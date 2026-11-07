# backend/omweb/skills/tools/__init__.py
"""
LLM tool surface for PELDRUN skills Progressive Disclosure.

Each submodule exposes:

    * ``TOOL_SCHEMA`` — dict in OpenAI function-calling format, ready
      to be appended to the model's ``tools=[...]`` list.
    * ``execute(**kwargs) -> dict`` — the tool implementation. Returns
      a dict with at minimum ``{"ok": bool}``. On failure, an
      ``error`` string and a stable ``code`` are included so the model
      (and downstream logs) can distinguish "not found" from "load
      failed" from "security violation".

Available tools:
    * ``load_skill``       — fetch a skill's body by ID.
    * ``load_sub_skill``   — fetch a sub-skill body of a router skill.
    * ``load_reference``   — fetch a reference file declared under a
                             skill's ``execution.references_dir``.

Import pattern used by the engine::

    from omweb.skills.tools import load_skill, load_sub_skill, load_reference

Each submodule is imported on demand by the engine's dispatch layer.
The package ``__init__`` re-exports the three modules as a convenience
for callers that want ``from omweb.skills.tools import load_skill``
to resolve to the module (not a function). That preserves the
``load_skill.TOOL_SCHEMA`` / ``load_skill.execute(...)`` access pattern
used throughout the engine.
"""

from omweb.skills.tools import load_reference, load_skill, load_sub_skill

__all__ = [
    "load_skill",
    "load_sub_skill",
    "load_reference",
]
