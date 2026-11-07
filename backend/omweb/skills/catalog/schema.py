# backend/omweb/skills/catalog/schema.py
"""
Pydantic schemas for the Skill Catalog subsystem.

A catalog item describes a skill available for one-click install from a
remote source (e.g. GitHub Pages). It mirrors the shape of
``ProviderPackageCatalogItem`` from ``omweb.provider_packages.schema``
so the frontend can render catalog cards uniformly across both
subsystems.

Design invariants:
    * ``download_url`` + ``checksum_sha256`` presence is the ONLY gate
      for installability. There is no "trust the server" mode.
    * Extra fields are allowed (``extra="allow"``) so future catalog
      metadata additions do not require a schema bump -- the frontend
      ignores unknown fields gracefully.
    * No install happens without a SHA-256. Missing checksum -> preview-only.
"""

from __future__ import annotations

import re
from typing import List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


# Validate catalog item IDs against the same pattern as skill IDs.
_SKILL_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_]{2,63}$")

# SHA-256 hex digest validation (64 lowercase hex chars).
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class DeveloperInfo(BaseModel):
    """Structured publisher metadata for a catalog item."""

    model_config = ConfigDict(extra="allow")

    name: str = Field(..., min_length=1)
    url: Optional[str] = None
    email: Optional[str] = None
    avatar_url: Optional[str] = None
    github_username: Optional[str] = None
    verified: bool = False


class SkillCatalogItem(BaseModel):
    """
    One entry in the skill catalog.

    Mirrors ``ProviderPackageCatalogItem`` but specialized for skills:
    includes ``has_sub_skills`` / ``sub_skills_count`` so the frontend
    can render the ROUTER badge without a second round-trip, and
    ``trust`` / ``stability`` to surface deprecation and verification.
    """

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    # -- Identity ---------------------------------------------------
    id: str
    name: str
    display_name: Optional[str] = None
    description: str = ""
    version: str

    # -- Source -----------------------------------------------------
    download_url: Optional[str] = None
    checksum_sha256: Optional[str] = None

    # -- Branding ---------------------------------------------------
    brand_color: Optional[str] = None
    icon: Optional[str] = None
    icon_type: Literal["name", "svg"] = "name"

    # -- Publisher --------------------------------------------------
    author: Optional[str] = None
    author_url: Optional[str] = None
    homepage: Optional[str] = None
    developer: Optional[DeveloperInfo] = None
    repository_url: Optional[str] = None
    license: Optional[str] = None
    docs_url: Optional[str] = None

    # -- Discovery --------------------------------------------------
    tags: List[str] = Field(default_factory=list)

    # -- Skill-specific ---------------------------------------------
    has_sub_skills: bool = False
    sub_skills_count: int = 0
    trust: Literal["verified", "community", "unverified"] = "unverified"
    stability: Literal["experimental", "beta", "stable", "deprecated"] = (
        "stable"
    )
    release_date: Optional[str] = None
    deprecation_message: Optional[str] = None

    # -- Install-time warnings --------------------------------------
    # Advisory strings shown in the install confirmation dialog.
    # E.g. "This skill uses the Router pattern..." or "Deprecated."
    install_warnings: List[str] = Field(default_factory=list)

    # -- Computed at load time --------------------------------------
    installable: bool = False
    installability_reason: str = ""


def is_installable(item: SkillCatalogItem) -> bool:
    """
    Return True iff the item has a download_url AND a valid SHA-256.

    Mirrors ``omweb.provider_packages.catalog.is_installable``.
    """
    if not item.download_url:
        return False
    if not item.checksum_sha256:
        return False
    if not _SHA256_RE.match(item.checksum_sha256.strip().lower()):
        return False
    return True


def installability_reason(item: SkillCatalogItem) -> str:
    """Human-readable explanation of installability."""
    if not item.download_url:
        return "Preview only -- no download URL published yet."
    if not item.checksum_sha256:
        return (
            "Preview only -- no checksum published. PELDRUN requires a "
            "SHA-256 before downloading any skill."
        )
    if not _SHA256_RE.match(item.checksum_sha256.strip().lower()):
        return (
            "Preview only -- the published checksum is not a valid "
            "SHA-256 hex digest."
        )
    return "Installable -- click to download and install."


__all__ = [
    "DeveloperInfo",
    "SkillCatalogItem",
    "is_installable",
    "installability_reason",
]
