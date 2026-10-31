# backend/omweb/skills/errors.py
"""
Exception hierarchy for the PELDRUN Skills subsystem.

Every exception carries:
  * A stable, machine-readable `code` (safe across releases).
  * A structured `details` dict (safe to serialize into HTTP responses).
  * A `to_dict()` helper matching the shape used by the tool bundles
    subsystem (``omweb.tools.bundles.errors.BundleError``) so the
    existing error-handling middleware and the frontend can treat both
    subsystems uniformly.

Callers should catch ``SkillError`` for a catch-all and inspect ``.code``
when they need to branch on the specific failure mode.
"""

from __future__ import annotations

from typing import Any, Dict, Optional


class SkillError(Exception):
    """
    Base class for every skill-related failure.

    Attributes:
        code:    Stable identifier, e.g. "SKILL_MANIFEST_INVALID".
        details: Structured context (skill_id, field, path, ...).
    """

    DEFAULT_CODE = "SKILL_ERROR"

    def __init__(
        self,
        message: str,
        *,
        code: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(message)
        self.code: str = code or self.DEFAULT_CODE
        self.details: Dict[str, Any] = dict(details or {})

    def to_dict(self) -> Dict[str, Any]:
        """Serialize for HTTP responses and structured logging."""
        return {
            "error": str(self),
            "code": self.code,
            "details": self.details,
        }


# ---------------------------------------------------------------------------
# Manifest & validation stage
# ---------------------------------------------------------------------------

class SkillManifestError(SkillError):
    """
    ``manifest.json`` is missing, malformed, or fails JSON-Schema validation.

    Raised by:
        * ``omweb.skills.manifest.parse_manifest`` — syntactic/structural issues.
        * ``omweb.skills.manifest.validate_manifest_schema`` — schema violations.
    """

    DEFAULT_CODE = "SKILL_MANIFEST_INVALID"


class SkillValidationError(SkillError):
    """
    The skill package payload does not conform to the skill format.

    Raised by:
        * ``omweb.skills.manager`` — before any file is written to disk.
        * ``omweb.skills.loader``  — when the instructions file is missing
          or violates the declared ``execution.instructions_file``.
    """

    DEFAULT_CODE = "SKILL_VALIDATION_FAILED"


# ---------------------------------------------------------------------------
# Archive extraction & security
# ---------------------------------------------------------------------------

class SkillExtractionError(SkillError):
    """
    The zip archive could not be extracted safely.

    Raised by:
        * ``omweb.skills.storage`` — corrupt zip, oversized entries,
          or filesystem write failures during extraction.
    """

    DEFAULT_CODE = "SKILL_EXTRACTION_FAILED"


class SkillSecurityError(SkillError):
    """
    A security policy was violated.

    Examples:
        * Path traversal inside the archive (``../`` or absolute paths).
        * Symlink entries.
        * Disallowed file extensions (``.exe``, ``.dll``, ``handler.py``).
        * Containment escape when reading a reference file at load time.

    Raised by:
        * ``omweb.skills.storage``  — during extraction.
        * ``omweb.skills.loader``   — during instructions/reference loading.
        * ``omweb.skills.manifest`` — when ``execution.instructions_file``
          or ``execution.references_dir`` escapes the package root.
    """

    DEFAULT_CODE = "SKILL_SECURITY_VIOLATION"


# ---------------------------------------------------------------------------
# Storage / lifecycle
# ---------------------------------------------------------------------------

class SkillStorageError(SkillError):
    """
    A storage-layer operation failed (blob write, install, delete, state save).

    Raised by:
        * ``omweb.skills.storage`` — atomic write/move failures.
        * ``omweb.skills.registry`` — when persisting ``skills_state.json``.
        * ``omweb.skills.manager``  — when the on-disk layout is inconsistent.
    """

    DEFAULT_CODE = "SKILL_STORAGE_FAILED"


class SkillNotFoundError(SkillError):
    """
    A skill identifier is not present in the registry.

    Includes the missing ``skill_id`` in ``details`` so the HTTP layer can
    build a precise 404 response and the frontend can echo the ID back to
    the user (e.g. "Skill 'code_review' was removed").
    """

    DEFAULT_CODE = "SKILL_NOT_FOUND"

    def __init__(self, skill_id: str, *, hint: Optional[str] = None) -> None:
        details: Dict[str, Any] = {"skill_id": skill_id}
        if hint:
            details["hint"] = hint
        message = f"Skill '{skill_id}' is not registered."
        if hint:
            message = f"{message} {hint}"
        super().__init__(
            message,
            code=self.DEFAULT_CODE,
            details=details,
        )
        self.skill_id = skill_id


class SkillDisabledError(SkillError):
    """
    The skill is installed but not currently activated.

    Includes the ``skill_id`` so the / resolver can surface a clear
    "This skill is currently disabled" message without a second lookup.
    """

    DEFAULT_CODE = "SKILL_DISABLED"

    def __init__(self, skill_id: str) -> None:
        super().__init__(
            f"Skill '{skill_id}' is disabled. Enable it before use.",
            code=self.DEFAULT_CODE,
            details={"skill_id": skill_id},
        )
        self.skill_id = skill_id


class SkillAlreadyExistsError(SkillError):
    """
    Attempted to install a skill whose ID is already registered.

    The ``manager.install_skill`` operation is idempotent for the same
    version + hash, but raises this error when the caller explicitly
    requests a fresh install without an upgrade flag, or when the
    incoming version conflicts with the active version and the operator
    did not choose replace/rollback semantics.
    """

    DEFAULT_CODE = "SKILL_ALREADY_EXISTS"

    def __init__(
        self,
        skill_id: str,
        *,
        existing_version: Optional[str] = None,
        incoming_version: Optional[str] = None,
    ) -> None:
        details: Dict[str, Any] = {"skill_id": skill_id}
        if existing_version:
            details["existing_version"] = existing_version
        if incoming_version:
            details["incoming_version"] = incoming_version
        super().__init__(
            f"Skill '{skill_id}' is already installed.",
            code=self.DEFAULT_CODE,
            details=details,
        )
        self.skill_id = skill_id


# ---------------------------------------------------------------------------
# Loading & resolution
# ---------------------------------------------------------------------------

class SkillLoadError(SkillError):
    """
    Failed to load a skill's content.

    Raised by:
        * ``omweb.skills.loader`` — missing instructions file, unreadable
          bytes, or an oversized instruction payload that exceeds
          ``limits.max_instructions_bytes``.

    This error is deliberately non-fatal at the engine level:
    ``peldrun_engine`` logs it and continues the run without the
    affected skill, so a broken skill can never take down a chat.
    """

    DEFAULT_CODE = "SKILL_LOAD_FAILED"


class SkillResolverError(SkillError):
    """
    Failed to resolve a ``/skill-id`` invocation from the chat composer.

    Covers ambiguities that are neither "not found" nor "disabled":
        * The slash command collides with a built-in system command
          (e.g. ``/clear``).
        * The skill is deprecated and the operator's settings forbid
          invoking deprecated skills.
        * The skill lacks ``capabilities.chat_invocable``.
    """

    DEFAULT_CODE = "SKILL_RESOLVER_FAILED"


# ---------------------------------------------------------------------------
# Assignment
# ---------------------------------------------------------------------------

class SkillAssignmentError(SkillError):
    """
    Failed to assign or unassign a skill to an agent.

    Raised by:
        * ``omweb.skills.assignments`` — unknown agent, unknown skill,
          duplicate assignment, or an attempt to mutate a locked agent
          (``settings.skills_editable: false``).
    """

    DEFAULT_CODE = "SKILL_ASSIGNMENT_FAILED"


__all__ = [
    "SkillError",
    "SkillManifestError",
    "SkillValidationError",
    "SkillExtractionError",
    "SkillSecurityError",
    "SkillStorageError",
    "SkillNotFoundError",
    "SkillDisabledError",
    "SkillAlreadyExistsError",
    "SkillLoadError",
    "SkillResolverError",
    "SkillAssignmentError",
]
