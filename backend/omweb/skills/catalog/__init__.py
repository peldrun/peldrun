# backend/omweb/skills/catalog/__init__.py
"""
Skill Catalog subsystem.

Provides a Pydantic-validated, cache-backed catalog of installable
skills (from GitHub Pages or a local file). Mirrors the Provider
Packages catalog in ``omweb.provider_packages.catalog``.

Public API:
    * ``SkillCatalog``                    -- the catalog class.
    * ``SkillCatalogItem``                -- one catalog item.
    * ``get_skill_catalog()``             -- process-wide singleton.
    * ``reset_skill_catalog_for_tests()`` -- test helper.
"""

from omweb.skills.catalog.catalog import (
    DEFAULT_REMOTE_CATALOG_URL,
    ENABLED_ENV_VAR,
    REMOTE_CATALOG_ENV_VAR,
    SkillCatalog,
    get_skill_catalog,
    reset_skill_catalog_for_tests,
)
from omweb.skills.catalog.schema import (
    DeveloperInfo,
    SkillCatalogItem,
    installability_reason,
    is_installable,
)

__all__ = [
    "DEFAULT_REMOTE_CATALOG_URL",
    "ENABLED_ENV_VAR",
    "REMOTE_CATALOG_ENV_VAR",
    "SkillCatalog",
    "get_skill_catalog",
    "reset_skill_catalog_for_tests",
    "DeveloperInfo",
    "SkillCatalogItem",
    "installability_reason",
    "is_installable",
]
