# backend/omweb/skills/manifest.py
"""
Skill manifest parsing, validation, and sanitization.

Responsibilities:
    * Parse SKILL.md (YAML frontmatter + markdown body).
    * Validate manifest.json against the canonical JSON Schema at
      ``docs/skills/manifest.schema.json`` (with a structural fallback
      when jsonschema or the schema file is unavailable).
    * Sanitize inline SVG icons (``icon_svg``) against XSS, external
      resource injection, XML bombs, and SVG rendering exploits.
    * Enforce PELDRUN version compatibility (``peldrun_min_version`` /
      ``peldrun_max_version``).
    * Expose a single high-level entry point: ``load_skill_package``.

Design notes:
    * ``icon_url`` is intentionally NOT supported. Icons must either be
      an inline ``icon_svg`` (sanitized here) or a relative ``icon``
      path inside the package. This avoids external network fetches
      and third-party tracking vectors.
    * ElementTree is used for SVG parsing because it is in the stdlib
      and has a conservative parser by default (no entity expansion
      beyond predefined entities, no external DTD fetches).
    * All exceptions are Skill* subclasses from
      ``omweb.skills.errors`` with structured ``details`` payloads.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
import xml.etree.ElementTree as ET

from omweb.skills.errors import (
    SkillManifestError,
    SkillSecurityError,
    SkillValidationError,
)


# ---------------------------------------------------------------------------
# Optional dependencies (both are already used elsewhere in the codebase;
# we degrade gracefully when they are absent to keep the subsystem usable
# in minimal deployments).
# ---------------------------------------------------------------------------

try:
    import jsonschema as _jsonschema
    _JSONSCHEMA_AVAILABLE = True
except ImportError:  # pragma: no cover - optional dependency
    _jsonschema = None  # type: ignore[assignment]
    _JSONSCHEMA_AVAILABLE = False

try:
    import yaml as _yaml
    _YAML_AVAILABLE = True
except ImportError:  # pragma: no cover - optional dependency
    _yaml = None  # type: ignore[assignment]
    _YAML_AVAILABLE = False


logger = logging.getLogger("omweb.skills.manifest")


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MANIFEST_FILENAME = "manifest.json"
SKILL_MD_FILENAME = "SKILL.md"
DEFAULT_INSTRUCTIONS_FILE = "SKILL.md"

# Absolute caps mirrored from the JSON Schema and enforced in code so the
# runtime behavior is identical even when jsonschema is unavailable.
MAX_SVG_BYTES = 32 * 1024
MAX_SVG_ELEMENTS = 200
MAX_SVG_DEPTH = 8
MAX_INSTRUCTIONS_BYTES = 1024 * 1024  # 1 MiB hard ceiling

# The packaged copy (``_manifest_schema.json``) is preferred for deploy
# isolation; the docs/ copy is the human-readable reference. Loading
# stops at the first file that exists and parses cleanly.
_THIS_FILE = Path(__file__).resolve()
_SCHEMA_CANDIDATES: Tuple[Path, ...] = (
    _THIS_FILE.parent / "_manifest_schema.json",
    _THIS_FILE.parent.parent.parent.parent
        / "docs"
        / "skills"
        / "manifest.schema.json",
)

_SEMVER_RE = re.compile(
    r"^\d+\.\d+\.\d+(?:-[a-z0-9.\-]+)?(?:\+[a-z0-9.\-]+)?$",
    re.IGNORECASE,
)
_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_]{2,63}$")


# ---------------------------------------------------------------------------
# SVG whitelists
# ---------------------------------------------------------------------------
#
# Any element or attribute not in these sets is rejected. The lists are
# deliberately conservative: they cover every legitimate icon shape
# emitted by common tools (Figma, Illustrator, Iconify, Lucide) while
# excluding anything that can execute code, load external resources, or
# reference third-party content.
#
# Notably excluded (and rejected by construction):
#   * ``script``, ``foreignObject``, ``iframe`` — script execution
#   * ``use``, ``image``, ``a`` — external references (href / xlink:href)
#   * ``style``, ``link`` — CSS injection
#   * ``animate*``, ``set`` — attribute manipulation (can rewrite href)
#   * ``class`` — deliberate omission; use attributes for styling

_SVG_ALLOWED_ELEMENTS = frozenset({
    "svg",
    "g",
    "path",
    "circle",
    "ellipse",
    "rect",
    "line",
    "polyline",
    "polygon",
    "defs",
    "linearGradient",
    "radialGradient",
    "stop",
    "clipPath",
    "mask",
    "title",
    "desc",
})

_SVG_ALLOWED_ATTRS = frozenset({
    # Geometry
    "d", "cx", "cy", "r", "rx", "ry", "x", "y", "x1", "x2", "y1", "y2",
    "width", "height", "points",
    # Paint
    "fill", "fill-opacity", "fill-rule",
    "stroke", "stroke-width", "stroke-linecap", "stroke-linejoin",
    "stroke-opacity", "stroke-dasharray", "stroke-dashoffset",
    "stop-color", "stop-opacity", "offset",
    # Composition
    "opacity", "transform",
    "gradientUnits", "gradientTransform",
    "clipPathUnits", "maskUnits",
    # Roots
    "viewBox", "preserveAspectRatio", "version",
    # Accessibility
    "role",
    # Identity (the value is prefixed with the skill's namespace)
    "id",
})

# Allow ARIA and data-* attributes by prefix. These never carry script
# or external URLs and are used extensively by icon sets for a11y.
_SVG_ALLOWED_ATTR_PREFIXES = ("aria-", "data-")

# Any of these substrings in an attribute value triggers immediate
# rejection regardless of which attribute it appears on.
_SVG_FORBIDDEN_VALUE_PATTERNS = (
    "javascript:",
    "vbscript:",
    "data:",
    "@import",
    "expression(",
)

# ``url(#id)`` is the only valid url() form (gradient / mask / clip
# references). Any other target — external scheme, absolute path, or
# ``javascript:`` — is a security violation.
_SVG_URL_RE = re.compile(r"url\s*\(\s*([^)]*?)\s*\)", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Schema loading (cached)
# ---------------------------------------------------------------------------

_schema_cache: Optional[Dict[str, Any]] = None
_schema_load_attempted: bool = False


def _load_schema() -> Optional[Dict[str, Any]]:
    """
    Load the skill manifest JSON Schema, caching on first call.

    Returns ``None`` when no schema file is available. Callers then fall
    back to structural validation in ``_validate_manifest_minimal``.
    """
    global _schema_cache, _schema_load_attempted
    if _schema_load_attempted:
        return _schema_cache
    _schema_load_attempted = True

    for candidate in _SCHEMA_CANDIDATES:
        if not candidate.is_file():
            continue
        try:
            data = json.loads(candidate.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                _schema_cache = data
                logger.debug("Loaded skill manifest schema from %s", candidate)
                return _schema_cache
            logger.warning(
                "Schema file at %s is not a JSON object; ignoring.",
                candidate,
            )
        except Exception as exc:
            logger.warning("Failed to load schema from %s: %s", candidate, exc)

    logger.warning(
        "No skill manifest schema file found at any candidate path. "
        "Falling back to structural validation. This is a degraded mode: "
        "only required fields and critical patterns will be enforced."
    )
    return None


# ---------------------------------------------------------------------------
# Semver helpers
# ---------------------------------------------------------------------------

def _parse_semver(version: str) -> Tuple[int, int, int]:
    """Parse a semver string into (major, minor, patch). Raises ValueError."""
    raw = str(version or "").strip()
    if not raw:
        raise ValueError("Empty version string.")
    core = raw.split("-", 1)[0].split("+", 1)[0]
    parts = core.split(".")
    if len(parts) != 3:
        raise ValueError(f"Invalid semver: {version!r}")
    return (int(parts[0]), int(parts[1]), int(parts[2]))


def _get_running_peldrun_version() -> Optional[str]:
    """Return the running PELDRUN version, or None when unavailable."""
    try:
        import peldrun  # noqa: PLC0415 — lazy import to avoid hard dependency

        version = getattr(peldrun, "__version__", None)
        if isinstance(version, str) and version.strip():
            return version.strip()
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# SKILL.md parsing
# ---------------------------------------------------------------------------

def parse_skill_md(content: str) -> Tuple[Dict[str, Any], str]:
    """
    Split a ``SKILL.md`` document into ``(frontmatter, body)``.

    The document MUST start with ``---`` on the first line and contain a
    closing ``---`` on its own line. Frontmatter is parsed with PyYAML
    when available; otherwise a minimal line-based parser handles the
    simple ``key: value`` shape that every PELDRUN skill uses.

    Raises:
        SkillValidationError: on missing delimiters, invalid YAML, or a
            frontmatter block that is not a mapping.
    """
    if not isinstance(content, str) or not content.strip():
        raise SkillValidationError(
            "SKILL.md content is empty.",
            details={"reason": "empty_content"},
        )

    normalized = content.replace("\r\n", "\n").replace("\r", "\n")

    if not normalized.startswith("---"):
        raise SkillValidationError(
            "SKILL.md must start with '---' YAML frontmatter.",
            details={"reason": "missing_frontmatter"},
        )

    # Skip the opening ``---\n`` and locate the closing delimiter.
    remainder = normalized[3:]
    close_match = re.search(r"\n---[ \t]*\n", remainder)
    if not close_match:
        raise SkillValidationError(
            "SKILL.md frontmatter is not closed with '---'.",
            details={"reason": "unterminated_frontmatter"},
        )

    frontmatter_text = remainder[: close_match.start()].strip()
    body = remainder[close_match.end():].strip()

    frontmatter = _parse_yaml_frontmatter(frontmatter_text)
    if not isinstance(frontmatter, dict):
        raise SkillValidationError(
            "SKILL.md frontmatter must be a YAML mapping.",
            details={"reason": "invalid_frontmatter_type"},
        )

    return frontmatter, body


def _parse_yaml_frontmatter(text: str) -> Dict[str, Any]:
    """Parse YAML frontmatter with PyYAML, falling back to a minimal parser."""
    if not text.strip():
        return {}

    if _YAML_AVAILABLE and _yaml is not None:
        try:
            parsed = _yaml.safe_load(text) or {}
            return parsed if isinstance(parsed, dict) else {}
        except Exception as exc:
            raise SkillValidationError(
                f"Invalid YAML in SKILL.md frontmatter: {exc}",
                details={
                    "reason": "invalid_yaml",
                    "parse_error": str(exc)[:200],
                },
            ) from exc

    return _parse_yaml_minimal(text)


def _parse_yaml_minimal(text: str) -> Dict[str, Any]:
    """
    Minimal fallback parser for the simple ``key: value`` frontmatter used
    by PELDRUN skills. Handles:
        * plain ``key: value``
        * quoted ``key: "value"`` / ``key: 'value'``
        * block scalars (``key: |`` / ``key: >``) with indented lines
        * inline lists (``key: [a, b, c]``)

    Nested mappings are not supported; the caller should ensure PyYAML is
    installed for skills that need richer YAML.
    """
    result: Dict[str, Any] = {}
    lines = text.split("\n")
    i = 0
    while i < len(lines):
        raw_line = lines[i]
        i += 1
        line = raw_line.rstrip()

        if not line or line.lstrip().startswith("#"):
            continue
        if line.startswith(" ") or line.startswith("\t"):
            continue  # stray indented content
        if ":" not in line:
            continue

        key, _, value = line.partition(":")
        key = key.strip()
        value = value.strip()
        if not key:
            continue

        if value in ("|", ">", "|-", ">-", "|+", ">+"):
            block_lines: List[str] = []
            while i < len(lines) and (
                lines[i].startswith("  ")
                or lines[i].startswith("\t")
                or not lines[i].strip()
            ):
                stripped = (
                    lines[i][2:] if lines[i].startswith("  ") else lines[i]
                )
                block_lines.append(stripped)
                i += 1
            result[key] = "\n".join(block_lines).strip()
        elif value.startswith("[") and value.endswith("]"):
            items = [
                x.strip().strip("'\"")
                for x in value[1:-1].split(",")
                if x.strip()
            ]
            result[key] = items
        else:
            result[key] = value.strip("'\"")

    return result


# ---------------------------------------------------------------------------
# SVG sanitization
# ---------------------------------------------------------------------------

# Register the SVG namespace once so ``ET.tostring`` renders ``<svg>``
# without the ``ns0:`` prefix that would otherwise break mask-image URLs.
try:
    ET.register_namespace("", "http://www.w3.org/2000/svg")
except Exception:  # pragma: no cover - namespace registry is global
    pass


def _strip_ns(tag: str) -> str:
    """Remove a leading ``{namespace}`` prefix from an XML tag or attribute."""
    if tag.startswith("{"):
        end = tag.find("}")
        if end >= 0:
            return tag[end + 1:]
    return tag


def _tree_depth(elem: ET.Element, current: int = 1) -> int:
    """Return the maximum nesting depth of an ElementTree element."""
    max_depth = current
    for child in elem:
        child_depth = _tree_depth(child, current + 1)
        if child_depth > max_depth:
            max_depth = child_depth
    return max_depth


def _has_forbidden_value(attr_value: str) -> Optional[str]:
    """Return the first forbidden pattern found in ``attr_value``, or None."""
    lowered = attr_value.lower()
    for pattern in _SVG_FORBIDDEN_VALUE_PATTERNS:
        if pattern in lowered:
            return pattern
    return None


def _find_non_local_url_ref(attr_value: str) -> Optional[str]:
    """
    Return the first ``url(...)`` target that is NOT a local ``#id``,
    or ``None`` when every reference is local.
    """
    for match in _SVG_URL_RE.finditer(attr_value):
        target = match.group(1).strip().strip("'\"")
        if not target.startswith("#"):
            return target
    return None


def sanitize_svg(raw_svg: str, *, skill_id: str) -> str:
    """
    Sanitize an inline SVG icon string.

    Enforces, in order:
        1. Non-empty, well-formed input under the byte-size cap.
        2. No ``<!DOCTYPE>`` / ``<!ENTITY>`` declarations (XML bombs).
        3. Well-formed XML parse.
        4. Root element is ``<svg>``.
        5. Depth and element-count caps.
        6. Element whitelist (no ``script``, ``use``, ``foreignObject``,
           ``image``, ``style``, ``animate*``, etc.).
        7. Attribute whitelist (no ``href``, no ``xlink:href``, no ``on*``,
           no ``style``).
        8. Forbidden-value blacklist (``javascript:``, ``data:``,
           ``vbscript:``, ``@import``, ``expression(``).
        9. Local-only ``url(#id)`` references.
       10. ``id`` prefixing: every id becomes ``peldrun_skill_<id>_<orig>``
           and matching ``url(#orig)`` references are rewritten in lockstep
           to avoid CSS/id collisions with the host page.

    Raises:
        SkillSecurityError: on any violation. The ``details`` dict
            contains a ``reason`` field plus context specific to the
            failure mode (element, attribute, size, etc.).
    """
    if not isinstance(raw_svg, str) or not raw_svg.strip():
        raise SkillSecurityError(
            "SVG icon is empty.",
            details={"reason": "svg_empty", "skill_id": skill_id},
        )

    # 1. Byte-size cap
    raw_bytes = raw_svg.encode("utf-8")
    if len(raw_bytes) > MAX_SVG_BYTES:
        raise SkillSecurityError(
            f"SVG icon exceeds the {MAX_SVG_BYTES}-byte cap.",
            details={
                "reason": "svg_too_large",
                "size_bytes": len(raw_bytes),
                "limit": MAX_SVG_BYTES,
                "skill_id": skill_id,
            },
        )

    # 2. DOCTYPE / ENTITY rejection
    lowered = raw_svg.lower()
    if "<!doctype" in lowered or "<!entity" in lowered:
        raise SkillSecurityError(
            "SVG icon must not contain DOCTYPE or ENTITY declarations.",
            details={
                "reason": "svg_doctype_entity_forbidden",
                "skill_id": skill_id,
            },
        )

    # 3. Parse XML
    try:
        root = ET.fromstring(raw_svg)
    except ET.ParseError as exc:
        raise SkillSecurityError(
            f"SVG icon is not well-formed XML: {exc}",
            details={
                "reason": "svg_malformed",
                "parse_error": str(exc)[:200],
                "skill_id": skill_id,
            },
        ) from exc

    # 4. Root element must be <svg>
    root_tag = _strip_ns(root.tag)
    if root_tag != "svg":
        raise SkillSecurityError(
            "SVG root element must be <svg>.",
            details={
                "reason": "svg_root_invalid",
                "root_tag": root_tag,
                "skill_id": skill_id,
            },
        )

    # 5. Depth and element-count caps
    if _tree_depth(root) > MAX_SVG_DEPTH:
        raise SkillSecurityError(
            f"SVG icon exceeds the maximum nesting depth ({MAX_SVG_DEPTH}).",
            details={"reason": "svg_too_deep", "skill_id": skill_id},
        )

    if sum(1 for _ in root.iter()) > MAX_SVG_ELEMENTS:
        raise SkillSecurityError(
            f"SVG icon exceeds the maximum element count ({MAX_SVG_ELEMENTS}).",
            details={
                "reason": "svg_too_many_elements",
                "skill_id": skill_id,
            },
        )

    # 6–9. Element / attribute / value / url validation
    for elem in root.iter():
        tag = _strip_ns(elem.tag)
        if tag not in _SVG_ALLOWED_ELEMENTS:
            raise SkillSecurityError(
                f"SVG element <{tag}> is not allowed.",
                details={
                    "reason": "svg_forbidden_element",
                    "element": tag,
                    "skill_id": skill_id,
                },
            )

        for attr_name, attr_value in list(elem.attrib.items()):
            attr_local = _strip_ns(attr_name)

            # Namespace declarations are managed by ElementTree and never
            # appear in ``.attrib``; guard defensively regardless.
            if attr_local == "xmlns" or attr_local.startswith("xmlns:"):
                continue

            if attr_local not in _SVG_ALLOWED_ATTRS:
                if not any(
                    attr_local.startswith(p)
                    for p in _SVG_ALLOWED_ATTR_PREFIXES
                ):
                    raise SkillSecurityError(
                        f"SVG attribute {attr_local!r} on <{tag}> is not allowed.",
                        details={
                            "reason": "svg_forbidden_attribute",
                            "attribute": attr_local,
                            "element": tag,
                            "skill_id": skill_id,
                        },
                    )

            forbidden = _has_forbidden_value(attr_value)
            if forbidden is not None:
                raise SkillSecurityError(
                    f"SVG attribute {attr_local!r} contains forbidden value "
                    f"{forbidden!r}.",
                    details={
                        "reason": "svg_forbidden_value",
                        "attribute": attr_local,
                        "pattern": forbidden,
                        "value_preview": attr_value[:80],
                        "skill_id": skill_id,
                    },
                )

            bad_url = _find_non_local_url_ref(attr_value)
            if bad_url is not None:
                raise SkillSecurityError(
                    f"SVG attribute {attr_local!r} contains a non-local "
                    f"url() reference: {bad_url!r}.",
                    details={
                        "reason": "svg_external_url",
                        "attribute": attr_local,
                        "target_preview": bad_url[:120],
                        "skill_id": skill_id,
                    },
                )

    # 10. Prefix IDs and rewrite matching url(#...) references
    id_prefix = f"peldrun_skill_{skill_id}_"
    id_map: Dict[str, str] = {}

    for elem in root.iter():
        original = elem.attrib.get("id")
        if not isinstance(original, str) or not original:
            continue
        new_id = id_prefix + original
        id_map[original] = new_id
        elem.attrib["id"] = new_id

    if id_map:
        for elem in root.iter():
            for attr_name, attr_value in list(elem.attrib.items()):
                if "url(" not in attr_value.lower():
                    continue

                def _rewrite(match: "re.Match[str]") -> str:
                    target = match.group(1).strip().strip("'\"")
                    if not target.startswith("#"):
                        return match.group(0)  # validated as local earlier
                    old_ref = target[1:]
                    new_ref = id_map.get(old_ref)
                    return f"url(#{new_ref})" if new_ref else match.group(0)

                new_value = _SVG_URL_RE.sub(_rewrite, attr_value)
                if new_value != attr_value:
                    elem.attrib[attr_name] = new_value

    return ET.tostring(root, encoding="unicode")


# ---------------------------------------------------------------------------
# Manifest validation
# ---------------------------------------------------------------------------

def validate_manifest_schema(manifest: Dict[str, Any]) -> None:
    """
    Validate a manifest dict against the canonical JSON Schema.

    Uses ``jsonschema`` when both the library and the schema file are
    available; otherwise falls back to structural validation that covers
    the same required fields and critical patterns.

    Raises:
        SkillManifestError: on any violation. Includes the failing path
            and validator when jsonschema is used.
    """
    schema = _load_schema()

    if _JSONSCHEMA_AVAILABLE and schema is not None:
        try:
            _jsonschema.validate(instance=manifest, schema=schema)
            return
        except _jsonschema.ValidationError as exc:
            raise SkillManifestError(
                f"Manifest schema validation failed: {exc.message}",
                details={
                    "path": list(exc.absolute_path),
                    "validator": exc.validator,
                    "value_preview": str(exc.instance)[:200],
                    "schema_path": list(exc.absolute_schema_path),
                },
            ) from exc
        except _jsonschema.SchemaError as exc:
            # Schema itself is broken — treat as a server misconfiguration,
            # not a user error, but still fail closed.
            raise SkillManifestError(
                f"Bundled skill manifest schema is invalid: {exc.message}",
                details={"reason": "schema_invalid"},
            ) from exc

    _validate_manifest_minimal(manifest)


def _validate_manifest_minimal(manifest: Dict[str, Any]) -> None:
    """
    Structural fallback used when jsonschema or the schema file is absent.

    Enforces the fields and patterns that MUST hold regardless of what
    the schema file says. This is intentionally narrower than the full
    JSON Schema; its job is to fail closed on the obvious problems.
    """
    required = (
        "schema_version",
        "id",
        "version",
        "name",
        "description",
        "execution",
    )
    for key in required:
        if key not in manifest:
            raise SkillManifestError(
                f"Manifest is missing required field: {key!r}.",
                details={"field": key, "reason": "missing_required"},
            )

    schema_version = str(manifest.get("schema_version", ""))
    if not _SEMVER_RE.match(schema_version):
        raise SkillManifestError(
            f"Manifest 'schema_version' must be semver, got {schema_version!r}.",
            details={
                "field": "schema_version",
                "value": schema_version,
                "reason": "invalid_semver",
            },
        )
    if not schema_version.startswith("1."):
        raise SkillManifestError(
            f"Unsupported schema_version major (only '1.x.y' is accepted), "
            f"got {schema_version!r}.",
            details={
                "field": "schema_version",
                "value": schema_version,
                "reason": "unsupported_major",
            },
        )

    skill_id = str(manifest.get("id", ""))
    if not _ID_RE.match(skill_id):
        raise SkillManifestError(
            f"Manifest 'id' must match ^[a-z0-9][a-z0-9_]{{2,63}}$, got {skill_id!r}.",
            details={
                "field": "id",
                "value": skill_id,
                "reason": "invalid_pattern",
            },
        )

    version = str(manifest.get("version", ""))
    if not _SEMVER_RE.match(version):
        raise SkillManifestError(
            f"Manifest 'version' must be semver, got {version!r}.",
            details={
                "field": "version",
                "value": version,
                "reason": "invalid_semver",
            },
        )

    name = manifest.get("name")
    if not isinstance(name, str) or not (1 <= len(name) <= 120):
        raise SkillManifestError(
            "Manifest 'name' must be a non-empty string of 1-120 characters.",
            details={"field": "name", "reason": "invalid_length"},
        )

    description = manifest.get("description")
    if not isinstance(description, str) or not (1 <= len(description) <= 500):
        raise SkillManifestError(
            "Manifest 'description' must be a non-empty string of 1-500 "
            "characters.",
            details={"field": "description", "reason": "invalid_length"},
        )

    execution = manifest.get("execution")
    if not isinstance(execution, dict):
        raise SkillManifestError(
            "Manifest 'execution' must be an object.",
            details={"field": "execution", "reason": "invalid_type"},
        )
    exec_type = execution.get("type")
    if exec_type != "instructions":
        raise SkillManifestError(
            f"Manifest 'execution.type' must be 'instructions' in this "
            f"release, got {exec_type!r}.",
            details={
                "field": "execution.type",
                "value": exec_type,
                "reason": "unsupported_execution_type",
            },
        )
    instructions_file = execution.get("instructions_file")
    if isinstance(instructions_file, str):
        if "/" in instructions_file or "\\" in instructions_file:
            raise SkillManifestError(
                "Manifest 'execution.instructions_file' must be a bare "
                "filename (no directory separators).",
                details={
                    "field": "execution.instructions_file",
                    "value": instructions_file,
                    "reason": "invalid_path",
                },
            )


# ---------------------------------------------------------------------------
# Version compatibility
# ---------------------------------------------------------------------------

def validate_compatibility(
    manifest: Dict[str, Any],
    *,
    running_version: Optional[str] = None,
) -> None:
    """
    Raise SkillValidationError when the manifest's declared compatibility
    window does not include the running PELDRUN version.

    A manifest without a ``compatibility`` block is always accepted.
    Malformed version strings inside the block are treated as author
    errors and rejected; a malformed running version is treated as a
    server misconfiguration and only logged (install is not blocked,
    because the operator cannot fix it from the skill package).
    """
    compat = manifest.get("compatibility")
    if not isinstance(compat, dict):
        return

    min_v = compat.get("peldrun_min_version")
    max_v = compat.get("peldrun_max_version")
    if not min_v and not max_v:
        return

    current_str = running_version or _get_running_peldrun_version()
    if current_str is None:
        logger.debug(
            "Skipping compatibility check: running PELDRUN version is "
            "unknown (peldrun.__version__ is not set)."
        )
        return
    try:
        current = _parse_semver(current_str)
    except ValueError:
        logger.warning(
            "Skipping compatibility check: running PELDRUN version %r is "
            "not valid semver.",
            current_str,
        )
        return

    if min_v:
        try:
            min_tuple = _parse_semver(min_v)
        except ValueError as exc:
            raise SkillValidationError(
                f"Manifest declares invalid 'peldrun_min_version': {min_v!r}.",
                details={
                    "field": "compatibility.peldrun_min_version",
                    "value": min_v,
                },
            ) from exc
        if current < min_tuple:
            raise SkillValidationError(
                f"Skill requires PELDRUN >= {min_v}, but this instance is "
                f"running {current_str}.",
                details={
                    "reason": "incompatible_peldrun_version",
                    "required_min": min_v,
                    "running_version": current_str,
                },
            )

    if max_v:
        try:
            max_tuple = _parse_semver(max_v)
        except ValueError as exc:
            raise SkillValidationError(
                f"Manifest declares invalid 'peldrun_max_version': {max_v!r}.",
                details={
                    "field": "compatibility.peldrun_max_version",
                    "value": max_v,
                },
            ) from exc
        if current > max_tuple:
            raise SkillValidationError(
                f"Skill supports PELDRUN <= {max_v}, but this instance is "
                f"running {current_str}.",
                details={
                    "reason": "incompatible_peldrun_version",
                    "required_max": max_v,
                    "running_version": current_str,
                },
            )


# ---------------------------------------------------------------------------
# High-level entry point
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ParsedSkillPackage:
    """Immutable result of parsing a skill package on disk."""

    manifest: Dict[str, Any]
    instructions: str
    frontmatter: Dict[str, Any]
    sanitized_icon_svg: Optional[str]
    package_dir: Path


def load_skill_package(
    package_dir: Path,
    *,
    running_version: Optional[str] = None,
) -> ParsedSkillPackage:
    """
    Load, validate, and sanitize a skill package from a directory.

    The directory MUST contain:
        * ``manifest.json`` — validated against the JSON Schema.
        * The instructions file declared in
          ``execution.instructions_file`` (defaults to ``SKILL.md``),
          which must start with a ``---`` YAML frontmatter block.

    Optional files (not loaded here):
        * ``icon_svg`` in the manifest (sanitized inline).
        * ``icon`` — a relative SVG path inside the package.
        * ``references/`` — loaded on demand by ``omweb.skills.loader``.

    Raises:
        SkillManifestError, SkillValidationError, SkillSecurityError.
    """
    package_dir = Path(package_dir).resolve()
    if not package_dir.is_dir():
        raise SkillValidationError(
            f"Skill package directory does not exist: {package_dir}",
            details={"package_dir": str(package_dir)},
        )

    # 1. Read and validate manifest.json
    manifest_path = package_dir / MANIFEST_FILENAME
    if not manifest_path.is_file():
        raise SkillManifestError(
            f"Skill package is missing {MANIFEST_FILENAME}.",
            details={"package_dir": str(package_dir)},
        )

    try:
        raw_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SkillManifestError(
            f"{MANIFEST_FILENAME} is not valid JSON: {exc}",
            details={"parse_error": str(exc)[:200]},
        ) from exc
    except OSError as exc:
        raise SkillManifestError(
            f"Failed to read {MANIFEST_FILENAME}: {exc}",
            details={"path": str(manifest_path)},
        ) from exc

    if not isinstance(raw_manifest, dict):
        raise SkillManifestError(
            f"{MANIFEST_FILENAME} must contain a JSON object.",
            details={"type": type(raw_manifest).__name__},
        )

    validate_manifest_schema(raw_manifest)
    validate_compatibility(raw_manifest, running_version=running_version)

    # 2. Resolve the instructions file path safely
    execution_block = raw_manifest.get("execution") or {}
    instructions_file = (
        execution_block.get("instructions_file")
        or DEFAULT_INSTRUCTIONS_FILE
    )
    if (
        not isinstance(instructions_file, str)
        or "/" in instructions_file
        or "\\" in instructions_file
    ):
        raise SkillSecurityError(
            "execution.instructions_file must be a bare filename (no "
            "path separators).",
            details={
                "reason": "instructions_file_has_separator",
                "value": str(instructions_file),
            },
        )

    instructions_path = package_dir / instructions_file
    try:
        resolved_instructions = instructions_path.resolve()
        resolved_instructions.relative_to(package_dir)
    except (OSError, ValueError):
        raise SkillSecurityError(
            "Resolved instructions path escapes the skill package directory.",
            details={
                "reason": "instructions_path_escape",
                "value": instructions_file,
            },
        )

    if not resolved_instructions.is_file():
        raise SkillValidationError(
            f"Skill package is missing {instructions_file}.",
            details={
                "package_dir": str(package_dir),
                "expected_file": instructions_file,
            },
        )

    try:
        raw_instructions = resolved_instructions.read_text(encoding="utf-8")
    except OSError as exc:
        raise SkillValidationError(
            f"Failed to read {instructions_file}: {exc}",
            details={"path": str(resolved_instructions)},
        ) from exc

    # 3. Instructions size cap (schema-provided limit narrows the ceiling)
    limits = raw_manifest.get("limits") or {}
    configured_cap = limits.get("max_instructions_bytes")
    max_bytes = MAX_INSTRUCTIONS_BYTES
    if isinstance(configured_cap, int) and configured_cap > 0:
        max_bytes = min(configured_cap, MAX_INSTRUCTIONS_BYTES)

    body_size = len(raw_instructions.encode("utf-8"))
    if body_size > max_bytes:
        raise SkillValidationError(
            f"{instructions_file} exceeds {max_bytes} bytes "
            f"({body_size} actual).",
            details={
                "reason": "instructions_too_large",
                "size_bytes": body_size,
                "limit": max_bytes,
            },
        )

    frontmatter, body = parse_skill_md(raw_instructions)

    # 4. Sanitize inline icon_svg (if present)
    sanitized_icon_svg: Optional[str] = None
    raw_icon_svg = raw_manifest.get("icon_svg")
    if isinstance(raw_icon_svg, str) and raw_icon_svg.strip():
        sanitized_icon_svg = sanitize_svg(
            raw_icon_svg,
            skill_id=str(raw_manifest["id"]),
        )

    return ParsedSkillPackage(
        manifest=raw_manifest,
        instructions=body,
        frontmatter=frontmatter,
        sanitized_icon_svg=sanitized_icon_svg,
        package_dir=package_dir,
    )


__all__ = [
    "MANIFEST_FILENAME",
    "SKILL_MD_FILENAME",
    "DEFAULT_INSTRUCTIONS_FILE",
    "ParsedSkillPackage",
    "parse_skill_md",
    "sanitize_svg",
    "validate_manifest_schema",
    "validate_compatibility",
    "load_skill_package",
]
