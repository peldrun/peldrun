# backend/peldrun/memory/tools/builtins/memory_search.py
"""
PELDRUN Memory Platform - Memory Search Tool.

Tool for retrieving memory entries via full-text search, invoked
by an agent DURING a run (``RouteDecision.DURING_EXECUTION``) or
by a future management surface that needs to preview matches
before saving.

Design invariants
-----------------

1. **Stateless execution.**
   No per-execution state lives on the instance. Every mutable
   variable lives on the stack of ``execute``, so concurrent
   invocations against the same instance are safe. Required by
   the agent runtime, which reuses one tool instance across
   turns.

2. **Dependency injection, not global lookup.**
   ``port`` and ``settings`` are supplied at construction time.
   The tool never imports ``omweb.memory`` and never reads the
   ``MemoryService`` singleton. This keeps the tool testable
   without a FastAPI app and preserves the rule that
   ``peldrun/`` does not import ``omweb/``.

3. **Mode enforcement at the tool boundary.**
   ``MemoryMode.DISABLED`` rejects every search with a
   structured error. ``READ_ONLY`` and ``READ_WRITE`` both
   permit reads (``MemoryMode.can_read`` is the single check).
   The plan's acceptance test #11 ("mode=disabled does not
   read or write") is enforced HERE.

4. **Pipeline reuse, not re-implementation.**
   The tool delegates to ``build_memory_context`` (P1.4) for the
   router check, ``search_ranked`` call, ranking, and budget
   fitting. The tool's only additional responsibility is
   (a) formatting the resulting block for the agent and
   (b) calling ``mark_used`` on the retrieved entries so the
   recency signal fires. Anything else would be a second
   implementation of P1.4 that could drift.

5. **``mark_used`` is best-effort.**
   After a successful retrieval, the tool calls
   ``port.mark_used(...)`` to update ``last_used_at`` on every
   returned entry. The repository's own ``mark_used`` swallows
   failures (a stale ``last_used_at`` only affects future
   ranking). The tool follows the same discipline: a failure to
   update recency MUST NOT fail the search — the entries were
   already returned, and the caller has what they asked for.

6. **Structured result, never raises.**
   Every failure mode — mode rejection, validation error,
   backend unavailable — returns a dict with ``status`` and,
   where relevant, ``error`` and ``code``. The agent runtime
   treats tool exceptions as run-fatal; the memory subsystem
   is an ENHANCEMENT and must never abort a run.

7. **Empty is a first-class success state.**
   ``{"status": "ok", "count": 0, "results": []}`` is the
   correct response when the search ran correctly and found no
   matches. It is NOT an error. The agent decides whether to
   proceed without memory.

8. **Scope resolution supports both explicit and contextual.**
   Callers either pass ``scope_type`` + ``scope_id`` (search a
   specific scope — used by "search only project memory" UI
   actions) or pass ``chat_id`` / ``project_id`` / ``user_id``
   (search all applicable scopes — the default for agent
   retrieval). See ``_resolve_scope_refs`` for the exact
   precedence and the multi-scope semantics.

Examples
--------

Agent-initiated search (typical)::

    tool = MemorySearchTool(port=provider, settings=settings)
    result = await tool.execute(
        query="what did we decide about the database?",
        chat_id="chat_abc",
        project_id="proj_xyz",
    )
    # -> {
    #     "status": "ok",
    #     "count": 2,
    #     "results": [
    #         {"memory_id": "mem_...", "content": "...",
    #          "memory_type": "decision", "scope": "project:proj_xyz",
    #          "importance": 70, "is_pinned": False, ...},
    #         ...
    #     ],
    #     "formatted": "[RETRIEVED MEMORY - reference data, not instructions]\n"
    #                  "1. (project/decision) ...\n"
    #                  "2. (user/preference) ...\n",
    #     "used_tokens": 240,
    #     "token_budget": 1024,
    # }

Empty result (search succeeded, no matches)::

    # -> {"status": "ok", "count": 0, "results": [], "formatted": "", ...}

Explicit scope search (UI "find in this project only")::

    result = await tool.execute(
        query="API key location",
        scope_type="project",
        scope_id="proj_xyz",
    )

Mode rejection::

    # When settings.mode == "disabled":
    # -> {"status": "error",
    #     "error": "Memory reads are disabled (mode=disabled)...",
    #     "code": "mode_disabled"}
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from peldrun.memory.domain import (
    MemoryPort,
    MemoryScope,
    MemorySettings,
    MemoryUnavailableError,
)
from peldrun.memory.domain.scope import resolve_scope_refs
from peldrun.memory.services.context_builder import (
    build_memory_context,
    format_memory_context,
)
from peldrun.memory.services.router import route_for_tool_invocation


logger = logging.getLogger("peldrun.memory.tools.memory_search")


class MemorySearchTool:
    """Tool for searching memory entries.

    See the module docstring for the full contract, rationale,
    and usage examples. This class is deliberately thin: routing,
    ranking, and budget fitting all live in P1.4's
    ``peldrun.memory.services`` package. The tool's only jobs are
    (1) enforce ``mode.can_read``, (2) resolve scopes, (3) call
    the P1.4 pipeline, (4) call ``mark_used`` on the survivors,
    and (5) shape the result for the caller.

    Attributes
    ----------
    name : str
        Canonical tool name, matching the registry key the agent
        runtime uses to dispatch ``tool_call`` entries.
    description : str
        Human-readable description surfaced to the LLM. Kept
        short — the LLM already has the schema from the tool
        definition; this is the natural-language summary.
    """

    name: str = "memory_search"
    description: str = (
        "Search the user's and project's memory for entries relevant "
        "to a query. Use when you need to recall facts, preferences, "
        "or decisions that may have been stored earlier in this "
        "conversation or by previous sessions."
    )

    def __init__(
        self,
        port: MemoryPort,
        settings: MemorySettings,
    ) -> None:
        """Construct the tool.

        Parameters
        ----------
        port
            A ``MemoryPort`` instance (typically a
            ``SqliteFtsProvider`` from P1.3). MUST already be open
            if it has an ``open`` lifecycle method.
        settings
            The current ``MemorySettings`` snapshot. The tool
            reads ``settings.mode`` to enforce DISABLED, and
            ``settings.default_limit`` as the default result cap.
            A caller can override the cap per call via ``limit``.

        Raises
        ------
        TypeError
            When ``port`` or ``settings`` are the wrong type.
        """
        if port is None:
            raise TypeError("port must be a MemoryPort instance, got None")
        if not isinstance(settings, MemorySettings):
            raise TypeError(
                f"settings must be MemorySettings, got "
                f"{type(settings).__name__}"
            )
        self._port: MemoryPort = port
        self._settings: MemorySettings = settings

    async def execute(
        self,
        *,
        query: str,
        scope_type: Optional[str] = None,
        scope_id: Optional[str] = None,
        chat_id: Optional[str] = None,
        project_id: Optional[str] = None,
        user_id: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Search memory entries and return a formatted result.

        Parameters
        ----------
        query
            The search query. May be empty string — the pipeline
            then falls back to a recency listing (see
            ``SqliteMemoryRepository.search_ranked``'s
            empty-query fallback). An empty query with no
            scopes is still rejected as ``validation_error``
            because a search with zero scopes cannot succeed.

        scope_type
            Optional explicit scope kind. When provided, MUST be
            paired with ``scope_id``. Search is restricted to
            THIS scope only (no multi-scope inclusion). Useful
            for UI actions like "find in this project only".

        scope_id
            Optional explicit scope owner. See ``scope_type``.

        chat_id, project_id, user_id
            Contextual hints. When explicit scope is NOT
            provided, these are passed to
            ``resolve_scope_refs`` (P1.1) to build the multi-
            scope list. The resulting refs are ordered by
            precedence (user > project > chat > agent) — see
            ``MemoryScope.precedence``. All three are optional;
            ``resolve_scope_refs`` guarantees at least
            ``user:default``.

        limit
            Optional override for the maximum number of results.
            When ``None``, uses ``settings.default_limit``. The
            override is bounded by ``MemorySettings.default_limit``'s
            Pydantic validator (1-50). Values outside that range
            will raise a ``pydantic.ValidationError``, which the
            caller sees as a Python exception (NOT a structured
            tool error) — this is intentional: the limit is a
            programmer parameter, not user input, so a caller
            passing ``limit=9999`` has a bug that should surface
            loudly.

        Returns
        -------
        Dict[str, Any]
            On success (with or without matches)::

                {
                    "status": "ok",
                    "count": int,                  # 0 allowed
                    "query": str,
                    "scope_refs": list[str],
                    "router_decision": str,        # always "during_execution"
                    "ranker_version": str,
                    "used_tokens": int,
                    "token_budget": int,
                    "dropped_for_budget": int,
                    "results": [
                        {
                            "memory_id": str,
                            "content": str,
                            "memory_type": str,
                            "scope": str,           # "type:id"
                            "importance": int,
                            "is_pinned": bool,
                            "created_at": str,      # ISO 8601
                            "last_used_at": str|None,
                        },
                        ...
                    ],
                    "formatted": str,   # ready-to-inject block;
                                        # empty string when count==0
                }

            On failure::

                {
                    "status": "error",
                    "error": str,
                    "code": str,   # "mode_disabled" | "validation_error"
                                   # | "backend_unavailable"
                }

            ``status="ok"`` with ``count=0`` is a SUCCESS (the
            search ran, nothing matched). Only ``status="error"``
            signals a failure of the search itself.
        """
        # ------------------------------------------------------------------
        # 1. Mode enforcement
        # ------------------------------------------------------------------
        #
        # The check happens BEFORE any work: in DISABLED mode the
        # tool returns immediately without touching the port. Reads
        # are allowed in READ_ONLY and READ_WRITE; the single
        # ``can_read`` property encapsulates that rule (see
        # ``MemoryMode.can_read`` in P1.1).
        if not self._settings.mode.can_read:
            logger.info(
                "MemorySearchTool rejected: mode=%s disallows reads "
                "(query_preview=%r).",
                self._settings.mode.value,
                query[:60] if query else "",
            )
            return {
                "status": "error",
                "error": (
                    f"Memory reads are disabled (mode="
                    f"{self._settings.mode.value}). Set "
                    f"memory mode to 'read_only' or 'read_write' "
                    f"to enable searching."
                ),
                "code": "mode_disabled",
            }

        # ------------------------------------------------------------------
        # 2. Structural validation of query
        # ------------------------------------------------------------------
        #
        # The query type check is a programmer-error guard: a
        # non-string query (bytes, list, dict) would produce a
        # confusing error deep inside the pipeline. We fail fast
        # with a clear message.
        #
        # An empty-string query is VALID (the pipeline's empty-
        # query fallback returns recent entries). It is NOT a
        # validation error. Only a non-string is rejected.
        if not isinstance(query, str):
            return {
                "status": "error",
                "error": (
                    f"query must be a string, got "
                    f"{type(query).__name__}"
                ),
                "code": "validation_error",
            }

        # ------------------------------------------------------------------
        # 3. Resolve scopes
        # ------------------------------------------------------------------
        try:
            scope_refs = self._resolve_scope_refs(
                scope_type=scope_type,
                scope_id=scope_id,
                chat_id=chat_id,
                project_id=project_id,
                user_id=user_id,
            )
        except ValueError as exc:
            return {
                "status": "error",
                "error": f"Cannot resolve memory scope: {exc}",
                "code": "validation_error",
            }

        if not scope_refs:
            # ``resolve_scope_refs`` guarantees at least one ref
            # (``user:default``) when given no hints, so an empty
            # result here means the caller's arguments were
            # structured in a way the helper couldn't process.
            # This is a defensive belt — it should never fire.
            return {
                "status": "error",
                "error": "No scopes resolved for search.",
                "code": "validation_error",
            }

        # ------------------------------------------------------------------
        # 4. Build the route (DURING_EXECUTION)
        # ------------------------------------------------------------------
        #
        # The tool has already decided to retrieve — that is the
        # WHOLE POINT of invoking this tool. The router's
        # ``route_for_tool_invocation`` gives us the correct
        # ``RouteDecision.DURING_EXECUTION`` value, which the
        # MemoryContext will carry so monitoring (P1.9) can
        # attribute the retrieval to the tool path vs. the
        # automatic dispatcher path.
        route = route_for_tool_invocation(query)

        # ------------------------------------------------------------------
        # 5. Apply the optional limit override
        # ------------------------------------------------------------------
        #
        # ``build_memory_context`` reads ``settings.default_limit``
        # when calling ``search_ranked``. To override per call, we
        # use Pydantic's ``model_copy`` to produce an effective
        # settings object with the caller's limit. The original
        # ``self._settings`` is NOT mutated — a subsequent call
        # without a limit uses the original default.
        effective_settings = self._settings
        if limit is not None:
            # Bounded by default_limit's Pydantic validator (1-50).
            # A caller passing an out-of-range value gets a
            # ValidationError — see the docstring's ``limit``
            # parameter for why that is the correct behavior.
            effective_settings = self._settings.model_copy(
                update={"default_limit": int(limit)}
            )

        # ------------------------------------------------------------------
        # 6. Run the pipeline
        # ------------------------------------------------------------------
        try:
            context = await build_memory_context(
                query=query,
                scope_refs=scope_refs,
                route=route,
                provider=self._port,
                settings=effective_settings,
            )
        except MemoryUnavailableError as exc:
            logger.error(
                "MemorySearchTool: backend unavailable (query=%r, "
                "scopes=%r): %s",
                query[:100],
                scope_refs,
                exc,
            )
            return {
                "status": "error",
                "error": (
                    "Memory backend unavailable; search could not be "
                    "completed. Try again shortly."
                ),
                "code": "backend_unavailable",
            }
        except Exception as exc:
            # ``build_memory_context`` already catches provider
            # errors internally and returns an empty context. Any
            # exception that escapes here is truly unexpected
            # (a bug in the pipeline itself, a bad ``scope_refs``
            # value, etc.). Surface it as a structured error
            # rather than crashing the run.
            logger.exception(
                "MemorySearchTool: unexpected pipeline error "
                "(query=%r, scopes=%r): %s",
                query[:100],
                scope_refs,
                exc,
            )
            return {
                "status": "error",
                "error": (
                    "Memory search encountered an unexpected error. "
                    "The run will continue without memory."
                ),
                "code": "internal_error",
            }

        # ------------------------------------------------------------------
        # 7. Empty-result fast path
        # ------------------------------------------------------------------
        #
        # Empty is a valid success state — the search ran
        # correctly and found nothing above the relevance
        # threshold. No ``mark_used`` call (nothing to mark), no
        # formatting (empty block), just a well-formed response.
        if context.is_empty:
            logger.debug(
                "MemorySearchTool: zero matches for query=%r "
                "(scopes=%r, router=%s).",
                query[:100],
                scope_refs,
                context.router_decision,
            )
            return {
                "status": "ok",
                "count": 0,
                "query": query,
                "scope_refs": list(scope_refs),
                "router_decision": context.router_decision,
                "ranker_version": context.ranker_version,
                "used_tokens": 0,
                "token_budget": context.token_budget,
                "dropped_for_budget": 0,
                "results": [],
                "formatted": "",
            }

        # ------------------------------------------------------------------
        # 8. Mark used (best-effort)
        # ------------------------------------------------------------------
        #
        # Fires the recency signal that the ranker relies on.
        # The repository's ``mark_used`` swallows its own
        # failures, but we wrap it here too so that a
        # hypothetical provider that raises on mark_used
        # cannot fail the search — the entries are already
        # returned, and the caller has what they asked for.
        try:
            await self._port.mark_used(
                [entry.memory_id for entry in context.memories]
            )
        except Exception as exc:
            logger.debug(
                "MemorySearchTool: mark_used failed for %d entries "
                "(search result unaffected): %s",
                len(context.memories),
                exc,
            )

        # ------------------------------------------------------------------
        # 9. Format the block (single source of truth: P1.4)
        # ------------------------------------------------------------------
        #
        # ``format_memory_context`` produces the exact string
        # that will appear in the LLM prompt when this block is
        # injected. ``context.used_tokens`` was computed against
        # this exact string during pipeline execution, so the two
        # are guaranteed consistent. See
        # ``peldrun.memory.services.context_builder``'s module
        # docstring for why the formatter lives in one place.
        formatted = format_memory_context(context)

        # ------------------------------------------------------------------
        # 10. Build the structured result list
        # ------------------------------------------------------------------
        #
        # The results list is a structured projection of
        # ``context.memories``. Callers who need programmatic
        # access (the UI's "Save these to chat context" button,
        # future automation) use this. Callers who just want
        # to inject the block (the agent's usual path) use
        # ``formatted``.
        results: List[Dict[str, Any]] = []
        for entry in context.memories:
            results.append(
                {
                    "memory_id": entry.memory_id,
                    "content": entry.content,
                    "memory_type": entry.memory_type.value,
                    "scope": (
                        f"{entry.scope_type.value}:{entry.scope_id}"
                    ),
                    "importance": int(entry.importance),
                    "is_pinned": bool(entry.is_pinned),
                    "created_at": entry.created_at.isoformat(),
                    "last_used_at": (
                        entry.last_used_at.isoformat()
                        if entry.last_used_at is not None
                        else None
                    ),
                }
            )

        logger.info(
            "MemorySearchTool: %d match(es) for query=%r "
            "(scopes=%r, tokens=%d/%d).",
            len(results),
            query[:100],
            scope_refs,
            context.used_tokens,
            context.token_budget,
        )

        return {
            "status": "ok",
            "count": len(results),
            "query": query,
            "scope_refs": list(scope_refs),
            "router_decision": context.router_decision,
            "ranker_version": context.ranker_version,
            "used_tokens": context.used_tokens,
            "token_budget": context.token_budget,
            "dropped_for_budget": context.dropped_for_budget,
            "results": results,
            "formatted": formatted,
        }

    # ------------------------------------------------------------------
    # Scope resolution
    # ------------------------------------------------------------------

    def _resolve_scope_refs(
        self,
        *,
        scope_type: Optional[str],
        scope_id: Optional[str],
        chat_id: Optional[str],
        project_id: Optional[str],
        user_id: Optional[str],
    ) -> List[str]:
        """Return the list of ``"<scope_type>:<scope_id>"`` refs to search.

        Two modes, in priority order:

          1. **Explicit.** Both ``scope_type`` and ``scope_id``
             supplied. Search is restricted to EXACTLY this
             scope. Used by UI actions like "find in this
             project only".

          2. **Contextual.** No explicit scope; ``chat_id`` /
             ``project_id`` / ``user_id`` supplied (any subset).
             Delegates to ``resolve_scope_refs`` (P1.1), which
             returns the multi-scope list ordered by precedence
             (user > project > chat > agent). This is the normal
             path for agent-driven retrieval.

        Note on the difference from ``memory_save``'s resolver:
        save uses ``resolve_scope`` (returns ONE scope — the
        innermost), search uses ``resolve_scope_refs`` (returns
        ALL applicable scopes). The asymmetry is intentional:

          * A save targets ONE specific destination — writing
            the same fact into multiple scopes would be a bug
            (you'd have duplicates in retrieval results).
          * A search reads across every scope the caller is
            authorized to see — missing a scope means missing
            relevant facts.

        Raises
        ------
        ValueError
            When ``scope_type`` and ``scope_id`` are provided
            without each other, when ``scope_type`` is not a
            valid ``MemoryScope`` member, or when ``scope_id``
            is empty after trimming.
        """
        # Branch 1: explicit scope (search ONLY this scope).
        if scope_type is not None or scope_id is not None:
            if scope_type is None or scope_id is None:
                raise ValueError(
                    "scope_type and scope_id must be provided "
                    "together (got scope_type="
                    f"{scope_type!r}, scope_id={scope_id!r})"
                )
            try:
                resolved_type = MemoryScope(scope_type)
            except ValueError:
                valid = ", ".join(s.value for s in MemoryScope)
                raise ValueError(
                    f"unknown scope_type {scope_type!r}; "
                    f"expected one of: {valid}"
                ) from None
            if not scope_id.strip():
                raise ValueError("scope_id must be non-empty")
            return [f"{resolved_type.value}:{scope_id.strip()}"]

        # Branch 2: contextual (multi-scope).
        #
        # ``resolve_scope_refs`` always returns at least
        # ``["user:default"]``, so this never returns []. The
        # caller's empty-check downstream is purely defensive.
        return resolve_scope_refs(
            chat_id=chat_id,
            project_id=project_id,
            user_id=user_id,
        )


__all__ = ["MemorySearchTool"]
