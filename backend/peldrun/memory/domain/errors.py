# backend/peldrun/memory/domain/errors.py
"""
PELDRUN Memory Platform - Exception Hierarchy.

The COMPLETE, CLOSED set of exceptions raised by the memory
subsystem. Every failure mode maps to exactly one of these types;
no module outside this package may define a new memory-related
exception.

Why a closed hierarchy matters
------------------------------

The dispatcher (P1.5) wraps memory retrieval in a try/except that
MUST distinguish three scenarios with completely different
consequences:

    1. **Memory is disabled / unavailable.**
       Return an empty ``MemoryContext``. The run proceeds
       normally without memory. Swallow the exception SILENTLY
       (log at DEBUG). This is the common case when the operator
       set ``mode=disabled`` or the provider crashed on startup.

    2. **The caller made an invalid request.**
       Surface a clear, structured error to the user. Do NOT
       swallow: a ``MemoryValidationError`` from a bad
       ``scope_id`` means the caller (usually the UI) has a bug
       that must be fixed. Silently returning an empty context
       would hide the bug and make memory "work but never
       return anything", which is nearly impossible to diagnose.

    3. **A scope boundary was violated.**
       This is a SECURITY event, not a normal error. Never
       swallow, never retry, never downgrade to a warning. Log
       at ERROR level with full context and propagate. The
       only correct response is to fail the request (HTTP 403)
       and let the security team decide whether the caller's
       behavior was malicious or merely a bug.

A FLAT exception hierarchy (all errors inheriting directly from
``Exception``) would force every try/except site to enumerate
the specific exception types it cares about. Any new exception
added later would silently bypass every existing ``except
MemoryValidationError:`` clause and land in a bare ``except
Exception:`` -- typically the wrong behavior.

The hierarchy below encodes the three scenarios as two top-level
branches plus one security branch:

    MemoryError                       (root -- never caught directly)
    ├── MemoryUnavailableError        (scenario 1: swallow silently)
    ├── MemoryOperationError          (scenario 2: surface to user)
    │   ├── MemoryValidationError     (bad input)
    │   └── MemoryNotFoundError       (missing entry -- 404)
    └── ScopeViolationError           (scenario 3: never swallow)

Two behavioral properties
-------------------------

Every exception carries:

    * ``retryable``       -- whether a subsequent attempt at the
                            same operation could plausibly succeed.
                            Used by the WritePipeline to decide
                            whether to retry a failed write, and
                            by the retrieval layer to decide
                            whether to fall back to an empty
                            context.

    * ``client_facing``   -- whether the exception's message can
                            be shown to the end user verbatim.
                            Internal exceptions (e.g. an
                            SQLite corruption error surfaced as
                            ``MemoryUnavailableError``) carry
                            messages intended for logs, NOT for
                            the UI. Client-facing exceptions
                            carry messages already written for a
                            non-technical reader.

Encoding these as properties (rather than as separate classes)
keeps the exception tree small while giving callers a single,
uniform way to decide "what do I do?".
"""

from __future__ import annotations

from typing import Any, Dict, Optional


# ---------------------------------------------------------------------------
# Root
# ---------------------------------------------------------------------------


class MemoryError(Exception):
    """Root of the memory exception tree.

    NEVER caught directly. Every consumer of the memory subsystem
    catches one of the three concrete branches
    (``MemoryUnavailableError``, ``MemoryOperationError``, or
    ``ScopeViolationError``) -- never the root. Catching the root
    is equivalent to writing ``except Exception:`` inside the
    memory boundary, which is exactly the pattern this hierarchy
    exists to prevent.

    Carries common attributes:

      * ``message``     -- the human-readable message. Always
                           present.
      * ``context``     -- structured key-value pairs describing
                           the failure (e.g. ``{"memory_id": "..."}``).
                           Always a dict; empty when no extra
                           context was supplied.
      * ``cause``       -- the underlying exception, if any. This
                           is separate from ``__cause__`` (which
                           is set via ``raise ... from ...``) so
                           callers can inspect the chain without
                           unwrapping ``Exception.__cause__``.

    Attributes ``retryable`` and ``client_facing`` are declared
    as class-level defaults on the root, so a caller inspecting
    ``exc.retryable`` on ANY memory exception gets a well-defined
    boolean. Subclasses override them as needed.
    """

    # Class-level defaults. Subclasses override. A caller reading
    # ``exc.retryable`` on the root class (a bug -- the root is
    # never supposed to be instantiated) gets the SAFEST possible
    # values: not retryable, not client-facing. This is a
    # defense-in-depth choice: if the root ever escapes due to a
    # bug, the response is to log and refuse, not to retry in a
    # loop or display an internal error to the user.
    retryable: bool = False
    client_facing: bool = False

    def __init__(
        self,
        message: str,
        *,
        context: Optional[Dict[str, Any]] = None,
        cause: Optional[BaseException] = None,
    ) -> None:
        """Construct a memory exception.

        Parameters
        ----------
        message
            Human-readable message. Non-empty; trimmed by the
            constructor. Required so that every exception instance
            carries enough information to be logged usefully.
        context
            Optional structured key-value pairs describing the
            failure. Stored as ``self.context`` (always a dict,
            never ``None``) so callers never need a guard.
        cause
            Optional underlying exception. Stored as ``self.cause``
            (the explicit attribute), not ``self.__cause__``.
            Callers that need to raise ``from`` should do so at
            the raise site; this constructor's ``cause`` is for
            diagnostics, not for Python's exception chaining.
        """
        if not isinstance(message, str):
            raise TypeError(
                f"message must be a string, got {type(message).__name__}"
            )
        stripped = message.strip()
        if not stripped:
            raise ValueError("message must not be empty")
        super().__init__(stripped)
        self.message: str = stripped
        self.context: Dict[str, Any] = dict(context) if context else {}
        self.cause: Optional[BaseException] = cause

    def __str__(self) -> str:
        """Return the message, plus context when present.

        Format: ``"<message> [<k1>=<v1>, <k2>=<v2>]"`` when context
        is non-empty; the bare message otherwise. This is what
        appears in log lines and (for client-facing exceptions)
        in API responses.
        """
        if not self.context:
            return self.message
        ctx_str = ", ".join(
            f"{k}={v!r}" for k, v in sorted(self.context.items())
        )
        return f"{self.message} [{ctx_str}]"

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-safe representation for API responses.

        Shape::

            {
                "error": "<ExceptionClassName>",
                "message": "<message>",
                "context": { ... },
                "retryable": <bool>,
                "client_facing": <bool>,
            }

        The ``error`` key carries the exception class name (a
        stable string contract, e.g. ``"ScopeViolationError"``),
        so the frontend can branch on it without depending on
        message text.
        """
        return {
            "error": type(self).__name__,
            "message": self.message,
            "context": dict(self.context),
            "retryable": self.retryable,
            "client_facing": self.client_facing,
        }


# ---------------------------------------------------------------------------
# Scenario 1: Memory is disabled / unavailable
# ---------------------------------------------------------------------------


class MemoryUnavailableError(MemoryError):
    """The memory subsystem cannot serve the request.

    Raised when:

      * ``MemoryMode.DISABLED`` is active and a caller attempts a
        write through the standard channels
        (``memory_save``, agent tools, extraction).
      * The provider failed to initialize (SQLite file locked,
        disk full, corrupt schema). This is the ``cause`` that
        ``MemoryService.retrieve`` catches and converts into an
        empty ``MemoryContext``.
      * A transient backend failure occurred (e.g. FTS index
        corruption). The write pipeline retries once.

    Behavioral contract
    -------------------
    * ``retryable = True``   -- a subsequent attempt MAY succeed
                                (e.g. disk full after cleanup,
                                provider initialized after a
                                transient SQLite lock).
    * ``client_facing = False`` -- the message is for logs. The
                                   API layer catches this and
                                   returns an empty result or a
                                   generic "memory unavailable"
                                   status, NOT the raw message
                                   (which may contain SQLite
                                   internals).

    Callers
    -------
    * ``MemoryService.retrieve``  -- catches and returns
                                     ``MemoryContext.empty()``.
    * ``WritePipeline``           -- retries once on write; falls
                                     through to a clear error on
                                     the second failure.
    * ``memory_save`` tool        -- surfaces a user-facing
                                     "memory is disabled" message
                                     derived from the mode, not
                                     from this exception's text.
    """

    retryable = True
    client_facing = False


# ---------------------------------------------------------------------------
# Scenario 2: The caller made an invalid request
# ---------------------------------------------------------------------------


class MemoryOperationError(MemoryError):
    """The caller's request was well-formed at the boundary but invalid inside.

    Root of the "user-visible error" branch. Split into two
    concrete subclasses because they map to DIFFERENT HTTP status
    codes and DIFFERENT user messages:

      * ``MemoryValidationError`` -> 400 Bad Request
      * ``MemoryNotFoundError``   -> 404 Not Found

    Callers that need a single ``except`` for "any user-visible
    memory error" catch this branch's root and inspect
    ``isinstance`` or ``__class__.__name__`` to distinguish.
    Callers that care about the specific HTTP mapping catch the
    concrete subclass directly.

    Behavioral contract
    -------------------
    * ``retryable = False``      -- invalid input is invalid;
                                    retrying with the same input
                                    fails identically. A retry
                                    loop would spin forever.
    * ``client_facing = True``   -- the message is already written
                                    for a non-technical reader
                                    (it names the offending field
                                    and the rule violated) and
                                    can be surfaced verbatim.
    """

    retryable = False
    client_facing = True


class MemoryValidationError(MemoryOperationError):
    """The caller supplied an invalid input.

    Raised when:

      * ``MemoryEntry`` construction fails validation (content
        too long, tags exceed cap, etc.) -- the field validator
        raises ``ValueError``, which the repository wraps in this
        exception before returning to the API.
      * ``MemorySettings`` has inconsistent fields (reservation
        exceeds budget).
      * ``scope_id`` contains forbidden characters (``:``,
        control characters). Note: this case can ALSO raise
        ``ScopeViolationError`` depending on whether the caller
        was merely formatting the id wrong (validation) or was
        deliberately trying to escape their scope (violation).
        The boundary between the two is decided at the CALL SITE,
        not here. This exception is for the "formatting was
        wrong" side.
      * An import package's manifest does not match the schema.

    Extra fields
    ------------
    ``field_name``
        The name of the specific field that failed validation.
        ``None`` when the failure is not field-scoped (e.g. "the
        entire manifest is malformed"). The API layer uses this
        to highlight the offending input in the UI.
    """

    def __init__(
        self,
        message: str,
        *,
        field_name: Optional[str] = None,
        context: Optional[Dict[str, Any]] = None,
        cause: Optional[BaseException] = None,
    ) -> None:
        if field_name is not None:
            merged_context: Dict[str, Any] = dict(context) if context else {}
            # ``setdefault`` so an explicit ``context["field_name"]``
            # (rare, but possible from a caller wrapping an existing
            # error) takes precedence over the constructor arg.
            merged_context.setdefault("field_name", field_name)
            context = merged_context
        super().__init__(message, context=context, cause=cause)
        self.field_name: Optional[str] = field_name


class MemoryNotFoundError(MemoryOperationError):
    """The requested entry does not exist (or is not visible in the caller's scope).

    Raised when:

      * ``get``, ``update``, or ``delete`` is called with a
        ``memory_id`` that no entry matches.
      * A caller asks for an entry that EXISTS in the database but
        belongs to another scope. The repository deliberately
        returns ``MemoryNotFoundError`` (not
        ``ScopeViolationError``) in this case, because:

          - Telling the caller "this exists but you cannot see it"
            leaks the existence of another user's data (an
            information-disclosure vulnerability).
          - The caller's action (e.g. clicking a link to a memory
            that was moved) is functionally the same as clicking a
            link to a memory that was deleted -- both are "not
            there" from the caller's perspective.

        The distinction between "truly absent" and "exists in
        another scope" is recorded in the audit log but NEVER
        surfaced in the error. This is the ONE exception to the
        "always truthful" principle, and it is deliberate.

    Extra fields
    ------------
    ``memory_id``
        The identifier that failed to resolve. Always present.
    """

    def __init__(
        self,
        memory_id: str,
        *,
        message: Optional[str] = None,
        context: Optional[Dict[str, Any]] = None,
        cause: Optional[BaseException] = None,
    ) -> None:
        if not isinstance(memory_id, str) or not memory_id.strip():
            raise ValueError(
                "memory_id must be a non-empty string when raising "
                "MemoryNotFoundError"
            )
        clean_id = memory_id.strip()
        effective_message = message or f"Memory entry not found: {clean_id}"
        merged_context: Dict[str, Any] = dict(context) if context else {}
        merged_context.setdefault("memory_id", clean_id)
        super().__init__(
            effective_message, context=merged_context, cause=cause
        )
        self.memory_id: str = clean_id


# ---------------------------------------------------------------------------
# Scenario 3: SECURITY EVENT -- scope boundary violated
# ---------------------------------------------------------------------------


class ScopeViolationError(MemoryError):
    """The caller attempted to access a memory outside its authorized scope.

    Raised when the SERVER-SIDE scope check detects that the
    caller's effective scope does NOT include the target entry's
    scope. This is a hard security boundary violation -- the
    caller is asking for data they were never authorized to
    access, and the system MUST refuse.

    Distinction from ``MemoryNotFoundError``
    ----------------------------------------
    The repository deliberately returns ``MemoryNotFoundError``
    when the entry exists in ANOTHER scope, to avoid leaking its
    existence (see that exception's docstring). This
    ``ScopeViolationError`` is raised only when a SEPARATE,
    EXPLICIT check fires:

      * The API layer's ``GET /api/memory/{id}`` has already
        loaded the entry and detected that its scope does not
        match the caller's session -- a direct, deliberate
        attempt to access another user's data by guessing the
        memory id.
      * The ``promote`` endpoint (P1.8) tries to move a
        project-scope entry to a user scope without the caller
        being the owner of BOTH scopes.
      * The import pipeline (P1.6) tries to write to
        ``scope_type=user`` without ``source_type=explicit_user``
        (which is the "user confirmed" marker).

    In every case the intent to cross the boundary is explicit
    and the system has already decided to surface the attempt
    (either to the caller or to the audit log). Silent
    ``MemoryNotFoundError`` would be WRONG here because the
    caller's request WOULD be granted if scope validation were
    skipped -- the refusal must be attributable and loud.

    Behavioral contract
    -------------------
    * ``retryable = False``     -- retrying with the same scope
                                   fails identically; scope
                                   validation is deterministic.
    * ``client_facing = True``  -- the message says "you do not
                                   have permission", which is safe
                                   to show to the caller. It does
                                   NOT disclose the entry's content
                                   or its existence beyond "there
                                   is a boundary you cannot cross".

    Never swallow this exception
    ----------------------------
    Unlike ``MemoryUnavailableError`` (which the dispatcher
    deliberately catches and converts to an empty context), this
    exception MUST propagate. A dispatcher that caught it and
    returned an empty context would be *masking a security
    violation* -- the caller would see "no memories found" and
    continue, unaware that they had just attempted an
    unauthorized access. The audit log is the only place the
    attempt would be recorded, and even there, the failure would
    look like a normal "no result" rather than an attack signal.

    Extra fields
    ------------
    ``requested_scope``
        The scope the caller asked for, in
        ``"<scope_type>:<scope_id>"`` form.
    ``allowed_scopes``
        The complete list of scopes the caller's session is
        authorized to access, in the same string form. Used by
        the API layer to generate a "you can only access: X, Y,
        Z" error message without a second query.
    """

    retryable = False
    client_facing = True

    def __init__(
        self,
        message: str,
        *,
        requested_scope: Optional[str] = None,
        allowed_scopes: Optional[list[str]] = None,
        context: Optional[Dict[str, Any]] = None,
        cause: Optional[BaseException] = None,
    ) -> None:
        merged_context: Dict[str, Any] = dict(context) if context else {}
        if requested_scope is not None:
            merged_context.setdefault(
                "requested_scope", requested_scope
            )
        if allowed_scopes is not None:
            merged_context.setdefault(
                "allowed_scopes", list(allowed_scopes)
            )
        super().__init__(message, context=merged_context, cause=cause)
        self.requested_scope: Optional[str] = requested_scope
        self.allowed_scopes: list[str] = (
            list(allowed_scopes) if allowed_scopes else []
        )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


__all__ = [
    "MemoryError",
    "MemoryUnavailableError",
    "MemoryOperationError",
    "MemoryValidationError",
    "MemoryNotFoundError",
    "ScopeViolationError",
]
