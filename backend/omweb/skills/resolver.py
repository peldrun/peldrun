# backend/omweb/skills/resolver.py
"""
DEPRECATED / MERGED.

The ``/skill-id`` resolution logic originally planned for this module
is implemented in :func:`omweb.skills.assignments.resolve_skill_injection`
for two structural reasons:

    1. **Single merge point.** The resolver must combine the agent's
       ASSIGNED skills with the user's INVOKED skills (via ``/``) into
       one injection payload, deduplicating and preferring the
       invoked source when both channels reference the same ID.
       Keeping the merge next to the assignment state eliminates any
       risk of the two code paths diverging.

    2. **Fail-safe behavior.** A broken skill must never crash a
       chat run — the resolver catches ``SkillLoadError`` and
       ``SkillSecurityError`` per-skill, accumulates warnings, and
       returns a partial payload. That error-handling contract is
       tightly coupled to the assignment layer, which owns the
       decisions about which skills should be attempted.

This file is retained as a documentation stub for operators who
remember the original plan. Do NOT add logic here — extend
``assignments.py`` instead.
"""

__all__: list[str] = []
