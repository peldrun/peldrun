# backend/peldrun/tools/builtins/web_search.py
"""
PELDRUN Core Web Search Tool.

Executes web searches through SearchProviderFactory while enforcing:
- Optional max_results argument (None when omitted so user settings govern)
- Authoritative global default_max_results and per-provider max_results ceilings
- Per-provider capability limits (max_results_limit) and MAX_RESULTS_HARD_CAP (20)
- Run-scoped Circuit Breaker with full search-configuration signature invalidation
- Privacy-mode query sanitization, deduplication, and structured ToolResult error states

P1 Hardening:
- The final post-execution path explicitly classifies the provider response using
  `response.is_success` and `response.status` (plus actual result count) and never
  records success, resets the circuit breaker, or returns a non-error ToolResult
  when the underlying provider outcome was a failure.
- Distinguishes a valid empty search (search completed, zero matches) from a real
  provider failure (auth error, timeout, upstream error, quota exhaustion, etc.).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import hashlib
import inspect
import json
import logging
from pathlib import Path
import re
import time
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse, urlunparse

from pydantic import BaseModel, Field, field_validator

from peldrun.tools.base import BaseTool
from peldrun.tools.builtins.search_providers.base import (
    ProviderStatus,
    SearchProviderError,
    SearchResponse,
    SearchResultItem,
)
from peldrun.tools.builtins.search_providers.factory import (
    DEFAULT_MAX_RESULTS,
    MAX_RESULTS_HARD_CAP,
    MIN_RESULTS_LIMIT,
    SearchProviderFactory,
)
from peldrun.tools.contract import ToolResult

logger = logging.getLogger("peldrun.tools.web_search")

# Default circuit-breaker thresholds scoped per execution context + config signature
_CB_FAILURE_THRESHOLD: int = 3
_CB_COOLDOWN_SECONDS: float = 60.0

_EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
_BEARER_KEY_RE = re.compile(r"\b(?:sk-[A-Za-z0-9_-]{16,}|tvly-[A-Za-z0-9_-]{12,}|AIza[0-9A-Za-z_-]{20,})\b")

# Detects image search intent when search_type='auto' (default).
# Matches realistic user prompts including verbose direct-chat prompts
# such as "Search the web for images of the Eiffel Tower at sunset and
# show me the image URLs". Technical contexts (image processing, image
# compression, image recognition, ...) are excluded by requiring the
# image noun to appear in a search/request frame rather than simply
# being present anywhere in the query.
_IMAGE_QUERY_KEYWORDS_RE = re.compile(
    r"(?:"
    # "images of X" / "photos of X" / "pictures of X" / etc.
    r"\b(?:images?|pictures?|photos?|pics?|photographs?)"
    r"\s+(?:of|for|about|on|showing|related\s+to)\b"
    r"|"
    # "... for images" / "... for some photos" / "... for the pictures"
    r"\bfor\s+(?:some\s+|the\s+)?"
    r"(?:images?|pictures?|photos?|pics?|photographs?)\b"
    r"|"
    # "image search" / "photo search" / "picture search"
    r"\b(?:image|photo|picture)\s+search\b"
    r"|"
    # "show me images" / "find photos" / "get pictures" / "download images"
    r"\b(?:show|find|get|fetch|grab|download|pull|locate|search\s+for)"
    r"\s+(?:me\s+)?(?:some\s+|the\s+)?"
    r"(?:images?|pictures?|photos?|pics?|photographs?)\b"
    r"|"
    # "image URLs" / "photo links" / "picture files"
    r"\b(?:images?|pictures?|photos?)\s+(?:URLs?|links?|files?|sources?)\b"
    r"|"
    # Ends with an image noun (e.g. "Eiffel Tower sunset images")
    r"\b(?:images?|pictures?|photos?|pics?|photographs?)\s*[?.!]*\s*$"
    r")",
    re.IGNORECASE,
)

_IMAGE_SEARCH_TYPE_VALUES = frozenset(
    {"images", "image", "photo", "photos", "picture", "pictures", "pic", "pics"}
)


def _detect_image_intent_from_query(query: str) -> bool:
    """
    Return True when the query expresses an image-search request.

    Used only when search_type='auto' so web_search can serve image
    requests even when the dedicated image_search tool is not
    registered on the agent. Explicit search_type='text' always wins.
    """
    if not query or not isinstance(query, str):
        return False
    return bool(_IMAGE_QUERY_KEYWORDS_RE.search(query.strip()))


def _extract_image_search_topic(prompt: str) -> str:
    """
    Extract a clean image search topic from a verbose user prompt.

    Strategy (in order):
      1. Locate an explicit "images of X" / "photos of X" / "pictures of X"
         / "image search for X" pattern in the ORIGINAL prompt and capture
         X directly. This is resilient to verbose requests such as
         "Search the internet and show me images of Saint Petersburg" —
         where a naive strip-based approach would lose the subject after
         "and show me images of".
      2. If no explicit pattern exists, fall back to stripping leading
         search verbs and trailing filler words (image nouns, "from the
         internet", "online", ...) one at a time.
      3. Reject meta-only results ("the internet", "the web", "online")
         and fall back to the original prompt so we never send the image
         API an empty or meaningless topic.

    Examples
    --------
    "Search the internet and show me images of Saint Petersburg"
        -> "Saint Petersburg"
    "Search the web for images of the Eiffel Tower at sunset and show me the image URLs."
        -> "the Eiffel Tower at sunset"
    "Find pictures of cats"                 -> "cats"
    "Get image URLs for Mount Fuji"         -> "Mount Fuji"
    "image search for sunsets"              -> "sunsets"
    "Eiffel Tower sunset images"            -> "Eiffel Tower sunset"
    """
    text = (prompt or "").strip()
    if not text:
        return text

    # ------------------------------------------------------------------
    # 1. Primary strategy: extract "images of X" / "image search for X"
    #    from the ORIGINAL prompt. This avoids losing the subject when
    #    trailing instructions such as "and show me images of X" contain
    #    the actual topic.
    # ------------------------------------------------------------------
    primary = re.search(
        r"(?:"
        r"\b(?:images?|pictures?|photos?|pics?|photographs?)"
        r"(?:\s+(?:URLs?|links?|sources?|files?))?"
        r"\s+(?:of|for|about|on|showing|related\s+to|from)\s+"
        r"|"
        r"\b(?:image|photo|picture)\s+search\s+(?:for\s+)?"
        r")"
        r"(.+?)"
        r"(?:\s+(?:and|,)\s+"
        r"(?:show|give|send|return|display|provide|list|include|fetch)\b.*)?"
        r"(?:\s+from\s+(?:the\s+)?(?:internet|web|online|net)\b.*)?"
        r"\s*[?.!]*\s*$",
        text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if primary:
        topic = primary.group(1).strip(" .,!?;:")
        # Strip any residual "from/on/at the internet/web/online" that
        # survived the capture group.
        topic = re.sub(
            r"\s+(?:from|on|via|in|at)\s+(?:the\s+)?"
            r"(?:internet|web|net|online)\s*[?.!]*\s*$",
            "",
            topic,
            flags=re.IGNORECASE,
        )
        # Strip any residual "and ..." clause that leaked into the capture.
        topic = re.sub(
            r"\s+(?:and|,)\s+.*$",
            "",
            topic,
            flags=re.IGNORECASE,
        )
        # Strip bare trailing "internet/web/net/online" that survived
        # the capture group without a preposition (e.g. "cats online",
        # "puppies online").
        topic = re.sub(
            r"\s+(?:the\s+)?(?:internet|web|net|online)\s*[?.!]*\s*$",
            "",
            topic,
            flags=re.IGNORECASE,
        )
        topic = topic.strip(" .,!?;:")
        if topic and topic.lower() not in {
            "", "the internet", "internet", "the web", "web",
            "online", "the net", "net",
        }:
            return topic

    # ------------------------------------------------------------------
    # 2. Secondary strategy: strip leading verbs + trailing fillers.
    # ------------------------------------------------------------------
    text = re.sub(
        r"^(?:please\s+|could\s+you\s+|can\s+you\s+|would\s+you\s+|"
        r"i\s+want\s+to\s+)?"
        r"(?:search|find|get|show|fetch|grab|download|pull|locate|"
        r"look\s+up|look\s+for|see)\b"
        r"(?:\s+(?:the\s+web|the\s+internet|the\s+net|online|web|internet))?"
        r"(?:\s+for)?"
        r"(?:\s+me)?"
        r"\s+",
        "",
        text,
        flags=re.IGNORECASE,
    )

    # Strip "some/the images of ..." header if not already consumed.
    text = re.sub(
        r"^(?:some\s+|the\s+)?"
        r"(?:images?|pictures?|photos?|pics?|photographs?)"
        r"(?:\s+(?:of|for|about|on|from|showing|related\s+to))?\s+",
        "",
        text,
        flags=re.IGNORECASE,
    )

    # Strip trailing "from the internet" / "online" / "web" etc.
    text = re.sub(
        r"\s+(?:(?:from|on|via|in|at)\s+)?(?:the\s+)?"
        r"(?:internet|web|net|online)\s*[?.!]*\s*$",
        "",
        text,
        flags=re.IGNORECASE,
    )

    # Strip trailing "images"/"pictures"/"photos" nouns.
    text = re.sub(
        r"\s+(?:images?|pictures?|photos?|pics?|photographs?)\s*[?.!]*\s*$",
        "",
        text,
        flags=re.IGNORECASE,
    )

    # Strip trailing filler particles.
    text = re.sub(
        r"\s+(?:for|of|about|on|from)\s*$", "", text, flags=re.IGNORECASE,
    )

    text = text.strip(" .,!?;:")

    # ------------------------------------------------------------------
    # 3. Validate: never return a meta-only topic.
    # ------------------------------------------------------------------
    meta_only = {
        "", "the internet", "internet", "the web", "web",
        "online", "the net", "net", "the world wide web",
    }
    if text.lower() in meta_only:
        return prompt.strip()

    return text or prompt.strip()


# Provider statuses that represent a genuine non-success failure (as opposed to a
# valid empty search result).
_FAILURE_STATUS_VALUES: frozenset = frozenset(
    {
        ProviderStatus.CONFIGURATION_ERROR.value,
        ProviderStatus.AUTHENTICATION_ERROR.value,
        ProviderStatus.PERMISSION_ERROR.value,
        ProviderStatus.RATE_LIMITED.value,
        ProviderStatus.QUOTA_EXHAUSTED.value,
        ProviderStatus.TIMEOUT.value,
        ProviderStatus.UPSTREAM_ERROR.value,
        ProviderStatus.INVALID_RESPONSE.value,
        ProviderStatus.DISABLED.value,
    }
)


@dataclass
class _CircuitBreakerEntry:
    consecutive_failures: int = 0
    opened_at: Optional[float] = None
    last_error: Optional[str] = None
    config_signature: str = ""


class WebSearchArgs(BaseModel):
    """Canonical argument schema for the Core web_search tool."""

    query: str = Field(
        ...,
        min_length=1,
        description="Search query string to look up on the web.",
    )
    max_results: Optional[int] = Field(
        default=None,
        ge=MIN_RESULTS_LIMIT,
        le=MAX_RESULTS_HARD_CAP,
        description=(
            "Optional desired maximum number of results. Omit this argument to use "
            "PELDRUN Search Settings. The configured provider ceiling and provider "
            "capability maximum are always enforced."
        ),
    )
    search_type: Optional[str] = Field(
        default="auto",
        description=(
            "Type of search. 'auto' (default) detects image intent from the "
            "query. 'images' forces image search and returns direct image URLs. "
            "'text' forces web-page search."
        ),
    )

    @field_validator("query", mode="before")
    @classmethod
    def _validate_query(cls, value: Any) -> str:
        if not isinstance(value, str):
            raise ValueError("Search query must be a string.")
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("Search query cannot be empty or whitespace.")
        return cleaned

    @field_validator("max_results", mode="before")
    @classmethod
    def _validate_max_results(cls, value: Any) -> Optional[int]:
        if value is None:
            return None
        if isinstance(value, bool):
            raise ValueError("max_results must be an integer between 1 and 20, not a boolean.")
        if isinstance(value, int):
            return value
        if isinstance(value, float):
            if not value.is_integer():
                raise ValueError("max_results must be a whole integer, not a fractional float.")
            return int(value)
        if isinstance(value, str):
            stripped = value.strip()
            if not stripped:
                return None
            if (stripped[0] in "+-" and stripped[1:].isdigit()) or stripped.isdigit():
                return int(stripped)
            raise ValueError(f"max_results must be a valid integer between 1 and 20, got {value!r}.")
        raise ValueError(f"max_results must be an integer between 1 and 20, got {type(value).__name__}.")


def _compute_search_config_signature(search_config: Dict[str, Any]) -> str:
    """
    Compute a deterministic SHA-256 signature across all relevant [search] settings,
    including primary/fallback chain, global limits, timeouts, and per-provider
    credentials/endpoints/limits so any configuration update invalidates stale breaker state.
    """
    try:
        serialized = json.dumps(search_config or {}, sort_keys=True, default=str)
    except Exception:
        serialized = repr(sorted((search_config or {}).items()))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()[:24]


def _sanitize_query_for_privacy(query: str, privacy_mode: bool) -> str:
    """Redact accidental API keys or email addresses when privacy_mode is active."""
    cleaned = query.strip()
    if not privacy_mode:
        return cleaned
    cleaned = _EMAIL_RE.sub("[REDACTED_EMAIL]", cleaned)
    cleaned = _BEARER_KEY_RE.sub("[REDACTED_KEY]", cleaned)
    return cleaned


def _normalize_url_for_dedup(url: str) -> str:
    """Normalize a result URL for deterministic deduplication."""
    raw = (url or "").strip()
    if not raw:
        return ""
    try:
        parsed = urlparse(raw)
        scheme = (parsed.scheme or "https").lower()
        netloc = parsed.netloc.lower()
        path = parsed.path.rstrip("/") or "/"
        return urlunparse((scheme, netloc, path, "", parsed.query, ""))
    except Exception:
        return raw.lower().rstrip("/")


class WebSearchTool(BaseTool):
    """
    Autonomous agent tool for executing web searches via SearchProviderFactory.
    Preserves omitted max_results as None so user-configured global and per-provider
    settings in config.toml govern execution.
    """

    name: str = "web_search"
    description: str = (
        "Search the web for up-to-date information, documentation, news, and factual verification. "
        "Omit max_results to use PELDRUN Search Settings."
    )
    args_schema: type[BaseModel] = WebSearchArgs

    # Scoped circuit breaker states keyed by (execution_scope_key, config_signature)
    _circuit_breakers: Dict[Tuple[str, str], _CircuitBreakerEntry] = {}

    def __init__(
        self,
        workspace_root: Optional[str] = None,
        search_config: Optional[Dict[str, Any]] = None,
        run_id: Optional[str] = None,
        job_id: Optional[str] = None,
        session_id: Optional[str] = None,
        chat_id: Optional[str] = None,
        project_id: Optional[str] = None,
        **kwargs: Any,
    ) -> None:
        try:
            super().__init__(workspace_root=workspace_root, **kwargs)
        except TypeError:
            try:
                super().__init__()
            except Exception:
                pass

        self.workspace_root = workspace_root
        self._explicit_search_config = search_config
        self.run_id = run_id
        self.job_id = job_id
        self.session_id = session_id
        self.chat_id = chat_id
        self.project_id = project_id
        self._instance_scope_id = f"tool_instance:{id(self)}"

    @classmethod
    def reset_circuit_breakers(cls) -> None:
        """Clear all circuit-breaker entries (used by test suites and config reload hooks)."""
        cls._circuit_breakers.clear()

    def _resolve_execution_scope_key(
        self,
        run_id: Optional[str] = None,
        job_id: Optional[str] = None,
        session_id: Optional[str] = None,
        chat_id: Optional[str] = None,
    ) -> str:
        """
        Determine the isolation scope for circuit-breaker tracking.
        Uses explicit run/job/session/chat identifiers when available, falling back to
        the per-instance scope so separate runs never poison each other via a shared global key.
        """
        effective_run = run_id or self.run_id
        if effective_run:
            return f"run:{effective_run}"

        effective_job = job_id or self.job_id
        if effective_job:
            return f"job:{effective_job}"

        effective_session = session_id or self.session_id
        if effective_session:
            return f"session:{effective_session}"

        effective_chat = chat_id or self.chat_id
        if effective_chat:
            return f"chat:{effective_chat}"

        if self.workspace_root:
            return f"workspace:{self.workspace_root}:{self._instance_scope_id}"

        return self._instance_scope_id

    def _load_authoritative_search_config(
        self,
        override_config: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Load the authoritative [search] section from override_config, instance config,
        or config/config.toml on disk.
        """
        if isinstance(override_config, dict):
            return SearchProviderFactory._unwrap_search_section(override_config)

        if isinstance(self._explicit_search_config, dict):
            return SearchProviderFactory._unwrap_search_section(self._explicit_search_config)

        # Attempt canonical omweb config loader if available in process
        try:
            from omweb.config import load_config  # type: ignore[import-not-found]

            full_cfg = load_config()
            if isinstance(full_cfg, dict):
                return SearchProviderFactory._unwrap_search_section(full_cfg)
        except Exception:
            pass

        # Fallback: read config/config.toml directly relative to repository root
        candidate_paths = [
            Path.cwd() / "config" / "config.toml",
            Path.cwd().parent / "config" / "config.toml",
            Path(__file__).resolve().parents[4] / "config" / "config.toml",
        ]
        for path in candidate_paths:
            if path.is_file():
                try:
                    import tomllib  # Python 3.11+

                    with path.open("rb") as f:
                        data = tomllib.load(f)
                    if isinstance(data, dict):
                        return SearchProviderFactory._unwrap_search_section(data)
                except Exception as exc:
                    logger.debug("Failed reading search config from %s: %s", path, exc)

        return {
            "engine": "DuckDuckGo",
            "fallback_engines": [],
            "enabled": True,
            "default_max_results": DEFAULT_MAX_RESULTS,
            "lang": "en",
            "country": "us",
            "total_budget_seconds": 22.0,
            "max_retries": 0,
            "retry_delay": 1.0,
            "privacy_mode": False,
        }

    @staticmethod
    def _validate_optional_requested_count(raw_max_results: Any) -> Optional[int]:
        """
        Validate caller-supplied max_results without substituting a default of 5 when None.
        Raises ValueError on invalid types or out-of-range values (<= 0).
        Clamps positive integers above MAX_RESULTS_HARD_CAP to MAX_RESULTS_HARD_CAP (20).
        """
        if raw_max_results is None:
            return None

        if isinstance(raw_max_results, bool):
            raise ValueError("max_results must be an integer between 1 and 20, got boolean.")

        if isinstance(raw_max_results, int):
            parsed = raw_max_results
        elif isinstance(raw_max_results, float):
            if not raw_max_results.is_integer():
                raise ValueError(f"max_results must be a whole integer, got {raw_max_results}.")
            parsed = int(raw_max_results)
        elif isinstance(raw_max_results, str):
            stripped = raw_max_results.strip()
            if not stripped:
                return None
            if (stripped[0] in "+-" and stripped[1:].isdigit()) or stripped.isdigit():
                parsed = int(stripped)
            else:
                raise ValueError(f"max_results must be a valid integer, got {raw_max_results!r}.")
        else:
            raise ValueError(f"max_results must be an integer, got {type(raw_max_results).__name__}.")

        if parsed < MIN_RESULTS_LIMIT:
            raise ValueError(f"max_results must be between {MIN_RESULTS_LIMIT} and {MAX_RESULTS_HARD_CAP}, got {parsed}.")

        return min(parsed, MAX_RESULTS_HARD_CAP)

    @classmethod
    def _check_circuit_breaker(
        cls,
        scope_key: str,
        config_sig: str,
        cooldown_seconds: float = _CB_COOLDOWN_SECONDS,
    ) -> Optional[str]:
        """
        Return an actionable error message if the circuit breaker for (scope_key, config_sig)
        is currently open; otherwise return None.
        """
        key = (scope_key, config_sig)
        entry = cls._circuit_breakers.get(key)
        if not entry or entry.opened_at is None:
            return None

        elapsed = time.monotonic() - entry.opened_at
        if elapsed >= cooldown_seconds:
            # Cooldown elapsed: reset breaker to allow half-open retry
            cls._circuit_breakers.pop(key, None)
            return None

        remaining = max(1, int(round(cooldown_seconds - elapsed)))
        return (
            f"Search circuit breaker is open for the current execution after "
            f"{entry.consecutive_failures} consecutive failures "
            f"(retry available in {remaining}s). Last error: {entry.last_error or 'provider failure'}."
        )

    @classmethod
    def _record_breaker_outcome(
        cls,
        scope_key: str,
        config_sig: str,
        *,
        success: bool,
        error_message: Optional[str] = None,
        failure_threshold: int = _CB_FAILURE_THRESHOLD,
    ) -> None:
        """Update circuit-breaker state for the given execution scope and config signature."""
        key = (scope_key, config_sig)
        if success:
            cls._circuit_breakers.pop(key, None)
            return

        entry = cls._circuit_breakers.get(key)
        if entry is None:
            entry = _CircuitBreakerEntry(config_signature=config_sig)
            cls._circuit_breakers[key] = entry

        entry.consecutive_failures += 1
        entry.last_error = error_message
        if entry.consecutive_failures >= max(1, failure_threshold):
            entry.opened_at = time.monotonic()

    @staticmethod
    def _deduplicate_and_cap_results(
        items: List[SearchResultItem],
        effective_max_results: int,
    ) -> List[SearchResultItem]:
        """Deduplicate results by normalized URL while strictly capping at effective_max_results."""
        deduped: List[SearchResultItem] = []
        seen_urls: set[str] = set()

        for item in items or []:
            norm_url = _normalize_url_for_dedup(getattr(item, "url", ""))
            if norm_url and norm_url in seen_urls:
                continue
            if norm_url:
                seen_urls.add(norm_url)
            deduped.append(item)
            if len(deduped) >= effective_max_results:
                break

        return deduped[:effective_max_results]

    @staticmethod
    def _escape_md_link_text(text: str) -> str:
        """Escape characters that would break Markdown link syntax in link text."""
        return (text or "").replace("\\", "\\\\").replace("[", "\\[").replace("]", "\\]")

    @staticmethod
    def _escape_md_link_url(url: str) -> str:
        """Escape characters that would break Markdown link syntax inside parentheses."""
        return (url or "").replace(" ", "%20").replace("(", "%28").replace(")", "%29")

    @classmethod
    def _format_search_output(
        cls,
        query: str,
        response: SearchResponse,
        items: List[SearchResultItem],
        effective_max_results: int,
    ) -> str:
        """
        Render a SearchResponse into structured Markdown suitable for agent observation.

        Output contract:
        - Top-level heading with a bullet metadata block (query / provider / counts).
        - Optional blockquoted "Direct Answer" section when the provider supplies one.
        - Numbered result list where each entry uses a bold Markdown link, a metadata
          line (URL + optional publish date), and a blockquoted snippet.
        - Link text and URL are escaped so brackets/parentheses cannot corrupt the
          Markdown structure. `> snippet` is preferred over indented text to avoid
          accidental code-block interpretation in strict Markdown renderers.
        - An explicit italic line signals a valid empty search (not an error).
        """
        provider_name = getattr(response, "provider", None) or "SearchProvider"
        answer_box = getattr(response, "answer", None)

        lines: List[str] = [
            "## Web Search Results",
            "",
            f"- **Query:** `{query}`",
            f"- **Provider:** `{provider_name}`",
            f"- **Returned:** {len(items)} / {effective_max_results}",
        ]

        if answer_box and str(answer_box).strip():
            lines += [
                "",
                "### Direct Answer",
                "",
                f"> {str(answer_box).strip()}",
            ]

        if not items:
            lines += [
                "",
                "_No matching web search results were returned by the provider._",
            ]
            return "\n".join(lines)

        lines += ["", "### Results", ""]

        for idx, item in enumerate(items[:effective_max_results], start=1):
            title = (getattr(item, "title", None) or "Untitled Result").strip()
            url = (getattr(item, "url", None) or "").strip()
            snippet = (
                getattr(item, "snippet", None)
                or getattr(item, "content", None)
                or ""
            ).strip()
            published = getattr(item, "published_date", None)

            safe_title = cls._escape_md_link_text(title)
            if url:
                lines.append(
                    f"{idx}. **[{safe_title}]({cls._escape_md_link_url(url)})**"
                )
            else:
                lines.append(f"{idx}. **{safe_title}**")

            meta_parts: List[str] = []
            if url:
                meta_parts.append(f"`{url}`")
            if published:
                meta_parts.append(f"Published: {published}")
            if meta_parts:
                lines.append(f"   - {' | '.join(meta_parts)}")

            if snippet:
                lines.append(f"   > {snippet}")

        return "\n".join(lines)

    @staticmethod
    def _classify_provider_response(
        response: SearchResponse,
    ) -> Tuple[str, str, int]:
        """
        Classify the actual provider response as one of:
            - "success": usable results were returned.
            - "empty"  : search completed but matched zero results (valid outcome).
            - "failure": the underlying provider reported a genuine error status.

        Returns: (outcome_class, provider_status_value, result_count)
        """
        status_obj = getattr(response, "status", None)
        status_value = (
            status_obj.value if hasattr(status_obj, "value")
            else str(status_obj or "").strip().lower()
        )
        response_is_success = bool(getattr(response, "is_success", False))
        raw_results = list(getattr(response, "results", []) or [])
        result_count = len(raw_results)

        # Trust actual results if present, regardless of the is_success flag.
        if result_count > 0:
            return ("success", status_value, result_count)

        # No results: distinguish valid empty from true failure.
        if status_value == ProviderStatus.EMPTY.value:
            return ("empty", status_value, 0)

        if status_value in _FAILURE_STATUS_VALUES:
            return ("failure", status_value, 0)

        # Contract violations: success flag/status but zero results → treat as empty
        # (the search itself completed; it just found nothing).
        if response_is_success or status_value == ProviderStatus.SUCCESS.value:
            return ("empty", status_value or ProviderStatus.EMPTY.value, 0)

        # Fallback: no results and no clear status → genuine failure.
        return ("failure", status_value or "unknown", 0)

    async def _record_search_usage_safely(
        self,
        *,
        query: str,
        provider_name: str,
        status: str,
        latency_ms: float,
        results_count: int,
        requested_max_results: Optional[int],
        effective_max_results: Optional[int],
        cost_usd: float = 0.0,
        error_message: Optional[str] = None,
        run_id: Optional[str] = None,
        job_id: Optional[str] = None,
        chat_id: Optional[str] = None,
        project_id: Optional[str] = None,
    ) -> None:
        """Record search invocation telemetry into search_usage_store if available."""
        try:
            from peldrun.runtime import search_usage_store as sus_mod

            store_getter = getattr(sus_mod, "get_search_usage_store", None)
            if not callable(store_getter):
                return
            store = store_getter()

            record_cls = getattr(sus_mod, "SearchInvocationRecord", None) or getattr(
                sus_mod, "SearchUsageRecord", None
            )
            record_fn = (
                getattr(store, "record_search", None)
                or getattr(store, "record_invocation", None)
                or getattr(store, "record", None)
            )
            if not callable(record_fn):
                return

            payload = {
                "run_id": run_id or self.run_id,
                "job_id": job_id or self.job_id,
                "chat_id": chat_id or self.chat_id,
                "project_id": project_id or self.project_id or "default_project",
                "provider": provider_name,
                "query": query,
                "status": status,
                "results_count": results_count,
                "requested_max_results": requested_max_results,
                "effective_max_results": effective_max_results,
                "latency_ms": round(latency_ms, 2),
                "cost_usd": cost_usd,
                "error_message": error_message,
                "timestamp": time.time(),
            }

            if record_cls is not None:
                try:
                    if hasattr(record_cls, "model_fields"):
                        accepted = set(record_cls.model_fields.keys())
                    else:
                        accepted = set(inspect.signature(record_cls).parameters.keys())
                    filtered = {k: v for k, v in payload.items() if k in accepted}
                    record_obj = record_cls(**filtered)
                    res = record_fn(record_obj)
                    if inspect.isawaitable(res):
                        await res
                    return
                except Exception:
                    pass

            sig = inspect.signature(record_fn)
            accepts_kw = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values())
            filtered_kw = payload if accepts_kw else {k: v for k, v in payload.items() if k in sig.parameters}
            res = record_fn(**filtered_kw)
            if inspect.isawaitable(res):
                await res
        except Exception as exc:
            logger.debug("Search usage telemetry recording notice: %s", exc)

    async def _arun(
        self,
        query: str,
        max_results: Optional[int] = None,
        search_type: str = "auto",
        *,
        search_config: Optional[Dict[str, Any]] = None,
        override_provider: Optional[str] = None,
        run_id: Optional[str] = None,
        job_id: Optional[str] = None,
        session_id: Optional[str] = None,
        chat_id: Optional[str] = None,
        project_id: Optional[str] = None,
        **kwargs: Any,
    ) -> ToolResult:
        """
        Execute the search tool asynchronously and return a structured ToolResult.
        Never masks errors as plain successful strings and never substitutes 5 before
        consulting the authoritative search configuration in SearchProviderFactory.
        """
        start_ts = time.perf_counter()

        # ------------------------------------------------------------------
        # Resolve effective search mode.
        #
        #   "images"          -> explicit image search
        #   "text"            -> explicit web-page search (respects caller)
        #   "auto" (default)  -> detect image intent from the query keyword
        #
        # This routing runs inside web_search itself and imports
        # ImageSearchTool directly, so it works even when the dedicated
        # image_search tool is not registered on the agent.
        # ------------------------------------------------------------------
        effective_search_type = str(search_type or "auto").strip().lower()

        if effective_search_type in _IMAGE_SEARCH_TYPE_VALUES:
            use_image_search = True
            routing_reason = f"explicit search_type='{effective_search_type}'"
        elif effective_search_type == "text":
            use_image_search = False
            routing_reason = "explicit search_type='text'"
        elif effective_search_type == "auto":
            use_image_search = _detect_image_intent_from_query(query or "")
            routing_reason = (
                "auto-detected image keyword in query"
                if use_image_search
                else "no image keyword detected in query"
            )
        else:
            # Unknown value: default to text search (safe).
            use_image_search = False
            routing_reason = f"unknown search_type='{effective_search_type}', defaulting to text"

        if use_image_search:
            # Strip search verbs, image nouns, and trailing instructions
            # from the verbose user prompt so the image API receives a
            # clean topic instead of a full sentence.
            image_topic = _extract_image_search_topic(query or "")
            logger.info(
                "web_search routing to image search (%s) "
                "topic=%r (original_query=%r)",
                routing_reason,
                image_topic,
                query,
            )
            print(
                f"[WEB_SEARCH] Routing to image_search ({routing_reason}) "
                f"topic={image_topic!r}",
                flush=True,
            )
            from peldrun.tools.builtins.image_search import ImageSearchTool
            img_tool = ImageSearchTool(
                workspace_root=self.workspace_root,
                search_config=search_config,
            )
            return await img_tool._arun(
                query=image_topic,
                max_results=max_results,
            )
        elif effective_search_type == "auto":
            logger.debug(
                "web_search staying on text search (%s) for query %r",
                routing_reason,
                query,
            )

        # 1. Validate query
        if not isinstance(query, str) or not query.strip():
            return ToolResult(
                output="Search Error: Query cannot be empty.",
                exit_code=1,
                is_error=True,
                metadata={
                    "error_code": "EMPTY_SEARCH_QUERY",
                    "requested_max_results": max_results,
                },
            )

        # 2. Validate optional explicit max_results while preserving None when omitted
        try:
            explicit_requested_count = self._validate_optional_requested_count(max_results)
        except ValueError as val_err:
            return ToolResult(
                output=f"Search Error: Invalid max_results argument: {val_err}",
                exit_code=1,
                is_error=True,
                metadata={
                    "error_code": "INVALID_MAX_RESULTS_ARGUMENT",
                    "requested_max_results": max_results,
                },
            )

        # 3. Load and validate authoritative search configuration
        cfg = self._load_authoritative_search_config(search_config)
        if cfg.get("enabled") is False:
            return ToolResult(
                output="Search Error: External web search is disabled in PELDRUN Search Settings.",
                exit_code=1,
                is_error=True,
                metadata={
                    "error_code": "SEARCH_DISABLED",
                    "requested_max_results": explicit_requested_count,
                },
            )

        try:
            SearchProviderFactory.get_global_default_max_results(cfg, strict=True)
            for candidate_key in SearchProviderFactory.build_execution_chain(
                cfg, override_provider=override_provider
            ):
                SearchProviderFactory.get_configured_provider_max_results(
                    candidate_key,
                    cfg,
                    strict=True,
                )
        except ValueError as cfg_err:
            return ToolResult(
                output=f"Search Configuration Error: {cfg_err}",
                exit_code=1,
                is_error=True,
                metadata={
                    "error_code": "INVALID_SEARCH_CONFIGURATION",
                    "requested_max_results": explicit_requested_count,
                },
            )

        # 4. Check run-scoped, config-signature-bound Circuit Breaker
        scope_key = self._resolve_execution_scope_key(
            run_id=run_id,
            job_id=job_id,
            session_id=session_id,
            chat_id=chat_id,
        )
        config_sig = _compute_search_config_signature(cfg)
        breaker_msg = self._check_circuit_breaker(scope_key, config_sig)
        if breaker_msg:
            return ToolResult(
                output=f"Search Error: {breaker_msg}",
                exit_code=1,
                is_error=True,
                metadata={
                    "error_code": "SEARCH_CIRCUIT_BREAKER_OPEN",
                    "execution_scope": scope_key,
                    "config_signature": config_sig,
                    "requested_max_results": explicit_requested_count,
                },
            )

        # 5. Apply privacy sanitization if enabled
        privacy_mode = bool(cfg.get("privacy_mode", False))
        sanitized_query = _sanitize_query_for_privacy(query, privacy_mode=privacy_mode)

        # --- Diagnostic console output: search invocation ---
        try:
            enabled_chain = SearchProviderFactory.build_execution_chain(
                cfg, override_provider=override_provider
            )
        except Exception:
            enabled_chain = [str(cfg.get("engine") or "DuckDuckGo")]

        print(
            "\n"
            "+==================================================================\n"
            "| [WEB_SEARCH] Invocation\n"
            "+==================================================================\n"
            f"| Query          : {sanitized_query!r}\n"
            f"| Requested max  : {explicit_requested_count if explicit_requested_count is not None else '(from settings)'}\n"
            f"| Default max    : {cfg.get('default_max_results')}\n"
            f"| Primary engine : {cfg.get('engine')}\n"
            f"| Fallback chain : {cfg.get('fallback_engines') or []}\n"
            f"| Exec chain     : {' -> '.join(enabled_chain)}\n"
            f"| Privacy mode   : {cfg.get('privacy_mode', False)}\n"
            f"| Override prov  : {override_provider or '(none)'}\n"
            "+==================================================================",
            flush=True,
        )

        # 6. Delegate execution to SearchProviderFactory (computes per-provider effective_max_results)
        try:
            response = await SearchProviderFactory.search(
                query=sanitized_query,
                max_results=explicit_requested_count,
                search_config=cfg,
                override_provider=override_provider,
                **kwargs,
            )
        except Exception as exc:
            latency_ms = (time.perf_counter() - start_ts) * 1000.0
            err_str = str(exc) or exc.__class__.__name__
            print(
                "+==================================================================\n"
                "| [WEB_SEARCH] FAILED\n"
                "+==================================================================\n"
                f"| Query          : {sanitized_query!r}\n"
                f"| Error          : {err_str}\n"
                f"| Latency (ms)   : {round(latency_ms, 2)}\n"
                "+==================================================================\n",
                flush=True,
            )
            self._record_breaker_outcome(
                scope_key,
                config_sig,
                success=False,
                error_message=err_str,
            )
            primary_prov = str(override_provider or cfg.get("engine") or "DuckDuckGo")
            await self._record_search_usage_safely(
                query=sanitized_query,
                provider_name=primary_prov,
                status="failed",
                latency_ms=latency_ms,
                results_count=0,
                requested_max_results=explicit_requested_count,
                effective_max_results=None,
                error_message=err_str,
                run_id=run_id,
                job_id=job_id,
                chat_id=chat_id,
                project_id=project_id,
            )
            return ToolResult(
                output=f"Search Error: {err_str}",
                exit_code=1,
                is_error=True,
                metadata={
                    "error_code": "SEARCH_PROVIDER_FAILURE",
                    "provider": primary_prov,
                    "execution_scope": scope_key,
                    "config_signature": config_sig,
                    "requested_max_results": explicit_requested_count,
                    "latency_ms": round(latency_ms, 2),
                },
            )

        # ------------------------------------------------------------------
        # 7. Classify the ACTUAL provider outcome using response.is_success
        #    and response.status before recording any success.
        # ------------------------------------------------------------------
        latency_ms = (time.perf_counter() - start_ts) * 1000.0
        outcome_class, provider_status_value, raw_result_count = self._classify_provider_response(response)

        resp_meta: Dict[str, Any] = (
            dict(response.metadata)
            if hasattr(response, "metadata") and isinstance(getattr(response, "metadata"), dict)
            else {}
        )
        resolved_provider = str(
            getattr(response, "provider", None)
            or resp_meta.get("provider")
            or override_provider
            or cfg.get("engine")
            or "DuckDuckGo"
        )

        # ------------------------------------------------------------------
        # 7a. Genuine failure branch: propagate error, trip breaker, no false success.
        # ------------------------------------------------------------------
        if outcome_class == "failure":
            err_msg = (
                str(getattr(response, "error_message", None) or "").strip()
                or f"Provider '{resolved_provider}' returned status '{provider_status_value or 'unknown'}'."
            )
            err_code = (
                str(getattr(response, "error_code", None) or "").strip()
                or f"SEARCH_PROVIDER_{provider_status_value.upper() or 'FAILURE'}"
            )

            print(
                "+==================================================================\n"
                "| [WEB_SEARCH] FAILED (provider returned failure status)\n"
                "+==================================================================\n"
                f"| Query           : {sanitized_query!r}\n"
                f"| Provider        : {resolved_provider}\n"
                f"| Provider status : {provider_status_value or 'unknown'}\n"
                f"| Error           : {err_msg}\n"
                f"| Latency (ms)    : {round(latency_ms, 2)}\n"
                "+==================================================================\n",
                flush=True,
            )

            # Failure MUST advance the circuit breaker; do NOT reset it.
            self._record_breaker_outcome(
                scope_key,
                config_sig,
                success=False,
                error_message=err_msg,
            )

            await self._record_search_usage_safely(
                query=sanitized_query,
                provider_name=resolved_provider,
                status="failed",
                latency_ms=latency_ms,
                results_count=0,
                requested_max_results=explicit_requested_count,
                effective_max_results=None,
                error_message=err_msg,
                run_id=run_id,
                job_id=job_id,
                chat_id=chat_id,
                project_id=project_id,
            )

            return ToolResult(
                output=f"Search Error: {err_msg}",
                exit_code=1,
                is_error=True,
                metadata={
                    "error_code": err_code,
                    "provider": resolved_provider,
                    "provider_status": provider_status_value or "unknown",
                    "execution_scope": scope_key,
                    "config_signature": config_sig,
                    "requested_max_results": explicit_requested_count,
                    "latency_ms": round(latency_ms, 2),
                },
            )

        # ------------------------------------------------------------------
        # 7b. Success or valid EMPTY branch: record breaker success (an empty
        #     search is a successful operation, not a failure).
        # ------------------------------------------------------------------
        self._record_breaker_outcome(scope_key, config_sig, success=True)

        effective_max = int(
            getattr(response, "effective_max_results", None)
            or resp_meta.get("effective_max_results")
            or SearchProviderFactory.resolve_effective_max_results(
                resolved_provider,
                requested=explicit_requested_count,
                search_config=cfg,
            )
        )
        configured_max = (
            getattr(response, "configured_max_results", None)
            if hasattr(response, "configured_max_results")
            else resp_meta.get("configured_max_results")
        )
        provider_limit = int(
            getattr(response, "max_results_limit", None)
            or resp_meta.get("max_results_limit")
            or SearchProviderFactory.get_provider_capability(resolved_provider, cfg).max_results_limit
        )

        raw_items = list(getattr(response, "results", []) or [])
        capped_items = self._deduplicate_and_cap_results(raw_items, effective_max)
        response.results = capped_items

        formatted_output = self._format_search_output(
            query=sanitized_query,
            response=response,
            items=capped_items,
            effective_max_results=effective_max,
        )

        cost_usd = float(
            getattr(response, "estimated_cost_usd", None)
            or resp_meta.get("estimated_cost_usd", None)
            or getattr(response, "cost_usd", None)
            or resp_meta.get("cost_usd", None)
            or 0.0
        )
        cost_basis = str(
            getattr(response, "cost_basis", None)
            or resp_meta.get("cost_basis", "unknown")
            or "unknown"
        )

        # --- Diagnostic console output: search outcome (real, not assumed) ---
        if outcome_class == "success":
            outcome_label = "SUCCESS"
            telemetry_status = "completed"
        else:
            outcome_label = "EMPTY (search completed, no matching results)"
            telemetry_status = "empty"

        print(
            "+==================================================================\n"
            f"| [WEB_SEARCH] {outcome_label}\n"
            "+==================================================================\n"
            f"| Query           : {sanitized_query!r}\n"
            f"| Provider used   : {resolved_provider}\n"
            f"| Provider status : {provider_status_value or 'unknown'}\n"
            f"| Results count   : {len(capped_items)} / {effective_max} (limit {provider_limit})\n"
            f"| Latency (ms)    : {round(latency_ms, 2)}\n"
            f"| Cost (USD)      : {cost_usd:.4f}\n"
            f"| Cost basis      : {cost_basis}\n"
            "+==================================================================\n",
            flush=True,
        )

        await self._record_search_usage_safely(
            query=sanitized_query,
            provider_name=resolved_provider,
            status=telemetry_status,
            latency_ms=latency_ms,
            results_count=len(capped_items),
            requested_max_results=explicit_requested_count,
            effective_max_results=effective_max,
            cost_usd=cost_usd,
            run_id=run_id,
            job_id=job_id,
            chat_id=chat_id,
            project_id=project_id,
        )

        result_metadata: Dict[str, Any] = {
            **resp_meta,
            "provider": resolved_provider,
            "query": sanitized_query,
            "status": outcome_class,  # "success" or "empty"
            "provider_status": provider_status_value or "unknown",
            "requested_max_results": explicit_requested_count,
            "configured_max_results": configured_max,
            "effective_max_results": effective_max,
            "max_results_limit": provider_limit,
            "returned_results_count": len(capped_items),
            "latency_ms": round(latency_ms, 2),
            "execution_scope": scope_key,
            "config_signature": config_sig,
            "cost_usd": cost_usd,
            "estimated_cost_usd": cost_usd,
            "cost_basis": cost_basis,
        }

        return ToolResult(
            output=formatted_output,
            exit_code=0,
            is_error=False,
            metadata=result_metadata,
        )

    async def aexecute(
        self,
        query: Optional[str] = None,
        max_results: Optional[int] = None,
        **kwargs: Any,
    ) -> ToolResult:
        """
        Canonical async execution entrypoint used by ToolCallAgent and Web adapters.
        Always returns a structured ToolResult without masking errors.
        """
        if query is None and "q" in kwargs:
            query = kwargs.pop("q")
        return await self._arun(
            query=query if query is not None else "",
            max_results=max_results,
            **kwargs,
        )

    def _run(
        self,
        query: str,
        max_results: Optional[int] = None,
        **kwargs: Any,
    ) -> ToolResult:
        """Synchronous bridge when invoked from a non-async context."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        if loop and loop.is_running():
            import concurrent.futures

            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(
                    asyncio.run,
                    self._arun(query=query, max_results=max_results, **kwargs),
                )
                return future.result()

        return asyncio.run(self._arun(query=query, max_results=max_results, **kwargs))


__all__ = ["WebSearchArgs", "WebSearchTool"]
