# backend/omweb/skills/router/__init__.py
"""
Router package for PELDRUN Skills progressive disclosure.

Exposes the components used by the assignment layer to select the most
relevant skills for the current user message, and the components used
to render a compact catalog block into the system prompt.

Public surface:
    * ``BM25SkillRouter``    — deterministic BM25 ranking over skills.
    * ``build_catalog_block`` — render selected skills as a prompt block.
    * ``estimate_tokens``    — cheap char-based token estimate.

Callers SHOULD import from this package rather than the submodules so
internal refactors do not break call sites.
"""

from omweb.skills.router.bm25_router import BM25SkillRouter
from omweb.skills.router.catalog_builder import build_catalog_block
from omweb.skills.router.token_estimator import estimate_tokens

__all__ = [
    "BM25SkillRouter",
    "build_catalog_block",
    "estimate_tokens",
]
