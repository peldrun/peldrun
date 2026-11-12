# backend/peldrun/memory/domain/scope.py
"""
PELDRUN Memory Platform - Scope Resolution & Isolation Contract.

Defines the CANONICAL representation of a memory scope reference
(``MemoryScopeRef``), the parser that reconstructs it from its
string form, and the resolver that materializes the complete set of
scopes applicable to a given runtime context (chat / project / user
/ agent).

Why this module is security-critical
------------------------------------

Scope isolation is enforced by EXACT STRING MATCH on
``(scope_type, scope_id)`` pairs inside the SQLite query in P1.2:

    WHERE (scope_type = 'project' AND scope_id = ?)
       OR (scope_type = 'user'    AND scope_id = ?)

If two different project ids ever produce the same canonical string
-- through a buggy resolver, a hidden character, a missing trim, a
colon collision, or an accidental fallback -- then a single retrieval
call could read another project's memories. That is precisely the
failure that acceptance test #2 in the engineering plan
("project A cannot see project B's memory") exists to catch.

The three invariants below eliminate every known collision vector:

1. **No structural separators inside ``scope_id``.**
   ``scope_id`` MUST NOT contain ``:`` (the field separator). Two
   different logical scopes are therefore guaranteed to produce
   different strings: ``"project:a:b"`` is unparseable and rejected
   at construction time, so ``("project", "a:b")`` and
   ``("project:a", "b")`` cannot both exist as valid refs.

2. **Trimmed, whitespace-free identifiers.**
   Every ``scope_id`` is stripped of leading/trailing whitespace
   before construction. ``"proj_A "`` and ``"proj_A"`` therefore
   produce identical canonical strings, so a UI that occasionally
   emits trailing spaces cannot create a shadow scope.

3. **No implicit fallback that silently changes the scope.**
   ``resolve_scope_refs`` maps an empty ``project_id`` to "no
   project scope" -- it does NOT fall back to a "default project"
   or to a chat scope. A caller that passes ``project_id=""`` has
   made a mistake, and the resolver surfaces it by simply omitting
   the project scope. Silent fallbacks are the leading cause of
   accidental cross-scope reads in practice.

Design invariants
-----------------

1. **Pure domain.**
   Imports only stdlib + Pydantic + sibling ``enums`` module. No I/O,
   no repository, no framework.

2. **Frozen value objects.**
   ``MemoryScopeRef`` is ``frozen=True`` and hashable. Two refs with
   identical ``(scope_type, scope_id)`` compare equal and hash
   identically, so it can be used safely as a ``dict`` key or as a
   member of a ``set`` -- both useful in the ranker's deduplication
   and in the API layer's scope-check guards.

3. **Both forms are first-class.**
   Domain code prefers ``MemoryScopeRef`` (typed, validated). The
   SQLite column and the ``MemoryContext.scope_refs`` list prefer
   the canonical string form (``"project:acme"``). ``str(ref)``
   and ``MemoryScopeRef.from_string(text)`` are the single,
   tested bridge between the two; no code should ever concatenate
   ``f"{type}:{id}"`` by hand.

4. **Precedence order is preserved in ``resolve_scope_refs``.**
   The returned list is ordered by ``MemoryScope.precedence``
   descending (``user`` → ``project`` → ``chat`` → ``agent``).
   This order is what the injection layer in P1.5 relies on when
   labeling memories, and what the monitoring layer in P1.9 relies
   on when attributing token spend per scope. Callers MUST NOT
   re-sort the list.

5. **Errors are fail-fast and precise.**
   Every construction path raises ``ValueError`` (or Pydantic's
   ``ValidationError``, which subclasses it) with a message that
   names the offending field and the specific rule violated. A
   "silently drop the bad scope" policy was considered and
   rejected: silently dropping a scope means silently widening or
   narrowing the search, both of which are worse than a crash.
"""

from __future__ import annotations

from typing import Final, List, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

from peldrun.memory.domain.enums import MemoryScope


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_MAX_SCOPE_ID_CHARS: Final[int] = 256
"""Hard ceiling on ``scope_id`` length. Matches ``entry._MAX_SCOPE_ID_CHARS``.

Kept as a separate constant here (rather than importing from
``entry``) so this module remains usable even when ``entry`` cannot
be imported (e.g. during a very early bootstrap). The values are
contractually identical; a test in P1.1 verifies that.
"""

_DEFAULT_USER_ID: Final[str] = "default"
"""Fallback user id for single-user deployments.

The plan's canonical ``scope_refs`` example uses ``"user:default"``,
so this constant matches that expectation. Multi-user deployments
pass an explicit ``user_id`` and never hit the fallback.
"""

_SCOPE_SEPARATOR: Final[str] = ":"
"""The single character separating ``scope_type`` from ``scope_id``.

Any occurrence of this character inside ``scope_id`` is rejected at
construction time -- see the module docstring's safety rationale.
"""


# ---------------------------------------------------------------------------
# MemoryScopeRef
# ---------------------------------------------------------------------------


class MemoryScopeRef(BaseModel):
    """A validated ``(scope_type, scope_id)`` pair.

    The canonical value object for a scope reference. Implements
    ``__str__`` to produce the wire form (``"project:acme"``) and
    ``from_string`` to reverse it, so domain code never has to touch
    the separator character directly.

    Instances are frozen and hashable. Two refs are equal iff their
    ``scope_type`` and ``scope_id`` match, both after trimming.
    """

    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
        frozen=True,
    )

    scope_type: MemoryScope = Field(
        ...,
        description=(
            "The kind of scope this reference points at. One of the "
            "members of ``MemoryScope``."
        ),
    )
    scope_id: str = Field(
        ...,
        description=(
            "The concrete owner of this scope. For USER, the user id; "
            "for PROJECT, the project id; for CHAT, the chat id; for "
            "AGENT, the agent id. Trimmed at construction. MUST be "
            "non-empty, MUST NOT contain the ':' separator, and "
            f"MUST NOT exceed {_MAX_SCOPE_ID_CHARS} characters."
        ),
    )

    @field_validator("scope_id", mode="after")
    @classmethod
    def _validate_scope_id(cls, v: str) -> str:
        """Trim, non-empty, no ':' separator, length-capped.

        See the module docstring for the full safety rationale of
        each rule.
        """
        if not isinstance(v, str):
            raise ValueError(
                f"scope_id must be a string, got {type(v).__name__}"
            )
        stripped = v.strip()
        if not stripped:
            raise ValueError("scope_id must not be empty or whitespace-only")
        if _SCOPE_SEPARATOR in stripped:
            # This is the SINGLE most important validation in this
            # module. Allowing ':' inside scope_id would make
            # parse_scope_ref ambiguous and could -- with a buggy
            # parser -- collapse two different scopes into one.
            raise ValueError(
                f"scope_id must not contain the {_SCOPE_SEPARATOR!r} "
                f"separator (got {stripped!r}); the separator is "
                f"reserved for the '<scope_type>:<scope_id>' wire form"
            )
        if len(stripped) > _MAX_SCOPE_ID_CHARS:
            raise ValueError(
                f"scope_id exceeds {_MAX_SCOPE_ID_CHARS} chars "
                f"(got {len(stripped)})"
            )
        # Reject ASCII control characters (0x00-0x1F, 0x7F). A NUL
        # byte in a scope_id would cause SQLite to truncate the
        # value on write, producing a scope collision that is
        # almost impossible to diagnose after the fact.
        for ch in stripped:
            if ord(ch) < 0x20 or ord(ch) == 0x7F:
                raise ValueError(
                    f"scope_id must not contain control characters "
                    f"(found U+{ord(ch):04X})"
                )
        return stripped

    # ------------------------- conversions ---------------------------------

    def __str__(self) -> str:
        """Return the canonical wire form: ``"<scope_type>:<scope_id>"``."""
        return f"{self.scope_type.value}{_SCOPE_SEPARATOR}{self.scope_id}"

    def __repr__(self) -> str:
        """Return a developer-readable representation."""
        return f"MemoryScopeRef({str(self)!r})"

    def as_string(self) -> str:
        """Explicit alias for ``str(ref)``.

        Provided for callers that prefer explicit conversion over
        implicit stringification (e.g., ``"".join(refs)`` would use
        ``__str__`` implicitly, which is fine; but a caller building
        a list comprehension may want the intent to be obvious).
        """
        return str(self)

    # ------------------------- parsing -------------------------------------

    @classmethod
    def from_string(cls, text: str) -> "MemoryScopeRef":
        """Parse a canonical ``"<scope_type>:<scope_id>"`` string.

        Strict parsing rules (matching the writing side):

          * ``text`` must be a ``str``.
          * After trimming, ``text`` must be non-empty.
          * ``text`` MUST contain exactly one ``:`` separator
            position -- the FIRST occurrence is the separator; any
            subsequent ``:`` inside the id is rejected by the
            field validator (which is the correct behavior: a
            scope_id can never contain ':').
          * ``scope_type`` must be a valid ``MemoryScope`` member
            (case-insensitive).
          * ``scope_id`` must pass the field validator.

        Raises
        ------
        ValueError
            On every malformed input. The message names the specific
            rule violated.
        """
        if not isinstance(text, str):
            raise ValueError(
                f"scope ref must be a string, got {type(text).__name__}"
            )
        stripped = text.strip()
        if not stripped:
            raise ValueError("scope ref must not be empty")

        sep_idx = stripped.find(_SCOPE_SEPARATOR)
        if sep_idx < 0:
            raise ValueError(
                f"scope ref {stripped!r} is missing the "
                f"{_SCOPE_SEPARATOR!r} separator; expected "
                f"'<scope_type>:<scope_id>'"
            )

        type_part = stripped[:sep_idx].strip()
        id_part = stripped[sep_idx + 1 :].strip()

        if not type_part:
            raise ValueError(
                f"scope ref {stripped!r} has an empty scope_type "
                f"before the separator"
            )
        if not id_part:
            raise ValueError(
                f"scope ref {stripped!r} has an empty scope_id "
                f"after the separator"
            )

        try:
            scope_type = MemoryScope(type_part.lower())
        except ValueError:
            valid = ", ".join(sorted(m.value for m in MemoryScope))
            raise ValueError(
                f"scope ref {stripped!r} has an unknown scope_type "
                f"{type_part!r}; expected one of: {valid}"
            ) from None

        # Delegating the remaining validation (no ':' inside id,
        # no control chars, length cap) to the field validator
        # through normal construction. Any violation raises
        # ValueError with a message that names the exact rule.
        return cls(scope_type=scope_type, scope_id=id_part)


# ---------------------------------------------------------------------------
# Parsing convenience
# ---------------------------------------------------------------------------


def parse_scope_ref(text: str) -> MemoryScopeRef:
    """Parse a canonical scope-ref string into a ``MemoryScopeRef``.

    Thin module-level wrapper around ``MemoryScopeRef.from_string``
    so callers can import ``parse_scope_ref`` as a plain function
    (useful in Pydantic validators and functional composition
    chains without instantiating the class reference).
    """
    return MemoryScopeRef.from_string(text)


# ---------------------------------------------------------------------------
# Resolution -- from runtime context to canonical refs
# ---------------------------------------------------------------------------


def resolve_scope_refs(
    *,
    chat_id: Optional[str] = None,
    project_id: Optional[str] = None,
    user_id: Optional[str] = None,
    agent_id: Optional[str] = None,
    default_user_id: str = _DEFAULT_USER_ID,
) -> List[str]:
    """Materialize the complete set of applicable scope refs.

    This is THE function the dispatcher (P1.5) and engine (P1.5) call
    at the start of every turn. It answers the question:

        "For a runtime context described by these ids, which
        storage scopes must retrieval look in?"

    Rules
    -----
    * **User scope is always present.** A memory saved under USER
      scope belongs to the same person everywhere; excluding it from
      any retrieval would make user preferences invisible in most
      contexts. When ``user_id`` is ``None`` or whitespace-only,
      ``default_user_id`` is used -- single-user deployments get
      the expected ``"user:default"`` without extra configuration.
    * **Project scope is included iff ``project_id`` is provided and
      non-empty.** No fallback. A caller passing ``""`` explicitly
      gets no project scope.
    * **Chat scope is included iff ``chat_id`` is provided and
      non-empty.** Same rationale.
    * **Agent scope is included iff ``agent_id`` is provided and
      non-empty.** Currently reserved for future use; present in
      the signature so P2 callers do not have to change the
      invocation site.
    * **Ordered by precedence descending** (user → project → chat
      → agent), matching ``MemoryScope.precedence``. Callers MUST
      NOT re-sort -- the ordering is part of the contract with
      P1.5 (injection labels) and P1.9 (attribution).

    Returns
    -------
    List[str]
        Canonical strings in the form ``"<scope_type>:<scope_id>"``,
        ordered as described above. Ready to pass directly to
        ``SqliteMemoryRepository.search(query, scope_refs, ...)``
        (P1.2) and ``MemoryContext.scope_refs`` (see context.py).

    Raises
    ------
    ValueError
        When any of the provided ids is invalid (contains ``:``,
        contains control characters, exceeds the length cap). The
        error message names the offending parameter.

    Note on the input validation
    ----------------------------
    Each id is validated by constructing a real ``MemoryScopeRef``
    and then discarding it after stringification. This guarantees
    that the validation rules applied here are IDENTICAL to the
    rules applied anywhere else -- one source of truth, no drift.
    The construction cost is negligible compared to the SQLite
    query the result will feed.
    """
    refs: List[MemoryScopeRef] = []

    # --- User scope (always included; defaults to "user:default") -----
    effective_user = (
        user_id.strip()
        if isinstance(user_id, str) and user_id.strip()
        else default_user_id.strip()
        if isinstance(default_user_id, str)
        else ""
    )
    if not effective_user:
        # A default_user_id of "" is a genuine misconfiguration. We
        # fail fast rather than silently skipping user scope, which
        # would make user preferences invisible across the whole
        # deployment -- a very hard bug to notice until it matters.
        raise ValueError(
            "resolve_scope_refs: default_user_id resolved to an "
            "empty string; set a concrete user id or fix the "
            "caller's default_user_id argument"
        )
    refs.append(
        MemoryScopeRef(
            scope_type=MemoryScope.USER,
            scope_id=effective_user,
        )
    )

    # --- Project scope (optional) -------------------------------------
    if project_id is not None:
        pid = project_id.strip()
        if pid:
            refs.append(
                MemoryScopeRef(
                    scope_type=MemoryScope.PROJECT,
                    scope_id=pid,
                )
            )

    # --- Chat scope (optional) ----------------------------------------
    if chat_id is not None:
        cid = chat_id.strip()
        if cid:
            refs.append(
                MemoryScopeRef(
                    scope_type=MemoryScope.CHAT,
                    scope_id=cid,
                )
            )

    # --- Agent scope (optional; reserved for P2) ----------------------
    if agent_id is not None:
        aid = agent_id.strip()
        if aid:
            refs.append(
                MemoryScopeRef(
                    scope_type=MemoryScope.AGENT,
                    scope_id=aid,
                )
            )

    # Stringify last, once, in the order established above.
    return [ref.as_string() for ref in refs]


def resolve_scope(
    *,
    chat_id: Optional[str] = None,
    project_id: Optional[str] = None,
) -> MemoryScopeRef:
    """Resolve the SINGLE most-specific scope of a runtime context.

    Complements ``resolve_scope_refs``: this function answers "which
    scope IS this context?" rather than "which scopes can this
    context READ from?". Used by:

      * The UI "Save to memory" button, which must decide whether
        the current entry defaults to project scope or chat scope.
      * The ``promote`` endpoint in P1.8, which needs to know the
        entry's CURRENT scope before copying it into user scope.

    Rules
    -----
    * If ``project_id`` is provided and non-empty → PROJECT scope.
    * Otherwise, if ``chat_id`` is provided and non-empty → CHAT scope.
    * Otherwise → raise ``ValueError``.

    User scope is deliberately NOT returned by this function, because
    user scope is not "the scope of the current context" -- it is the
    cross-cutting scope that every context can read from. Use
    ``resolve_scope_refs`` when you need the read set.

    Returns
    -------
    MemoryScopeRef
        The innermost scope of the given context.

    Raises
    ------
    ValueError
        When neither ``project_id`` nor ``chat_id`` produces a
        non-empty id. Silent fallback to USER scope is deliberately
        avoided -- it would mask bugs where the caller forgot to
        pass a required context.
    """
    if project_id is not None:
        pid = project_id.strip()
        if pid:
            return MemoryScopeRef(
                scope_type=MemoryScope.PROJECT,
                scope_id=pid,
            )

    if chat_id is not None:
        cid = chat_id.strip()
        if cid:
            return MemoryScopeRef(
                scope_type=MemoryScope.CHAT,
                scope_id=cid,
            )

    raise ValueError(
        "resolve_scope: at least one of project_id or chat_id must "
        "be a non-empty string; got project_id="
        f"{project_id!r}, chat_id={chat_id!r}"
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


__all__ = [
    "MemoryScopeRef",
    "parse_scope_ref",
    "resolve_scope",
    "resolve_scope_refs",
]
